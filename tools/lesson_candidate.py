"""Fail-Closed Candidate Lifecycle & Memory Promotion Hardening Engine (ENG-OS-02 Phase 3C2).

Implements a deterministic, preview-only lesson candidate review engine on top of
the Phase 3B failure-event collector. Eliminates autonomous self-approval, rejects
untrusted deserialized authorization, hardens specialist provenance, and enforces
that candidate promotion is preview-only without modifying permanent memory.

SECURITY & ARCHITECTURAL INVARIANTS:
1. Primary Security Invariant:
   No model-controlled Python function, script, hook, candidate object, or deserialized
   artifact may independently establish human approval or authorize a permanent write
   to docs/agent/LESSONS.md.
2. Clear Security Separation:
   - Content Integrity: Deterministic SHA-256 hash of canonical JSON verifies data
     tampering ONLY. It does NOT authenticate a human decision.
   - Action Manifest Integrity: SHA-256 digest binding candidate content, canonical
     destination path, proposed lesson ID, and diff hash.
   - Evidence Provenance: Preserves verifiable step indices, source event IDs, and paths.
   - Specialist Provenance: Marked as AGENT_REPORTED_UNVERIFIED. Calling an allowlisted
     specialist name does not imply cryptographic or authenticated subagent execution.
   - Human Authorization: External developer decision, NEVER established by in-process code.
   - Promotion Authorization: Strictly external; in-process promotion is disabled.
   - Promotion Execution: Preview-only. No candidate engine API writes to LESSONS.md.
3. Deserialization Defense:
   - LessonCandidate.from_dict() treats all imported JSON as untrusted.
   - Imported "approved": true fields are strictly stripped and downgraded.
4. Markdown Injection & Secret Redaction:
   - Final rendering boundary enforces re-redaction of all candidate fields.
   - Neutralizes Markdown heading injection (#) and metadata bullet injection (- **).
"""
from __future__ import annotations

import dataclasses
import datetime
import difflib
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Ensure workspace root is in sys.path for direct imports
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

try:
    from tools.failure_collector import (
        EventEnvelope,
        redact_sensitive_text,
        validate_path_confinement,
    )
except ImportError:
    from failure_collector import (
        EventEnvelope,
        redact_sensitive_text,
        validate_path_confinement,
    )

try:
    from tools.execution_evidence import (
        EvidenceStore,
        ExecutionEvidenceEnvelope,
        RedGreenVerificationEngine,
    )
except ImportError:
    from execution_evidence import (
        EvidenceStore,
        ExecutionEvidenceEnvelope,
        RedGreenVerificationEngine,
    )

SCHEMA_VERSION = 1
MAX_EVIDENCE_READ_BYTES = 64 * 1024  # 64 KB strict read cap
MAX_FIELD_LENGTH_BYTES = 4096       # 4 KB field length cap
MAX_INLINE_FIELD_LENGTH_BYTES = 1024  # 1 KB inline field cap

TAXONOMY_CATEGORIES = (
    "DEFECT_IMPLEMENTATION",
    "DEFECT_REGRESSION",
    "LIMITATION_ENVIRONMENT",
    "ASSUMPTION_INCORRECT",
    "CLAIM_UNSUPPORTED",
    "ORCHESTRATION_FAILURE",
    "PROMPT_SCOPE_AMBIGUITY",
    "INCONCLUSIVE",
)

# Lifecycle states represent operational evaluation, NOT human approval
LIFECYCLE_STATES = (
    "OBSERVED",
    "NEEDS_EVIDENCE",
    "PROPOSED",
    "UNDER_REVIEW",
    "REVIEWED",
    "REPRODUCTION_CONFIRMED",
    "FIX_VERIFIED",
    "PREPARED_FOR_REVIEW",
    "REJECTED",
    "SUPERSEDED",
)

ALLOWED_SPECIALISTS = (
    "security-auditor",
    "systems-architect",
    "test-guardian",
)

CONFIDENCE_LEVELS = (
    "UNCONFIRMED",
    "LOW",
    "MEDIUM",
    "HIGH",
)


# -----------------------------------------------------------------------------
# Versioned Candidate Data Model
# -----------------------------------------------------------------------------

