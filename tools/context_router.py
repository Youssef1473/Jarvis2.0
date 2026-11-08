"""Deterministic Adaptive Context Router for Jarvis 2.0 (ENG-OS-02 Phase 2B).

Classifies engineering task prompts into domain categories, identifies canonical modules,
and compiles compact, traceable context packets from verified project architecture assets.
Operates purely deterministically with zero network calls, zero subprocesses, and zero file writes.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
LESSONS_REGISTRY_PATH = Path("docs/agent/LESSONS.md")
MAX_REGISTRY_BYTES = 128 * 1024  # 128 KB
MAX_RETRIEVED_LESSONS = 3
MAX_LESSONS_BUDGET_CHARS = 2400  # ~600 tokens

# -----------------------------------------------------------------------------
# Risk Tiers & Priority Definitions
# -----------------------------------------------------------------------------

RISK_TIERS = {
    "CRITICAL": 4,
    "HIGH": 3,
    "MEDIUM": 2,
    "LOW": 1,
}

CATEGORY_RISK_MAP = {
    "SECURITY_BOUNDARY": "CRITICAL",
    "WINDOWS_PROCESS_CONTAINMENT": "CRITICAL",
    "AUDIO_THREADING": "HIGH",
    "SQLITE_PERSISTENCE": "HIGH",
    "UNKNOWN": "HIGH",  # Fail-safe: ambiguous/unknown tasks escalate to HIGH risk
    "WINDOWS_UI_AUTOMATION": "MEDIUM",
    "API_COMPATIBILITY": "MEDIUM",
    "UI_FRONTEND": "MEDIUM",
    "ARCHITECTURE": "MEDIUM",
    "TESTING_VERIFICATION": "LOW",
    "DOCUMENTATION": "LOW",
}

# -----------------------------------------------------------------------------
# Compiled Deterministic Matching Patterns
# -----------------------------------------------------------------------------

CATEGORY_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "AUDIO_THREADING": [
        re.compile(r"\b(wasapi|kokoro|tts|wake[-_ ]?word|microphone|microuter|audio[-_ ]?playback|audio[-_ ]?stream|epoch|barge[-_ ]?in|cancellation|ring[-_ ]?buffer|sound|speech|mmcss|p[-_ ]?core|shm[-_ ]?transport)\b", re.IGNORECASE),
        re.compile(r"\bjarvis[./\\]audio\b", re.IGNORECASE),
        re.compile(r"\b(cancellation\.py|win32_affinity\.py|tts\.py|wake_word\.py)\b", re.IGNORECASE),
        re.compile(r"\b(global_epoch_state|mask_pcore_audio_isolated|mask_pcore_compute)\b", re.IGNORECASE),
    ],
    "SQLITE_PERSISTENCE": [
        re.compile(r"\b(sqlite|wal|single[-_ ]?writer|actor|pragmas?|read[-_ ]?pool|fts5|sqlite[-_ ]?vec|bm25|database|db|transaction|commit|schema|migration|facts[-_ ]?manager|interaction[-_ ]?memory)\b", re.IGNORECASE),
        re.compile(r"\bjarvis[./\\]core[./\\]memory\b", re.IGNORECASE),
        re.compile(r"\b(sqlite_base\.py|interaction_memory\.py|sleep_consolidator\.py)\b", re.IGNORECASE),
        re.compile(r"\b(singlewriteractor|journal_mode|synchronous)\b", re.IGNORECASE),
    ],
    "SECURITY_BOUNDARY": [
        re.compile(r"\b(security|boundary|dual[-_ ]?key|approval|taint|turn[-_ ]?taint|ssrf|egress|ip[-_ ]?pinning|token|secret|credential|privilege|sanitize|untrusted|auth|confine|path[-_ ]?traversal)\b", re.IGNORECASE),
        re.compile(r"\b(security_boundaries\.py|tool_orchestrator\.py)\b", re.IGNORECASE),
        re.compile(r"\b(dualkeyapprovalmanager|turntainttracker|validate_confined_path|validate_egress_url)\b", re.IGNORECASE),
        re.compile(r"\b(adr[-_ ]?004|adr[-_ ]?006)\b", re.IGNORECASE),
    ],
    "WINDOWS_UI_AUTOMATION": [
        re.compile(r"\b(uia|ui[-_ ]?automation|pywinauto|accessibility|element|window[-_ ]?hook|focus|click|keystroke|cdp|chrome[-_ ]?devtools|browser[-_ ]?cdp|tab|desktop[-_ ]?presence)\b", re.IGNORECASE),
        re.compile(r"\b(windows_uia\.py|browser_cdp\.py|window_hook_daemon\.py)\b", re.IGNORECASE),
        re.compile(r"\b(com[-_ ]?sta|setwineventhook|setwindowshookex)\b", re.IGNORECASE),
    ],
    "WINDOWS_PROCESS_CONTAINMENT": [
        re.compile(r"\b(job[-_ ]?object|containment|create_suspended|kill_on_job_close|job_object_limit[a-z0-9_]*|subproc(ess)?[-_ ]?containment|process[-_ ]?tree|orphan[-_ ]?reap(ing)?|appcontainer|job_object_sandbox)\b", re.IGNORECASE),
        re.compile(r"\b(job_object_sandbox\.py|appcontainer)\b", re.IGNORECASE),
        re.compile(r"\badr[-_ ]?002\b", re.IGNORECASE),
    ],
    "API_COMPATIBILITY": [
        re.compile(r"\b(api[-_ ]?compat(ibility)?|public[-_ ]?api|signature|breaking[-_ ]?change|shim|re[-_ ]?export|backward[-_ ]?compat|skill[-_ ]?registry|base[-_ ]?skill|skill[-_ ]?definition)\b", re.IGNORECASE),
        re.compile(r"\b(skills[./\\]base\.py|antigravity_bridge\.py)\b", re.IGNORECASE),
        re.compile(r"\b(skillregistry\.register|public_interfaces)\b", re.IGNORECASE),
    ],
    "UI_FRONTEND": [
        re.compile(r"\b(ui[-_ ]?server|hud|fastapi|starlette|websocket|frontend|holographic|html|css|jinja|dashboard|telemetry[-_ ]?feed)\b", re.IGNORECASE),
        re.compile(r"\bjarvis[./\\]ui\b", re.IGNORECASE),
        re.compile(r"\b(ui[./\\]server\.py|static|templates)\b", re.IGNORECASE),
    ],
    "TESTING_VERIFICATION": [
        re.compile(r"\b(tests?|pytest|unit[-_ ]?tests?|integration[-_ ]?tests?|mock|assertion|coverage|regression|fixture|benchmark|verifier|truthfulness|verification[-_ ]?runner)\b", re.IGNORECASE),
        re.compile(r"\b(run_fast_tests\.py|lint\.py|diagnostics\.py)\b", re.IGNORECASE),
        re.compile(r"\btests[./\\].*\.py\b", re.IGNORECASE),
    ],
    "ARCHITECTURE": [
        re.compile(r"\b(architect(ure|ural)?|blueprint|subsystem|project[-_ ]?map|epistemic|hierarchy|adr|adr[-_ ]?index|design[-_ ]?record|project[-_ ]?structure|module[-_ ]?ownership)\b", re.IGNORECASE),
        re.compile(r"(index\.md|project_map\.md|master_architectural_blueprint)", re.IGNORECASE),
    ],
    "DOCUMENTATION": [
        re.compile(r"\b(doc|documentation|readme|roadmap|session[-_ ]?log|project[-_ ]?state|lessons|handoff|changelog|markdown)\b", re.IGNORECASE),
        re.compile(r"\b(project_state\.md|session_log\.md|lessons\.md|roadmap\.md|handoff\.md)\b", re.IGNORECASE),
    ],
}

# -----------------------------------------------------------------------------
# Static Verified Context Catalog
# -----------------------------------------------------------------------------

CONTEXT_CATALOG: dict[str, dict[str, Any]] = {
    "AUDIO_THREADING": {
        "canonical_modules": [
            {"path": "jarvis/audio/tts.py", "reason": "Real-time WASAPI audio playback and Kokoro neural synthesis pipeline"},
            {"path": "jarvis/audio/wake_word.py", "reason": "Wake-word audio router and physical P-core MMCSS capture"},
            {"path": "jarvis/core/win32_affinity.py", "reason": "Hardware core affinity partitioning and MMCSS Pro Audio elevation"},
            {"path": "jarvis/core/cancellation.py", "reason": "GLOBAL_EPOCH_STATE cooperative cancellation engine"},
        ],
        "applicable_rules": [
            {"path": ".agents/rules/audio_threading.md", "reason": "WASAPI real-time audio threading, affinity masks, and preemption"}
        ],
        "applicable_adrs": [
            {"path": "docs/adr/ADR-003-epoch-based-audio-cancellation.md", "reason": "Cooperative epoch invalidation contract on user barge-in"},
            {"path": "docs/adr/ADR-005-win32-hardware-core-affinity.md", "reason": "Alder Lake 4P+8E CPU affinity partitioning (playback mask 0x0003, compute mask 0x00FC)"},
        ],
        "relevant_lessons": [],
        "mandatory_invariants": [
            "WASAPI playback and Kokoro synthesis threads must NEVER block on disk I/O, network queries, or LLM inference.",
            "WASAPI playback worker must apply MASK_PCORE_AUDIO_ISOLATED (0x0003) and register MMCSS Pro Audio.",
            "Kokoro neural synthesis worker runs on MASK_PCORE_COMPUTE (0x00FC).",
            "Barge-in cancellation must evaluate GLOBAL_EPOCH_STATE cooperatively at audio frame and loop iteration boundaries.",
        ],
        "verification_suite": [
            {"path": "tests/test_audio_affinity.py", "reason": "Affinity bitmask verification"},
            {"path": "tests/test_milestone3_audio_invariants.py", "reason": "Audio epoch preemption and ring buffer invariants"},
        ],
        "recommended_specialists": ["systems-architect"],
    },
    "SQLITE_PERSISTENCE": {
        "canonical_modules": [
            {"path": "jarvis/core/memory/sqlite_base.py", "reason": "SingleWriterActor serialization, WAL pragma setup, and connection pooling"},
            {"path": "jarvis/core/memory/interaction_memory.py", "reason": "Interaction history storage and retrieval"},
            {"path": "jarvis/core/memory/sleep_consolidator.py", "reason": "Background consolidation and vacuuming"},
        ],
        "applicable_rules": [
            {"path": ".agents/rules/persistence_sqlite.md", "reason": "SQLite WAL serialization, reader pool, and sqlite-vec"}
        ],
        "applicable_adrs": [
            {"path": "docs/adr/ADR-001-sqlite-single-writer-wal.md", "reason": "Single-writer serialization actor and WAL mode specification"}
        ],
        "relevant_lessons": [],
        "mandatory_invariants": [
            "All database mutations must serialize strictly through SingleWriterActor.",
            "Database connections must enforce PRAGMA journal_mode=WAL and PRAGMA synchronous=NORMAL.",
            "Readers must connect through read-only pools (mode=ro) and never execute write transactions.",
        ],
        "verification_suite": [
            {"path": "tests/test_milestone2_memory_hardening.py", "reason": "WAL serialization and concurrency safety"},
            {"path": "tests/test_ro_pool.py", "reason": "Read-only connection pool invariants"},
        ],
        "recommended_specialists": ["systems-architect"],
    },
    "SECURITY_BOUNDARY": {
        "canonical_modules": [
            {"path": "jarvis/core/security_boundaries.py", "reason": "DualKeyApprovalManager, path confinement, egress validation, and taint tracking"},
            {"path": "jarvis/core/pipeline/tool_orchestrator.py", "reason": "Tool dispatch authorization and cycle detection"},
        ],
        "applicable_rules": [
            {"path": ".agents/rules/security_boundaries.md", "reason": "Security boundaries, dual-key gates, path confinement, and SSRF egress defense"}
        ],
        "applicable_adrs": [
            {"path": "docs/adr/ADR-004-dual-key-tool-authorization-turn-taint.md", "reason": "Dual-key approval gating and TurnTaintTracker lifecycle"},
            {"path": "docs/adr/ADR-006-egress-validation-ip-pinning.md", "reason": "Outbound SSRF validation and socket IP pinning"},
        ],
        "relevant_lessons": [],
        "mandatory_invariants": [
            "All mutating tool actions must route through DualKeyApprovalManager.",
            "Filesystem paths must be validated via validate_confined_path before access.",
            "Outbound network requests must be validated via validate_egress_url with fail-closed DNS resolution and private IP blocking.",
            "Untrusted inputs (web scrapes, speech audio, telemetry) must be tracked via TurnTaintTracker.",
        ],
        "verification_suite": [
            {"path": "tests/test_security_boundaries.py", "reason": "Dual-key authorization and path confinement"},
            {"path": "tests/test_milestone1_security_hardening.py", "reason": "Egress validation and taint tracking"},
        ],
        "recommended_specialists": ["security-auditor"],
    },
    "WINDOWS_UI_AUTOMATION": {
        "canonical_modules": [
            {"path": "jarvis/skills/windows_uia.py", "reason": "Native Windows UI Automation and accessibility tree traversal"},
            {"path": "jarvis/skills/browser_cdp.py", "reason": "Direct Chrome DevTools Protocol browser automation"},
        ],
        "applicable_rules": [
            {"path": ".agents/skills/windows-automation/SKILL.md", "reason": "Semantic element inspection and reliable focus control"}
        ],
        "applicable_adrs": [],
        "relevant_lessons": [],
        "mandatory_invariants": [
            "Prefer semantic UI Automation tree navigation and pywinauto control over synthetic blind keystrokes.",
            "COM threading apartments (STA/MTA) must be initialized and uninitialized cleanly.",
            "CDP browser automation must communicate over loopback WebSockets without window stealing.",
        ],
        "verification_suite": [
            {"path": "tests/test_windows_uia.py", "reason": "UIA desktop and browser control verification"}
        ],
        "recommended_specialists": ["systems-architect"],
    },
    "WINDOWS_PROCESS_CONTAINMENT": {
        "canonical_modules": [
            {"path": "jarvis/skills/job_object_sandbox.py", "reason": "Win32 Job Object containment and orphan process tree reaping"}
        ],
        "applicable_rules": [
            {"path": ".agents/rules/security_boundaries.md", "reason": "Subprocess isolation and Job Object limit invariants"}
        ],
        "applicable_adrs": [
            {"path": "docs/adr/ADR-002-win32-job-object-containment.md", "reason": "Win32 Job Object limits and suspended process assignment"}
        ],
        "relevant_lessons": [],
        "mandatory_invariants": [
            "Child processes intended for containment must be spawned suspended (CREATE_SUSPENDED).",
            "Job Objects must be configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE before resuming the process.",
            "Fail closed (RuntimeError) if assignment to Job Object fails.",
            "subprocess.run(shell=True) and os.system() are strictly forbidden.",
        ],
        "verification_suite": [
            {"path": "tests/test_job_object_sandbox.py", "reason": "Job Object assignment, kill-on-close, and handle lifecycle"}
        ],
        "recommended_specialists": ["security-auditor"],
    },
    "API_COMPATIBILITY": {
        "canonical_modules": [
            {"path": "jarvis/skills/base.py", "reason": "SkillRegistry base class and tool decorators"},
            {"path": "jarvis/core/antigravity_bridge.py", "reason": "Antigravity sidecar bridge and IPC schema"},
        ],
        "applicable_rules": [
            {"path": "AGENTS.md", "reason": "Section 4: Preserve Public Interfaces and downstream compatibility"}
        ],
        "applicable_adrs": [],
        "relevant_lessons": [],
        "mandatory_invariants": [
            "Maintain backward compatibility with public APIs and re-export shims imported by tests/.",
            "Verify downstream callers before modifying signatures.",
        ],
        "verification_suite": [
            {"path": "tests/test_skills.py", "reason": "Skill registration and dispatch interface tests"}
        ],
        "recommended_specialists": ["test-guardian"],
    },
    "UI_FRONTEND": {
        "canonical_modules": [
            {"path": "jarvis/ui/server.py", "reason": "FastAPI/Starlette HUD and telemetry WebSocket server"}
        ],
        "applicable_rules": [
            {"path": ".agents/skills/jarvis-ui/SKILL.md", "reason": "Holographic design tokens and WebSocket event protocol"}
        ],
        "applicable_adrs": [],
        "relevant_lessons": [],
        "mandatory_invariants": [
            "Blocking I/O, heavy disk queries, or CPU-bound loops must never run on the Starlette/FastAPI asyncio event loop.",
            "Dedicated OS background worker threads must synchronize with asyncio loop via thread-safe queues.",
        ],
        "verification_suite": [
            {"path": "tests/test_ui_server.py", "reason": "HUD endpoint and WebSocket stream verification"}
        ],
        "recommended_specialists": ["systems-architect"],
    },
    "TESTING_VERIFICATION": {
        "canonical_modules": [
            {"path": "tools/run_fast_tests.py", "reason": "Tier 1 fast test runner and test tier orchestrator"},
            {"path": "tools/lint.py", "reason": "Fast static linting and syntax scanner (<20ms)"},
            {"path": "tools/diagnostics.py", "reason": "AST structural invariant scanner"},
        ],
        "applicable_rules": [
            {"path": ".agents/skills/verification-runner/SKILL.md", "reason": "5-tier test verification protocol"},
            {"path": "AGENTS.md", "reason": "Section 6: Evidence Hierarchy and Verification Truthfulness Contract"}
        ],
        "applicable_adrs": [],
        "relevant_lessons": [],
        "mandatory_invariants": [
            "Live tool execution output strictly supersedes historical documentation or claims.",
            "Never assert test success without live, zero-exit-code observable tool execution.",
            "Distinguish PASS, FAIL, TIMEOUT, RUNNING, CANCELLED, and NOT VERIFIED states.",
        ],
        "verification_suite": [
            {"path": "tests/test_diagnostics.py", "reason": "Diagnostic scanner tests"},
            {"path": "tests/test_lint_tool.py", "reason": "Lint runner tests"},
        ],
        "recommended_specialists": ["test-guardian"],
    },
    "ARCHITECTURE": {
        "canonical_modules": [
            {"path": "docs/architecture/INDEX.md", "reason": "Authoritative index of system architecture and epistemic hierarchy"},
            {"path": "docs/architecture/PROJECT_MAP.md", "reason": "Comprehensive directory and canonical module ownership map"},
            {"path": "JARVIS_2.0_MASTER_ARCHITECTURAL_BLUEPRINT.txt", "reason": "Master blueprint of system contracts and targets"},
        ],
        "applicable_rules": [
            {"path": "AGENTS.md", "reason": "Section 2: Architecture Orientation and Sources of Truth"}
        ],
        "applicable_adrs": [],
        "relevant_lessons": [],
        "mandatory_invariants": [
            "Epistemic authority: Live source > Verified test results > Accepted ADRs > Master Blueprint > Dynamic State.",
            "Cross-subsystem architectural changes require systems-architect deliberation.",
        ],
        "verification_suite": [
            {"path": "tests/test_diagnostics.py", "reason": "Structural invariant scanning"}
        ],
        "recommended_specialists": ["systems-architect"],
    },
    "DOCUMENTATION": {
        "canonical_modules": [
            {"path": "docs/agent/PROJECT_STATE.md", "reason": "Dynamic self-validating state snapshot"},
            {"path": "docs/agent/SESSION_LOG.md", "reason": "Append-only chronological session history"},
            {"path": "docs/agent/LESSONS.md", "reason": "Evidence-backed post-mortem registry"},
            {"path": "ROADMAP.md", "reason": "Milestone deliverables and roadmap"},
        ],
        "applicable_rules": [
            {"path": ".agents/skills/agent-continuity/SKILL.md", "reason": "Continuity lifecycle protocol and handoff standards"}
        ],
        "applicable_adrs": [],
        "relevant_lessons": [],
        "mandatory_invariants": [
            "Maintain documentation integrity: preserve unrelated comments and docstrings.",
            "Session history in SESSION_LOG.md is immutable and append-only.",
            "Entries in LESSONS.md require verified empirical reproduction.",
        ],
        "verification_suite": [],
        "recommended_specialists": [],
    },
    "UNKNOWN": {
        "canonical_modules": [
            {"path": "docs/architecture/PROJECT_MAP.md", "reason": "Fallback comprehensive module ownership map for unclassified or ambiguous requests"},
            {"path": "docs/architecture/INDEX.md", "reason": "Canonical architecture index for exploratory orientation"},
        ],
        "applicable_rules": [
            {"path": "AGENTS.md", "reason": "General engineering directives and safety constraints"}
        ],
        "applicable_adrs": [],
        "relevant_lessons": [],
        "mandatory_invariants": [
            "Ambiguous tasks must not be assumed low-risk; proceed with fail-safe High Reasoning.",
            "Inspect call sites and dependencies before mutating unfamiliar subsystems.",
        ],
        "verification_suite": [],
        "recommended_specialists": ["systems-architect"],
    },
}

# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------

@dataclass
class ClassificationResult:
    primary_category: str
    secondary_categories: list[str]
    matched_categories: list[str]
    confidence: str  # HIGH, MEDIUM, LOW
    risk_tier: str   # CRITICAL, HIGH, MEDIUM, LOW
    scores: dict[str, int]
    input_completeness: str  # COMPLETE, TRUNCATED, EMPTY, MALFORMED
    uncertainties: list[str] = field(default_factory=list)
    prompt: str = ""


# -----------------------------------------------------------------------------
# Core Router Engine
# -----------------------------------------------------------------------------

def classify_task(prompt: str | None, input_completeness: str = "COMPLETE") -> ClassificationResult:
    """Classifies a task prompt into one or more categories using deterministic regex matching.

    Primary intent is determined purely by match score (evidence density).
    Risk tier is computed separately as the maximum risk among all matched categories.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        completeness = "EMPTY" if isinstance(prompt, str) else "MALFORMED"
        return ClassificationResult(
            primary_category="UNKNOWN",
            secondary_categories=[],
            matched_categories=["UNKNOWN"],
            confidence="LOW",
            risk_tier="HIGH",  # Fail-safe: empty or malformed inputs escalate to HIGH risk
            scores={"UNKNOWN": 1},
            input_completeness=completeness,
            uncertainties=["Input prompt was missing, empty, or non-string; defaulted to UNKNOWN fallback."],
            prompt="",
        )

    clean_prompt = prompt.strip()
    scores: dict[str, int] = {}
    uncertainties: list[str] = []

    if input_completeness == "TRUNCATED":
        uncertainties.append("Input text was truncated or incomplete; classification was performed conservatively.")

    for category, patterns in CATEGORY_PATTERNS.items():
        score = 0
        for pattern in patterns:
            matches = pattern.findall(clean_prompt)
            if matches:
                # Award 1 point per distinct matching pattern, plus bonus for frequency
                score += len(matches)
        if score > 0:
            scores[category] = score

    if not scores:
        return ClassificationResult(
            primary_category="UNKNOWN",
            secondary_categories=[],
            matched_categories=["UNKNOWN"],
            confidence="LOW",
            risk_tier="HIGH",  # Fail-safe: unknown requests escalate to HIGH risk
            scores={"UNKNOWN": 1},
            input_completeness=input_completeness,
            uncertainties=["No recognized domain keywords matched; defaulted to UNKNOWN fallback."],
            prompt=clean_prompt,
        )

    # Sort categories by score descending
    sorted_cats = sorted(scores.keys(), key=lambda c: scores[c], reverse=True)
    primary_category = sorted_cats[0]
    secondary_categories = sorted_cats[1:]
    matched_categories = sorted_cats

    # Determine confidence based purely on match evidence (not risk)
    top_score = scores[primary_category]
    if len(sorted_cats) > 1 and scores[sorted_cats[0]] == scores[sorted_cats[1]]:
        confidence = "LOW"
        uncertainties.append(f"Ambiguous match: top categories '{sorted_cats[0]}' and '{sorted_cats[1]}' tied with score {top_score}.")
    elif top_score >= 3:
        confidence = "HIGH"
    elif top_score >= 2:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    # Compute overall risk tier separately as max risk of any matched category
    max_risk_val = max(RISK_TIERS[CATEGORY_RISK_MAP.get(c, "HIGH")] for c in matched_categories)
    risk_tier = next(name for name, val in RISK_TIERS.items() if val == max_risk_val)

    return ClassificationResult(
        primary_category=primary_category,
        secondary_categories=secondary_categories,
        matched_categories=matched_categories,
        confidence=confidence,
        risk_tier=risk_tier,
        scores=scores,
        input_completeness=input_completeness,
        uncertainties=uncertainties,
        prompt=clean_prompt,
    )


