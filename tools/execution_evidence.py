"""Execution Evidence Provenance & RED-GREEN Verification Engine (ENG-OS-03A2).

Provides cryptographic and empirical execution-evidence capture for Jarvis's continuous
engineering intelligence pipeline. Distinguishes raw failure observations, controlled
reproductions (RED), verified fixes (GREEN), and review-ready conclusions.

CORE INVARIANTS:
1. Untrusted Deserialization Defense & Durable Evidence Store:
   Only local trusted subprocess execution by ExecutionEvidenceCollector can assign
   provenance_status='AUTHORITATIVE_LOCAL_EXECUTION' and create an immutable ExecutionReceipt
   recorded in the local EvidenceStore (SQLite WAL registry + canonical JSON store).
   Deserialized candidate JSON referencing an execution must re-authenticate against the
   EvidenceStore and verify against disk before achieving 'VERIFIED_LOCAL_EXECUTION'.
2. Strict RED-GREEN Paired Verification & Implementation Non-Identity:
   - RED requires an actual baseline execution where the relevant test was collected,
     executed, and failed with an outcome consistent with the candidate's defect.
   - GREEN requires a valid paired RED execution against the exact same test definition
     (identical test file SHA-256 and test selection) and identical test configuration,
     passing with exit code 0.
   - GREEN MUST demonstrate a relevant source or configuration state change relative to RED.
     An unchanged implementation rerun (e.g. flaky test) is strictly rejected as
     UNCHANGED_IMPLEMENTATION_RERUN.
3. Execution Argument & Target Confinement:
   Test runner arguments are strictly restricted against an allowlist; dangerous pytest
   flags (-p, -o, -c, --rootdir, --override-ini, etc.) and shell metacharacters are rejected.
4. Permanent Promotion Prohibition:
   Execution evidence establishes empirical verification for human reviewers. It does
   NOT grant authorization to write to docs/agent/LESSONS.md. Candidate promotion
   remains unconditionally fail-closed (PROMOTION_PROHIBITED).
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

try:
    from tools.failure_collector import (
        redact_sensitive_text,
        validate_path_confinement,
    )
except ImportError:
    from failure_collector import (
        redact_sensitive_text,
        validate_path_confinement,
    )

SCHEMA_VERSION = 2
MAX_SNIPPET_BYTES = 2048  # 2 KB bounded output snippet cap
DEFAULT_EVIDENCE_DIR = WORKSPACE_ROOT / "data" / "execution_evidence"

# Allowed execution roles
EXECUTION_ROLES = ("BASELINE", "FIX_VERIFICATION")

# Allowed termination reasons
TERMINATION_REASONS = (
    "NORMAL",
    "TIMEOUT",
    "SIGNAL",
    "EXCEPTION",
    "INFRASTRUCTURE_FAILURE",
)

# Allowed evidence completeness states
EVIDENCE_COMPLETENESS_STATES = (
    "COMPLETE",
    "PARTIAL",
    "TAMPERED",
    "MALFORMED",
)

# Allowed provenance statuses
PROVENANCE_STATUSES = (
    "AUTHORITATIVE_LOCAL_EXECUTION",
    "VERIFIED_LOCAL_EXECUTION",
    "IMPORTED_UNVERIFIED",
    "SYNTHETIC_REJECTED",
)

# Allowed Pytest Options Policy
ALLOWED_PYTEST_FLAGS = {
    "-v", "-vv", "-q", "-qq", "-s", "-x", "--capture=sys", "--capture=fd", "--capture=no"
}
ALLOWED_PYTEST_PREFIXES = (
    "--maxfail=", "--tb="
)
PROHIBITED_PYTEST_TOKENS = (
    "-p", "-c", "-o", "--override-ini", "--rootdir", "--import-mode",
    "--assert", "--basetemp", "--pyargs", "--confcutdir", "--noconftest"
)
SHELL_METAS = ("&", "|", ";", ">", "<", "`", "$", "\n", "\r")


class ExecutionIdentityConflictError(ValueError):
    """Raised when an execution_id is reused with conflicting execution content."""


# =============================================================================
# Workstream D: Collector Argument Security Policy
# =============================================================================

@dataclass(frozen=True)
class ExecutionRequest:
    """Validated request for subprocess execution."""
    test_target: str
    execution_role: str = "BASELINE"
    selector: str | None = None
    allowed_flags: tuple[str, ...] = field(default_factory=tuple)


def validate_collector_arguments(
    test_target: str | Path,
    extra_args: list[str] | None = None,
    allowed_root: Path | None = None,
) -> tuple[Path, str, list[str]]:
    """Strictly validates target test path, node selector, and pytest options.

    Fails closed with ValueError if path traversal, unknown flags, prohibited options,
    or shell metacharacters are encountered.
    """
    root = (allowed_root or WORKSPACE_ROOT).resolve()
    target_raw = str(test_target).strip()
    if not target_raw:
        raise ValueError("test_target cannot be empty.")

    # Split target file and optional selector node
    parts = target_raw.split("::", 1)
    file_part = parts[0].strip()
    node_part = parts[1].strip() if len(parts) > 1 else ""

    p = Path(file_part)
    if not p.is_absolute():
        p = (root / p).resolve()
    else:
        p = p.resolve()

    valid, msg = validate_path_confinement(p, allowed_root=root)
    if not valid or not p.exists() or not p.is_file():
        raise ValueError(f"Invalid target test path: {msg or 'File not found on disk'}")

    # Validate extra arguments
    sanitized_args: list[str] = []
    if extra_args:
        i = 0
        while i < len(extra_args):
            arg = str(extra_args[i]).strip()
            # Shell metacharacter check
            for meta in SHELL_METAS:
                if meta in arg:
                    raise ValueError(f"Prohibited shell metacharacter '{meta}' in pytest option: '{arg}'")

            # Prohibited flag check
            lower_arg = arg.lower()
            for prohibited in PROHIBITED_PYTEST_TOKENS:
                if lower_arg == prohibited or lower_arg.startswith((f"{prohibited}=", f"{prohibited}:")):
                    raise ValueError(f"Prohibited pytest option: '{arg}' (arbitrary config/plugin alteration forbidden).")

            # -k or -m flag with separate argument
            if arg in ("-k", "-m"):
                if i + 1 >= len(extra_args):
                    raise ValueError(f"Option '{arg}' requires a value argument.")
                val = str(extra_args[i + 1]).strip()
                for meta in SHELL_METAS:
                    if meta in val:
                        raise ValueError(f"Prohibited shell metacharacter in '{arg}' expression: '{val}'")
                sanitized_args.extend([arg, val])
                i += 2
                continue

            # Flag validation
            if arg in ALLOWED_PYTEST_FLAGS or any(arg.startswith(pfx) for pfx in ALLOWED_PYTEST_PREFIXES):
                sanitized_args.append(arg)
                i += 1
                continue

            raise ValueError(f"Prohibited or unallowlisted pytest option: '{arg}'")

    rel_path_str = str(p.relative_to(root)).replace("\\", "/")
    full_target_str = f"{rel_path_str}::{node_part}" if node_part else rel_path_str
    return p, full_target_str, sanitized_args


# =============================================================================
# Workstream A: Immutable ExecutionReceipt & Canonical Hashing
# =============================================================================

def canonical_hash_dict(data: dict[str, Any], excluded_keys: set[str] | None = None) -> str:
    """Computes a deterministic SHA-256 digest over a dictionary."""
    ex = excluded_keys or set()
    filtered = {k: v for k, v in data.items() if k not in ex}
    canonical_json = json.dumps(filtered, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionReceipt:
    """Deeply immutable, unforgeable witness record of an observed execution."""

    schema_version: int
    receipt_id: str                      # RCPT-<uuid4[:12]>
    execution_id: str                    # EXEC-<uuid4[:12]>
    candidate_id: str                    # LCAND-<hex12>
    source_event_ids: tuple[str, ...]
    execution_pair_id: str               # PAIR-<uuid4[:12]>
    execution_role: str                  # BASELINE | FIX_VERIFICATION
    captured_at_utc: str                 # ISO 8601 UTC
    argument_vector: tuple[str, ...]
    working_directory: str
    repository_revision: str
    working_tree_clean: bool
    exit_code: int
    termination_reason: str
    started_at_utc: str
    completed_at_utc: str
    duration_seconds: float
    test_collection_count: int
    tests_passed: int
    tests_failed: int
    tests_skipped: int
    tests_errored: int
    executed_test_ids: tuple[str, ...]
    test_outcomes: tuple[tuple[str, str], ...]
    failure_signatures: tuple[str, ...]
    target_test_file: str
    target_test_file_sha256: str
    source_state_manifest: tuple[tuple[str, str], ...]
    configuration_manifest: tuple[tuple[str, str], ...]
    environment_fingerprint: tuple[tuple[str, str], ...]
    stdout_sha256: str
    stderr_sha256: str
    stdout_snippet: str
    stderr_snippet: str
    junit_xml_sha256: str | None
    evidence_completeness: str
    content_digest: str

    def to_dict(self) -> dict[str, Any]:
        """Serializes receipt to canonical dictionary representation."""
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "execution_id": self.execution_id,
            "candidate_id": self.candidate_id,
            "source_event_ids": list(self.source_event_ids),
            "execution_pair_id": self.execution_pair_id,
            "execution_role": self.execution_role,
            "captured_at_utc": self.captured_at_utc,
            "argument_vector": list(self.argument_vector),
            "working_directory": self.working_directory,
            "repository_revision": self.repository_revision,
            "working_tree_clean": self.working_tree_clean,
            "exit_code": self.exit_code,
            "termination_reason": self.termination_reason,
            "started_at_utc": self.started_at_utc,
            "completed_at_utc": self.completed_at_utc,
            "duration_seconds": self.duration_seconds,
            "test_collection_count": self.test_collection_count,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "tests_skipped": self.tests_skipped,
            "tests_errored": self.tests_errored,
            "executed_test_ids": list(self.executed_test_ids),
            "test_outcomes": dict(self.test_outcomes),
            "failure_signatures": list(self.failure_signatures),
            "target_test_file": self.target_test_file,
            "target_test_file_sha256": self.target_test_file_sha256,
            "source_state_manifest": dict(self.source_state_manifest),
            "configuration_manifest": dict(self.configuration_manifest),
            "environment_fingerprint": dict(self.environment_fingerprint),
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "stdout_snippet": self.stdout_snippet,
            "stderr_snippet": self.stderr_snippet,
            "junit_xml_sha256": self.junit_xml_sha256,
            "evidence_completeness": self.evidence_completeness,
            "content_digest": self.content_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionReceipt:
        """Constructs ExecutionReceipt from dictionary, converting structures to immutable tuples."""
        digest = data.get("content_digest", "")
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            receipt_id=str(data.get("receipt_id", "")),
            execution_id=str(data.get("execution_id", "")),
            candidate_id=str(data.get("candidate_id", "")),
            source_event_ids=tuple(str(x) for x in data.get("source_event_ids", [])),
            execution_pair_id=str(data.get("execution_pair_id", "")),
            execution_role=str(data.get("execution_role", "BASELINE")),
            captured_at_utc=str(data.get("captured_at_utc", "")),
            argument_vector=tuple(str(x) for x in data.get("argument_vector", [])),
            working_directory=str(data.get("working_directory", "")),
            repository_revision=str(data.get("repository_revision", "")),
            working_tree_clean=bool(data.get("working_tree_clean", False)),
            exit_code=int(data.get("exit_code", -1)),
            termination_reason=str(data.get("termination_reason", "NORMAL")),
            started_at_utc=str(data.get("started_at_utc", "")),
            completed_at_utc=str(data.get("completed_at_utc", "")),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            test_collection_count=int(data.get("test_collection_count", 0)),
            tests_passed=int(data.get("tests_passed", 0)),
            tests_failed=int(data.get("tests_failed", 0)),
            tests_skipped=int(data.get("tests_skipped", 0)),
            tests_errored=int(data.get("tests_errored", 0)),
            executed_test_ids=tuple(str(x) for x in data.get("executed_test_ids", [])),
            test_outcomes=tuple((str(k), str(v)) for k, v in data.get("test_outcomes", {}).items()),
            failure_signatures=tuple(str(x) for x in data.get("failure_signatures", [])),
            target_test_file=str(data.get("target_test_file", "")),
            target_test_file_sha256=str(data.get("target_test_file_sha256", "")),
            source_state_manifest=tuple((str(k), str(v)) for k, v in data.get("source_state_manifest", {}).items()),
            configuration_manifest=tuple((str(k), str(v)) for k, v in data.get("configuration_manifest", {}).items()),
            environment_fingerprint=tuple((str(k), str(v)) for k, v in data.get("environment_fingerprint", {}).items()),
            stdout_sha256=str(data.get("stdout_sha256", "")),
            stderr_sha256=str(data.get("stderr_sha256", "")),
            stdout_snippet=str(data.get("stdout_snippet", "")),
            stderr_snippet=str(data.get("stderr_snippet", "")),
            junit_xml_sha256=data.get("junit_xml_sha256"),
            evidence_completeness=str(data.get("evidence_completeness", "COMPLETE")),
            content_digest=digest,
        )

    def compute_content_digest(self) -> str:
        """Computes deterministic digest excluding self.content_digest."""
        d = self.to_dict()
        return canonical_hash_dict(d, excluded_keys={"content_digest"})


# =============================================================================
# Workstream B: Durable Evidence Store (SQLite WAL + Canonical Artifacts)
# =============================================================================

class EvidenceStore:
    """Manages immutable execution records in SQLite WAL registry and JSON artifacts."""

    def __init__(self, evidence_dir: Path | str | None = None, workspace_root: Path | str | None = None):
        self.workspace_root = Path(workspace_root or WORKSPACE_ROOT).resolve()
        self.evidence_dir = Path(evidence_dir or (self.workspace_root / "data" / "execution_evidence")).resolve()
        self.receipts_dir = self.evidence_dir / "receipts"
        self.registry_db_path = self.evidence_dir / "evidence_registry.db"
        self._init_storage()

    def _connect(self, timeout: float = 15.0, isolation_level: str | None = None) -> sqlite3.Connection:
        """Creates a SQLite connection with explicit WAL and durability pragmas."""
        conn = sqlite3.connect(self.registry_db_path, timeout=timeout, isolation_level=isolation_level)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)};")
        return conn

    def _init_storage(self) -> None:
        """Initializes directories and SQLite WAL schema with authoritative payload storage."""
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)

        with self._connect(timeout=15.0) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    execution_pair_id TEXT NOT NULL,
                    execution_role TEXT NOT NULL,
                    target_test_file TEXT NOT NULL,
                    target_test_file_sha256 TEXT NOT NULL,
                    exit_code INTEGER NOT NULL,
                    content_digest TEXT NOT NULL,
                    captured_at_utc TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    payload_json TEXT
                );
            """)
            # Migration check: verify if payload_json column exists
            cols = [col[1] for col in conn.execute("PRAGMA table_info(execution_receipts);").fetchall()]
            if "payload_json" not in cols:
                conn.execute("ALTER TABLE execution_receipts ADD COLUMN payload_json TEXT;")
                conn.commit()

            # Migration check: verify no duplicate execution_ids exist before creating unique index
            cur = conn.execute("""
                SELECT execution_id, COUNT(*) as cnt
                FROM execution_receipts
                GROUP BY execution_id
                HAVING cnt > 1;
            """)
            dups = cur.fetchall()
            if dups:
                dup_ids = [d[0] for d in dups]
                raise RuntimeError(
                    f"EvidenceStore migration failed: duplicate execution_ids found in registry: {dup_ids}. "
                    "Manual conflict resolution required to preserve historical evidence integrity."
                )
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_receipts_exec_id
                ON execution_receipts(execution_id);
            """)
            conn.commit()

        # Idempotent migration of any legacy records with NULL payload_json
        self.migrate_receipt_payloads()

    def migrate_receipt_payloads(self) -> dict[str, Any]:
        """Migrates unpopulated receipt registry rows by backfilling from verified valid artifacts.

        Guarantees:
        - Only backfills when the corresponding JSON artifact exists and is structurally valid.
        - Verifies receipt_id, execution_id, and content_digest match before importing.
        - Preserves missing, corrupted, or conflicting artifacts without modification.
        - Idempotent and restartable.
        - Returns an audit dictionary of migrated and skipped records.
        """
        report: dict[str, Any] = {
            "unfilled_found": 0,
            "backfilled": 0,
            "skipped_missing": [],
            "skipped_corrupt": [],
            "skipped_mismatch": [],
        }

        with self._connect(timeout=15.0) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT receipt_id, execution_id, content_digest, artifact_path
                FROM execution_receipts
                WHERE payload_json IS NULL;
            """)
            unfilled = cur.fetchall()
            report["unfilled_found"] = len(unfilled)

            for rcpt_id, exec_id, exp_digest, rel_art in unfilled:
                target_path = (self.evidence_dir / rel_art).resolve()
                if not target_path.exists() or not target_path.is_file():
                    report["skipped_missing"].append(rcpt_id)
                    continue

                try:
                    raw_text = target_path.read_text(encoding="utf-8")
                    data = json.loads(raw_text)
                    rcpt = ExecutionReceipt.from_dict(data)
                    computed_digest = rcpt.compute_content_digest()

                    if rcpt.receipt_id != rcpt_id or rcpt.execution_id != exec_id:
                        report["skipped_mismatch"].append(f"{rcpt_id}: identity mismatch")
                        continue

                    if computed_digest != exp_digest:
                        report["skipped_mismatch"].append(f"{rcpt_id}: digest mismatch")
                        continue

                    payload_str = json.dumps(rcpt.to_dict(), indent=2, ensure_ascii=False)
                    conn.execute("""
                        UPDATE execution_receipts
                        SET payload_json = ?
                        WHERE receipt_id = ? AND payload_json IS NULL;
                    """, (payload_str, rcpt_id))
                    report["backfilled"] += 1
                except (json.JSONDecodeError, OSError, ValueError, KeyError):
                    report["skipped_corrupt"].append(rcpt_id)
                    continue

            conn.commit()

        return report

    def register_receipt(self, receipt: ExecutionReceipt) -> tuple[bool, str, Path]:
        """Atomically registers an immutable execution receipt with SQLite authority and disk projection.

        Guarantees:
        - SQLite is the authoritative source for receipt identity and canonical payload.
        - receipt_id is unique.
        - execution_id is unique.
        - SQLite transaction commits before filesystem projection.
        - Identical replay (same execution_id and content_digest) returns (True, 'IDEMPOTENT_REPLAY', path).
        - Reuse of execution_id or receipt_id with different content raises ExecutionIdentityConflictError.
        - Filesystem projection uses fsync and atomic replacement.
        """
        artifact_path = self.receipts_dir / f"{receipt.receipt_id}.json"
        rel_artifact = str(artifact_path.relative_to(self.evidence_dir)).replace("\\", "/")
        payload_str = json.dumps(receipt.to_dict(), indent=2, ensure_ascii=False)

        max_busy_retries = 5
        base_backoff = 0.05

        for attempt in range(max_busy_retries):
            try:
                with self._connect(timeout=15.0, isolation_level=None) as conn:
                    conn.execute("BEGIN IMMEDIATE;")
                    try:
                        # 1. Check existing records by receipt_id or execution_id
                        cur = conn.execute("""
                            SELECT receipt_id, execution_id, content_digest, artifact_path
                            FROM execution_receipts
                            WHERE receipt_id = ? OR execution_id = ?;
                        """, (receipt.receipt_id, receipt.execution_id))
                        existing_rows = cur.fetchall()

                        if existing_rows:
                            for ex_rcpt_id, ex_exec_id, ex_digest, ex_rel_art in existing_rows:
                                # Idempotent replay: exact same execution_id AND content_digest
                                if ex_exec_id == receipt.execution_id and ex_digest == receipt.content_digest:
                                    target_art = (self.evidence_dir / ex_rel_art).resolve()
                                    if not target_art.exists():
                                        self._write_receipt_file_atomic(target_art, receipt)
                                    conn.execute("COMMIT;")
                                    return True, "IDEMPOTENT_REPLAY", target_art
                                else:
                                    # Conflicting record: execution_id or receipt_id reused with divergent content
                                    conn.execute("ROLLBACK;")
                                    raise ExecutionIdentityConflictError(
                                        f"Execution identity conflict: execution_id='{receipt.execution_id}' "
                                        f"or receipt_id='{receipt.receipt_id}' already registered with different content."
                                    )

                        # 2. Insert into SQLite registry FIRST (authoritative commit before projection)
                        conn.execute("""
                            INSERT INTO execution_receipts (
                                receipt_id, execution_id, candidate_id, execution_pair_id, execution_role,
                                target_test_file, target_test_file_sha256, exit_code, content_digest,
                                captured_at_utc, artifact_path, payload_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """, (
                            receipt.receipt_id,
                            receipt.execution_id,
                            receipt.candidate_id,
                            receipt.execution_pair_id,
                            receipt.execution_role,
                            receipt.target_test_file,
                            receipt.target_test_file_sha256,
                            receipt.exit_code,
                            receipt.content_digest,
                            receipt.captured_at_utc,
                            rel_artifact,
                            payload_str,
                        ))
                        conn.execute("COMMIT;")

                        # 3. Project artifact file atomically with fsync
                        self._write_receipt_file_atomic(artifact_path, receipt)
                        return True, "PERSISTED", artifact_path
                    except BaseException:
                        try:
                            conn.execute("ROLLBACK;")
                        except sqlite3.Error:
                            pass
                        raise
            except sqlite3.OperationalError as exc:
                if ("locked" in str(exc).lower() or "busy" in str(exc).lower()) and attempt < max_busy_retries - 1:
                    time.sleep(base_backoff * (2 ** attempt))
                    continue
                raise

        raise sqlite3.OperationalError("EvidenceStore database locked after maximum busy retries.")

    def _write_receipt_file_atomic(self, target_path: Path, receipt: ExecutionReceipt) -> bool:
        """Writes receipt JSON to a unique temporary file, flushes to disk, and atomically replaces target."""
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            unique_tmp = target_path.parent / f"{target_path.stem}.tmp_{os.getpid()}_{uuid.uuid4().hex[:8]}"
            content = json.dumps(receipt.to_dict(), indent=2, ensure_ascii=False)
            with open(unique_tmp, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            for attempt in range(5):
                try:
                    unique_tmp.replace(target_path)
                    return True
                except OSError:
                    if attempt == 4:
                        if unique_tmp.exists():
                            try:
                                unique_tmp.unlink()
                            except OSError:
                                pass
                        return False
                    time.sleep(0.01 * (attempt + 1))
            return False
        except OSError:
            return False

    def record_receipt(self, receipt: ExecutionReceipt) -> Path:
        """Persists immutable receipt JSON and records in SQLite WAL registry.

        Backward-compatible wrapper delegating to register_receipt.
        """
        _, _, path = self.register_receipt(receipt)
        return path

    def has_receipt(self, receipt_id: str) -> bool:
        """Checks if a receipt exists in SQLite registry."""
        if not receipt_id:
            return False
        with self._connect(timeout=5.0) as conn:
            cur = conn.execute("SELECT 1 FROM execution_receipts WHERE receipt_id = ? LIMIT 1;", (receipt_id,))
            return cur.fetchone() is not None

    def verify_receipt(self, receipt_id: str) -> tuple[bool, str, ExecutionReceipt | None]:
        """Authenticates receipt against authoritative SQLite registry and disk projection.

        Guarantees:
        - SQLite is the canonical authority.
        - Missing or unpopulated canonical payload is rejected (RECEIPT_PAYLOAD_UNPOPULATED).
        - Malformed payload is rejected (RECEIPT_MALFORMED).
        - Payload digest mismatch with registry is rejected (RECEIPT_PAYLOAD_MISMATCH).
        - Payload identity mismatch with registry is rejected (RECEIPT_TAMPERED).
        - Missing disk projection is reported (RECEIPT_ARTIFACT_MISSING).
        - Tampered or malformed disk projection is rejected (RECEIPT_TAMPERED / RECEIPT_MALFORMED).
        - Returns authoritative ExecutionReceipt object constructed from canonical SQLite payload.
        """
        if not receipt_id:
            return False, "EMPTY_RECEIPT_ID", None

        # 1. Look up SQLite registry row
        with self._connect(timeout=5.0) as conn:
            cur = conn.execute("""
                SELECT execution_id, content_digest, artifact_path, payload_json
                FROM execution_receipts
                WHERE receipt_id = ?;
            """, (receipt_id,))
            row = cur.fetchone()

        if not row:
            return False, "RECEIPT_NOT_IN_REGISTRY", None

        reg_exec_id, reg_digest, rel_artifact, payload_json = row

        # 2. Canonical payload validation (SQLite Authority)
        if payload_json is None or not str(payload_json).strip():
            return False, "RECEIPT_PAYLOAD_UNPOPULATED: Canonical SQLite payload is missing or NULL.", None

        try:
            canonical_data = json.loads(payload_json)
            canonical_receipt = ExecutionReceipt.from_dict(canonical_data)
        except (json.JSONDecodeError, OSError, ValueError, KeyError) as exc:
            return False, f"RECEIPT_MALFORMED: Canonical SQLite payload is malformed: {exc}", None

        # Verify identity matches registry row
        if canonical_receipt.receipt_id != receipt_id or canonical_receipt.execution_id != reg_exec_id:
            return False, "RECEIPT_TAMPERED: Identity mismatch between registry row and canonical payload.", None

        # Verify canonical payload digest matches registry content_digest
        if canonical_receipt.compute_content_digest() != reg_digest:
            return False, "RECEIPT_PAYLOAD_MISMATCH: Canonical payload digest does not match registry content_digest.", None

        # 3. Check receipt projection existence
        artifact_path = (self.evidence_dir / rel_artifact).resolve()
        if not artifact_path.exists() or not artifact_path.is_file():
            return False, f"RECEIPT_ARTIFACT_MISSING: '{artifact_path}' not found.", None

        # 4. Verify disk artifact projection matches registry and canonical payload
        try:
            data = json.loads(artifact_path.read_text(encoding="utf-8"))
            disk_receipt = ExecutionReceipt.from_dict(data)
            disk_digest = disk_receipt.compute_content_digest()

            if disk_receipt.execution_id != reg_exec_id or disk_receipt.receipt_id != receipt_id:
                return False, f"RECEIPT_TAMPERED: Execution ID mismatch between artifact ({disk_receipt.execution_id}) and registry ({reg_exec_id}).", None

            if disk_digest != reg_digest or disk_digest != disk_receipt.content_digest:
                return False, "RECEIPT_TAMPERED: Artifact content digest mismatch with registry.", None

            return True, "VALID_STORED_RECEIPT", canonical_receipt
        except (json.JSONDecodeError, OSError, ValueError, KeyError) as exc:
            return False, f"RECEIPT_MALFORMED: Artifact projection file is malformed: {exc}", None

    def reconcile_receipt_projections(self) -> dict[str, Any]:
        """Reconciles filesystem JSON receipt projections against authoritative SQLite payloads."""
        summary: dict[str, Any] = {
            "checked": 0,
            "rebuilt": 0,
            "clean": 0,
            "orphaned": [],
        }
        with self._connect(timeout=15.0) as conn:
            cur = conn.execute("""
                SELECT receipt_id, content_digest, artifact_path, payload_json
                FROM execution_receipts;
            """)
            rows = cur.fetchall()

        summary["checked"] = len(rows)
        known_ids = set()

        for rcpt_id, dig, rel_art, payload_json in rows:
            known_ids.add(rcpt_id)
            target = (self.evidence_dir / rel_art).resolve()
            needs_rebuild = False
            if not target.exists():
                needs_rebuild = True
            else:
                try:
                    data = json.loads(target.read_text(encoding="utf-8"))
                    receipt = ExecutionReceipt.from_dict(data)
                    if receipt.compute_content_digest() != dig:
                        needs_rebuild = True
                    else:
                        summary["clean"] += 1
                except (json.JSONDecodeError, OSError, ValueError):
                    needs_rebuild = True

            if needs_rebuild and payload_json:
                try:
                    receipt = ExecutionReceipt.from_dict(json.loads(payload_json))
                    if self._write_receipt_file_atomic(target, receipt):
                        summary["rebuilt"] += 1
                except (json.JSONDecodeError, OSError, ValueError, KeyError):
                    continue

        # Check for orphan files
        for f in self.receipts_dir.glob("*.json"):
            if f.stem not in known_ids:
                summary["orphaned"].append(f.name)

        return summary


# =============================================================================
# ExecutionEvidenceEnvelope Data Model & Serialization
# =============================================================================

@dataclass
class ExecutionEvidenceEnvelope:
    """Envelope representation of execution evidence for in-process inspection and candidate binding."""

    schema_version: int = SCHEMA_VERSION
    envelope_id: str = ""                       # "EEV-<hex12>"
    receipt_id: str = ""                        # "RCPT-<uuid4[:12]>"
    execution_id: str = ""                      # UUID hex
    candidate_id: str = ""                      # Linked candidate ID ("LCAND-<hex12>")
    source_event_ids: list[str] = field(default_factory=list)
    execution_pair_id: str = ""                 # UUID hex linking RED and GREEN runs
    execution_role: str = "BASELINE"            # "BASELINE" | "FIX_VERIFICATION"
    captured_at_utc: str = ""                   # ISO 8601 UTC

    executable_path: str = ""
    executable_version: str = ""
    argument_vector: list[str] = field(default_factory=list)
    working_directory: str = ""
    operating_system: str = ""
    python_version: str = ""
    test_runner: str = "pytest"
    test_runner_version: str = ""
    execution_environment: str = "SANDBOX"
    repository_revision: str = ""
    working_tree_clean: bool = False

    exit_code: int = -1
    termination_reason: str = "NORMAL"
    started_at_utc: str = ""
    completed_at_utc: str = ""
    duration_seconds: float = 0.0
    test_collection_count: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_skipped: int = 0
    tests_errored: int = 0
    executed_test_ids: list[str] = field(default_factory=list)
    test_outcomes: dict[str, str] = field(default_factory=dict)
    failure_signatures: list[str] = field(default_factory=list)

    target_test_file: str = ""
    target_test_file_sha256: str = ""
    source_state_manifest: dict[str, str] = field(default_factory=dict)
    configuration_manifest: dict[str, str] = field(default_factory=dict)
    environment_fingerprint: dict[str, str] = field(default_factory=dict)
    stdout_sha256: str = ""
    stderr_sha256: str = ""
    stdout_snippet: str = ""
    stderr_snippet: str = ""
    junit_xml_sha256: str | None = None
    evidence_completeness: str = "COMPLETE"
    provenance_status: str = "AUTHORITATIVE_LOCAL_EXECUTION"
    content_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializes envelope to dictionary with sorted keys."""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        trusted_local: bool = False,
        store: EvidenceStore | None = None,
    ) -> ExecutionEvidenceEnvelope:
        """Constructs ExecutionEvidenceEnvelope from a dictionary.

        DURABLE EVIDENCE RE-AUTHENTICATION:
        Imported dictionaries cannot assert AUTHORITATIVE_LOCAL_EXECUTION at face value.
        If a receipt_id is provided and verified against the durable EvidenceStore,
        provenance_status is assigned 'VERIFIED_LOCAL_EXECUTION'. Otherwise, it is downgraded
        to 'IMPORTED_UNVERIFIED'.
        """
        if not isinstance(data, dict):
            raise TypeError(f"Expected dict, got {type(data).__name__}")

        receipt_id = str(data.get("receipt_id", "") or data.get("envelope_id", ""))
        prov = "IMPORTED_UNVERIFIED"

        if trusted_local:
            prov = "AUTHORITATIVE_LOCAL_EXECUTION"
        elif store and receipt_id and store.has_receipt(receipt_id):
            valid, _, _ = store.verify_receipt(receipt_id)
            if valid:
                prov = "VERIFIED_LOCAL_EXECUTION"

        def _str(k: str, default: str = "") -> str:
            v = data.get(k)
            return str(v) if v is not None else default

        def _int(k: str, default: int = 0) -> int:
            v = data.get(k)
            try:
                return int(v) if v is not None else default
            except (ValueError, TypeError):
                return default

        def _float(k: str, default: float = 0.0) -> float:
            v = data.get(k)
            try:
                return float(v) if v is not None else default
            except (ValueError, TypeError):
                return default

        def _list(k: str) -> list[str]:
            v = data.get(k)
            return [str(x) for x in v] if isinstance(v, list) else []

        def _dict(k: str) -> dict[str, str]:
            v = data.get(k)
            return {str(k_): str(v_) for k_, v_ in v.items()} if isinstance(v, dict) else {}

        role = _str("execution_role", "BASELINE")
        if role not in EXECUTION_ROLES:
            role = "BASELINE"

        term = _str("termination_reason", "NORMAL")
        if term not in TERMINATION_REASONS:
            term = "NORMAL"

        compl = _str("evidence_completeness", "COMPLETE")
        if compl not in EVIDENCE_COMPLETENESS_STATES:
            compl = "COMPLETE"

        junit_sha = data.get("junit_xml_sha256")
        junit_sha_str = str(junit_sha) if junit_sha is not None else None

        return cls(
            schema_version=_int("schema_version", SCHEMA_VERSION),
            envelope_id=_str("envelope_id"),
            receipt_id=_str("receipt_id", receipt_id),
            execution_id=_str("execution_id"),
            candidate_id=_str("candidate_id"),
            source_event_ids=_list("source_event_ids"),
            execution_pair_id=_str("execution_pair_id"),
            execution_role=role,
            captured_at_utc=_str("captured_at_utc"),
            executable_path=_str("executable_path"),
            executable_version=_str("executable_version"),
            argument_vector=_list("argument_vector"),
            working_directory=_str("working_directory"),
            operating_system=_str("operating_system"),
            python_version=_str("python_version"),
            test_runner=_str("test_runner", "pytest"),
            test_runner_version=_str("test_runner_version"),
            execution_environment=_str("execution_environment", "SANDBOX"),
            repository_revision=_str("repository_revision"),
            working_tree_clean=bool(data.get("working_tree_clean", False)),
            exit_code=_int("exit_code", -1),
            termination_reason=term,
            started_at_utc=_str("started_at_utc"),
            completed_at_utc=_str("completed_at_utc"),
            duration_seconds=_float("duration_seconds", 0.0),
            test_collection_count=_int("test_collection_count", 0),
            tests_passed=_int("tests_passed", 0),
            tests_failed=_int("tests_failed", 0),
            tests_skipped=_int("tests_skipped", 0),
            tests_errored=_int("tests_errored", 0),
            executed_test_ids=_list("executed_test_ids"),
            test_outcomes=_dict("test_outcomes"),
            failure_signatures=_list("failure_signatures"),
            target_test_file=_str("target_test_file"),
            target_test_file_sha256=_str("target_test_file_sha256"),
            source_state_manifest=_dict("source_state_manifest"),
            configuration_manifest=_dict("configuration_manifest"),
            environment_fingerprint=_dict("environment_fingerprint"),
            stdout_sha256=_str("stdout_sha256"),
            stderr_sha256=_str("stderr_sha256"),
            stdout_snippet=_str("stdout_snippet"),
            stderr_snippet=_str("stderr_snippet"),
            junit_xml_sha256=junit_sha_str,
            evidence_completeness=compl,
            provenance_status=prov,
            content_digest=_str("content_digest"),
        )

    @classmethod
    def from_receipt(cls, receipt: ExecutionReceipt, *, trusted_local: bool = True) -> ExecutionEvidenceEnvelope:
        """Constructs an envelope backed by an immutable ExecutionReceipt."""
        d = receipt.to_dict()
        prov = "AUTHORITATIVE_LOCAL_EXECUTION" if trusted_local else "VERIFIED_LOCAL_EXECUTION"
        env = cls.from_dict(d, trusted_local=trusted_local)
        env.envelope_id = f"EEV-{receipt.receipt_id[5:]}" if receipt.receipt_id.startswith("RCPT-") else f"EEV-{receipt.receipt_id}"
        env.receipt_id = receipt.receipt_id
        env.provenance_status = prov
        env.content_digest = receipt.content_digest
        return env

    def compute_content_digest(self) -> str:
        """Computes deterministic digest over outcome and integrity fields."""
        return canonical_hash_dict(self.to_dict(), excluded_keys={"envelope_id", "provenance_status", "content_digest"})

    def verify_structural_integrity(self) -> tuple[bool, str]:
        """Validates internal consistency and field structure."""
        if not self.envelope_id and not self.receipt_id:
            return False, "envelope_id and receipt_id cannot both be empty."
        if not self.execution_id:
            return False, "execution_id cannot be empty."
        if not self.candidate_id:
            return False, "candidate_id cannot be empty."
        if not self.execution_pair_id:
            return False, "execution_pair_id cannot be empty."
        if self.execution_role not in EXECUTION_ROLES:
            return False, f"Invalid execution_role '{self.execution_role}'."
        if self.termination_reason not in TERMINATION_REASONS:
            return False, f"Invalid termination_reason '{self.termination_reason}'."
        if not self.target_test_file:
            return False, "target_test_file cannot be empty."
        if not self.target_test_file_sha256:
            return False, "target_test_file_sha256 cannot be empty."
        if not self.repository_revision:
            return False, "repository_revision cannot be empty."
        if len(self.stdout_snippet.encode("utf-8")) > MAX_SNIPPET_BYTES * 2:
            return False, "stdout_snippet exceeds bounded size cap."
        if len(self.stderr_snippet.encode("utf-8")) > MAX_SNIPPET_BYTES * 2:
            return False, "stderr_snippet exceeds bounded size cap."
        return True, "OK"


# =============================================================================
# Workstream B: Subprocess Execution Witness & Evidence Collector
# =============================================================================

class ExecutionEvidenceCollector:
    """Witnesses local subprocess test execution, captures state, and records immutable receipts."""

    def __init__(
        self,
        workspace_root: Path | str | None = None,
        evidence_dir: Path | str | None = None,
    ):
        self.workspace_root = Path(workspace_root or WORKSPACE_ROOT).resolve()
        self.evidence_dir = Path(evidence_dir or (self.workspace_root / "data" / "execution_evidence")).resolve()
        self.evidence_store = EvidenceStore(evidence_dir=self.evidence_dir, workspace_root=self.workspace_root)

    def _build_isolated_environment(self) -> dict[str, str]:
        """Constructs a narrowly isolated execution environment for pytest subprocesses.

        Whitelists essential Windows system environment variables while scrubbing
        unapproved injection vectors (PYTEST_ADDOPTS, PYTEST_PLUGINS) and binding
        PYTHONPATH strictly to the authorized workspace root.
        """
        allowed_sys_keys = {
            "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP",
            "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
            "COMSPEC", "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
            "PROCESSOR_IDENTIFIER", "OS", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"
        }
        isolated_env: dict[str, str] = {}
        for k, v in os.environ.items():
            k_upper = k.upper()
            if k_upper in allowed_sys_keys:
                isolated_env[k] = v

        # Strictly bind PYTHONPATH to the authorized workspace root
        isolated_env["PYTHONPATH"] = str(self.workspace_root)

        # Enforce pytest plugin autoload isolation
        isolated_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

        # Explicitly scrub prohibited configuration vectors
        isolated_env.pop("PYTEST_ADDOPTS", None)
        isolated_env.pop("PYTEST_PLUGINS", None)

        return isolated_env

    def _resolve_pytest_bin(self) -> list[str]:
        """Resolves the pytest command using workspace resolver if available."""
        try:
            from tools.run_fast_tests import get_pytest_cmd
            return get_pytest_cmd()
        except ImportError:
            pytest_exe = Path(sys.executable).parent / "pytest.exe"
            if pytest_exe.exists():
                return [str(pytest_exe)]
            parent_pytest = self.workspace_root.parent / ".venv" / "Scripts" / "pytest.exe"
            if parent_pytest.exists():
                return [str(parent_pytest)]
            return [sys.executable, "-m", "pytest"]

    def _get_git_context(self) -> tuple[str, bool]:
        """Extracts current git commit HEAD and clean status."""
        rev = "UNKNOWN_REVISION"
        clean = False
        try:
            proc_rev = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc_rev.returncode == 0 and proc_rev.stdout.strip():
                rev = proc_rev.stdout.strip()

            proc_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc_status.returncode == 0:
                clean = len(proc_status.stdout.strip()) == 0
        except (OSError, subprocess.SubprocessError):
            pass
        return rev, clean

    def _capture_source_state_manifest(self, candidate_id: str | None = None) -> dict[str, str]:
        """Captures SHA-256 hashes of tracked modified source files and known project modules."""
        manifest: dict[str, str] = {}
        try:
            proc = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    if len(line) < 4:
                        continue
                    fname = line[3:].strip().replace('"', "")
                    # Fingerprint modified python source files (excluding tests/ and data/)
                    if fname.endswith(".py") and not fname.startswith("tests/") and not fname.startswith("data/"):
                        fpath = self.workspace_root / fname
                        if fpath.exists() and fpath.is_file():
                            manifest[fname.replace("\\", "/")] = hashlib.sha256(fpath.read_bytes()).hexdigest()
        except (OSError, subprocess.SubprocessError):
            pass

        # Also inspect workspace root for direct module files in fixtures (e.g. app.py, worker.py)
        for cand_file in self.workspace_root.glob("*.py"):
            if cand_file.name not in ("conftest.py",) and not cand_file.name.startswith("test_"):
                manifest[cand_file.name] = hashlib.sha256(cand_file.read_bytes()).hexdigest()
        for cand_file in (self.workspace_root / "src").glob("**/*.py"):
            rel_name = str(cand_file.relative_to(self.workspace_root)).replace("\\", "/")
            manifest[rel_name] = hashlib.sha256(cand_file.read_bytes()).hexdigest()

        return manifest

    def _capture_configuration_manifest(self) -> dict[str, str]:
        """Captures SHA-256 hashes of test runner configurations and fixtures."""
        cfg_manifest: dict[str, str] = {}
        for cfg_name in ("pytest.ini", "conftest.py", "pyproject.toml"):
            p = self.workspace_root / cfg_name
            if p.exists() and p.is_file():
                cfg_manifest[cfg_name] = hashlib.sha256(p.read_bytes()).hexdigest()
        # Also check tests/conftest.py
        tests_conftest = self.workspace_root / "tests" / "conftest.py"
        if tests_conftest.exists() and tests_conftest.is_file():
            cfg_manifest["tests/conftest.py"] = hashlib.sha256(tests_conftest.read_bytes()).hexdigest()
        return cfg_manifest

    def _parse_junit_xml(
        self, xml_path: Path
    ) -> tuple[int, int, int, int, int, list[str], dict[str, str], list[str]]:
        """Parses JUnit XML report to extract test counts, node IDs, outcomes, and signatures."""
        if not xml_path.exists() or xml_path.stat().st_size == 0:
            return 0, 0, 0, 0, 0, [], {}, []

        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()

            total = 0
            failures = 0
            errors = 0
            skipped = 0

            test_ids: list[str] = []
            test_outcomes: dict[str, str] = {}
            signatures: list[str] = []

            suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
            for suite in suites:
                total += int(suite.attrib.get("tests", 0))
                failures += int(suite.attrib.get("failures", 0))
                errors += int(suite.attrib.get("errors", 0))
                skipped += int(suite.attrib.get("skipped", 0))

                for tc in suite.findall("testcase"):
                    classname = tc.attrib.get("classname", "")
                    name = tc.attrib.get("name", "")
                    node_id = f"{classname}::{name}" if classname else name
                    test_ids.append(node_id)

                    fail_elem = tc.find("failure")
                    err_elem = tc.find("error")
                    skip_elem = tc.find("skipped")

                    if fail_elem is not None:
                        test_outcomes[node_id] = "FAILED"
                        msg = fail_elem.attrib.get("message") or fail_elem.text or "Assertion failed"
                        signatures.append(msg.strip()[:500])
                    elif err_elem is not None:
                        test_outcomes[node_id] = "ERROR"
                        msg = err_elem.attrib.get("message") or err_elem.text or "Execution error"
                        signatures.append(msg.strip()[:500])
                    elif skip_elem is not None:
                        test_outcomes[node_id] = "SKIPPED"
                    else:
                        test_outcomes[node_id] = "PASSED"

            passed = max(0, total - (failures + errors + skipped))
            return total, passed, failures, skipped, errors, test_ids, test_outcomes, signatures
        except (ET.ParseError, OSError, ValueError):
            return 0, 0, 0, 0, 0, [], {}, []

    def capture_test_execution(
        self,
        *,
        candidate_id: str,
        source_event_ids: list[str],
        execution_role: str,
        test_target: str | Path,
        execution_pair_id: str | None = None,
        extra_args: list[str] | None = None,
        timeout_seconds: float = 45.0,
        persist: bool = True,
    ) -> ExecutionEvidenceEnvelope:
        """Executes a targeted test command under observation and produces an authoritative envelope."""
        if execution_role not in EXECUTION_ROLES:
            raise ValueError(f"Invalid execution_role '{execution_role}'. Must be one of: {EXECUTION_ROLES}")

        if execution_role == "FIX_VERIFICATION" and not execution_pair_id:
            raise ValueError("execution_pair_id is required for FIX_VERIFICATION executions.")

        # Workstream D: Strict Argument and Path Validation
        target_path, full_target_str, sanitized_flags = validate_collector_arguments(
            test_target, extra_args=extra_args, allowed_root=self.workspace_root
        )

        pair_id = execution_pair_id or f"PAIR-{uuid.uuid4().hex[:12]}"
        exec_id = f"EXEC-{uuid.uuid4().hex[:12]}"
        receipt_id = f"RCPT-{uuid.uuid4().hex[:12]}"

        rel_test_file = str(target_path.relative_to(self.workspace_root)).replace("\\", "/")
        test_file_sha256 = hashlib.sha256(target_path.read_bytes()).hexdigest()

        # Build pytest command vector
        pytest_cmd = self._resolve_pytest_bin()
        xml_tmp = Path(tempfile.gettempdir()) / f"jarvis_junit_{uuid.uuid4().hex[:8]}.xml"

        cmd = list(pytest_cmd)
        cmd.append(full_target_str)
        cmd.append(f"--junitxml={xml_tmp}")
        cmd.append("-q")
        if sanitized_flags:
            cmd.extend(sanitized_flags)

        git_rev, git_clean = self._get_git_context()
        source_manifest = self._capture_source_state_manifest(candidate_id=candidate_id)
        config_manifest = self._capture_configuration_manifest()

        now_started = datetime.datetime.now(datetime.UTC)
        started_iso = now_started.isoformat()

        exit_code = -1
        term_reason = "NORMAL"
        stdout_raw = ""
        stderr_raw = ""

        child_env = self._build_isolated_environment()
        try:
            res = subprocess.run(
                cmd,
                cwd=str(self.workspace_root),
                env=child_env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = res.returncode
            stdout_raw = res.stdout or ""
            stderr_raw = res.stderr or ""
        except subprocess.TimeoutExpired as exc:
            term_reason = "TIMEOUT"
            stdout_raw = exc.stdout or "" if isinstance(exc.stdout, str) else ""
            stderr_raw = exc.stderr or "" if isinstance(exc.stderr, str) else ""
        except OSError as exc:
            term_reason = "INFRASTRUCTURE_FAILURE"
            stderr_raw = f"Subprocess launch error: {exc}"

        now_completed = datetime.datetime.now(datetime.UTC)
        completed_iso = now_completed.isoformat()
        duration = max(0.0, (now_completed - now_started).total_seconds())

        total_c, pass_c, fail_c, skip_c, err_c, test_ids, test_outcomes, signatures = (
            0, 0, 0, 0, 0, [], {}, []
        )
        junit_sha = None

        if xml_tmp.exists():
            try:
                xml_bytes = xml_tmp.read_bytes()
                junit_sha = hashlib.sha256(xml_bytes).hexdigest()
                (
                    total_c, pass_c, fail_c, skip_c, err_c,
                    test_ids, test_outcomes, signatures
                ) = self._parse_junit_xml(xml_tmp)
            finally:
                try:
                    xml_tmp.unlink()
                except OSError:
                    pass

        completeness = "COMPLETE"
        if term_reason != "NORMAL":
            completeness = "PARTIAL"
        elif total_c == 0 and exit_code != 0:
            completeness = "PARTIAL"
            signatures.append(f"Test runner error (exit {exit_code}) without test collection")

        clean_stdout_snip, _ = redact_sensitive_text(stdout_raw[:MAX_SNIPPET_BYTES])
        clean_stderr_snip, _ = redact_sensitive_text(stderr_raw[:MAX_SNIPPET_BYTES])
        redacted_signatures = [redact_sensitive_text(s)[0] for s in signatures]

        env_fingerprint = {
            "python_version": sys.version.split()[0],
            "pytest_version": self._get_pytest_version(),
            "platform": platform.platform(),
            "env_isolation_policy": "STRICT_NARROW",
            "pytest_disable_plugin_autoload": child_env.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"),
            "pythonpath_bound": "WORKSPACE_ROOT",
        }

        # Build raw receipt dictionary for content digest computation
        receipt_dict: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "receipt_id": receipt_id,
            "execution_id": exec_id,
            "candidate_id": candidate_id,
            "source_event_ids": list(source_event_ids),
            "execution_pair_id": pair_id,
            "execution_role": execution_role,
            "captured_at_utc": completed_iso,
            "argument_vector": cmd,
            "working_directory": str(self.workspace_root),
            "repository_revision": git_rev,
            "working_tree_clean": git_clean,
            "exit_code": exit_code,
            "termination_reason": term_reason,
            "started_at_utc": started_iso,
            "completed_at_utc": completed_iso,
            "duration_seconds": round(duration, 4),
            "test_collection_count": total_c,
            "tests_passed": pass_c,
            "tests_failed": fail_c,
            "tests_skipped": skip_c,
            "tests_errored": err_c,
            "executed_test_ids": test_ids,
            "test_outcomes": test_outcomes,
            "failure_signatures": redacted_signatures,
            "target_test_file": rel_test_file,
            "target_test_file_sha256": test_file_sha256,
            "source_state_manifest": source_manifest,
            "configuration_manifest": config_manifest,
            "environment_fingerprint": env_fingerprint,
            "stdout_sha256": hashlib.sha256(stdout_raw.encode("utf-8")).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr_raw.encode("utf-8")).hexdigest(),
            "stdout_snippet": clean_stdout_snip,
            "stderr_snippet": clean_stderr_snip,
            "junit_xml_sha256": junit_sha,
            "evidence_completeness": completeness,
        }

        content_digest = canonical_hash_dict(receipt_dict)
        receipt_dict["content_digest"] = content_digest

        # Construct immutable ExecutionReceipt
        receipt = ExecutionReceipt.from_dict(receipt_dict)

        if persist:
            self.evidence_store.record_receipt(receipt)

        # Construct and return envelope
        envelope = ExecutionEvidenceEnvelope.from_receipt(receipt, trusted_local=True)
        return envelope

    def _get_pytest_version(self) -> str:
        """Gets pytest version string."""
        try:
            import pytest
            return pytest.__version__
        except (ImportError, AttributeError):
            return "unknown"


# =============================================================================
# Workstream C: RED-GREEN Verification Protocol
# =============================================================================

class RedGreenVerificationEngine:
    """Enforces empirical pairing rules between baseline RED executions and fix GREEN executions."""

    @classmethod
    def validate_baseline_red(
        cls,
        envelope: ExecutionEvidenceEnvelope,
        candidate_or_signal: Any = None,
        workspace_root: Path | str | None = None,
        store: EvidenceStore | None = None,
    ) -> tuple[bool, str]:
        """Validates that a baseline execution envelope demonstrates defect reproduction (RED)."""
        valid_struct, err = envelope.verify_structural_integrity()
        if not valid_struct:
            return False, f"Structural integrity failure: {err}"

        # 1. Provenance Verification
        if envelope.provenance_status not in ("AUTHORITATIVE_LOCAL_EXECUTION", "VERIFIED_LOCAL_EXECUTION"):
            return False, f"Untrusted provenance: status is '{envelope.provenance_status}' (expected AUTHORITATIVE_LOCAL_EXECUTION or VERIFIED_LOCAL_EXECUTION)."

        # 2. Evidence Store Authenticity (if receipt exists and store provided)
        if store and envelope.receipt_id:
            if not store.has_receipt(envelope.receipt_id):
                return False, f"Receipt '{envelope.receipt_id}' not found in EvidenceStore registry."
            valid_receipt, receipt_msg, _ = store.verify_receipt(envelope.receipt_id)
            if not valid_receipt:
                return False, f"Store authenticity verification failed: {receipt_msg}"

        if envelope.execution_role != "BASELINE":
            return False, f"Role mismatch: envelope role is '{envelope.execution_role}' (expected BASELINE)."

        if envelope.evidence_completeness != "COMPLETE":
            return False, f"Incomplete evidence: completeness status is '{envelope.evidence_completeness}'."

        if envelope.termination_reason != "NORMAL":
            return False, f"Abnormal termination: reason is '{envelope.termination_reason}' (expected NORMAL)."

        if envelope.test_collection_count <= 0:
            return False, "Test collection failure: 0 tests collected."

        if envelope.tests_failed <= 0 and envelope.tests_errored <= 0:
            return False, "Absence of failure: no failed or errored tests recorded in baseline run."

        if envelope.tests_skipped >= envelope.test_collection_count:
            return False, "Invalid reproduction: all tests were skipped."

        if envelope.exit_code == 0:
            return False, "Invalid reproduction: process exit code is 0 (expected non-zero failure exit code)."

        # Must have at least one failing or errored test outcome
        failing_nodes = [k for k, v in envelope.test_outcomes.items() if v in ("FAILED", "ERROR")]
        if not failing_nodes:
            return False, "Invalid RED baseline: test_outcomes contains 0 failed or errored test outcomes."

        # Candidate ID check
        if candidate_or_signal and hasattr(candidate_or_signal, "candidate_id"):
            cand_id = getattr(candidate_or_signal, "candidate_id", "")
            if cand_id and envelope.candidate_id and envelope.candidate_id != cand_id:
                return False, f"Candidate ID mismatch: envelope has '{envelope.candidate_id}' while candidate is '{cand_id}'."

        # Validate that the test file exists on disk
        root = Path(workspace_root or envelope.working_directory or WORKSPACE_ROOT).resolve()
        target_path = (root / envelope.target_test_file).resolve()
        if not target_path.exists() or not target_path.is_file():
            # Check direct path fallback
            p_direct = Path(envelope.target_test_file).resolve()
            if not p_direct.exists() or not p_direct.is_file():
                return False, f"Test artifact missing: '{envelope.target_test_file}' not found on disk."

        # Workstream C: Structured Failure Symptom Matching
        if candidate_or_signal:
            expected_signal = ""
            if isinstance(candidate_or_signal, str):
                expected_signal = candidate_or_signal
            elif hasattr(candidate_or_signal, "observed_behavior"):
                obs = candidate_or_signal.observed_behavior or {}
                expected_signal = str(obs.get("failure_signal") or obs.get("summary") or "")

            if expected_signal.strip():
                clean_exp = expected_signal.lower().strip()
                combined_output = (
                    " ".join(envelope.failure_signatures) + " " +
                    envelope.stderr_snippet + " " +
                    envelope.stdout_snippet
                ).lower()

                # 1. Exact phrase matching
                if clean_exp in combined_output:
                    return True, "VALID_BASELINE_RED"

                # 2. Extract technical tokens (exceptions, error codes, identifiers)
                sig_tokens = [w for w in re.findall(r"\b[a-zA-Z0-9_]{3,}\b", clean_exp) if w not in ("test", "error", "failed", "failure", "with", "from", "step")]
                
                # Require matching significant tokens
                matched_tokens = [t for t in sig_tokens if t in combined_output]
                if len(sig_tokens) > 0 and len(matched_tokens) == 0:
                    return False, f"Failure symptom inconsistency: observed output did not match expected failure signal '{expected_signal[:100]}'."

        return True, "VALID_BASELINE_RED"

    @classmethod
    def validate_fix_green(
        cls,
        green_envelope: ExecutionEvidenceEnvelope,
        red_envelope: ExecutionEvidenceEnvelope,
        candidate_or_signal: Any = None,
        workspace_root: Path | str | None = None,
        store: EvidenceStore | None = None,
    ) -> tuple[bool, str]:
        """Validates that a GREEN execution envelope confirms fix verification against a valid RED run."""
        # 1. Validate RED baseline first
        red_valid, red_msg = cls.validate_baseline_red(
            red_envelope, candidate_or_signal=candidate_or_signal, workspace_root=workspace_root, store=store
        )
        if not red_valid:
            return False, f"Invalid baseline RED envelope: {red_msg}"

        # 2. GREEN Structural and provenance verification
        valid_struct, err = green_envelope.verify_structural_integrity()
        if not valid_struct:
            return False, f"GREEN structural integrity failure: {err}"

        if green_envelope.provenance_status not in ("AUTHORITATIVE_LOCAL_EXECUTION", "VERIFIED_LOCAL_EXECUTION"):
            return False, f"Untrusted GREEN provenance: status is '{green_envelope.provenance_status}'."

        if store and green_envelope.receipt_id:
            if not store.has_receipt(green_envelope.receipt_id):
                return False, f"GREEN receipt '{green_envelope.receipt_id}' not found in EvidenceStore registry."
            valid_receipt, receipt_msg, _ = store.verify_receipt(green_envelope.receipt_id)
            if not valid_receipt:
                return False, f"GREEN store authenticity verification failed: {receipt_msg}"

        if green_envelope.execution_role != "FIX_VERIFICATION":
            return False, f"GREEN role mismatch: envelope role is '{green_envelope.execution_role}' (expected FIX_VERIFICATION)."

        if green_envelope.execution_pair_id != red_envelope.execution_pair_id:
            return False, (
                f"Execution pair mismatch: GREEN pair '{green_envelope.execution_pair_id}' "
                f"does not match RED pair '{red_envelope.execution_pair_id}'."
            )

        if green_envelope.evidence_completeness != "COMPLETE":
            return False, f"Incomplete GREEN evidence: completeness status is '{green_envelope.evidence_completeness}'."

        if green_envelope.termination_reason != "NORMAL":
            return False, f"Abnormal GREEN termination: reason is '{green_envelope.termination_reason}'."

        # 3. Test Definition Invariance: exact file and SHA-256 match
        if green_envelope.target_test_file != red_envelope.target_test_file:
            return False, (
                f"Test target mismatch: GREEN targeted '{green_envelope.target_test_file}' "
                f"while RED targeted '{red_envelope.target_test_file}'."
            )

        if green_envelope.target_test_file_sha256 != red_envelope.target_test_file_sha256:
            return False, (
                f"Test definition mutated: test file SHA-256 changed between RED "
                f"({red_envelope.target_test_file_sha256[:12]}) and GREEN "
                f"({green_envelope.target_test_file_sha256[:12]}). Must verify against identical test."
            )

        # 4. Test Selection Invariance
        if set(green_envelope.executed_test_ids) != set(red_envelope.executed_test_ids):
            return False, (
                f"Test selection mismatch: executed test IDs in GREEN ({green_envelope.executed_test_ids}) "
                f"do not match RED ({red_envelope.executed_test_ids})."
            )

        # 5. Configuration Manifest Invariance
        if green_envelope.configuration_manifest != red_envelope.configuration_manifest:
            return False, "CONFIGURATION_MUTATED: Pytest configuration or fixtures changed between RED and GREEN."

        # 5b. Environment Fingerprint Comparability
        if green_envelope.environment_fingerprint != red_envelope.environment_fingerprint:
            red_env = dict(red_envelope.environment_fingerprint)
            green_env = dict(green_envelope.environment_fingerprint)
            for k in ("python_version", "pytest_version", "env_isolation_policy", "pytest_disable_plugin_autoload"):
                if red_env.get(k) != green_env.get(k):
                    return False, f"ENVIRONMENT_MUTATED: Execution environment key '{k}' changed between RED ({red_env.get(k)}) and GREEN ({green_env.get(k)})."

        # 6. Pass Requirements
        if green_envelope.test_collection_count <= 0:
            return False, "GREEN collection failure: 0 tests collected."

        if green_envelope.tests_failed > 0 or green_envelope.tests_errored > 0:
            return False, f"GREEN failure: {green_envelope.tests_failed} failed, {green_envelope.tests_errored} errored."

        if green_envelope.tests_passed <= 0:
            return False, "GREEN absence of pass: 0 tests passed."

        if green_envelope.exit_code != 0:
            return False, f"GREEN non-zero exit: exit code is {green_envelope.exit_code} (expected 0)."

        # 7. Positive Resolution of Specific Failed Nodes
        red_failures = [node for node, out in red_envelope.test_outcomes.items() if out in ("FAILED", "ERROR")]
        if not red_failures:
            return False, "Invalid RED baseline: 0 failed test outcomes to resolve in GREEN."

        for red_node_id in red_failures:
            green_outcome = green_envelope.test_outcomes.get(red_node_id)
            if green_outcome != "PASSED":
                return False, f"Test '{red_node_id}' failed in RED but outcome in GREEN is '{green_outcome}' (expected PASSED)."

        # 8. Workstream C: Implementation State Fingerprint & Flaky Rerun Detection
        # An unchanged codebase cannot claim fix verification merely because a second run passes
        if (
            green_envelope.source_state_manifest == red_envelope.source_state_manifest
            and green_envelope.configuration_manifest == red_envelope.configuration_manifest
        ):
            return False, (
                "UNCHANGED_IMPLEMENTATION_RERUN: GREEN implementation state is identical to RED baseline. "
                "Re-executing an unmodified codebase cannot establish fix verification (flaky or unverified rerun)."
            )

        # 9. Verify Modified Files are Relevant to Candidate
        if candidate_or_signal:
            affected_subsystems: list[str] = []
            if hasattr(candidate_or_signal, "affected_subsystems"):
                affected_subsystems = [s.lower() for s in getattr(candidate_or_signal, "affected_subsystems", []) if s]

            if affected_subsystems:
                # Collect modified source files
                changed_sources = [
                    f for f, h in green_envelope.source_state_manifest.items()
                    if red_envelope.source_state_manifest.get(f) != h
                ]
                if changed_sources:
                    relevant_match = False
                    for src in changed_sources:
                        src_lower = src.lower()
                        for sub in affected_subsystems:
                            if sub in src_lower:
                                relevant_match = True
                                break
                        if relevant_match:
                            break

                    if not relevant_match:
                        return False, (
                            f"UNRELATED_SOURCE_CHANGE: Modified files {changed_sources} do not match "
                            f"candidate's affected subsystems ({affected_subsystems})."
                        )

        return True, "VALID_FIX_GREEN"


# =============================================================================
# Helper Utilities
# =============================================================================

def create_authoritative_red_green_pair(
    collector: ExecutionEvidenceCollector,
    *,
    candidate_id: str,
    source_event_ids: list[str],
    test_target: str | Path,
) -> tuple[ExecutionEvidenceEnvelope, ExecutionEvidenceEnvelope]:
    """Helper that captures a RED execution followed by a paired GREEN execution."""
    red = collector.capture_test_execution(
        candidate_id=candidate_id,
        source_event_ids=source_event_ids,
        execution_role="BASELINE",
        test_target=test_target,
    )
    green = collector.capture_test_execution(
        candidate_id=candidate_id,
        source_event_ids=source_event_ids,
        execution_role="FIX_VERIFICATION",
        test_target=test_target,
        execution_pair_id=red.execution_pair_id,
    )
    return red, green