@dataclass
class LessonCandidate:
    """Versioned candidate data model for evidence-based lesson review."""
    schema_version: int = SCHEMA_VERSION
    candidate_id: str = ""
    source_event_ids: list[str] = field(default_factory=list)
    failure_classification: str = "INCONCLUSIVE"
    affected_subsystems: list[str] = field(default_factory=list)
    observed_behavior: dict[str, Any] = field(default_factory=dict)
    root_cause_hypothesis: dict[str, Any] = field(default_factory=dict)
    verified_root_cause: str | None = None
    evidence_references: list[dict[str, Any]] = field(default_factory=list)
    reproduction_procedure: str = ""
    scope_of_applicability: str = ""
    corrective_recommendation: str = ""
    confidence_level: str = "UNCONFIRMED"
    known_counterexamples: list[str] = field(default_factory=list)
    regression_test: str = ""
    deduplication_key: str = ""
    review_status: str = "OBSERVED"
    review_findings: list[dict[str, Any]] = field(default_factory=list)
    execution_envelopes: list[dict[str, Any]] = field(default_factory=list)
    evidence_provenance_status: str = "NO_EVIDENCE"
    human_approval: dict[str, Any] = field(default_factory=lambda: {
        "approved": False,
        "status": "UNAUTHORIZED_PREVIEW_ONLY",
        "note": "Untrusted artifact; human authorization cannot be established in code.",
    })

    def attach_execution_envelope(self, envelope: ExecutionEvidenceEnvelope | dict[str, Any]) -> None:
        """Attaches an execution envelope, updating evidence status and deduplicating by envelope_id."""
        if isinstance(envelope, ExecutionEvidenceEnvelope):
            env_dict = envelope.to_dict()
        elif isinstance(envelope, dict):
            env_dict = dict(envelope)
        else:
            raise TypeError(f"Expected ExecutionEvidenceEnvelope or dict, got {type(envelope).__name__}")

        env_id = env_dict.get("envelope_id", "")
        for idx, existing in enumerate(self.execution_envelopes):
            if existing.get("envelope_id") == env_id and env_id:
                self.execution_envelopes[idx] = env_dict
                self._update_evidence_status()
                return

        self.execution_envelopes.append(env_dict)
        self._update_evidence_status()

    def get_execution_envelopes(
        self,
        store: Any = None,
        workspace_root: Path | str | None = None,
    ) -> list[ExecutionEvidenceEnvelope]:
        """Returns deserialized ExecutionEvidenceEnvelope objects authenticated against EvidenceStore."""
        ev_store = store
        if ev_store is None:
            try:
                ev_store = EvidenceStore(workspace_root=workspace_root or WORKSPACE_ROOT)
            except (OSError, ValueError, sqlite3.Error):
                ev_store = None

        envelopes: list[ExecutionEvidenceEnvelope] = []
        for d in self.execution_envelopes:
            try:
                receipt_id = str(d.get("receipt_id") or d.get("envelope_id") or "")
                # If store has receipt, verify authenticity and use it
                if ev_store and receipt_id and ev_store.has_receipt(receipt_id):
                    valid, _, receipt = ev_store.verify_receipt(receipt_id)
                    if valid and receipt:
                        env = ExecutionEvidenceEnvelope.from_receipt(receipt, trusted_local=False)
                        envelopes.append(env)
                        continue

                trusted = d.get("provenance_status") == "AUTHORITATIVE_LOCAL_EXECUTION"
                env = ExecutionEvidenceEnvelope.from_dict(d, trusted_local=trusted, store=ev_store)
                envelopes.append(env)
            except (KeyError, ValueError, TypeError):
                pass
        return envelopes

    def _update_evidence_status(self, store: Any = None, workspace_root: Path | str | None = None) -> None:
        """Recalculates evidence_provenance_status based on current envelopes."""
        if self.has_verified_fix(workspace_root=workspace_root, store=store):
            self.evidence_provenance_status = "FIX_VERIFIED"
        elif self.has_verified_reproduction(workspace_root=workspace_root, store=store):
            self.evidence_provenance_status = "REPRODUCTION_CONFIRMED"
        elif self.execution_envelopes:
            self.evidence_provenance_status = "EVIDENCE_ATTACHED_UNVERIFIED"
        elif self.verified_root_cause or self.reproduction_procedure or self.regression_test:
            self.evidence_provenance_status = "HISTORICAL_STRING_ONLY"
        else:
            self.evidence_provenance_status = "NO_EVIDENCE"

    def has_verified_reproduction(
        self,
        workspace_root: Path | str | None = None,
        store: Any = None,
    ) -> bool:
        """Returns True if at least one attached envelope is a valid verified baseline RED."""
        ev_store = store
        if ev_store is None:
            try:
                ev_store = EvidenceStore(workspace_root=workspace_root or WORKSPACE_ROOT)
            except (OSError, ValueError, sqlite3.Error):
                ev_store = None

        for env in self.get_execution_envelopes(store=ev_store, workspace_root=workspace_root):
            if env.execution_role == "BASELINE":
                valid, _ = RedGreenVerificationEngine.validate_baseline_red(
                    env, candidate_or_signal=self, workspace_root=workspace_root, store=ev_store
                )
                if valid:
                    return True
        return False

    def has_verified_fix(
        self,
        workspace_root: Path | str | None = None,
        store: Any = None,
    ) -> bool:
        """Returns True if a valid paired RED-GREEN execution set exists."""
        ev_store = store
        if ev_store is None:
            try:
                ev_store = EvidenceStore(workspace_root=workspace_root or WORKSPACE_ROOT)
            except (OSError, ValueError, sqlite3.Error):
                ev_store = None

        envelopes = self.get_execution_envelopes(store=ev_store, workspace_root=workspace_root)
        red_map: dict[str, ExecutionEvidenceEnvelope] = {}
        for env in envelopes:
            if env.execution_role == "BASELINE":
                valid, _ = RedGreenVerificationEngine.validate_baseline_red(
                    env, candidate_or_signal=self, workspace_root=workspace_root, store=ev_store
                )
                if valid:
                    red_map[env.execution_pair_id] = env

        for env in envelopes:
            if env.execution_role == "FIX_VERIFICATION":
                pair_red = red_map.get(env.execution_pair_id)
                if pair_red:
                    valid, _ = RedGreenVerificationEngine.validate_fix_green(
                        env, pair_red, candidate_or_signal=self, workspace_root=workspace_root, store=ev_store
                    )
                    if valid:
                        return True
        return False

    def is_empirically_verified(
        self,
        workspace_root: Path | str | None = None,
        store: Any = None,
    ) -> bool:
        """Returns True if candidate has valid paired execution verification."""
        return self.has_verified_fix(workspace_root=workspace_root, store=store)

    def to_dict(self) -> dict[str, Any]:
        """Convert candidate to dictionary preserving nested data structures."""
        d = dataclasses.asdict(self)
        if not self.execution_envelopes:
            d.pop("execution_envelopes", None)
        if self.evidence_provenance_status == "NO_EVIDENCE":
            d.pop("evidence_provenance_status", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LessonCandidate:
        """Construct candidate from dictionary validating schema version, types, and untrusted input.

        STRICT INVARIANTS:
        1. Validates top-level mapping type.
        2. Validates schema version.
        3. Deliberately handles explicit null for sequence and mapping fields.
        4. Validates sequence and mapping field types (fails closed on malformed structures).
        5. Rejects invalid enum values for failure_classification and confidence_level.
        6. Rejects malformed specialist findings and evidence references.
        7. Untrusted Deserialization Defense: Strips and downgrades any imported approval flag.
        8. Deserialized review status cannot claim APPROVED.
        """
        if not isinstance(data, dict):
            raise TypeError(f"Expected dict, got {type(data).__name__}")
        ver = data.get("schema_version", SCHEMA_VERSION)
        if ver != SCHEMA_VERSION:
            raise ValueError(f"Unsupported schema version: {ver} (expected {SCHEMA_VERSION})")

        # Untrusted Deserialization Defense: Strip and downgrade any imported approval flag
        sanitized_ha = {
            "approved": False,
            "status": "UNAUTHORIZED_PREVIEW_ONLY",
            "note": "Untrusted artifact; human authorization cannot be established in code.",
        }

        # Sequence field helper
        def _parse_str_list(field_name: str) -> list[str]:
            raw = data.get(field_name)
            if raw is None:
                return []
            if not isinstance(raw, (list, tuple)):
                raise TypeError(f"Field '{field_name}' must be a list, got {type(raw).__name__}")
            for idx, item in enumerate(raw):
                if not isinstance(item, str):
                    raise TypeError(f"Item {idx} in '{field_name}' must be a string, got {type(item).__name__}")
                if not item.strip() and field_name in ("source_event_ids", "affected_subsystems", "known_counterexamples"):
                    raise ValueError(f"Item {idx} in '{field_name}' cannot be an empty or whitespace-only string.")
            return list(raw)

        # Mapping field helper
        def _parse_dict(field_name: str) -> dict[str, Any]:
            raw = data.get(field_name)
            if raw is None:
                return {}
            if not isinstance(raw, dict):
                raise TypeError(f"Field '{field_name}' must be a dict, got {type(raw).__name__}")
            return dict(raw)

        # String field helper
        def _parse_str(field_name: str, default: str = "") -> str:
            raw = data.get(field_name)
            if raw is None:
                return default
            if not isinstance(raw, str):
                raise TypeError(f"Field '{field_name}' must be a string, got {type(raw).__name__}")
            return raw

        source_event_ids = _parse_str_list("source_event_ids")
        affected_subsystems = _parse_str_list("affected_subsystems")
        known_counterexamples = _parse_str_list("known_counterexamples")
        observed_behavior = _parse_dict("observed_behavior")
        root_cause_hypothesis = _parse_dict("root_cause_hypothesis")

        # Observed behavior internal type validation
        if "exit_code" in observed_behavior and observed_behavior["exit_code"] is not None and not isinstance(observed_behavior["exit_code"], int):
            raise TypeError(f"Field 'exit_code' in 'observed_behavior' must be an int or None, got {type(observed_behavior['exit_code']).__name__}")
        if "step_index" in observed_behavior and observed_behavior["step_index"] is not None and not isinstance(observed_behavior["step_index"], int):
            raise TypeError(f"Field 'step_index' in 'observed_behavior' must be an int or None, got {type(observed_behavior['step_index']).__name__}")
        if "tool_name" in observed_behavior and observed_behavior["tool_name"] is not None and not isinstance(observed_behavior["tool_name"], str):
            raise TypeError(f"Field 'tool_name' in 'observed_behavior' must be a string or None, got {type(observed_behavior['tool_name']).__name__}")
        if "summary" in observed_behavior and observed_behavior["summary"] is not None and not isinstance(observed_behavior["summary"], str):
            raise TypeError(f"Field 'summary' in 'observed_behavior' must be a string or None, got {type(observed_behavior['summary']).__name__}")

        # Evidence references validation
        raw_evidence = data.get("evidence_references")
        if raw_evidence is None:
            evidence_references: list[dict[str, Any]] = []
        elif not isinstance(raw_evidence, (list, tuple)):
            raise TypeError(f"Field 'evidence_references' must be a list, got {type(raw_evidence).__name__}")
        else:
            evidence_references = []
            for idx, item in enumerate(raw_evidence):
                if not isinstance(item, dict):
                    raise TypeError(f"Item {idx} in 'evidence_references' must be a dict, got {type(item).__name__}")
                ev_dict = dict(item)
                if "path" in ev_dict and not isinstance(ev_dict["path"], str):
                    raise TypeError(f"Field 'path' in evidence reference {idx} must be a string, got {type(ev_dict['path']).__name__}")
                if "status" in ev_dict and not isinstance(ev_dict["status"], str):
                    raise TypeError(f"Field 'status' in evidence reference {idx} must be a string, got {type(ev_dict['status']).__name__}")
                if "error" in ev_dict and ev_dict["error"] is not None and not isinstance(ev_dict["error"], str):
                    raise TypeError(f"Field 'error' in evidence reference {idx} must be a string or None, got {type(ev_dict['error']).__name__}")
                if "snippet" in ev_dict and ev_dict["snippet"] is not None and not isinstance(ev_dict["snippet"], str):
                    raise TypeError(f"Field 'snippet' in evidence reference {idx} must be a string or None, got {type(ev_dict['snippet']).__name__}")
                evidence_references.append(ev_dict)

        # Review findings validation
        raw_findings = data.get("review_findings")
        if raw_findings is None:
            review_findings: list[dict[str, Any]] = []
        elif not isinstance(raw_findings, (list, tuple)):
            raise TypeError(f"Field 'review_findings' must be a list, got {type(raw_findings).__name__}")
        else:
            review_findings = []
            for idx, item in enumerate(raw_findings):
                if not isinstance(item, dict):
                    raise TypeError(f"Item {idx} in 'review_findings' must be a dict, got {type(item).__name__}")
                finding_dict = dict(item)
                if "specialist" in finding_dict:
                    spec = finding_dict["specialist"]
                    if spec not in ALLOWED_SPECIALISTS:
                        raise ValueError(
                            f"Invalid specialist '{spec}' in review finding. Allowed: {', '.join(ALLOWED_SPECIALISTS)}"
                        )
                    # Provenance hardening: UNCONDITIONALLY overwrite provenance_status
                    finding_dict["provenance_status"] = "AGENT_REPORTED_UNVERIFIED"
                elif "action" not in finding_dict:
                    raise ValueError(f"Item {idx} in 'review_findings' must contain a 'specialist' or 'action' identifier.")
                if "findings" in finding_dict and not isinstance(finding_dict["findings"], str):
                    raise TypeError(f"Field 'findings' in review finding {idx} must be a string, got {type(finding_dict['findings']).__name__}")
                review_findings.append(finding_dict)

        # Enum validation
        raw_fc = data.get("failure_classification", "INCONCLUSIVE")
        if raw_fc is None:
            failure_classification = "INCONCLUSIVE"
        elif not isinstance(raw_fc, str):
            raise TypeError(f"Field 'failure_classification' must be a string, got {type(raw_fc).__name__}")
        elif raw_fc not in TAXONOMY_CATEGORIES:
            raise ValueError(f"Invalid failure_classification '{raw_fc}'. Allowed: {', '.join(TAXONOMY_CATEGORIES)}")
        else:
            failure_classification = raw_fc

        raw_cl = data.get("confidence_level", "UNCONFIRMED")
        if raw_cl is None:
            confidence_level = "UNCONFIRMED"
        elif not isinstance(raw_cl, str):
            raise TypeError(f"Field 'confidence_level' must be a string, got {type(raw_cl).__name__}")
        elif raw_cl not in CONFIDENCE_LEVELS:
            raise ValueError(f"Invalid confidence_level '{raw_cl}'. Allowed: {', '.join(CONFIDENCE_LEVELS)}")
        else:
            confidence_level = raw_cl

        # Verified root cause (str or None)
        raw_vrc = data.get("verified_root_cause")
        if raw_vrc is not None and not isinstance(raw_vrc, str):
            raise TypeError(f"Field 'verified_root_cause' must be a string or None, got {type(raw_vrc).__name__}")
        verified_root_cause = raw_vrc

        # Deserialized review status cannot claim APPROVED
        raw_status = data.get("review_status", "OBSERVED")
        if not isinstance(raw_status, str) or raw_status not in LIFECYCLE_STATES or raw_status == "APPROVED":
            status = "PREPARED_FOR_REVIEW" if verified_root_cause else "OBSERVED"
        else:
            status = raw_status

        # Execution envelopes (untrusted deserialization defense)
        raw_envs = data.get("execution_envelopes")
        execution_envelopes: list[dict[str, Any]] = []
        if isinstance(raw_envs, (list, tuple)):
            for item in raw_envs:
                if isinstance(item, dict):
                    item_copy = dict(item)
                    # Untrusted Deserialization Defense: imported from JSON is UNVERIFIED
                    if item_copy.get("provenance_status") == "AUTHORITATIVE_LOCAL_EXECUTION":
                        item_copy["provenance_status"] = "IMPORTED_UNVERIFIED"
                    execution_envelopes.append(item_copy)

        # Provenance status determination
        if execution_envelopes:
            ev_status = "IMPORTED_UNVERIFIED"
        elif verified_root_cause or _parse_str("reproduction_procedure") or _parse_str("regression_test"):
            ev_status = "HISTORICAL_STRING_ONLY"
        else:
            ev_status = "NO_EVIDENCE"

        return cls(
            schema_version=ver,
            candidate_id=_parse_str("candidate_id"),
            source_event_ids=source_event_ids,
            failure_classification=failure_classification,
            affected_subsystems=affected_subsystems,
            observed_behavior=observed_behavior,
            root_cause_hypothesis=root_cause_hypothesis,
            verified_root_cause=verified_root_cause,
            evidence_references=evidence_references,
            reproduction_procedure=_parse_str("reproduction_procedure"),
            scope_of_applicability=_parse_str("scope_of_applicability"),
            corrective_recommendation=_parse_str("corrective_recommendation"),
            confidence_level=confidence_level,
            known_counterexamples=known_counterexamples,
            regression_test=_parse_str("regression_test"),
            deduplication_key=_parse_str("deduplication_key"),
            review_status=status,
            review_findings=review_findings,
            execution_envelopes=execution_envelopes,
            evidence_provenance_status=ev_status,
            human_approval=sanitized_ha,
        )

    @property
    def layer_a_facts(self) -> dict[str, Any]:
        """Layer A: Verified facts from observation and evidence."""
        return {
            "observed_behavior": self.observed_behavior,
            "source_event_ids": self.source_event_ids,
            "evidence_references": self.evidence_references,
        }

    @property
    def layer_b_proposals(self) -> dict[str, Any]:
        """Layer B: Root-cause hypotheses and provisional classification."""
        return {
            "failure_classification": self.failure_classification,
            "root_cause_hypothesis": self.root_cause_hypothesis,
            "affected_subsystems": self.affected_subsystems,
        }

    @property
    def layer_c_verified(self) -> dict[str, Any]:
        """Layer C: Verified lesson attributes eligible for human inspection."""
        return {
            "verified_root_cause": self.verified_root_cause,
            "reproduction_procedure": self.reproduction_procedure,
            "corrective_recommendation": self.corrective_recommendation,
            "regression_test": self.regression_test,
            "scope_of_applicability": self.scope_of_applicability,
            "confidence_level": self.confidence_level,
            "execution_envelopes": self.execution_envelopes,
            "evidence_provenance_status": self.evidence_provenance_status,
            "is_empirically_verified": self.is_empirically_verified(),
        }


# -----------------------------------------------------------------------------
# Canonical Content Hash Calculation (Content Integrity ONLY)
# -----------------------------------------------------------------------------

def compute_candidate_hash(candidate: LessonCandidate | dict[str, Any]) -> str:
    """Computes a deterministic canonical SHA-256 content-integrity digest of candidate fields.

    SECURITY NOTICE:
    This digest verifies data integrity (tamper detection) ONLY.
    It does NOT authenticate a human decision, authorize promotion, or verify identity.
    """
    if isinstance(candidate, LessonCandidate):
        raw_dict = candidate.to_dict()
    else:
        raw_dict = dict(candidate)

    # Exclude operational/approval/evidence fields from content hash
    excluded_fields = {
        "human_approval",
        "review_status",
        "execution_envelopes",
        "evidence_provenance_status",
    }
    hashable_dict = {k: v for k, v in raw_dict.items() if k not in excluded_fields}
    canonical_json = json.dumps(hashable_dict, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# Event to Candidate Transformation
# -----------------------------------------------------------------------------

def create_candidate_from_event(
    event: EventEnvelope | dict[str, Any],
    candidate_id: str | None = None,
) -> LessonCandidate:
    """Transforms an observed EventEnvelope into a controlled LessonCandidate.

    Strict Invariants:
    - Layer A facts are extracted and sanitized.
    - Layer B hypothesis is initialized to 'INCONCLUSIVE' with explicit uncertainty.
    - Layer C verified root cause remains strictly None.
    - Review status begins in 'OBSERVED'.
    - Human approval is unapproved (UNAUTHORIZED_PREVIEW_ONLY).
    """
    if isinstance(event, EventEnvelope):
        ev_dict = event.to_dict()
    elif isinstance(event, dict):
        ev_dict = dict(event)
    else:
        raise TypeError(f"Expected EventEnvelope or dict, got {type(event).__name__}")

    event_id = ev_dict.get("event_id", "")
    tool_name = ev_dict.get("tool_name")
    step_index = ev_dict.get("step_index")
    tool_outcome = ev_dict.get("tool_outcome", "UNKNOWN")
    error_status = ev_dict.get("error_status", "UNKNOWN")
    failure_signal = ev_dict.get("failure_signal")
    exit_code = ev_dict.get("exit_code")
    safe_summary = ev_dict.get("safe_summary", "")
    identity_status = ev_dict.get("identity_status", "RELIABLE")
    raw_evidence_refs = ev_dict.get("evidence_references", {})

    # Sanitize observed text
    sanitized_signal, _ = redact_sensitive_text(failure_signal or "")
    sanitized_summary, _ = redact_sensitive_text(safe_summary or "")

    observed_behavior = {
        "summary": sanitized_summary,
        "failure_signal": sanitized_signal if failure_signal else None,
        "exit_code": exit_code,
        "step_index": step_index,
        "tool_name": tool_name,
        "tool_outcome": tool_outcome,
        "error_status": error_status,
        "timestamp": ev_dict.get("timestamp", ""),
    }

    # Bounded sanitized evidence references
    evidence_refs: list[dict[str, Any]] = []
    if isinstance(raw_evidence_refs, dict):
        for k, v in raw_evidence_refs.items():
            if isinstance(v, str):
                s_val, _ = redact_sensitive_text(v)
                evidence_refs.append({"ref_key": k, "target": s_val, "status": "RECORDED"})
            elif isinstance(v, dict):
                evidence_refs.append(v)
    elif isinstance(raw_evidence_refs, list):
        evidence_refs.extend(raw_evidence_refs)

    # Deterministic or random candidate identifier
    if not candidate_id:
        seed = f"{event_id}:{step_index}:{tool_name}" if event_id else f"anon:{datetime.datetime.now(datetime.UTC).isoformat()}"
        cid_digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        candidate_id = f"LCAND-{cid_digest}"

    # Deduplication key handling
    if identity_status == "RELIABLE" and step_index is not None:
        dedup_key = f"REL:{tool_name or 'tool'}:step_{step_index}:{exit_code}"
    else:
        disc = event_id or "missing_id"
        dedup_key = f"INCOMPLETE_DISCRIMINATED:{disc}"

    # Initial provisional Layer B hypothesis (strictly inconclusive at birth)
    root_cause_hyp = {
        "hypothesis": "Unverified initial execution failure observation",
        "rationale": "Event recorded by failure collector; root cause is unverified.",
        "supporting_evidence": [event_id] if event_id else [],
        "alternative_explanations": [
            "Transient execution environment boundary restriction",
            "Tool argument or invocation syntax defect",
            "Target process abnormal termination",
        ],
        "disproving_criteria": "Deterministic reproduction showing defect is reproducible or an artifact of observation",
        "requires_reproduction": True,
        "environment_specific": False,
    }

    return LessonCandidate(
        schema_version=SCHEMA_VERSION,
        candidate_id=candidate_id,
        source_event_ids=[event_id] if event_id else [],
        failure_classification="INCONCLUSIVE",
        affected_subsystems=[],
        observed_behavior=observed_behavior,
        root_cause_hypothesis=root_cause_hyp,
        verified_root_cause=None,  # Layer C strictly empty
        evidence_references=evidence_refs,
        reproduction_procedure="",
        scope_of_applicability="",
        corrective_recommendation="",
        confidence_level="UNCONFIRMED",
        known_counterexamples=[],
        regression_test="",
        deduplication_key=dedup_key,
        review_status="OBSERVED",
        review_findings=[],
        human_approval={
            "approved": False,
            "status": "UNAUTHORIZED_PREVIEW_ONLY",
            "note": "Untrusted artifact; human authorization cannot be established in code.",
        },
    )


# -----------------------------------------------------------------------------
# Classification Policy Engine
# -----------------------------------------------------------------------------

def propose_classification(
    candidate: LessonCandidate,
    category: str,
    hypothesis: str,
    rationale: str,
    supporting_evidence: list[str],
    alternative_explanations: list[str] | None = None,
    disproving_criteria: str = "",
    requires_reproduction: bool = True,
    environment_specific: bool = False,
    affected_subsystems: list[str] | None = None,
) -> LessonCandidate:
    """Applies a proposed classification according to the 8-category taxonomy.

    Strict Invariants:
    - Validates category against TAXONOMY_CATEGORIES.
    - Records comprehensive hypothesis rationale and uncertainty metadata.
    - Does NOT mutate Layer C verified root cause.
    - Transitions lifecycle state to 'PROPOSED' if previously 'OBSERVED'.
    """
    if category not in TAXONOMY_CATEGORIES:
        raise ValueError(
            f"Invalid category '{category}'. Must be one of: {', '.join(TAXONOMY_CATEGORIES)}"
        )

    sanitized_hyp, _ = redact_sensitive_text(hypothesis)
    sanitized_rat, _ = redact_sensitive_text(rationale)
    sanitized_disp, _ = redact_sensitive_text(disproving_criteria)

    candidate.failure_classification = category
    candidate.root_cause_hypothesis = {
        "hypothesis": sanitized_hyp,
        "rationale": sanitized_rat,
        "supporting_evidence": list(supporting_evidence),
        "alternative_explanations": list(alternative_explanations or []),
        "disproving_criteria": sanitized_disp,
        "requires_reproduction": bool(requires_reproduction),
        "environment_specific": bool(environment_specific),
    }

    if affected_subsystems:
        candidate.affected_subsystems = list(affected_subsystems)

    if candidate.review_status in ("OBSERVED", "NEEDS_EVIDENCE"):
        candidate.review_status = "PROPOSED"

    return candidate


# -----------------------------------------------------------------------------
# Layer C Root Cause Verification
# -----------------------------------------------------------------------------

def verify_root_cause(
    candidate: LessonCandidate,
    verified_root_cause: str,
    reproduction_procedure: str,
    corrective_recommendation: str,
    regression_test: str,
    scope_of_applicability: str,
    confidence_level: str = "HIGH",
    known_counterexamples: list[str] | None = None,
) -> LessonCandidate:
    """Elevates candidate with verified Layer C findings from reproduction testing.

    Strict Invariants:
    - Validates confidence level.
    - Verifies that root cause, reproduction, recommendation, and test are provided.
    - Does NOT approve the candidate (human approval required).
    """
    if confidence_level not in CONFIDENCE_LEVELS:
        raise ValueError(
            f"Invalid confidence_level '{confidence_level}'. Must be one of: {', '.join(CONFIDENCE_LEVELS)}"
        )

    if not verified_root_cause or not verified_root_cause.strip():
        raise ValueError("verified_root_cause cannot be empty.")
    if not reproduction_procedure or not reproduction_procedure.strip():
        raise ValueError("reproduction_procedure cannot be empty.")
    if not corrective_recommendation or not corrective_recommendation.strip():
        raise ValueError("corrective_recommendation cannot be empty.")
    if not regression_test or not regression_test.strip():
        raise ValueError("regression_test cannot be empty.")
    if not scope_of_applicability or not scope_of_applicability.strip():
        raise ValueError("scope_of_applicability cannot be empty.")

    sanitized_root, _ = redact_sensitive_text(verified_root_cause)
    sanitized_repro, _ = redact_sensitive_text(reproduction_procedure)
    sanitized_rec, _ = redact_sensitive_text(corrective_recommendation)
    sanitized_test, _ = redact_sensitive_text(regression_test)
    sanitized_scope, _ = redact_sensitive_text(scope_of_applicability)

    candidate.verified_root_cause = sanitized_root
    candidate.reproduction_procedure = sanitized_repro
    candidate.corrective_recommendation = sanitized_rec
    candidate.regression_test = sanitized_test
    candidate.scope_of_applicability = sanitized_scope
    candidate.confidence_level = confidence_level
    candidate.known_counterexamples = list(known_counterexamples or [])

    if candidate.review_status in ("OBSERVED", "PROPOSED"):
        candidate.review_status = "UNDER_REVIEW"

    return candidate


# -----------------------------------------------------------------------------
# Read-Only Specialist Review Protocol (Hardened Provenance)
# -----------------------------------------------------------------------------

def add_specialist_finding(
    candidate: LessonCandidate,
    specialist_name: str,
    findings: str,
    recommendations: list[str] | None = None,
    concerns: list[str] | None = None,
    confidence_assessment: str = "MEDIUM",
) -> LessonCandidate:
    """Appends an advisory evaluation finding from a restricted specialist.

    Strict Invariants (Hardened in Phase 3C2):
    - Specialist must be one of: security-auditor, systems-architect, test-guardian.
    - Explicitly records provenance_status as 'AGENT_REPORTED_UNVERIFIED'.
      Calling an allowlisted name does not establish cryptographic or subagent authenticity.
    - Findings are advisory only; does NOT grant approval or alter root cause.
    - Updates review status to 'REVIEWED'.
    """
    if specialist_name not in ALLOWED_SPECIALISTS:
        raise ValueError(
            f"Specialist '{specialist_name}' not permitted. Allowed: {', '.join(ALLOWED_SPECIALISTS)}"
        )

    sanitized_findings, _ = redact_sensitive_text(findings)
    sanitized_concerns = [redact_sensitive_text(c)[0] for c in (concerns or [])]
    sanitized_recs = [redact_sensitive_text(r)[0] for r in (recommendations or [])]

    finding_record = {
        "specialist": specialist_name,
        "provenance_status": "AGENT_REPORTED_UNVERIFIED",
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "findings": sanitized_findings,
        "recommendations": sanitized_recs,
        "concerns": sanitized_concerns,
        "confidence_assessment": confidence_assessment,
    }

    candidate.review_findings.append(finding_record)
    if candidate.review_status in ("PROPOSED", "UNDER_REVIEW"):
        candidate.review_status = "REVIEWED"

    return candidate


# -----------------------------------------------------------------------------
# Evidence Collection & Confinement Engine
# -----------------------------------------------------------------------------

def collect_evidence_reference(
    target_path: Path | str,
    allowed_root: Path | str,
    conversation_id: str | None = None,
    max_bytes: int = MAX_EVIDENCE_READ_BYTES,
) -> dict[str, Any]:
    """Safely collects an evidence file reference with path confinement and bounding."""
    p = Path(target_path)
    root = Path(allowed_root)

    # 1. Path confinement check
    is_confined, confinement_err = validate_path_confinement(p, root)
    if not is_confined:
        return {
            "path": str(p),
            "status": "UNAVAILABLE",
            "error": confinement_err,
            "snippet": None,
            "truncated": False,
        }

    # 2. Conversation isolation check
    path_str = str(p.resolve()).replace("/", "\\")
    if conversation_id and "\\brain\\" in path_str:
        parts = path_str.split("\\brain\\")
        if len(parts) > 1:
            target_conv = parts[1].split("\\")[0]
            if target_conv and target_conv.lower() != conversation_id.lower():
                return {
                    "path": str(p),
                    "status": "UNAVAILABLE",
                    "error": "CROSS_CONVERSATION_ACCESS_REJECTED",
                    "snippet": None,
                    "truncated": False,
                }

    # 3. File existence check
    if not p.is_file():
        return {
            "path": str(p),
            "status": "UNAVAILABLE",
            "error": "FILE_NOT_FOUND",
            "snippet": None,
            "truncated": False,
        }

    # 4. Safe bounded read
    try:
        file_size = p.stat().st_size
        truncated = file_size > max_bytes

        with open(p, "rb") as f:
            raw_bytes = f.read(max_bytes)

        text_content = raw_bytes.decode("utf-8", errors="replace")
        sanitized_snippet, _ = redact_sensitive_text(text_content)

        return {
            "path": str(p),
            "status": "CAPTURED",
            "error": None,
            "size_bytes": file_size,
            "read_bytes": len(raw_bytes),
            "truncated": truncated,
            "truncated_bytes": max(0, file_size - max_bytes),
            "snippet": sanitized_snippet,
        }
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {
            "path": str(p),
            "status": "UNAVAILABLE",
            "error": f"READ_ERROR: {type(exc).__name__}",
            "snippet": None,
            "truncated": False,
        }


# -----------------------------------------------------------------------------
# Deduplication & Lifecycle State Management
# -----------------------------------------------------------------------------

LIFECYCLE_RANK = {
    "OBSERVED": 1,
    "NEEDS_EVIDENCE": 2,
    "PROPOSED": 3,
    "HYPOTHESIS_RECORDED": 3,
    "UNDER_REVIEW": 4,
    "REVIEWED": 5,
    "REPRODUCTION_CONFIRMED": 6,
    "FIX_VERIFIED": 7,
    "PREPARED_FOR_REVIEW": 8,
    "REJECTED": 9,
    "SUPERSEDED": 10,
}


def is_monotonic_transition(from_state: str, to_state: str) -> bool:
    """Returns True if lifecycle transition from from_state to to_state is monotonic.

    Terminal states (REJECTED, SUPERSEDED) cannot be transitioned out of.
    Active states can transition forward or to terminal states, but cannot regress.
    """
    if from_state in ("REJECTED", "SUPERSEDED"):
        return to_state == from_state
    if to_state in ("REJECTED", "SUPERSEDED"):
        return True
    return LIFECYCLE_RANK.get(to_state, 0) >= LIFECYCLE_RANK.get(from_state, 0)


def is_evidence_equivalent(cand_a: LessonCandidate, cand_b: LessonCandidate) -> bool:
    """Evaluates whether two candidates possess equivalent observable evidence.

    Compares:
    - tool_name
    - exit_code
    - failure_signal (normalized string)
    - evidence_references (canonical JSON representation)
    - collection_status (normalized string)
    """
    obs_a = cand_a.observed_behavior or {}
    obs_b = cand_b.observed_behavior or {}

    if obs_a.get("tool_name") != obs_b.get("tool_name"):
        return False
    if obs_a.get("exit_code") != obs_b.get("exit_code"):
        return False

    sig_a = (obs_a.get("failure_signal") or "").strip()
    sig_b = (obs_b.get("failure_signal") or "").strip()
    if sig_a != sig_b:
        return False

    stat_a = (obs_a.get("collection_status") or "").strip()
    stat_b = (obs_b.get("collection_status") or "").strip()
    if stat_a != stat_b:
        return False

    refs_a = cand_a.evidence_references or []
    refs_b = cand_b.evidence_references or []

    def _canon_ref(ref: Any) -> str:
        if isinstance(ref, dict):
            return json.dumps(ref, sort_keys=True)
        return str(ref)

    return sorted(_canon_ref(r) for r in refs_a) == sorted(_canon_ref(r) for r in refs_b)


def check_candidate_deduplication(
    candidate: LessonCandidate,
    existing_candidates: list[LessonCandidate],
) -> tuple[bool, str, LessonCandidate | None]:
    """Evaluates candidate against existing candidate registry for deduplication.

    Decouples event identity, candidate identity, evidence equivalence, and deduplication fingerprint.
    Eliminates short-circuiting on deduplication key over conflicting evidence.

    Returns:
        tuple[bool, str, LessonCandidate | None]:
            - (True, "EXACT_RELIABLE_DUPLICATE", existing): Exact candidate or reliable key duplicate with matching evidence.
            - (False, "CANDIDATE_EVIDENCE_CONFLICT", existing): Same identity or fingerprint, but differing failure signal/references.
            - (False, "CANDIDATE_ID_COLLISION_CONFLICT", existing): Candidate ID matches but source events differ.
            - (False, "POTENTIAL_DUPLICATE_INCOMPLETE_IDENTITY", existing): Incomplete identity key matches.
            - (False, "UNIQUE", None): No matching candidate found.
            - (False, "NO_KEY", None): Candidate lacks deduplication key.
    """
    key = candidate.deduplication_key
    if not key:
        return False, "NO_KEY", None

    is_reliable = key.startswith("REL:")

    for existing in existing_candidates:
        # Candidate ID match: verify event identity and evidence equivalence
        if existing.candidate_id == candidate.candidate_id:
            if sorted(existing.source_event_ids) != sorted(candidate.source_event_ids):
                return False, "CANDIDATE_ID_COLLISION_CONFLICT", existing
            if not is_evidence_equivalent(existing, candidate):
                return False, "CANDIDATE_EVIDENCE_CONFLICT", existing
            return True, "EXACT_RELIABLE_DUPLICATE", existing

        # Deduplication fingerprint match across distinct candidate IDs
        if existing.deduplication_key == key:
            if is_reliable:
                if is_evidence_equivalent(existing, candidate):
                    return True, "EXACT_RELIABLE_DUPLICATE", existing
                return False, "CANDIDATE_EVIDENCE_CONFLICT", existing
            return False, "POTENTIAL_DUPLICATE_INCOMPLETE_IDENTITY", existing

    return False, "UNIQUE", None


def validate_lifecycle_transition(
    candidate: LessonCandidate,
    new_state: str,
    enforce_monotonic: bool = True,
    require_execution_provenance: bool = False,
    workspace_root: Path | str | None = None,
) -> tuple[bool, str]:
    """Evaluates candidate against semantic lifecycle prerequisites.

    Enforces:
    1. Valid lifecycle state member.
    2. Terminal states (REJECTED, SUPERSEDED) cannot transition out.
    3. Terminal states can be entered from any active state.
    4. Monotonic rank progression (regressions rejected).
    5. Direct skip rejection: OBSERVED and NEEDS_EVIDENCE cannot jump directly to
       REPRODUCTION_CONFIRMED, FIX_VERIFIED, or PREPARED_FOR_REVIEW.
    6. PROPOSED cannot jump directly to FIX_VERIFIED without reproduction confirmation.
    7. Target PROPOSED requires non-empty root_cause_hypothesis.hypothesis.
    8. Target REVIEWED requires non-empty candidate.review_findings.
    9. Target REPRODUCTION_CONFIRMED requires valid baseline reproduction execution envelope.
    10. Target FIX_VERIFIED requires valid paired RED-GREEN execution envelopes.
    11. Target PREPARED_FOR_REVIEW requires verified Layer C data:
       - verified_root_cause is not None and non-empty
       - reproduction_procedure is non-empty
       - corrective_recommendation is non-empty
       - regression_test is non-empty
       - If require_execution_provenance=True: requires candidate.has_verified_fix().
    """
    canonical_state = "PROPOSED" if new_state == "HYPOTHESIS_RECORDED" else new_state
    if canonical_state not in LIFECYCLE_STATES:
        return False, f"Invalid lifecycle state '{new_state}'. Must be one of: {', '.join(LIFECYCLE_STATES)}"

    prev_state = candidate.review_status

    # Self-transition is always valid
    if prev_state == canonical_state:
        return True, "OK"

    # Terminal state protection
    if prev_state in ("REJECTED", "SUPERSEDED"):
        return False, f"Cannot transition out of terminal state '{prev_state}' to '{canonical_state}'."

    # Monotonic rank regression check
    if enforce_monotonic and not is_monotonic_transition(prev_state, canonical_state):
        return (
            False,
            f"Non-monotonic lifecycle regression: cannot transition candidate from '{prev_state}' to '{canonical_state}'.",
        )

    # Direct jump restrictions
    if prev_state in ("OBSERVED", "NEEDS_EVIDENCE") and canonical_state in (
        "REPRODUCTION_CONFIRMED", "FIX_VERIFIED", "PREPARED_FOR_REVIEW"
    ):
        return (
            False,
            f"Invalid lifecycle transition: candidate in '{prev_state}' state cannot jump directly to '{canonical_state}'.",
        )

    if prev_state == "PROPOSED" and canonical_state == "FIX_VERIFIED":
        return (
            False,
            "Invalid lifecycle transition: candidate in 'PROPOSED' state cannot jump directly to 'FIX_VERIFIED' without reproduction confirmation.",
        )

    # Semantic prerequisites for PROPOSED
    if canonical_state in ("PROPOSED", "HYPOTHESIS_RECORDED"):
        hyp = candidate.root_cause_hypothesis or {}
        if not (hyp.get("hypothesis") and str(hyp.get("hypothesis")).strip()):
            return False, f"Cannot transition candidate to '{canonical_state}': root_cause_hypothesis must contain a non-empty hypothesis."

    # Semantic prerequisites for REVIEWED
    if canonical_state == "REVIEWED" and not candidate.review_findings:
        return False, "Cannot transition candidate to 'REVIEWED': review_findings must not be empty."

    # Semantic prerequisites for REPRODUCTION_CONFIRMED
    if canonical_state == "REPRODUCTION_CONFIRMED" and not candidate.has_verified_reproduction(workspace_root=workspace_root):
        return False, "Cannot transition candidate to 'REPRODUCTION_CONFIRMED': valid baseline reproduction execution envelope required."

    # Semantic prerequisites for FIX_VERIFIED
    if canonical_state == "FIX_VERIFIED" and not candidate.has_verified_fix(workspace_root=workspace_root):
        return False, "Cannot transition candidate to 'FIX_VERIFIED': valid paired RED-GREEN execution envelopes required."

    # Semantic prerequisites for PREPARED_FOR_REVIEW
    if canonical_state == "PREPARED_FOR_REVIEW":
        if not candidate.verified_root_cause or not candidate.verified_root_cause.strip():
            return False, "Cannot transition candidate to 'PREPARED_FOR_REVIEW': verified_root_cause is missing or empty."
        if not candidate.reproduction_procedure or not candidate.reproduction_procedure.strip():
            return False, "Cannot transition candidate to 'PREPARED_FOR_REVIEW': reproduction_procedure is missing or empty."
        if not candidate.corrective_recommendation or not candidate.corrective_recommendation.strip():
            return False, "Cannot transition candidate to 'PREPARED_FOR_REVIEW': corrective_recommendation is missing or empty."
        if not candidate.regression_test or not candidate.regression_test.strip():
            return False, "Cannot transition candidate to 'PREPARED_FOR_REVIEW': regression_test is missing or empty."
        if require_execution_provenance and not candidate.has_verified_fix(workspace_root=workspace_root):
            return False, "Cannot transition candidate to 'PREPARED_FOR_REVIEW': empirical fix verification (FIX_VERIFIED) required."

    return True, "OK"


def transition_candidate_lifecycle(
    candidate: LessonCandidate,
    new_state: str,
    reason: str = "",
    enforce_monotonic: bool = True,
    require_execution_provenance: bool = False,
    workspace_root: Path | str | None = None,
) -> None:
    """Manages explicit lifecycle state transitions enforcing semantic prerequisites and preserving audit records."""
    valid, err_msg = validate_lifecycle_transition(
        candidate,
        new_state,
        enforce_monotonic=enforce_monotonic,
        require_execution_provenance=require_execution_provenance,
        workspace_root=workspace_root,
    )
    if not valid:
        raise ValueError(err_msg)

    canonical_state = "PROPOSED" if new_state == "HYPOTHESIS_RECORDED" else new_state
    prev_state = candidate.review_status
    candidate.review_status = canonical_state

    if canonical_state in ("REJECTED", "SUPERSEDED"):
        audit_note = {
            "action": f"TRANSITION_TO_{canonical_state}",
            "previous_state": prev_state,
            "reason": reason or "Explicit lifecycle update",
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        candidate.review_findings.append(audit_note)


# -----------------------------------------------------------------------------
# Canonical Destination Confinement Engine
# -----------------------------------------------------------------------------

def is_lexical_reparse_point_or_link(path: Path | str) -> bool:
    """Checks whether a lexical path component is a symlink, junction, or reparse point.

    Uses non-following lstat. Returns False if the component does not exist.
    Returns True if it is a symlink, junction, or reparse point.
    """
    try:
        st = os.lstat(str(path))
        if stat.S_ISLNK(st.st_mode):
            return True
        if (
            hasattr(st, "st_file_attributes")
            and hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT")
            and bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        ):
            return True
        return bool(hasattr(os.path, "isjunction") and os.path.isjunction(str(path)))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True  # Fail-closed on stat error


def _validate_canonical_destination_with_root(
    target_path: Path | str,
    root: Path | str,
) -> Path:
    """Validates that a destination path is strictly the canonical docs/agent/LESSONS.md file
    under the specified root, checking lexical path components before following any links.

    STRICT INVARIANTS:
    1. Lexical route matching: Normalized candidate path must match root/docs/agent/LESSONS.md.
    2. Reparse-point inspection: Non-following lstat on every component from destination up to root.
    3. Traversal, non-canonical filenames, or non-canonical subdirectories are rejected lexically.
    4. Nonexistent final destination is handled safely without following nonexistent targets.
    """
    root_path = Path(root).resolve()
    expected_lexical = Path(os.path.normpath(str(root_path / "docs" / "agent" / "LESSONS.md")))

    raw_p = Path(target_path)
    if raw_p.is_absolute():
        lexical_p = Path(os.path.normpath(str(raw_p)))
    else:
        lexical_p = Path(os.path.normpath(str(root_path / raw_p)))

    # 1. Enforce lexical route equivalence (Windows case-insensitivity defense)
    if os.path.normcase(str(lexical_p)) != os.path.normcase(str(expected_lexical)):
        raise ValueError(
            f"Destination violation: '{target_path}' is not the canonical lessons destination. "
            f"Target must strictly resolve to '{expected_lexical}'."
        )

    # 2. Check lexical route components for symlinks or reparse points BEFORE resolving
    curr = lexical_p
    while True:
        if is_lexical_reparse_point_or_link(curr):
            raise ValueError(
                f"Destination violation: path component '{curr}' is a reparse point or symbolic link."
            )
        if os.path.normcase(str(curr)) == os.path.normcase(str(root_path)) or curr == curr.parent:
            break
        curr = curr.parent

    return lexical_p


def validate_canonical_destination(
    target_path: Path | str,
) -> Path:
    """Validates that a destination path is strictly the canonical docs/agent/LESSONS.md file.

    STRICT PRODUCTION INVARIANT:
    Strictly locked to the production WORKSPACE_ROOT.
    Does NOT consult any mutable global test root.
    """
    return _validate_canonical_destination_with_root(target_path, root=WORKSPACE_ROOT)


# -----------------------------------------------------------------------------
# Markdown Rendering Sanitization & Injection Prevention Pipeline
# -----------------------------------------------------------------------------

BIDI_CONTROL_RE = re.compile(r"[\u202A-\u202E\u2066-\u2069\u200E\u200F\u061C]")
DANGEROUS_CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]")
ZERO_WIDTH_CHARS_RE = re.compile(r"[\u200B-\u200D\uFEFF\u00AD]")
UNICODE_SPACES_RE = re.compile(r"[\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]")
HORIZONTAL_RULE_RE = re.compile(r"(?m)^([ \t]*)([-*_])(?=(?:[ \t]*\2){2,}\s*$)")
HEADING_INJECTION_RE = re.compile(r"(?m)^([ \t]*)(#+)")
LIST_METADATA_ITEM_RE = re.compile(r"(?m)^([ \t]*)([-*+]|\d+[.)])(?=[ \t]+\*\*)")
IMAGE_EMBED_RE = re.compile(r"!\[")
LINK_CLOSE_RE = re.compile(r"\](?=[ \t]*[\(\[])")
LINK_OPEN_RE = re.compile(r"\[(?=[^\]\r\n]*\][ \t]*[\(\[])")
REF_DEF_RE = re.compile(r"(?m)^([ \t]*)\[(?=[^\]\r\n]*\][ \t]*:)")
REF_DEF_COLON_RE = re.compile(r"(?m)^([ \t]*\[[^\]\r\n]*\])[ \t]*:")
URL_SCHEME_RE = re.compile(r"\b(https?|ftp)://")
WWW_URL_RE = re.compile(r"\b(www)\.")
SPECIAL_SCHEME_RE = re.compile(r"\b(javascript|data|vbscript):")
PIPE_RE = re.compile(r"\|")
BLOCKQUOTE_RE = re.compile(r"(?m)^([ \t]*)(>+)")


def normalize_unicode_text(text: str) -> str:
    """Normalizes newline sequences, strips bidi/Trojan Source/zero-width chars, and unifies whitespace."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = BIDI_CONTROL_RE.sub("", text)
    text = DANGEROUS_CONTROL_RE.sub("", text)
    text = ZERO_WIDTH_CHARS_RE.sub("", text)
    text = UNICODE_SPACES_RE.sub(" ", text)
    return text


def defang_markdown_links(text: str) -> str:
    """Renders URL-like values, markdown links, autolinks, and images as inert text.

    Strict Invariants:
    1. Escapes closing brackets preceding link/reference targets (]( and ][):
       [click](url) -> \\[click\\](url)
       [[nested]](url) -> [[nested\\](url)
       \\\\[click](url) -> \\\\[click\\](url)
    2. Escapes opening brackets preceding matched closing brackets:
       [click](url) -> \\[click\\](url)
    3. Escapes reference link definitions:
       [ref]: url -> \\[ref\\]\\: url
    4. Defangs bare URLs (https://, http://, ftp://) into inert text (https\\://, etc.).
    5. Defangs dangerous schemes (javascript:, data:, vbscript:) into inert text.
    6. Leaves standalone brackets (e.g. [REDACTED_API_KEY]) intact.
    """
    if not text:
        return ""
    # 1. Neutralize link closing brackets
    text = LINK_CLOSE_RE.sub(r"\]", text)
    # 2. Neutralize link opening brackets
    text = LINK_OPEN_RE.sub(r"\[", text)
    # 3. Neutralize reference definitions
    text = REF_DEF_RE.sub(r"\1\[", text)
    text = REF_DEF_COLON_RE.sub(r"\1\:", text)
    # 4. Defang bare URLs and schemes
    text = URL_SCHEME_RE.sub(r"\1\://", text)
    text = WWW_URL_RE.sub(r"www\.", text)
    text = SPECIAL_SCHEME_RE.sub(r"\1\:", text)
    return text


def sanitize_html(text: str) -> str:
    """Escapes raw HTML tags to prevent HTML injection in markdown renderers."""
    if not text:
        return ""
    return text.replace("<", "&lt;").replace(">", "&gt;")


def balance_code_fences(text: str) -> str:
    """Detects unclosed markdown code fences and safely closes them.

    Implements CommonMark-compliant fence tracking:
    - Code fences open with 0..3 spaces followed by 3+ backticks or 3+ tildes.
    - Backtick fences cannot contain backticks in the info string.
    - Closing fence matches the opening delimiter character, has at least as many
      delimiter characters as the open fence, starts with 0..3 spaces, and contains
      no non-whitespace characters after the delimiter.
    - Accurately tracks nested/interleaved fences and balances unclosed fences.
    """
    if not text:
        return ""

    open_marker: str | None = None
    open_len: int = 0

    lines = text.split("\n")
    for line in lines:
        indent_len = len(line) - len(line.lstrip(" "))
        if indent_len > 3:
            continue
        stripped = line.lstrip(" ")
        if not stripped:
            continue

        if open_marker is None:
            if stripped.startswith("```"):
                run_len = len(stripped) - len(stripped.lstrip("`"))
                if run_len >= 3:
                    info_str = stripped[run_len:].strip()
                    if "`" not in info_str:
                        open_marker = "`"
                        open_len = run_len
            elif stripped.startswith("~~~"):
                run_len = len(stripped) - len(stripped.lstrip("~"))
                if run_len >= 3:
                    open_marker = "~"
                    open_len = run_len
        else:
            if stripped.startswith(open_marker * open_len):
                marker_run = len(stripped) - len(stripped.lstrip(open_marker))
                rest = stripped[marker_run:].strip()
                if not rest:
                    open_marker = None
                    open_len = 0

    if open_marker is not None:
        closing_fence = open_marker * open_len
        text = text.rstrip() + f"\n{closing_fence}\n"

    return text


def sanitize_inline_text(
    field_name: str,
    value: Any,
    max_len: int = MAX_INLINE_FIELD_LENGTH_BYTES,
) -> str:
    """Sanitizes single-line metadata fields.

    Strict Invariants:
    1. Converts value to string, normalizes Unicode and bidi controls, strips newlines.
    2. Enforces length cap.
    3. Re-applies secret redaction; fails closed on incomplete redaction.
    4. Escapes HTML tags.
    5. Defangs markdown links, bare URLs, and images.
    6. Escapes table pipe characters (| -> \\|).
    7. Strips leading/trailing whitespace.
    """
    if value is None:
        return ""
    val_str = str(value)
    if not val_str:
        return ""

    val_str = normalize_unicode_text(val_str)
    if len(val_str) > max_len:
        raise ValueError(f"Field '{field_name}' exceeds maximum permitted length of {max_len} bytes.")

    redacted, redaction_status = redact_sensitive_text(val_str)
    if redaction_status == "REDACTION_INCOMPLETE":
        raise ValueError(f"Sanitization failed for field '{field_name}': unredacted sensitive data detected.")

    # Single-line: replace all newlines with a space
    redacted = re.sub(r"[\r\n]+", " ", redacted)
    redacted = sanitize_html(redacted)
    redacted = defang_markdown_links(redacted)
    redacted = PIPE_RE.sub(r"\|", redacted)
    return redacted.strip()


def sanitize_multiline_text(
    field_name: str,
    value: Any,
    max_len: int = MAX_FIELD_LENGTH_BYTES,
    indent_subsequent: str | None = None,
) -> str:
    """Sanitizes multiline paragraph text for safe inclusion in Markdown.

    Strict Invariants:
    1. Normalizes newlines and Unicode whitespace/zero-width/bidi characters.
    2. Enforces length cap.
    3. Re-applies secret redaction; fails closed on incomplete redaction.
    4. Escapes HTML tags.
    5. Neutralizes image embed markdown (![ -> \\![).
    6. Defangs markdown links and bare URLs.
    7. Escapes table pipe characters (| -> \\|).
    8. Neutralizes blockquotes (> -> \\>).
    9. Neutralizes Markdown heading injection (# -> \\#).
    10. Neutralizes horizontal rules (---, ***, ___).
    11. Neutralizes metadata list item injection (- **, * **, 1. **).
    12. Balances code fences to prevent block leaks.
    13. Optionally indents subsequent lines to nest under Markdown bullets.
    """
    if value is None:
        return ""
    val_str = str(value)
    if not val_str:
        return ""

    val_str = normalize_unicode_text(val_str)
    if len(val_str) > max_len:
        raise ValueError(f"Field '{field_name}' exceeds maximum permitted length of {max_len} bytes.")

    redacted, redaction_status = redact_sensitive_text(val_str)
    if redaction_status == "REDACTION_INCOMPLETE":
        raise ValueError(f"Sanitization failed for field '{field_name}': unredacted sensitive data detected.")

    # Escape HTML
    sanitized = sanitize_html(redacted)

    # Neutralize image embeds
    sanitized = IMAGE_EMBED_RE.sub(r"\![", sanitized)

    # Defang links and bare URLs
    sanitized = defang_markdown_links(sanitized)

    # Neutralize table pipes
    sanitized = PIPE_RE.sub(r"\|", sanitized)

    # Neutralize blockquotes
    sanitized = BLOCKQUOTE_RE.sub(r"\1\\\2", sanitized)

    # Balance code fences
    sanitized = balance_code_fences(sanitized)

    # Neutralize Markdown heading injection
    sanitized = HEADING_INJECTION_RE.sub(r"\1\\\2", sanitized)

    # Neutralize horizontal rules
    sanitized = HORIZONTAL_RULE_RE.sub(r"\1\\\2", sanitized)

    # Neutralize metadata list item injection
    sanitized = LIST_METADATA_ITEM_RE.sub(r"\1\\\2", sanitized)

    if indent_subsequent:
        lines = sanitized.split("\n")
        if len(lines) > 1:
            sanitized = lines[0] + "\n" + "\n".join(
                (indent_subsequent + line if line.strip() else line) for line in lines[1:]
            )

    return sanitized


def sanitize_enum_field(
    field_name: str,
    value: Any,
    allowed_values: tuple[str, ...],
    default: str,
) -> str:
    """Validates an enum-like field value against allowlist, returning default on mismatch."""
    if value is None:
        return default
    val_str = str(value).strip()
    if val_str in allowed_values:
        return val_str
    return default


def sanitize_markdown_field(
    field_name: str,
    value: str | None,
    max_len: int = MAX_FIELD_LENGTH_BYTES,
) -> str:
    """Sanitizes text for safe inclusion in LESSONS.md markdown (backward compatibility wrapper)."""
    return sanitize_multiline_text(field_name, value, max_len=max_len)


# -----------------------------------------------------------------------------
# Non-Writing Promotion Preview & Action Manifest Engine
# -----------------------------------------------------------------------------

def parse_existing_lessons(lessons_file: Path | str) -> list[dict[str, str]]:
    """Parses existing lessons from docs/agent/LESSONS.md."""
    p = Path(lessons_file)
    if not p.exists():
        return []

    content = p.read_text(encoding="utf-8")
    lessons: list[dict[str, str]] = []

    pattern = re.compile(
        r"^##\s+(LES-\d+):\s+([^\r\n]+)\r?\n(.*?)(?=\n##\s+LES-|\Z)",
        re.MULTILINE | re.DOTALL,
    )

    for m in pattern.finditer(content):
        lid = m.group(1).strip()
        title = m.group(2).strip()
        body = m.group(3).strip()

        subsystem_m = re.search(r"-\s+\*\*Subsystem Domain:\*\*\s+([^\r\n]+)", body)
        prob_m = re.search(r"-\s+\*\*Observed Problem:\*\*\r?\n(.*?)(?=\n-\s+\*\*|\Z)", body, re.DOTALL)
        rule_m = re.search(r"-\s+\*\*Prevention Rules?:\*\*\r?\n(.*?)(?=\n-\s+\*\*|\Z)", body, re.DOTALL)

        lessons.append({
            "lesson_id": lid,
            "title": title,
            "subsystem": subsystem_m.group(1).strip() if subsystem_m else "",
            "observed_problem": prob_m.group(1).strip() if prob_m else "",
            "prevention_rule": rule_m.group(1).strip() if rule_m else "",
            "full_body": body,
        })

    return lessons


def get_next_lesson_id(lessons_file: Path | str) -> str:
    """Calculates the next sequential LES-XXX ID from existing lessons file."""
    existing = parse_existing_lessons(lessons_file)
    max_id = 0
    for l_entry in existing:
        m = re.match(r"^LES-(\d+)$", l_entry.get("lesson_id", ""))
        if m:
            max_id = max(max_id, int(m.group(1)))
    return f"LES-{max_id + 1:03d}"


def format_lesson_markdown(
    candidate: LessonCandidate,
    lesson_id: str,
    title: str | None = None,
) -> str:
    """Formats candidate strictly into the canonical 8-field LESSONS.md format.

    Re-applies final sanitization, secret redaction, and structural injection defenses.
    """
    clean_lid = sanitize_inline_text("lesson_id", lesson_id, max_len=64)

    raw_title = title or candidate.observed_behavior.get("summary") or "Unspecified Lesson"
    c_title = sanitize_inline_text("title", raw_title, max_len=512)

    if candidate.affected_subsystems:
        clean_subsystems = [
            sanitize_inline_text("subsystem", s, max_len=128)
            for s in candidate.affected_subsystems
            if s
        ]
        subsystems = ", ".join(clean_subsystems) if clean_subsystems else "General / Engineering Integrity"
    else:
        subsystems = "General / Engineering Integrity"

    today_str = datetime.date.today().isoformat()

    raw_obs = candidate.observed_behavior.get("summary", "No observed problem description provided.")
    obs_prob = sanitize_multiline_text("observed_problem", raw_obs, indent_subsequent="  ")

    if candidate.observed_behavior.get("failure_signal"):
        clean_sig = sanitize_inline_text(
            "failure_signal",
            str(candidate.observed_behavior["failure_signal"]),
            max_len=1024,
        ).replace("`", "'")
        obs_prob += f"\n  Failure signal: `{clean_sig}`"

    if candidate.observed_behavior.get("exit_code") is not None:
        clean_exit = sanitize_inline_text(
            "exit_code",
            str(candidate.observed_behavior["exit_code"]),
            max_len=32,
        )
        obs_prob += f"\n  Process exit code: `{clean_exit}`"

    repro = sanitize_multiline_text(
        "reproduction_procedure",
        candidate.reproduction_procedure or "No reproduction procedure documented.",
        indent_subsequent="  ",
    )
    verified_mech = sanitize_multiline_text(
        "verified_root_cause",
        candidate.verified_root_cause or "Unverified root cause mechanism.",
        indent_subsequent="    ",
    )
    rule = sanitize_multiline_text(
        "corrective_recommendation",
        candidate.corrective_recommendation or "No codified prevention rule.",
        indent_subsequent="     ",
    )
    test_or_files = sanitize_multiline_text(
        "regression_test",
        candidate.regression_test or "No regression test specified.",
        indent_subsequent="    ",
    )
    confidence = sanitize_enum_field(
        "confidence_level",
        candidate.confidence_level,
        CONFIDENCE_LEVELS,
        default="UNCONFIRMED",
    )
    scope = sanitize_multiline_text(
        "scope_of_applicability",
        candidate.scope_of_applicability or "Scope not defined.",
        indent_subsequent="    ",
    )

    md = (
        f"## {clean_lid}: {c_title}\n\n"
        f"- **Lesson ID:** `{clean_lid}`\n"
        f"- **Subsystem Domain:** {subsystems}\n"
        f"- **Date Recorded:** {today_str}\n"
        f"- **Observed Problem:**\n"
        f"  {obs_prob}\n"
        f"- **Reproduction Evidence:**\n"
        f"  {repro}\n"
        f"- **Verified Root Cause & Unresolved Mechanisms:**\n"
        f"  - *Verified Mechanism:* {verified_mech}\n"
        f"- **Prevention Rules:**\n"
        f"  1. {rule}\n"
        f"- **Supporting Files & Commands:**\n"
        f"  - {test_or_files}\n"
        f"- **Confidence & Applicability Boundaries:**\n"
        f"  - *Confidence:* **{confidence}**\n"
        f"  - *Applicability:* {scope}\n"
    )
    return md


def _prepare_promotion_diff_with_root(
    candidate: LessonCandidate,
    lessons_file: Path | str,
    *,
    root: Path | str,
    title: str | None = None,
) -> tuple[str, str]:
    """Generates an exact unified diff of proposed promotion without mutating the file.

    Internal implementation helper accepting explicit root for isolated testing.
    """
    root_path = Path(root).resolve()
    dest_path = _validate_canonical_destination_with_root(lessons_file, root=root_path)
    existing_content = dest_path.read_text(encoding="utf-8") if dest_path.exists() else ""
    next_id = get_next_lesson_id(dest_path)
    new_block = format_lesson_markdown(candidate, lesson_id=next_id, title=title)

    sep = "\n---\n\n" if existing_content.rstrip().endswith("\n") else "\n\n---\n\n"
    proposed_content = existing_content.rstrip() + sep + new_block

    diff_lines = list(difflib.unified_diff(
        existing_content.splitlines(keepends=True),
        proposed_content.splitlines(keepends=True),
        fromfile=str(dest_path),
        tofile=str(dest_path),
    ))
    return "".join(diff_lines), next_id


def prepare_promotion_diff(
    candidate: LessonCandidate,
    lessons_file: Path | str,
    title: str | None = None,
) -> tuple[str, str]:
    """Generates an exact unified diff of proposed promotion without mutating the file.

    STRICT PRODUCTION INVARIANT:
    Always uses WORKSPACE_ROOT. Accepts no root override parameter.
    """
    return _prepare_promotion_diff_with_root(
        candidate,
        lessons_file,
        root=WORKSPACE_ROOT,
        title=title,
    )


def _prepare_promotion_preview_with_root(
    candidate: LessonCandidate,
    lessons_file: Path | str | None = None,
    *,
    root: Path | str,
    title: str | None = None,
) -> dict[str, Any]:
    """Prepares a non-writing promotion preview under an explicit root.

    Internal implementation helper accepting explicit root for isolated testing.
    """
    root_path = Path(root).resolve()
    target = lessons_file or (root_path / "docs" / "agent" / "LESSONS.md")
    dest_path = _validate_canonical_destination_with_root(target, root=root_path)

    # Layer C completeness check
    if not candidate.verified_root_cause:
        raise ValueError("Cannot prepare promotion preview: verified_root_cause is missing.")
    if not candidate.reproduction_procedure:
        raise ValueError("Cannot prepare promotion preview: reproduction_procedure is missing.")
    if not candidate.corrective_recommendation:
        raise ValueError("Cannot prepare promotion preview: corrective_recommendation is missing.")
    if not candidate.scope_of_applicability:
        raise ValueError("Cannot prepare promotion preview: scope_of_applicability is missing.")

    # Calculate next ID and diff without writing
    diff_text, proposed_lesson_id = _prepare_promotion_diff_with_root(
        candidate, dest_path, root=root_path, title=title
    )
    rendered_md = format_lesson_markdown(candidate, lesson_id=proposed_lesson_id, title=title)

    # Candidate content hash
    candidate_content_hash = compute_candidate_hash(candidate)

    # Diff hash
    diff_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()

    # Cryptographic Action Manifest Digest
    action_manifest = {
        "candidate_id": candidate.candidate_id,
        "candidate_content_hash": candidate_content_hash,
        "canonical_destination": str(dest_path),
        "diff_hash": diff_hash,
        "proposed_lesson_id": proposed_lesson_id,
    }
    canonical_manifest_json = json.dumps(
        action_manifest, sort_keys=True, separators=(",", ":"), default=str
    )
    action_manifest_digest = hashlib.sha256(canonical_manifest_json.encode("utf-8")).hexdigest()

    # Check potential conflicts
    conflicts: list[str] = []
    if dest_path.exists():
        existing_lessons = parse_existing_lessons(dest_path)
        cand_summary = candidate.observed_behavior.get("summary", "").lower()
        cand_words = {w for w in re.findall(r"\b[a-zA-Z0-9_\-]{4,}\b", cand_summary)}

        for l_entry in existing_lessons:
            l_title = l_entry.get("title", "").lower()
            l_prob = l_entry.get("observed_problem", "").lower()
            target_text = f"{l_title} {l_prob}"

            has_substring = bool(cand_summary and (cand_summary in l_title or cand_summary in l_prob))
            target_words = {w for w in re.findall(r"\b[a-zA-Z0-9_\-]{4,}\b", target_text)}
            overlap = cand_words.intersection(target_words)

            if has_substring or len(overlap) >= 2:
                conflicts.append(
                    f"Possible overlap with existing {l_entry.get('lesson_id')}: '{l_entry.get('title')}' "
                    f"(common terms: {', '.join(sorted(overlap)) if overlap else 'substring'})"
                )

    unresolved_concerns: list[str] = []
    for finding in candidate.review_findings:
        for c in finding.get("concerns", []):
            if c:
                clean_c = sanitize_inline_text("concern", str(c), max_len=512)
                unresolved_concerns.append(clean_c)

    return {
        "status": "PREVIEW_ONLY",
        "action_manifest_digest": action_manifest_digest,
        "candidate_id": candidate.candidate_id,
        "proposed_lesson_id": proposed_lesson_id,
        "canonical_destination": str(dest_path),
        "candidate_content_hash": candidate_content_hash,
        "diff_hash": diff_hash,
        "rendered_markdown": rendered_md,
        "proposed_diff": diff_text,
        "evidence_references": candidate.evidence_references,
        "execution_evidence": {
            "evidence_provenance_status": candidate.evidence_provenance_status,
            "has_verified_reproduction": candidate.has_verified_reproduction(workspace_root=root_path),
            "has_verified_fix": candidate.has_verified_fix(workspace_root=root_path),
            "is_empirically_verified": candidate.is_empirically_verified(workspace_root=root_path),
            "envelope_count": len(candidate.execution_envelopes),
        },
        "review_status": candidate.review_status,
        "potential_conflicts": conflicts,
        "unresolved_concerns": unresolved_concerns,
        "notice": (
            "PREVIEW-ONLY: This preview demonstrates the proposed addition to LESSONS.md. "
            "No automatic promotion has occurred. The action_manifest_digest binds the "
            "exact diff and destination for audit verification. Writing to permanent "
            "memory requires explicit human action."
        ),
    }


def prepare_promotion_preview(
    candidate: LessonCandidate,
    lessons_file: Path | str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Prepares a non-writing promotion preview and action-manifest digest.

    STRICT PRODUCTION INVARIANT:
    Always uses WORKSPACE_ROOT. Accepts no root override parameter.
    """
    return _prepare_promotion_preview_with_root(
        candidate,
        lessons_file=lessons_file,
        root=WORKSPACE_ROOT,
        title=title,
    )


def _stage_for_human_approval_with_root(
    candidate: LessonCandidate,
    lessons_file: Path | str | None = None,
    *,
    root: Path | str,
) -> dict[str, Any]:
    """Prepares candidate for human review under explicit root (preview-only)."""
    root_path = Path(root).resolve()
    target = lessons_file or (root_path / "docs" / "agent" / "LESSONS.md")
    preview = _prepare_promotion_preview_with_root(candidate, lessons_file=target, root=root_path)
    candidate.review_status = "PREPARED_FOR_REVIEW"
    return preview


def stage_for_human_approval(
    candidate: LessonCandidate,
    lessons_file: Path | str | None = None,
) -> dict[str, Any]:
    """Prepares candidate for human review (preview-only).

    STRICT PRODUCTION INVARIANT:
    Always uses WORKSPACE_ROOT. Accepts no root override parameter.
    """
    return _stage_for_human_approval_with_root(
        candidate,
        lessons_file=lessons_file,
        root=WORKSPACE_ROOT,
    )


# -----------------------------------------------------------------------------
# Fail-Closed Deprecated Authorization APIs
# -----------------------------------------------------------------------------

def apply_human_approval(
    candidate: LessonCandidate,
    approval_hash: str | None = None,
    approver: str = "developer",
) -> tuple[bool, str]:
    """FAIL-CLOSED: In-process model execution cannot establish human approval.

    In accordance with ENG-OS-02 Phase 3C2, this function strictly fails closed.
    No model-controlled Python function or script may establish human approval.
    """
    return (
        False,
        (
            "SELF_APPROVAL_PROHIBITED: Model-controlled Python execution cannot establish human approval. "
            "Candidate promotion is preview-only. Human authorization requires external human interaction."
        ),
    )


def validate_approval_integrity(candidate: LessonCandidate) -> tuple[bool, str]:
    """FAIL-CLOSED: In-process human approval validation is disabled.

    In accordance with ENG-OS-02 Phase 3C2, candidate promotion is preview-only.
    """
    return (
        False,
        (
            "APPROVAL_UNSUPPORTED: In-process human approval validation is disabled in Phase 3C2. "
            "Candidate promotion is preview-only and permanent memory writes require external human action."
        ),
    )


def promote_to_lessons_file(
    candidate: LessonCandidate,
    lessons_file: Path | str,
    confirmed: bool = False,
    title: str | None = None,
) -> tuple[bool, str]:
    """FAIL-CLOSED: Autonomous promotion to permanent memory is strictly disabled.

    In accordance with ENG-OS-02 Phase 3C2, candidate promotion is preview-only.
    No model-controlled Python function or script may autonomously append to
    permanent engineering memory. Use prepare_promotion_preview() instead.
    """
    return (
        False,
        (
            "PROMOTION_PROHIBITED: Autonomous promotion to permanent memory is strictly disabled. "
            "Candidate promotion is preview-only. Permanent memory writes require external human action."
        ),
    )