# -----------------------------------------------------------------------------
# Dynamic Engineering Lesson Registry & Safe Retrieval Engine
# -----------------------------------------------------------------------------

# Discriminating technical signal patterns with scoring weights
KNOWN_SIGNAL_PATTERNS: list[tuple[str, str, int]] = [
    # SQLite
    (r"\bsinglewriteractor\b", "singlewriteractor", 15),
    (r"\bdeadlocks?\b", "deadlock", 12),
    (r"\bsqlite[-_ ]vec\b", "sqlite-vec", 15),
    (r"\bknn\b", "knn", 12),
    (r"\bfts5\b", "fts5", 15),
    (r"\bbm25\b", "bm25", 12),
    (r"\bread[-_ ]pool\b|\breader[-_ ]pool\b", "read-pool", 15),
    (r"\bstarvation\b", "starvation", 12),
    (r"\bincremental[-_ ]vacuum\b", "incremental-vacuum", 15),
    (r"\bshadow[-_ ]tables?\b", "shadow-table", 12),
    # Process Containment
    (r"\bcreate_suspended\b", "create_suspended", 15),
    (r"\bkill[-_ ]on[-_ ]job[-_ ]close\b|\bkill[-_ ]on[-_ ]close\b", "kill-on-close", 15),
    (r"\bjob[-_ ]object\b", "job-object", 12),
    (r"\borphan[-_ ](process|processes|child|children)\b", "orphan-process", 12),
    # Security
    (r"\bturntainttracker\b", "turntainttracker", 15),
    (r"\bdualkeyapprovalmanager\b", "dualkeyapprovalmanager", 15),
    (r"\bwebhooks?\b", "webhook", 15),
    (r"\btaint(ed|ing)?\b", "taint", 12),
    # API Compatibility
    (r"\bskillregistry\b", "skillregistry", 15),
    (r"\bbreaking[-_ ]change\b", "breaking-change", 12),
    (r"\bcaller[-_ ]shims?\b|\bshims?\b", "shim", 10),
    (r"\bre[-_ ]exports?\b", "re-export", 10),
    (r"\bdecorator[-_ ]signature\b|\btool[-_ ]registration\b", "decorator", 12),
    # Audio Threading
    (r"\block[-_ ]free\b", "lock-free", 15),
    (r"\bmutex(es)?\b", "mutex", 12),
    (r"\bspsc\b", "spsc", 15),
    (r"\bring[-_ ]buffers?\b", "ring-buffer", 12),
    (r"\baudio[-_ ]dropouts?\b", "audio-dropout", 12),
    # Host / Sandbox Network Testing
    (r"\bephemeral[-_ ]ports?\b", "ephemeral-port", 15),
    (r"\b(windows[-_ ]defender|firewall)\b", "firewall", 12),
    (r"\bloopback\b", "loopback", 8),
    (r"\b127\.0\.0\.1\b", "127.0.0.1", 10),
    (r"\bwinerror\s*10013\b|\b10013\b", "winerror-10013", 15),
    (r"\bmock[-_ ]transports?\b|\bsockets?\s+.*mock\b|\bmock\s+.*sockets?\b", "mock-transport", 12),
    (r"\bdropped[-_ ](udp|packets?|datagrams?)\b", "dropped-packet", 12),
    (r"\bsocket[-_ ]timeout\b|\bsocket[-_ ]drop\b", "socket-timeout", 12),
    (r"\bsockets?\b", "socket", 8),
    (r"\bnetwork\b", "network", 6),
]


