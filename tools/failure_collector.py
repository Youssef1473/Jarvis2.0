"""Deterministic Failure-Event Collector & Evidence Envelope (ENG-OS-02 Phase 3B2).

Captures objective, evidence-grounded execution failure events from Antigravity lifecycle
hooks (PostToolUse and Stop) with strict privacy preservation, bounded evidence references,
robust secret redaction, and deterministic deduplication.

DATA-INTEGRITY & CORRECTNESS SPECIFICATIONS (Phase 3B2):
1. Incomplete Identity Policy:
   - RELIABLE: Step index (PostToolUse) or execution number (Stop) present. Deduplication is strictly idempotent.
   - INCOMPLETE_DISCRIMINATED: Identifier missing, discriminator computed from payload.
   - UNRESOLVABLE_DEDUPLICATION: Collision on incomplete identity preserves secondary records with collision suffix
     rather than silently discarding potentially independent events.
2. Error-Field Semantics:
   - Explicitly records `error_status`: EMPTY_STRING, NON_EMPTY, EXPLICIT_NULL, OMITTED, MALFORMED.
   - Host tool_outcome (SUCCESS | FAILURE | UNKNOWN) is separated from numeric process exit code.
   - Missing error or empty string never fabricates exit_code=0; exit_code remains None unless reliably extracted.
3. Strict Exit-Code Extraction:
   - Assigned HIGH confidence ONLY when unambiguously established by the host runner (e.g. 'exit status N'
     in error string or exact process exit banner). Random nested text in command stdout is never promoted to exit code.
4. Routine Success Filtering:
   - Successful commands are skipped without disk I/O or JSON persistence.
5. Windows Evidence Confinement:
   - Canonical root validation, full-chain junction/reparse-point rejection, 64 KB read cap.
6. Storage Confinement:
   - Writes to session artifact directory outside repository; skips persistence safely if safe root unavailable.
7. Fail-Open Protocol:
   - Always emits documented empty JSON `{}` on stdout; errors go to stderr.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_EVIDENCE_READ_BYTES = 64 * 1024  # 64 KB strict read cap
MAX_SUMMARY_LENGTH = 500  # Bounded summary cap
MAX_SIGNAL_LENGTH = 500  # Bounded failure signal cap

# -----------------------------------------------------------------------------
# Data Model / Versioned Evidence Envelope
# -----------------------------------------------------------------------------

@dataclass
class EventEnvelope:
    """Versioned evidence envelope for engineering execution events."""
    schema_version: int = SCHEMA_VERSION
    event_id: str = ""
    event_type: str = ""  # "POST_TOOL_USE", "STOP", "UNKNOWN"
    identity_status: str = "RELIABLE"  # "RELIABLE", "INCOMPLETE_DISCRIMINATED", "UNRESOLVABLE_DEDUPLICATION"
    conversation_reference: str | None = None
    step_index: int | None = None
    execution_number: int | None = None
    tool_name: str | None = None
    tool_outcome: str = "UNKNOWN"  # "SUCCESS", "FAILURE", "UNKNOWN", "NOT_APPLICABLE"
    error_status: str = "UNKNOWN"  # "EMPTY_STRING", "NON_EMPTY", "EXPLICIT_NULL", "OMITTED", "MALFORMED"
    failure_signal: str | None = None
    exit_code: int | None = None
    exit_code_confidence: str = "UNAVAILABLE"  # "HIGH", "LOW", "UNAVAILABLE"
    termination_reason: str | None = None
    fully_idle: bool | None = None
    timestamp: str = ""
    evidence_references: dict[str, Any] = field(default_factory=dict)
    safe_summary: str = ""
    redaction_status: str = "CLEAN"  # "CLEAN", "REDACTED", "REDACTION_INCOMPLETE"
    collection_status: str = "UNKNOWN"  # "OBSERVED_FAILURE", "OBSERVED_TIMEOUT", "OBSERVED_CANCELLATION", "OBSERVED_INCOMPLETE", "SUCCESS", "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        """Convert envelope to dictionary preserving None vs empty representations."""
        return dataclasses.asdict(self)


# -----------------------------------------------------------------------------
# Redaction Engine
# -----------------------------------------------------------------------------

REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Bearer tokens
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9_\-\.]{12,}\b"), r"\1[REDACTED_TOKEN]"),
    # Common API key formats (Anthropic, OpenAI, GitHub, Gemini/Google AI)
    (re.compile(r"\b(sk-[A-Za-z0-9]{20,})\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(ghp_[A-Za-z0-9]{30,})\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\b(AIza[0-9A-Za-z-_]{30,40})\b"), "[REDACTED_GOOGLE_API_KEY]"),
    # Key-value secret assignments in configs/flags/errors (negative lookahead avoids re-redacting)
    (re.compile(r"(?i)\b((?:api[_-]?key|secret|password|token|access[_-]?key)\s*[:=]\s*['\"]?)(?!\s*\[REDACTED)([^\s'\"]{8,})(['\"]?)"), r"\1[REDACTED]\3"),
    # Windows user home directory disclosure (e.g., C:\Users\Username)
    (re.compile(r"(?i)\b([A-Z]:\\Users\\)[a-zA-Z0-9_\.\-]+"), r"\1[USERNAME]"),
]


def redact_sensitive_text(text: str) -> tuple[str, str]:
    """Applies conservative regex redaction to strip credentials, keys, and usernames.

    Returns:
        tuple[str, str]: (sanitized_text, redaction_status)
        redaction_status is 'REDACTED' if changes occurred, otherwise 'CLEAN'.
    """
    if not text:
        return text, "CLEAN"

    try:
        sanitized = text
        modified = False
        for pattern, replacement in REDACTION_PATTERNS:
            new_text = pattern.sub(replacement, sanitized)
            if new_text != sanitized:
                modified = True
                sanitized = new_text

        return sanitized, ("REDACTED" if modified else "CLEAN")
    except (re.error, TypeError, ValueError):
        return "[SANITIZATION_FAILED]", "REDACTION_INCOMPLETE"


# -----------------------------------------------------------------------------
# Privacy-Preserving Reference Masking & Event ID Calculation
# -----------------------------------------------------------------------------

def mask_conversation_id(conversation_id: str | None) -> str | None:
    """Generates a stable, privacy-preserving reference from a conversation ID."""
    if not conversation_id:
        return None
    conv_str = str(conversation_id).strip()
    if not conv_str:
        return None
    digest = hashlib.sha256(conv_str.encode("utf-8")).hexdigest()[:12]
    return f"conv-{digest}"


def compute_event_id(
    conv_ref: str | None,
    event_type: str,
    step_idx: int | None = None,
    exec_num: int | None = None,
    discriminator: str | None = None,
) -> tuple[str, str]:
    """Computes a stable deterministic SHA-256 event ID and reports identity status.

    Returns:
        tuple[str, str]: (event_id, identity_status)
        identity_status is 'RELIABLE' if sequence number present, otherwise 'INCOMPLETE_DISCRIMINATED'.
    """
    c_part = conv_ref or "anon"
    if event_type == "POST_TOOL_USE":
        if step_idx is not None:
            identity_key = f"{c_part}:step_{step_idx}:POST_TOOL_USE"
            return hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:24], "RELIABLE"
        disc = discriminator or "missing_step"
        identity_key = f"{c_part}:step_none_{disc}:POST_TOOL_USE"
        return hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:24], "INCOMPLETE_DISCRIMINATED"

    if event_type == "STOP":
        if exec_num is not None:
            identity_key = f"{c_part}:exec_{exec_num}:STOP"
            return hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:24], "RELIABLE"
        disc = discriminator or "missing_exec"
        identity_key = f"{c_part}:exec_none_{disc}:STOP"
        return hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:24], "INCOMPLETE_DISCRIMINATED"

    disc = discriminator or "generic"
    identity_key = f"{c_part}:{disc}:{event_type}"
    return hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:24], "INCOMPLETE_DISCRIMINATED"


# -----------------------------------------------------------------------------
# Strict Exit Code Extraction
# -----------------------------------------------------------------------------

HOST_ERROR_EXIT_PATTERN = re.compile(r"(?i)\bexit\s+status\s+(-?\d+)\b")
RUNNER_FOOTER_EXIT_PATTERN = re.compile(r"(?im)^Command\s+exited\s+with\s+code\s+(-?\d+)\.?$")


def extract_exit_code(error_str: str | None, output_snippet: str | None = None) -> tuple[int | None, str]:
    """Extracts numeric exit code ONLY from verified outer process sources.

    Strict Invariant:
    - Matches 'exit status N' in host-provided error_str (HIGH confidence).
    - Matches isolated '^Command exited with code N' runner footer in output_snippet (HIGH confidence).
    - Arbitrary occurrences inside nested command output or text bodies are rejected as UNAVAILABLE.

    Returns:
        tuple[int | None, str]: (exit_code, exit_code_confidence)
    """
    # 1. Inspect host-provided error string (most authoritative)
    if error_str:
        m = HOST_ERROR_EXIT_PATTERN.search(error_str)
        if m:
            try:
                return int(m.group(1)), "HIGH"
            except (ValueError, OverflowError):
                pass

    # 2. Inspect exact runner footer in output snippet
    if output_snippet:
        m = RUNNER_FOOTER_EXIT_PATTERN.search(output_snippet)
        if m:
            try:
                return int(m.group(1)), "HIGH"
            except (ValueError, OverflowError):
                pass

    return None, "UNAVAILABLE"


# -----------------------------------------------------------------------------
# Safe Windows Evidence Confinement & Reader
# -----------------------------------------------------------------------------

def resolve_step_output_file(step_idx: int, artifact_dir: str | Path | None) -> Path | None:
    """Resolves the expected step output file path safely if available."""
    if not artifact_dir:
        return None
    base = Path(artifact_dir)
    candidate = base / ".system_generated" / "steps" / str(step_idx) / "output.txt"
    if candidate.exists():
        return candidate
    parent_candidate = base.parent / ".system_generated" / "steps" / str(step_idx) / "output.txt"
    if parent_candidate.exists():
        return parent_candidate
    return candidate


def is_reparse_point_or_link(path: Path) -> bool:
    """Checks whether a path is a symbolic link, junction, or reparse point."""
    try:
        if path.is_symlink():
            return True
        if hasattr(os.path, "isjunction") and os.path.isjunction(str(path)):
            return True
        st = os.lstat(str(path))
        return bool(
            hasattr(st, "st_file_attributes")
            and hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT")
            and (st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        )
    except (OSError, ValueError):
        return True  # Fail-closed on stat error


def validate_path_confinement(target_path: Path, allowed_root: Path) -> tuple[bool, str]:
    """Verifies that target_path is strictly within allowed_root and free of links/junctions."""
    try:
        root_resolved = Path(allowed_root).resolve()
        file_resolved = Path(target_path).resolve()

        try:
            file_resolved.relative_to(root_resolved)
        except ValueError:
            return False, "PATH_TRAVERSAL_REJECTED"

        # Check target itself and all parent components between target and root
        curr = target_path
        while curr != allowed_root and curr != curr.parent:
            if curr.exists() and is_reparse_point_or_link(curr):
                return False, "REPARSE_POINT_REJECTED"
            curr = curr.parent

        return True, "SAFE"
    except OSError:
        return False, "CONFINEMENT_CHECK_ERROR"


def read_step_evidence(
    target_path: Path | None,
    allowed_root: Path | None,
    max_bytes: int = MAX_EVIDENCE_READ_BYTES,
) -> tuple[str | None, str, dict[str, Any]]:
    """Safely reads bounded step output within an allowed directory boundary."""
    metadata: dict[str, Any] = {
        "status": "UNAVAILABLE",
        "bytes_read": 0,
        "truncated": False,
        "resolved_path": str(target_path) if target_path else None,
    }

    if not target_path or not allowed_root:
        return None, "CLEAN", metadata

    try:
        is_safe, reason = validate_path_confinement(target_path, allowed_root)
        if not is_safe:
            metadata["status"] = reason
            return None, "CLEAN", metadata

        file_resolved = Path(target_path).resolve()
        if not file_resolved.is_file():
            metadata["status"] = "FILE_NOT_FOUND"
            return None, "CLEAN", metadata

        file_size = file_resolved.stat().st_size
        metadata["file_size_bytes"] = file_size

        with open(file_resolved, "rb") as f:
            raw_bytes = f.read(max_bytes + 1)

        is_truncated = len(raw_bytes) > max_bytes
        if is_truncated:
            raw_bytes = raw_bytes[:max_bytes]

        metadata["bytes_read"] = len(raw_bytes)
        metadata["truncated"] = is_truncated
        metadata["status"] = "SUCCESS"

        try:
            text = raw_bytes.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, ValueError):
            metadata["status"] = "DECODE_ERROR"
            return None, "REDACTION_INCOMPLETE", metadata

        sanitized, red_status = redact_sensitive_text(text)
        return sanitized, red_status, metadata

    except OSError as exc:
        metadata["status"] = f"ERROR: {type(exc).__name__}"
        return None, "REDACTION_INCOMPLETE", metadata


# -----------------------------------------------------------------------------
# Event Parsers (PostToolUse & Stop)
# -----------------------------------------------------------------------------

def parse_post_tool_use(payload: dict[str, Any], allowed_root: Path | None = None) -> EventEnvelope | None:
    """Parses a PostToolUse payload into an EventEnvelope.

    Explicitly identifies error_status and separates host tool outcome from numeric exit code.
    """
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return None

    tool_name = tool_call.get("name")
    if not tool_name or not isinstance(tool_name, str):
        tool_name = "unknown_tool"

    # Step index
    raw_step = payload.get("stepIdx")
    step_idx: int | None = None
    if raw_step is not None:
        try:
            step_idx = int(raw_step)
        except (ValueError, TypeError):
            step_idx = None

    # Error field semantic discrimination
    if "error" not in payload:
        error_status = "OMITTED"
        raw_error = None
    elif payload["error"] is None:
        error_status = "EXPLICIT_NULL"
        raw_error = None
    elif isinstance(payload["error"], str):
        if payload["error"] == "":
            error_status = "EMPTY_STRING"
            raw_error = ""
        else:
            error_status = "NON_EMPTY"
            raw_error = payload["error"]
    else:
        error_status = "NON_EMPTY"
        raw_error = str(payload["error"])

    conv_id = payload.get("conversationId")
    conv_ref = mask_conversation_id(str(conv_id) if conv_id is not None else None)

    # Tool args summary
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    cmd_summary = ""
    if "CommandLine" in args:
        cmd_summary = str(args["CommandLine"])
    elif "toolSummary" in args:
        cmd_summary = str(args["toolSummary"])
    elif "Path" in args:
        cmd_summary = f"file({args['Path']})"

    redacted_cmd, cmd_red_status = redact_sensitive_text(cmd_summary[:MAX_SUMMARY_LENGTH])

    evidence_meta: dict[str, Any] = {}
    evidence_red_status = "CLEAN"
    output_snippet: str | None = None

    if error_status in ("EMPTY_STRING", "EXPLICIT_NULL", "OMITTED"):
        # Tool execution did not report an error
        tool_outcome = "SUCCESS"
        collection_status = "SUCCESS"
        failure_signal = None
        exit_code = None
        exit_code_confidence = "UNAVAILABLE"
        err_red_status = "CLEAN"
    else:
        # Tool execution reported an error
        tool_outcome = "FAILURE"
        sanitized_error, err_red_status = redact_sensitive_text(str(raw_error)[:MAX_SIGNAL_LENGTH])
        failure_signal = sanitized_error

        # Read evidence file only for failures
        if step_idx is not None:
            art_dir = payload.get("artifactDirectoryPath")
            target_evidence = resolve_step_output_file(step_idx, art_dir)
            effective_root = allowed_root or (Path(art_dir).resolve() if art_dir else None)
            if target_evidence and effective_root:
                out_text, evidence_red_status, evidence_meta = read_step_evidence(target_evidence, effective_root)
                if out_text:
                    output_snippet = out_text[-1000:]

        exit_code, exit_code_confidence = extract_exit_code(sanitized_error, output_snippet)

        lower_err = sanitized_error.lower()
        if "cancel" in lower_err:
            collection_status = "OBSERVED_CANCELLATION"
        elif "timeout" in lower_err or "timed out" in lower_err:
            collection_status = "OBSERVED_TIMEOUT"
        else:
            collection_status = "OBSERVED_FAILURE"

    statuses = {cmd_red_status, evidence_red_status, err_red_status}
    if "REDACTION_INCOMPLETE" in statuses:
        composite_redaction = "REDACTION_INCOMPLETE"
    elif "REDACTED" in statuses:
        composite_redaction = "REDACTED"
    else:
        composite_redaction = "CLEAN"

    if failure_signal:
        safe_summary = f"Tool '{tool_name}' failed: {failure_signal[:120]}"
    else:
        safe_summary = f"Tool '{tool_name}' completed"
    if redacted_cmd:
        safe_summary += f" [cmd: {redacted_cmd[:100]}]"

    discriminator = None
    if step_idx is None:
        disc_seed = f"{tool_name}:{cmd_summary}:{raw_error or ''}"
        discriminator = hashlib.sha256(disc_seed.encode("utf-8")).hexdigest()[:12]

    event_id, identity_status = compute_event_id(
        conv_ref=conv_ref,
        event_type="POST_TOOL_USE",
        step_idx=step_idx,
        discriminator=discriminator,
    )
    timestamp = datetime.datetime.now(datetime.UTC).isoformat()

    return EventEnvelope(
        schema_version=SCHEMA_VERSION,
        event_id=event_id,
        event_type="POST_TOOL_USE",
        identity_status=identity_status,
        conversation_reference=conv_ref,
        step_index=step_idx,
        execution_number=None,
        tool_name=tool_name,
        tool_outcome=tool_outcome,
        error_status=error_status,
        failure_signal=failure_signal,
        exit_code=exit_code,
        exit_code_confidence=exit_code_confidence,
        termination_reason=None,
        fully_idle=None,
        timestamp=timestamp,
        evidence_references=evidence_meta,
        safe_summary=safe_summary[:MAX_SUMMARY_LENGTH],
        redaction_status=composite_redaction,
        collection_status=collection_status,
    )


def parse_stop(payload: dict[str, Any]) -> EventEnvelope | None:
    """Parses a Stop payload into an EventEnvelope."""
    raw_exec = payload.get("executionNum")
    exec_num: int | None = None
    if raw_exec is not None:
        try:
            exec_num = int(raw_exec)
        except (ValueError, TypeError):
            exec_num = None

    raw_term = payload.get("terminationReason")
    term_reason: str | None = str(raw_term) if raw_term is not None else None

    # Error field in Stop payload
    if "error" not in payload:
        error_status = "OMITTED"
        raw_error = None
    elif payload["error"] is None:
        error_status = "EXPLICIT_NULL"
        raw_error = None
    elif isinstance(payload["error"], str):
        error_status = "EMPTY_STRING" if payload["error"] == "" else "NON_EMPTY"
        raw_error = payload["error"]
    else:
        error_status = "NON_EMPTY"
        raw_error = str(payload["error"])

    raw_idle = payload.get("fullyIdle")
    fully_idle: bool | None = bool(raw_idle) if isinstance(raw_idle, bool) else None

    conv_id = payload.get("conversationId")
    conv_ref = mask_conversation_id(str(conv_id) if conv_id is not None else None)

    sanitized_error = None
    red_status = "CLEAN"
    if raw_error:
        sanitized_error, red_status = redact_sensitive_text(str(raw_error)[:MAX_SIGNAL_LENGTH])

    if term_reason == "USER_CANCELED":
        collection_status = "OBSERVED_CANCELLATION"
    elif term_reason in ("TIMEOUT", "MAX_INVOCATIONS", "MAX_FORCED_INVOCATIONS", "MAX_TOKEN_BUDGET_EXCEEDED"):
        collection_status = "OBSERVED_TIMEOUT"
    elif term_reason == "ERROR" or (sanitized_error and sanitized_error.strip()):
        collection_status = "OBSERVED_FAILURE"
    elif fully_idle is False:
        collection_status = "OBSERVED_INCOMPLETE"
    elif term_reason == "NO_TOOL_CALL":
        collection_status = "SUCCESS"
    else:
        collection_status = "UNKNOWN"

    safe_summary = f"Stop event: terminationReason={term_reason or 'NONE'}, fullyIdle={fully_idle}"
    if sanitized_error:
        safe_summary += f", error={sanitized_error[:100]}"

    discriminator = None
    if exec_num is None:
        disc_seed = f"{term_reason or ''}:{raw_error or ''}:{fully_idle}"
        discriminator = hashlib.sha256(disc_seed.encode("utf-8")).hexdigest()[:12]

    event_id, identity_status = compute_event_id(
        conv_ref=conv_ref,
        event_type="STOP",
        exec_num=exec_num,
        discriminator=discriminator,
    )
    timestamp = datetime.datetime.now(datetime.UTC).isoformat()

    return EventEnvelope(
        schema_version=SCHEMA_VERSION,
        event_id=event_id,
        event_type="STOP",
        identity_status=identity_status,
        conversation_reference=conv_ref,
        step_index=None,
        execution_number=exec_num,
        tool_name=None,
        tool_outcome="NOT_APPLICABLE",
        error_status=error_status,
        failure_signal=sanitized_error,
        exit_code=None,
        exit_code_confidence="UNAVAILABLE",
        termination_reason=term_reason,
        fully_idle=fully_idle,
        timestamp=timestamp,
        evidence_references={},
        safe_summary=safe_summary[:MAX_SUMMARY_LENGTH],
        redaction_status=red_status,
        collection_status=collection_status,
    )


# -----------------------------------------------------------------------------
# Storage & Deduplication Manager (Incomplete Identity Policy)
# -----------------------------------------------------------------------------

def is_safe_storage_path(target_dir: Path) -> bool:
    """Verifies that target_dir is not an unconfined or sensitive system root."""
    try:
        resolved = target_dir.resolve()
        if len(resolved.parts) <= 1:
            return False
        if resolved.drive and resolved == Path(resolved.drive + "\\"):
            return False
        sys_drive = os.environ.get("SystemDrive", "C:")
        windir = Path(os.environ.get("SystemRoot", f"{sys_drive}\\Windows")).resolve()
        return not (resolved == windir or windir in resolved.parents)
    except (OSError, ValueError):
        return False


def resolve_storage_directory(
    payload: dict[str, Any] | None = None,
    custom_dir: Path | None = None,
) -> Path | None:
    """Determines the appropriate persistence directory for failure events.

    Returns None if no safe destination is established (prevents workspace pollution).
    """
    if custom_dir is not None:
        return Path(custom_dir)

    env_dir = os.environ.get("JARVIS_LEARNING_EVENTS_DIR")
    if env_dir:
        candidate = Path(env_dir)
        if is_safe_storage_path(candidate):
            return candidate

    if payload and isinstance(payload.get("artifactDirectoryPath"), str):
        art_path = Path(payload["artifactDirectoryPath"])
        if is_safe_storage_path(art_path):
            return art_path / "learning_events"

    return None


def persist_event_envelope(envelope: EventEnvelope, storage_dir: Path) -> tuple[bool, str]:
    """Persists an EventEnvelope as JSON adhering to the Incomplete Identity Policy.

    Policy Invariants:
    1. RELIABLE identity: Target file existence indicates genuine idempotent duplicate delivery.
       Returns (True, "DUPLICATE_IDEMPOTENT") without re-writing.
    2. INCOMPLETE identity: Target file existence may represent an independent event with identical content.
       Writes secondary record with suffix '_unresolvable_<seq>' and identity_status='UNRESOLVABLE_DEDUPLICATION'.
       Guarantees independent events are never silently discarded.

    Returns:
        tuple[bool, str]: (success, status_message)
    """
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
        target_file = storage_dir / f"{envelope.event_id}.json"

        if target_file.exists():
            if envelope.identity_status == "RELIABLE":
                return True, "DUPLICATE_IDEMPOTENT"

            # Incomplete identity collision: Do not silently discard!
            envelope.identity_status = "UNRESOLVABLE_DEDUPLICATION"
            counter = 1
            while True:
                alt_file = storage_dir / f"{envelope.event_id}_unresolvable_{counter}.json"
                if not alt_file.exists():
                    target_file = alt_file
                    break
                counter += 1

        temp_file = storage_dir / f"{target_file.stem}.tmp_{os.getpid()}"
        data_json = json.dumps(envelope.to_dict(), indent=2, ensure_ascii=False)

        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(data_json)

        temp_file.replace(target_file)
        return True, "RECORDED"

    except OSError as exc:
        return False, f"STORAGE_FAILURE: {type(exc).__name__}: {exc}"


# -----------------------------------------------------------------------------
# Hook Main Entry Point (Fail-Open Protocol)
# -----------------------------------------------------------------------------

def process_hook_payload(
    raw_payload: str | dict[str, Any],
    custom_storage_dir: Path | None = None,
    allowed_root: Path | None = None,
    stderr_stream=sys.stderr,
) -> tuple[dict[str, Any], EventEnvelope | None]:
    """Processes a hook payload, returning (hook_response, envelope_or_none)."""
    if isinstance(raw_payload, str):
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            stderr_stream.write(f"[failure_collector] Malformed JSON input: {exc}\n")
            return {}, None
    elif isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        stderr_stream.write("[failure_collector] Invalid payload type (expected dict or JSON string)\n")
        return {}, None

    if not isinstance(payload, dict):
        stderr_stream.write("[failure_collector] Invalid payload structure (expected dict)\n")
        return {}, None

    is_stop_event = ("terminationReason" in payload) or ("fullyIdle" in payload)
    envelope: EventEnvelope | None = None
    if "toolCall" in payload:
        envelope = parse_post_tool_use(payload, allowed_root=allowed_root)
    elif is_stop_event:
        envelope = parse_stop(payload)

    hook_response: dict[str, Any] = {"decision": "stop"} if is_stop_event else {}

    if envelope is None:
        return hook_response, None

    # Routine success filtering: Skip persistence for non-failures
    should_persist = envelope.collection_status in (
        "OBSERVED_FAILURE",
        "OBSERVED_TIMEOUT",
        "OBSERVED_CANCELLATION",
        "OBSERVED_INCOMPLETE",
    )

    if should_persist:
        storage_dir = resolve_storage_directory(payload, custom_dir=custom_storage_dir)
        if storage_dir is None:
            stderr_stream.write(
                "[failure_collector] Storage skipped: safe destination unavailable\n"
            )
        else:
            success, msg = persist_event_envelope(envelope, storage_dir)
            if not success:
                stderr_stream.write(f"[failure_collector] Warning: {msg}\n")

    return hook_response, envelope


def main(
    stdin_stream=sys.stdin,
    stdout_stream=sys.stdout,
    stderr_stream=sys.stderr,
) -> int:
    """Fail-open hook CLI entry point."""
    written = False
    response_payload: dict[str, Any] = {}
    try:
        raw_input = stdin_stream.read()
        if raw_input and raw_input.strip():
            resp, _envelope = process_hook_payload(raw_input, stderr_stream=stderr_stream)
            if isinstance(resp, dict):
                response_payload = resp

    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        stderr_stream.write(f"[failure_collector] Unexpected error: {exc}\n")

    finally:
        if not written:
            try:
                stdout_stream.write(json.dumps(response_payload) + "\n")
                stdout_stream.flush()
                written = True
            except OSError as out_err:
                stderr_stream.write(f"[failure_collector] stdout write error: {out_err}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