def extract_lesson_signals(lesson_text: str) -> list[tuple[re.Pattern[str], str, int]]:
    """Extracts compiled discriminating signals from lesson text."""
    signals: list[tuple[re.Pattern[str], str, int]] = []
    for pat_str, name, weight in KNOWN_SIGNAL_PATTERNS:
        compiled = re.compile(pat_str, re.IGNORECASE)
        if compiled.search(lesson_text):
            signals.append((compiled, name, weight))
    return signals


@dataclass
class LessonEntry:
    id: str
    title: str
    subsystem_domain: str
    date_recorded: str
    status: str = "ACTIVE_LEGACY"  # ACTIVE, ACTIVE_LEGACY, DEPRECATED, SUPERSEDED, UNSUPPORTED
    observed_problem: str = ""
    reproduction_evidence: str = ""
    root_cause: str = ""
    prevention_rules: str = ""
    supporting_files: str = ""
    confidence: str = "MEDIUM"
    applicability: str = ""
    applicable_categories: list[str] = field(default_factory=list)
    required_environments: list[str] = field(default_factory=list)
    excluded_environments: list[str] = field(default_factory=list)
    required_scopes: list[str] = field(default_factory=list)
    excluded_scopes: list[str] = field(default_factory=list)
    lesson_signals: list[tuple[re.Pattern[str], str, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.lesson_signals:
            combined = f"{self.title}\n{self.subsystem_domain}\n{self.observed_problem}\n{self.root_cause}\n{self.prevention_rules}"
            self.lesson_signals = extract_lesson_signals(combined)


# In-process stat cache to guarantee microsecond-fast repeated lookups
_REGISTRY_CACHE: dict[str, tuple[int, int, list[LessonEntry]]] = {}


def _normalize_field_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", name.lower())
    if "lessonid" in cleaned:
        return "lesson_id"
    if "subsystem" in cleaned:
        return "subsystem_domain"
    if "date" in cleaned:
        return "date_recorded"
    if "status" in cleaned:
        return "status"
    if "problem" in cleaned:
        return "observed_problem"
    if "evidence" in cleaned or "reproduction" in cleaned:
        return "reproduction_evidence"
    if "rootcause" in cleaned or "mechanism" in cleaned:
        return "root_cause"
    if "prevention" in cleaned or "rule" in cleaned:
        return "prevention_rules"
    if "supporting" in cleaned or "file" in cleaned or "command" in cleaned:
        return "supporting_files"
    if "confidence" in cleaned or "applicability" in cleaned or "boundary" in cleaned:
        return "applicability_boundaries"
    return cleaned


def map_lesson_to_categories(subsystem_domain: str, title: str, prevention_rules: str = "") -> list[str]:
    """Maps a lesson's metadata to router domain categories deterministically."""
    combined_text = f"{subsystem_domain} {title}"
    categories: list[str] = []

    for cat, patterns in CATEGORY_PATTERNS.items():
        if cat == "UNKNOWN":
            continue
        for pat in patterns:
            if pat.search(combined_text):
                if cat not in categories:
                    categories.append(cat)
                break

    domain_lower = subsystem_domain.lower()
    if re.search(r"\b(test|testing)\b", domain_lower) and "TESTING_VERIFICATION" not in categories:
        categories.append("TESTING_VERIFICATION")
    if re.search(r"\b(security|boundary|ipc)\b", domain_lower) and "SECURITY_BOUNDARY" not in categories:
        categories.append("SECURITY_BOUNDARY")
    if re.search(r"\b(audio|wasapi|tts)\b", domain_lower) and "AUDIO_THREADING" not in categories:
        categories.append("AUDIO_THREADING")
    if re.search(r"\b(sqlite|persistence|memory)\b", domain_lower) and "SQLITE_PERSISTENCE" not in categories:
        categories.append("SQLITE_PERSISTENCE")
    if re.search(r"\b(uia|ui automation|accessibility)\b", domain_lower) and "WINDOWS_UI_AUTOMATION" not in categories:
        categories.append("WINDOWS_UI_AUTOMATION")
    if re.search(r"\b(job object|containment|process isolation)\b", domain_lower) and "WINDOWS_PROCESS_CONTAINMENT" not in categories:
        categories.append("WINDOWS_PROCESS_CONTAINMENT")
    if (re.search(r"\b(compatibility|public api)\b", domain_lower) or (re.search(r"\bapi\b", domain_lower) and "wasapi" not in domain_lower)) and "API_COMPATIBILITY" not in categories:
        categories.append("API_COMPATIBILITY")
    if re.search(r"\b(ui|frontend|server)\b", domain_lower) and "UI_FRONTEND" not in categories:
        categories.append("UI_FRONTEND")
    if (domain_lower.startswith("architecture") or domain_lower == "architecture" or "system architecture" in domain_lower) and "ARCHITECTURE" not in categories:
        categories.append("ARCHITECTURE")
    if re.search(r"\b(doc|documentation|session)\b", domain_lower) and "DOCUMENTATION" not in categories:
        categories.append("DOCUMENTATION")

    return categories


VALID_LIFECYCLE_STATUSES: dict[str, str] = {
    "ACTIVE": "ACTIVE",
    "ACTIVE_LEGACY": "ACTIVE_LEGACY",
    "LEGACY": "ACTIVE_LEGACY",
    "DEPRECATED": "DEPRECATED",
    "SUPERSEDED": "SUPERSEDED",
    "UNSUPPORTED": "UNSUPPORTED",
}


def parse_lesson_registry(
    registry_path: Path | str | None = None,
    max_bytes: int = MAX_REGISTRY_BYTES,
    allow_external_fixtures: bool = False,
) -> list[LessonEntry]:
    """Safely parses permanent engineering lessons from docs/agent/LESSONS.md.

    Enforces:
    - Zero subprocesses, zero network, read-only filesystem access.
    - 128KB file size limit to prevent memory exhaustion.
    - Strict path boundary confinement: production callers default to WORKSPACE_ROOT confinement.
    - Path traversal ('..') and external paths are rejected unless allow_external_fixtures=True.
    - Symlinks and Windows reparse points resolving outside permitted boundaries are rejected.
    - Stat-based in-process mtime caching.
    - Deduplication by lesson ID at header parse time.
    - Machine-readable status, environment, and scope parsing with backward-compatible defaults.
    - Strict fail-closed parsing of lifecycle statuses.

    Security & Confinement Notes:
    - Production callers (e.g. hook context router) always omit registry_path, resolving to
      WORKSPACE_ROOT / docs/agent/LESSONS.md.
    - TOCTOU Limitation: In-process Python stat() followed by read_text() has an inherent filesystem
      window where underlying files could theoretically change. Mitigated by in-process mtime caching
      and read-only access.
    """
    if registry_path is None:
        target_path = (WORKSPACE_ROOT / LESSONS_REGISTRY_PATH).resolve()
    else:
        raw_path = Path(registry_path)
        if not raw_path.is_absolute():
            target_path = WORKSPACE_ROOT / raw_path
        else:
            target_path = raw_path

    try:
        resolved_target = target_path.resolve(strict=False)
        resolved_root = WORKSPACE_ROOT.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        try:
            sys.stderr.write(f"[ContextRouter] Path resolution failed for '{registry_path}': {e}\n")
            sys.stderr.flush()
        except (OSError, UnicodeEncodeError):
            pass
        return []

    if not allow_external_fixtures:
        try:
            resolved_target.relative_to(resolved_root)
        except ValueError:
            try:
                sys.stderr.write(
                    f"[ContextRouter] Access denied: registry path '{registry_path}' resolves outside workspace root ({resolved_target})\n"
                )
                sys.stderr.flush()
            except (OSError, UnicodeEncodeError):
                pass
            return []

    if not resolved_target.exists() or not resolved_target.is_file():
        try:
            sys.stderr.write(f"[ContextRouter] Lessons registry not found at {resolved_target}; skipping dynamic retrieval\n")
            sys.stderr.flush()
        except (OSError, UnicodeEncodeError):
            pass
        return []

    try:
        st = resolved_target.stat()
        file_size = st.st_size
        mtime_ns = st.st_mtime_ns

        if file_size > max_bytes:
            sys.stderr.write(
                f"[ContextRouter] Lessons registry exceeds {max_bytes} bytes limit ({file_size} bytes); skipping dynamic retrieval\n"
            )
            sys.stderr.flush()
            return []

        cache_key = str(resolved_target)
        cached = _REGISTRY_CACHE.get(cache_key)
        if cached and cached[0] == mtime_ns and cached[1] == file_size:
            return cached[2]

        raw_content = resolved_target.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError) as e:
        try:
            sys.stderr.write(f"[ContextRouter] Error reading lessons registry at {resolved_target}: {e}\n")
            sys.stderr.flush()
        except (OSError, UnicodeEncodeError):
            pass
        return []

    if not raw_content.strip():
        return []

    lesson_header_pattern = re.compile(r"^##\s+(LES-[A-Za-z0-9_-]+)\s*:\s*(.+)$", re.MULTILINE)
    matches = list(lesson_header_pattern.finditer(raw_content))
    if not matches:
        return []

    field_regex = re.compile(r"-\s+\*\*([^*]+):\*\*\s*([\s\S]*?)(?=(?:\n-\s+\*\*|\Z))")
    lessons: list[LessonEntry] = []
    seen_ids: set[str] = set()

    for idx, match in enumerate(matches):
        lid = match.group(1).strip()
        title = match.group(2).strip()

        if lid in seen_ids:
            try:
                sys.stderr.write(f"[ContextRouter] Duplicate lesson ID '{lid}' encountered; skipping duplicate\n")
                sys.stderr.flush()
            except (OSError, UnicodeEncodeError):
                pass
            continue

        seen_ids.add(lid)

        start_pos = match.end()
        end_pos = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw_content)
        body = raw_content[start_pos:end_pos]

        field_matches = field_regex.findall(body)
        raw_fields_grouped: dict[str, list[str]] = {}
        for raw_k, raw_v in field_matches:
            norm_k = _normalize_field_name(raw_k)
            raw_fields_grouped.setdefault(norm_k, []).append(raw_v.strip())

        fields = {k: v[0] for k, v in raw_fields_grouped.items()}

        subsystem_domain = fields.get("subsystem_domain", "")
        prevention_rules = fields.get("prevention_rules", "")

        # Mandatory fields validation
        if not subsystem_domain or not prevention_rules:
            try:
                sys.stderr.write(f"[ContextRouter] Lesson '{lid}' missing mandatory fields; skipping malformed entry\n")
                sys.stderr.flush()
            except (OSError, UnicodeEncodeError):
                pass
            continue

        # Status parsing with fail-closed validation and backward compatibility
        status_decls = raw_fields_grouped.get("status")
        if status_decls is None:
            # Missing status field: backward compatibility policy
            if "[DEPRECATED]" in title or "[DEPRECATED]" in prevention_rules:
                status = "DEPRECATED"
            elif "[SUPERSEDED]" in title or "[SUPERSEDED]" in prevention_rules:
                status = "SUPERSEDED"
            else:
                status = "ACTIVE_LEGACY"
        else:
            cleaned_tokens: list[str] = []
            for raw_status in status_decls:
                s = raw_status.strip()
                if s.startswith("`") and s.endswith("`") and len(s) >= 2:
                    s = s[1:-1].strip()
                cleaned_tokens.append(re.sub(r"\s+", " ", s).upper())

            unique_tokens = set(cleaned_tokens)
            if len(unique_tokens) > 1:
                try:
                    sys.stderr.write(
                        f"[ContextRouter] Lesson '{lid}' has conflicting status declarations {unique_tokens}; set to UNSUPPORTED\n"
                    )
                    sys.stderr.flush()
                except (OSError, UnicodeEncodeError):
                    pass
                status = "UNSUPPORTED"
            else:
                token = cleaned_tokens[0]
                if token in VALID_LIFECYCLE_STATUSES:
                    status = VALID_LIFECYCLE_STATUSES[token]
                else:
                    try:
                        sys.stderr.write(
                            f"[ContextRouter] Lesson '{lid}' has unknown/invalid status '{token}'; set to UNSUPPORTED\n"
                        )
                        sys.stderr.flush()
                    except (OSError, UnicodeEncodeError):
                        pass
                    status = "UNSUPPORTED"

        # Extract confidence and applicability
        boundaries_text = fields.get("applicability_boundaries", "")
        conf_match = re.search(r"-\s+\*Confidence:\*\s*(.+)", boundaries_text)
        confidence = conf_match.group(1).strip() if conf_match else "MEDIUM"
        app_match = re.search(r"-\s+\*Applicability:\*\s*(.+)", boundaries_text)
        applicability = app_match.group(1).strip() if app_match else boundaries_text

        # Required / Excluded Environment
        req_env: list[str] = []
        req_env_match = re.search(r"-\s+\*Required Environment:\*\s*(.+)", boundaries_text)
        if req_env_match:
            req_env = [e.strip().upper() for e in req_env_match.group(1).split(",")]
        elif re.search(r"\b(inside|under)\s+.*sandbox\b", applicability, re.IGNORECASE):
            req_env = ["SANDBOX"]

        ex_env: list[str] = []
        ex_env_match = re.search(r"-\s+\*Excluded Environment:\*\s*(.+)", boundaries_text)
        if ex_env_match:
            ex_env = [e.strip().upper() for e in ex_env_match.group(1).split(",")]
        elif "outside the sandbox with unrestricted network access" in applicability.lower() or "outside the sandbox" in applicability.lower():
            ex_env = ["HOST", "UNRESTRICTED NETWORK"]

        # Required / Excluded Scope
        req_scope: list[str] = []
        req_scope_match = re.search(r"-\s+\*Required Scope:\*\s*(.+)", boundaries_text)
        if req_scope_match:
            req_scope = [s.strip().upper() for s in req_scope_match.group(1).split(",")]
        elif "applies to all tests in `tests/`" in applicability.lower() or re.search(r"\btests?\b", applicability, re.IGNORECASE):
            req_scope = ["TEST"]

        ex_scope: list[str] = []
        ex_scope_match = re.search(r"-\s+\*Excluded Scope:\*\s*(.+)", boundaries_text)
        if ex_scope_match:
            ex_scope = [s.strip().upper() for s in ex_scope_match.group(1).split(",")]

        applicable_categories = map_lesson_to_categories(subsystem_domain, title, prevention_rules)

        full_lesson_text = f"{title}\n{subsystem_domain}\n{fields.get('observed_problem', '')}\n{fields.get('root_cause', '')}\n{prevention_rules}"
        signals = extract_lesson_signals(full_lesson_text)

        entry = LessonEntry(
            id=lid,
            title=title,
            subsystem_domain=subsystem_domain,
            date_recorded=fields.get("date_recorded", ""),
            status=status,
            observed_problem=fields.get("observed_problem", ""),
            reproduction_evidence=fields.get("reproduction_evidence", ""),
            root_cause=fields.get("root_cause", ""),
            prevention_rules=prevention_rules,
            supporting_files=fields.get("supporting_files", ""),
            confidence=confidence,
            applicability=applicability,
            applicable_categories=applicable_categories,
            required_environments=req_env,
            excluded_environments=ex_env,
            required_scopes=req_scope,
            excluded_scopes=ex_scope,
            lesson_signals=signals,
        )
        lessons.append(entry)

    _REGISTRY_CACHE[cache_key] = (mtime_ns, file_size, lessons)
    return lessons


def sanitize_inert_text(text: str) -> str:
    """Sanitizes text to prevent Markdown injection, HTML execution, and delimiter attacks."""
    if not text:
        return ""
    # Strip script and style blocks including their contents
    clean = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", "", text, flags=re.IGNORECASE)
    # Strip any remaining HTML tags
    clean = re.sub(r"<[^>]+>", "", clean)
    # Defang Markdown links: [anchor](url) -> [anchor]
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"[\1]", clean)
    # Defang backticks that could break code fences
    clean = clean.replace("```", "'''")
    return clean.strip()


# Evidence classification patterns (symmetric host and sandbox analysis)
HOST_ENVIRONMENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(host\s+machine|dedicated\s+(windows\s+)?host|on\s+host|standalone\s+(windows\s+)?(host\s+)?(execution|test)|bare\s+metal|outside\s+(([a-z0-9_-]+\s+){0,3})(sandbox|container|managed\s+environment)|without\s+(([a-z0-9_-]+\s+){0,3})sandbox|bypass(ing)?\s+(([a-z0-9_-]+\s+){0,3})sandbox|no\s+sandbox|network\s+(restrictions?\s+are\s+not\s+applied|is\s+open|is\s+unrestricted|is\s+permitted)|unrestricted\s+network)\b",
        re.IGNORECASE,
    ),
]

SANDBOX_ENVIRONMENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(inside\s+(([a-z0-9_-]+\s+){0,3})(sandbox|container|appcontainer)|under\s+(([a-z0-9_-]+\s+){0,3})sandbox|in\s+([a-z0-9_-]+\s+)?sandbox|standard\s+sandbox|sandboxed\s+(environment|test|runner|mode)|appcontainer\s+sandbox)\b",
        re.IGNORECASE,
    ),
]

NEGATION_HOST_PATTERN: re.Pattern[str] = re.compile(
    r"\b(not|never|no|don't|do\s+not|avoid|without)\s+([a-z0-9_-]+\s+){0,3}(host|bare\s+metal)\b",
    re.IGNORECASE,
)

NEGATION_SANDBOX_PATTERN: re.Pattern[str] = re.compile(
    r"\b(not|never|don't|do\s+not|avoid)\s+([a-z0-9_-]+\s+){0,3}(in\s+sandbox|inside\s+sandbox|in\s+container|inside\s+container|sandboxed)\b",
    re.IGNORECASE,
)

HYPOTHETICAL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(if\s+(we\s+)?(were|are|run|running|had)|what\s+if|suppose|supposing|hypothetical(ly)?|assuming|assume)\b", re.IGNORECASE),
]

HISTORICAL_TRANSITION_PATTERN: re.Pattern[str] = re.compile(
    r"\b(previously|in\s+previous\s+(session|run|turn)|earlier|historically|past\s+run)\b.*?\b(but\s+now|now|currently|current\s+task|for\s+this\s+run)\b",
    re.IGNORECASE | re.DOTALL,
)

TEST_SCOPE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(tests?|pytest|unit[-_ ]?tests?|integration[-_ ]?tests?|test_[a-z0-9_]+|tests[./\\].*\.py)\b", re.IGNORECASE),
]

PRODUCTION_SCOPE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(live\s+production|production\s+(worker|thread|pipeline|environment|server|service)|in\s+production)\b", re.IGNORECASE),
]


def classify_task_evidence(prompt: str) -> dict[str, str]:
    """Distinguishes USER-DECLARED CONTEXT from VERIFIED RUNTIME CONTEXT.

    - USER-DECLARED CONTEXT: parsed deterministically from explicit prompt statements
      using symmetric independent analysis of host and sandbox indicators.
    - VERIFIED RUNTIME CONTEXT: reported strictly as UNKNOWN as no external probe subprocesses
      or network queries are executed by the router.

    Invariants:
    - Symmetrical evaluation of host vs sandbox indicators.
    - Quoted logs, tracebacks, and error strings are stripped prior to environment matching.
    - Hypothetical and conditional clauses do not constitute affirmative environment statements.
    - Contradictory statements (both host and sandbox present) classify as UNKNOWN.
    - Negated phrases (e.g. 'not on host', 'not in sandbox') do not trigger affirmative matches.
    """
    clean = prompt.strip()
    if not clean:
        return {
            "user_declared_environment": "UNKNOWN",
            "user_declared_scope": "UNKNOWN",
            "verified_runtime_environment": "UNKNOWN",
        }

    # 1. Strip quoted strings (logs, error messages, code blocks)
    unquoted = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', " ", clean)

    # 2. Check for hypothetical framing (e.g. "what if we ran on host", "suppose inside sandbox")
    if any(p.search(unquoted) for p in HYPOTHETICAL_PATTERNS):
        env = "UNKNOWN"
    else:
        # 3. Check for temporal/historical transition: "previously on host, but now in sandbox"
        m_trans = HISTORICAL_TRANSITION_PATTERN.search(unquoted)
        target_text = unquoted[m_trans.start(3):] if m_trans else unquoted

        # 4. Check negation
        neg_host = bool(NEGATION_HOST_PATTERN.search(target_text))
        neg_sandbox = bool(NEGATION_SANDBOX_PATTERN.search(target_text))

        # 5. Affirmative pattern match
        has_host = any(p.search(target_text) for p in HOST_ENVIRONMENT_PATTERNS) and not neg_host
        has_sandbox = any(p.search(target_text) for p in SANDBOX_ENVIRONMENT_PATTERNS) and not neg_sandbox

        if has_host and has_sandbox:
            env = "UNKNOWN"
        elif has_host:
            env = "HOST"
        elif has_sandbox:
            env = "SANDBOX"
        else:
            env = "UNKNOWN"

    # Scope classification
    scope = "UNKNOWN"
    for pat in PRODUCTION_SCOPE_PATTERNS:
        if pat.search(unquoted) or pat.search(clean):
            scope = "PRODUCTION"
            break
    if scope == "UNKNOWN":
        for pat in TEST_SCOPE_PATTERNS:
            if pat.search(unquoted) or pat.search(clean):
                scope = "TEST"
                break

    return {
        "user_declared_environment": env,
        "user_declared_scope": scope,
        "verified_runtime_environment": "UNKNOWN",
    }


def check_technical_collision(prompt_lower: str, lesson: LessonEntry) -> bool:
    """Disqualifies candidate lessons when common terms appear in unrelated technical contexts."""
    # Sockets: network vs CPU socket
    if (
        any(s[1] in ("loopback", "127.0.0.1", "winerror-10013", "socket-timeout", "socket") for s in lesson.lesson_signals)
        and re.search(r"\b(cpu|processor|hardware|lga|am4|am5|motherboard)\s+socket\b", prompt_lower)
        and not re.search(r"\b(network|port|bind|connect|udp|tcp|winerror|ip|listen|traffic|firewall)\b", prompt_lower)
    ):
        return True

    # Loopback: network socket vs WASAPI audio loopback
    if (
        "loopback" in prompt_lower
        and (re.search(r"\b(wasapi|audio|speaker|microphone|record|sound)\s+.*loopback\b", prompt_lower) or re.search(r"\bloopback\s+(capture|recording|endpoint)\b", prompt_lower))
        and not re.search(r"\b(socket|port|bind|connect|udp|tcp|winerror|127\.0\.0\.1|network|ip|firewall)\b", prompt_lower)
        and "audio" not in lesson.subsystem_domain.lower()
    ):
        return True

    # Packets: network datagram vs audio frame packet
    if (
        "packet" in prompt_lower
        and re.search(r"\b(audio\s+frame\s+packet|audio\s+packet|frame\s+packet)\b", prompt_lower)
        and not re.search(r"\b(network|socket|udp|tcp|ip|datagram|telemetry)\b", prompt_lower)
        and "audio" not in lesson.subsystem_domain.lower()
    ):
        return True

    # Job Object sandbox vs network test sandbox
    if "job" in prompt_lower and "object" in prompt_lower:
        if "process" in lesson.subsystem_domain.lower() or "job" in lesson.subsystem_domain.lower():
            return False
        if not re.search(r"\b(socket|udp|tcp|127\.0\.0\.1|loopback|pytest|tests?)\b", prompt_lower):
            return True

    return False


def retrieve_relevant_lessons(
    prompt: str | None,
    matched_categories: list[str],
    registry_path: Path | str | None = None,
    max_lessons: int = MAX_RETRIEVED_LESSONS,
    budget_chars: int = MAX_LESSONS_BUDGET_CHARS,
    allow_external_fixtures: bool = False,
) -> list[dict[str, Any]]:
    """Dynamically retrieves relevant, verified engineering lessons from LESSONS.md.

    Enforces:
    - Lifecycle state filtering (strictly fail-closed: only ACTIVE and ACTIVE_LEGACY permitted).
    - Category overlap requirement.
    - Explicit environment and scope boundary enforcement without hardcoded lesson IDs.
    - Technical collision disambiguation (CPU socket, audio loopback, audio packets).
    - Discriminating problem signal scoring.
    - Deterministic ranking: score descending, then lesson ID ascending.
    - Strict maximum lesson count (max 3).
    - Strict context character/token budget (~2400 chars, ~600 tokens).
    - Inert reference data sanitization (link defanging, code fence protection).
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return []

    if not matched_categories or matched_categories == ["UNKNOWN"]:
        return []

    lessons = parse_lesson_registry(
        registry_path=registry_path,
        allow_external_fixtures=allow_external_fixtures,
    )
    if not lessons:
        return []

    clean_prompt = prompt.strip()
    clean_prompt_lower = clean_prompt.lower()
    evidence = classify_task_evidence(clean_prompt)
    declared_env = evidence["user_declared_environment"]
    declared_scope = evidence["user_declared_scope"]

    candidates: list[tuple[int, LessonEntry, str]] = []

    for lesson in lessons:
        # 1. Lifecycle state check: strictly fail-closed, only ACTIVE and ACTIVE_LEGACY permitted
        if lesson.status not in ("ACTIVE", "ACTIVE_LEGACY"):
            continue

        # 2. Category overlap check
        cat_overlap = set(lesson.applicable_categories) & set(matched_categories)
        if not cat_overlap:
            continue

        # 3. Environment contradiction check
        if declared_env != "UNKNOWN":
            if declared_env in lesson.excluded_environments:
                continue
            if lesson.required_environments and declared_env not in lesson.required_environments:
                continue
        else:
            # If user declared UNKNOWN environment:
            # Lessons that explicitly require HOST cannot match without host evidence
            if "HOST" in lesson.required_environments and declared_env != "HOST":
                continue

        # 4. Scope contradiction check
        if declared_scope != "UNKNOWN":
            if declared_scope in lesson.excluded_scopes:
                continue
            if lesson.required_scopes and declared_scope not in lesson.required_scopes:
                continue

        # 5. Technical collision check
        if check_technical_collision(clean_prompt_lower, lesson):
            continue

        # 6. Relevance scoring
        score = 0
        reasons: list[str] = []

        primary_cat = matched_categories[0] if matched_categories else ""
        if primary_cat in lesson.applicable_categories:
            score += 10
            reasons.append(f"Domain: {primary_cat}")
        elif cat_overlap:
            score += 5
            reasons.append(f"Domain: {next(iter(cat_overlap))}")

        # Check discriminating signals
        matched_signals: list[str] = []
        signal_points = 0
        for compiled_pat, name, weight in lesson.lesson_signals:
            if compiled_pat.search(clean_prompt_lower):
                matched_signals.append(name)
                signal_points += weight

        if not matched_signals:
            continue

        score += signal_points
        reasons.append(f"Signals: {', '.join(matched_signals)}")

        if declared_scope in lesson.required_scopes:
            score += 2
        if declared_env != "UNKNOWN" and declared_env in lesson.required_environments:
            score += 3

        if score >= 18:
            reason_str = "; ".join(reasons)
            candidates.append((score, lesson, reason_str))

    # Deterministic ranking: score descending, then lesson ID ascending
    candidates.sort(key=lambda c: (-c[0], c[1].id))
    top_candidates = candidates[:max_lessons]

    retrieved: list[dict[str, Any]] = []
    total_chars = 0

    for score, lesson, reason in top_candidates:
        sanitized_rule = sanitize_inert_text(lesson.prevention_rules)
        rule_summary = sanitized_rule[:500] if len(sanitized_rule) > 500 else sanitized_rule

        item = {
            "id": lesson.id,
            "title": sanitize_inert_text(lesson.title),
            "subsystem": sanitize_inert_text(lesson.subsystem_domain),
            "relevance_reason": reason,
            "prevention_rules": rule_summary,
            "applicability": sanitize_inert_text(lesson.applicability),
            "provenance": f"{registry_path or 'docs/agent/LESSONS.md'} [INERT HISTORICAL REFERENCE]",
        }

        item_chars = len(json.dumps(item))
        if total_chars + item_chars > budget_chars:
            if not retrieved:
                item["prevention_rules"] = rule_summary[:200] + "... [TRUNCATED BUDGET]"
                retrieved.append(item)
            break

        retrieved.append(item)
        total_chars += item_chars

    return retrieved


def resolve_context(
    classification: ClassificationResult,
    catalog: dict[str, dict[str, Any]] = CONTEXT_CATALOG,
    registry_path: Path | str | None = None,
    allow_external_fixtures: bool = False,
) -> dict[str, Any]:
    """Resolves and merges catalog entries across all matched categories without duplication."""
    canonical_modules: list[dict[str, str]] = []
    applicable_rules: list[dict[str, str]] = []
    applicable_adrs: list[dict[str, str]] = []
    relevant_lessons: list[dict[str, Any]] = []
    mandatory_invariants: list[str] = []
    verification_suite: list[dict[str, str]] = []
    recommended_specialists: list[str] = []

    seen_module_paths = set()
    seen_rule_paths = set()
    seen_adr_paths = set()
    seen_lesson_ids = set()
    seen_invariants = set()
    seen_test_paths = set()
    seen_specialists = set()

    uncertainties = list(classification.uncertainties)

    # 1. Dynamic retrieval of verified engineering lessons from permanent registry
    dynamic_lessons = retrieve_relevant_lessons(
        prompt=classification.prompt,
        matched_categories=classification.matched_categories,
        registry_path=registry_path,
        allow_external_fixtures=allow_external_fixtures,
    )
    for d_lesson in dynamic_lessons:
        lid = d_lesson.get("id")
        if lid and lid not in seen_lesson_ids:
            seen_lesson_ids.add(lid)
            relevant_lessons.append(d_lesson)

    for cat in classification.matched_categories:
        cat_data = catalog.get(cat)
        if not cat_data:
            uncertainties.append(f"Category '{cat}' missing from context catalog.")
            continue

        # Merge canonical modules
        for mod in cat_data.get("canonical_modules", []):
            p = mod.get("path")
            if p and p not in seen_module_paths:
                seen_module_paths.add(p)
                canonical_modules.append(mod)

        # Merge rules
        for rule in cat_data.get("applicable_rules", []):
            p = rule.get("path")
            if p and p not in seen_rule_paths:
                seen_rule_paths.add(p)
                applicable_rules.append(rule)

        # Merge ADRs
        for adr in cat_data.get("applicable_adrs", []):
            p = adr.get("path")
            if p and p not in seen_adr_paths:
                seen_adr_paths.add(p)
                applicable_adrs.append(adr)

        # Merge custom catalog lessons (preserves compatibility with test mock catalogs)
        for lesson in cat_data.get("relevant_lessons", []):
            lid = lesson.get("id")
            if lid and lid not in seen_lesson_ids:
                seen_lesson_ids.add(lid)
                relevant_lessons.append(lesson)

        # Merge invariants
        for inv in cat_data.get("mandatory_invariants", []):
            if inv not in seen_invariants:
                seen_invariants.add(inv)
                mandatory_invariants.append(inv)

        # Merge verification tests
        for test in cat_data.get("verification_suite", []):
            p = test.get("path")
            if p and p not in seen_test_paths:
                seen_test_paths.add(p)
                verification_suite.append(test)

        # Merge specialists
        for spec in cat_data.get("recommended_specialists", []):
            if spec not in seen_specialists:
                seen_specialists.add(spec)
                recommended_specialists.append(spec)

    packet = {
        "schema_version": SCHEMA_VERSION,
        "task_classification": {
            "primary": classification.primary_category,
            "secondary": classification.secondary_categories,
            "confidence": classification.confidence,
            "risk_tier": classification.risk_tier,
            "scores": classification.scores,
        },
        "input_completeness": classification.input_completeness,
        "canonical_modules": canonical_modules,
        "applicable_rules": applicable_rules,
        "applicable_adrs": applicable_adrs,
        "relevant_lessons": relevant_lessons,
        "mandatory_invariants": mandatory_invariants,
        "verification_suite": verification_suite,
        "recommended_specialists": recommended_specialists,
        "uncertainties": uncertainties,
    }
    return packet


def build_context_packet(
    prompt: str | None,
    input_completeness: str = "COMPLETE",
    catalog: dict[str, dict[str, Any]] = CONTEXT_CATALOG,
    registry_path: Path | str | None = None,
    allow_external_fixtures: bool = False,
) -> dict[str, Any]:
    """High-level API: classifies prompt and builds a unified context packet."""
    classification = classify_task(prompt, input_completeness=input_completeness)
    return resolve_context(
        classification,
        catalog=catalog,
        registry_path=registry_path,
        allow_external_fixtures=allow_external_fixtures,
    )


def serialize_hook_response(packet: dict[str, Any]) -> str:
    """Serializes context packet into documented Antigravity PreInvocation output format.

    Uses ephemeralMessage so the packet is dropped after the current turn and does not
    pollute future turns. Does not emit unsupported fields.
    """
    packet_json = json.dumps(packet, indent=2)
    message_content = f"[CONTEXT_PACKET: REFERENCE METADATA ONLY - INERT DATA]\n{packet_json}"

    response = {
        "injectSteps": [
            {
                "ephemeralMessage": message_content
            }
        ]
    }
    return json.dumps(response)


def process_hook_input(raw_json: str) -> str:
    """Parses hook stdin JSON and produces PreInvocation output.

    Safe against malformed input, missing fields, or empty strings.
    """
    if not isinstance(raw_json, str) or not raw_json.strip():
        # Empty input: return safe fallback packet
        packet = build_context_packet(None, input_completeness="EMPTY")
        return serialize_hook_response(packet)

    try:
        payload = json.loads(raw_json)
        if not isinstance(payload, dict):
            packet = build_context_packet(None, input_completeness="MALFORMED")
            return serialize_hook_response(packet)
    except (json.JSONDecodeError, UnicodeDecodeError):
        packet = build_context_packet(None, input_completeness="MALFORMED")
        return serialize_hook_response(packet)

    # Prevent duplicate context injection if PreInvocation fires on subsequent model calls
    # within the same turn (invocationNum > 0). The ephemeralMessage from invocationNum == 0
    # remains present for the duration of the turn.
    invocation_num = payload.get("invocationNum")
    if isinstance(invocation_num, int) and invocation_num > 0:
        try:
            sys.stderr.write(f"[ContextRouter] Invocation #{invocation_num}: duplicate injection suppressed (turn active)\n")
            sys.stderr.flush()
        except (OSError, UnicodeEncodeError):
            pass
        return json.dumps({"injectSteps": []})

    # Extract user input safely
    user_input = payload.get("lastUserInput")
    if user_input is None:
        user_input = payload.get("prompt") or payload.get("last_user_input")

    completeness = "COMPLETE"
    if payload.get("isTruncated") is True or payload.get("truncated") is True:
        completeness = "TRUNCATED"

    packet = build_context_packet(str(user_input) if user_input is not None else None, input_completeness=completeness)

    try:
        category = packet["task_classification"]["primary"]
        risk = packet["task_classification"]["risk_tier"]
        inv_idx = invocation_num if isinstance(invocation_num, int) else 0
        sys.stderr.write(f"[ContextRouter] Invocation #{inv_idx}: classified as {category} (risk: {risk})\n")
        sys.stderr.flush()
    except (OSError, UnicodeEncodeError, KeyError):
        pass

    return serialize_hook_response(packet)


def main() -> int:
    """CLI / hook entry point."""
    try:
        raw_input = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        raw_input = ""

    output = process_hook_input(raw_input)
    sys.stdout.write(output + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
