# Jarvis 2.0 — Project Map & Subsystem Architecture

This document provides a concise, ground-truth reference for developers and agents navigating the Jarvis 2.0 codebase.

---

## 1. Primary Entry Points

| Entry Point | Path | Role |
| :--- | :--- | :--- |
| **Daemon Entry Point** | [`run.py`](../../run.py) | CLI bootstrap, service lifecycle, argument parser (`--stealth`, `--omniroute`, `--check-health`). |
| **Web HUD & Telemetry Server** | [`jarvis/ui/server.py`](../../jarvis/ui/server.py) | FastAPI / Starlette server for the Holographic Web HUD (port 8000) and WebSocket feeds. |
| **Antigravity Sidecar Worker** | [`jarvis/core/antigravity_bridge.py`](../../jarvis/core/antigravity_bridge.py) | Persistent warm `agy.exe` child process in `data/agy_runtime_workspace` using `--input-format stream-json`. |
| **Fast Test Runner** | [`tools/run_fast_tests.py`](../../tools/run_fast_tests.py) | Two-tier test runner (fast parallel pytest suite + isolated serial hardware suite). |
| **Lint & Diagnostics** | [`tools/lint.py`](../../tools/lint.py), [`tools/diagnostics.py`](../../tools/diagnostics.py) | Sub-20ms ruff lint runner and AST structural invariant scanning. |

---

## 2. Directory Layout & Subsystem Ownership

```
Jarvis_2.0/
├── jarvis/
│   ├── audio/                 # Audio I/O, WASAPI playback (tts.py), mic capture (wake_word.py), Kokoro TTS, Whisper STT, SHM transport
│   ├── context/               # Prompt assembly, context bounding, flow state mirroring
│   ├── conversation/          # LIFO ReasoningBookmarkStack for barge-in thought resumption
│   ├── core/                  # Brain, orchestrator, cognitive router, reflex engines, memory, security
│   │   ├── cognition/         # CognitiveRouter: tier classification, thinking budgets, failover, cadence tracker
│   │   ├── evolution/         # Self-evolution AST validation, canary testing, and AppContainer isolation
│   │   ├── memory/            # 4-tier SQLite WAL + sqlite-vec memory subsystem (mixins & SingleWriterActor)
│   │   └── pipeline/          # ToolOrchestrator, streaming tool parser, interim speech, profiler
│   ├── routing/               # ONNX intent router for fast CPU-side neural intent classification
│   ├── skills/                # 36 specialized tool modules registered with SkillRegistry
│   ├── speculative/           # SpeculativeEngine and EnvironmentEpochTracker for TOCTOU gating
│   └── ui/                    # Holographic HUD server, WebSocket broadcaster, desktop overlay
├── data/
│   ├── agy_runtime_workspace/ # Isolated runtime workspace for the persistent agy sidecar
│   └── prompts/               # Curated prompts loaded at runtime by ContextOrchestrator
├── tests/                     # Test suites (unit, integration, mock-isolated, concurrency)
├── tools/                     # Developer tools (test runner, linter, diagnostics, slicer)
└── docs/                      # Architectural maps, ADRs, milestones, runbooks, and design documentation
```

---

## 3. Canonical Subsystems and Module Ownership

The following modules represent the authoritative canonical implementations for critical system boundaries:

| Subsystem Domain | Canonical Module | Primary Responsibilities | Architectural Reference |
| :--- | :--- | :--- | :--- |
| **Security & Tool Boundaries** | [`jarvis/core/security_boundaries.py`](../../jarvis/core/security_boundaries.py) | `DualKeyApprovalManager` (confirmation gates), `validate_confined_path` (path traversal guard), `validate_egress_url` (SSRF/private IP defense), `TurnTaintTracker` (telemetry taint tracking). | [ADR-004](../adr/ADR-004-dual-key-tool-authorization-turn-taint.md), [ADR-006](../adr/ADR-006-egress-validation-ip-pinning.md) |
| **Process Containment & Sandboxing** | [`jarvis/skills/job_object_sandbox.py`](../../jarvis/skills/job_object_sandbox.py) | Kernel-level Win32 Job Objects configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Suspended process creation (`CREATE_SUSPENDED`), fail-closed kill on assignment failure, orphan process tree reaping. | [ADR-002](../adr/ADR-002-win32-job-object-containment.md) |
| **Persistent Storage & Serialization** | [`jarvis/core/memory/sqlite_base.py`](../../jarvis/core/memory/sqlite_base.py) | SQLite WAL mode (`PRAGMA journal_mode=WAL`), `SingleWriterActor` write serialization queue, read-only connection pooling, `sqlite-vec` KNN tables fused with FTS5 BM25. | [ADR-001](../adr/ADR-001-sqlite-single-writer-wal.md) |
| **Preemptive Cancellation** | [`jarvis/core/cancellation.py`](../../jarvis/core/cancellation.py) | Shared mutable epoch counter (`GLOBAL_EPOCH_STATE`) for cooperative worker invalidation during user barge-in interrupts across audio and LLM pipelines. | [ADR-003](../adr/ADR-003-epoch-based-audio-cancellation.md) |
| **Win32 CPU Affinity & MMCSS** | [`jarvis/core/win32_affinity.py`](../../jarvis/core/win32_affinity.py) | Hardware core affinity partitioning (Intel Alder Lake 4P+8E), MMCSS Pro Audio elevation, and EcoQoS power throttling. | [ADR-005](../adr/ADR-005-win32-hardware-core-affinity.md) |
| **Audio Playback & Neural TTS** | [`jarvis/audio/tts.py`](../../jarvis/audio/tts.py) | Real-time WASAPI audio playback loop (Logical CPUs 0–1 via `0x0003`), Kokoro-82M neural style blending on compute P-cores (`0x00FC`), zero-copy SHM transport. | [ADR-003](../adr/ADR-003-epoch-based-audio-cancellation.md), [ADR-005](../adr/ADR-005-win32-hardware-core-affinity.md) |
| **Wake-Word Capture & Audio Routing** | [`jarvis/audio/wake_word.py`](../../jarvis/audio/wake_word.py) | Dedicated `MicRouterWorker` loop, openWakeWord neural inference, physical P-core affinity (`0x0055`) with MMCSS Pro Audio. | [ADR-005](../adr/ADR-005-win32-hardware-core-affinity.md) |
| **Browser & CDP Automation** | [`jarvis/skills/browser_cdp.py`](../../jarvis/skills/browser_cdp.py) | Sub-5ms Chrome DevTools Protocol (CDP) WebSocket automation for Chromium browsers, loopback port confinement, silent tab navigation. | [ADR-006](../adr/ADR-006-egress-validation-ip-pinning.md) |
| **Auxiliary Background Daemons** | [`jarvis/vision/window_hook_daemon.py`](../../jarvis/vision/window_hook_daemon.py)<br>[`jarvis/vision/dhash_monitor.py`](../../jarvis/vision/dhash_monitor.py)<br>[`jarvis/core/memory/sleep_consolidator.py`](../../jarvis/core/memory/sleep_consolidator.py)<br>[`jarvis/core/cognition/cadence_tracker.py`](../../jarvis/core/cognition/cadence_tracker.py) | Background maintenance workers executing on Efficiency Cores (`MASK_ECORE_BACKGROUND = 0xFF00`) with Windows EcoQoS enabled: window event hooks, perceptual hashing, memory sleep consolidation, cadence tracking. | [ADR-005](../adr/ADR-005-win32-hardware-core-affinity.md) |
| **UI HUD & Telemetry Server** | [`jarvis/ui/server.py`](../../jarvis/ui/server.py) | FastAPI / Starlette asynchronous server for the Holographic HUD (port 8000), real-time WebSocket feeds, system health telemetry. | [ADR-004](../adr/ADR-004-dual-key-tool-authorization-turn-taint.md) |
| **Developer & Verification Tooling** | [`tools/run_fast_tests.py`](../../tools/run_fast_tests.py)<br>[`tools/lint.py`](../../tools/lint.py)<br>[`tools/diagnostics.py`](../../tools/diagnostics.py)<br>[`tools/agy_context_slicer.py`](../../tools/agy_context_slicer.py) | Test execution runners, static linting, AST structural invariant checks, and token-bounded context recovery utilities. | [INDEX.md](INDEX.md) |

---

## 4. Core Call Paths

### A. Full-Duplex Conversational Voice Turn
```
[User Speech]
       │
       ▼
1. Mic Stream Capture & Wake Word (`jarvis/audio/wake_word.py`)
       │ Physical P-Core Affinity (0x0055) & MMCSS Pro Audio (`jarvis/core/win32_affinity.py`)
       ▼
2. Speech-to-Text (`jarvis/audio/stt.py`)
       │ faster-whisper CUDA INT8 + VRAM Arbiter
       ▼
3. Prompt Assembly (`jarvis/context/context_orchestrator.py`)
       │ SOVEREIGN_IDENTITY + BASE_SYSTEM_PROMPT + <1.5KB JIT Memory Envelope
       ▼
4. Cognitive Routing (`jarvis/core/cognition/cognitive_router.py`)
       │ Tier 0: Regex/Habit Reflex (<15ms)
       │ Tier 1: Local ONNX Intent Router (CPUExecutionProvider)
       │ Tier 2/3: AntigravityBridge (Gemini 3.8 / Claude Sonnet) or Gemini Cloud Fallback
       ▼
5. Incremental Stream Parsing (`jarvis/core/streaming_tool_parser.py`)
       │ Yields text tokens -> ProsodicChunker (`jarvis/audio/prosodic_chunker.py`)
       │ Yields tool call chunks -> ToolOrchestrator (`jarvis/core/pipeline/tool_orchestrator.py`)
       ▼
6. Speech Synthesis & Playback
       │ Kokoro-82M ONNX latent style blend on Compute P-Cores (0x00FC) (`jarvis/audio/tts.py`)
       │ Zero-copy shared memory SPSC transport (`jarvis/audio/shm_transport.py`)
       │ WASAPI playback loop pinned to Logical CPUs 0–1 (0x0003) with MMCSS Pro Audio
       ▼
[WASAPI Speaker Output]
```

### B. Tool Execution & Security Pipeline
```
Streaming Tool Call
       │
       ▼
1. ToolOrchestrator.execute_step (`jarvis/core/pipeline/tool_orchestrator.py`)
       │ Strips injected authorization tokens to prevent prompt forgery
       │ Cycle detector: flags repetitive identical calls
       ▼
2. Security & Boundaries (`jarvis/core/security_boundaries.py`)
       │ DualKeyApprovalManager: requires explicit user confirmation for mutating operations (ADR-004)
       │ validate_confined_path: enforces filesystem boundary confinement
       │ validate_egress_url: blocks private IP / SSRF targets with IP pinning (ADR-006)
       │ TurnTaintTracker: prevents tainted ambient telemetry from driving privileged actions
       ▼
3. Sandboxed Execution & Tool Actuation
       │ Sandboxed commands: Win32 Job Object containment (`jarvis/skills/job_object_sandbox.py`, ADR-002)
       │ Self-evolution AST gate: dynamic validation sandbox (`jarvis/core/evolution/sandbox.py`)
       │ In-process tools: SkillRegistry dispatch in `jarvis/skills/<module>.py`
       ▼
4. Output Feedback to Brain Context
```

### C. Persistent Memory Subsystem (`jarvis/core/memory/`)
The `MemoryManager` class combines focused mixins into a single cohesive facade:
- **`SqliteBaseMixin`** ([`sqlite_base.py`](../../jarvis/core/memory/sqlite_base.py)): Manages WAL mode, 256MB `mmap_size`, read-only connection pooling, and serialized writes via `SingleWriterActor` ([ADR-001](../adr/ADR-001-sqlite-single-writer-wal.md)).
- **`TemporalGraphMixin`** ([`temporal_graph.py`](../../jarvis/core/memory/temporal_graph.py)): Bi-temporal knowledge graph edges and Markov habit transitions.
- **`FactsManagerMixin`** ([`facts_manager.py`](../../jarvis/core/memory/facts_manager.py)): Atomic facts and core user profile preferences (Tier 1 & Tier 2).
- **`TasksCalendarMixin`** ([`tasks_calendar.py`](../../jarvis/core/memory/tasks_calendar.py)): Tasks, deadlines, calendar events.
- **`DocumentVaultMixin`** ([`document_vault.py`](../../jarvis/core/memory/document_vault.py)): JIT document vaults, chunk sandwiching, attention-boundary formatting (Tier 3).
- **`InteractionMemoryMixin`** ([`interaction_memory.py`](../../jarvis/core/memory/interaction_memory.py)): Conversation turn logs and episodic failure lessons (Tier 4).
- **`VectorMemoryEngine`** ([`vector_engine.py`](../../jarvis/core/memory/vector_engine.py)): Native `sqlite-vec` KNN virtual tables fused with FTS5 BM25 via Reciprocal Rank Fusion (RRF).

---

## 5. Canonical Modules vs. Compatibility Shims

When authoring new features or refactoring, always import from the canonical module:

| Domain | Canonical Module | Compatibility Shims | Purpose |
| :--- | :--- | :--- | :--- |
| **Win32 Affinity & Scheduling** | `jarvis.core.win32_affinity` | `jarvis.audio.affinity`<br>`jarvis.core.thread_affinity` | MMCSS Pro Audio, P-Core (`0x0003`, `0x0055`, `0x00FC`) / E-Core (`0xFF00`) bitmasks |
| **Preemptive Cancellation** | `jarvis.core.cancellation` | `jarvis.audio.cancellation` | Shared mutable `GLOBAL_EPOCH_STATE` for cooperative worker invalidation |
| **Streaming Tool Parser** | `jarvis.core.streaming_tool_parser` | `jarvis.core.stream_tool_parser` | Incremental streaming JSON tool tokenizer |
| **Prosodic Clause Chunker** | `jarvis.audio.prosodic_chunker` | `jarvis.audio.chunker` | Natural punctuation pause chunking for Kokoro TTS |
| **MCP Process Manager** | `jarvis.core.mcp_manager` | `jarvis.skills.mcp_manager` | Process lifecycle manager for external Model Context Protocol (MCP) servers |
| **Win32 Process Containment** | `jarvis.skills.job_object_sandbox` | — | Canonical Win32 Job Object kernel containment, suspended process spawn, orphan reaping |
| **Self-Evolution AST Gate** | `jarvis.core.evolution.sandbox` | — | AST loop detection, canary watchdog, and snapshot rollback for dynamically generated code |

---

## 6. Test Infrastructure & Verification

Tests live in the [`tests/`](../../tests/) directory and are managed by [`tools/run_fast_tests.py`](../../tools/run_fast_tests.py):

| Test Mode | Command | Scope |
| :--- | :--- | :--- |
| **Targeted Test** | `python tools/run_fast_tests.py tests/test_<name>.py` | Runs a single test file directly with pytest. |
| **Fast Parallel Suite** | `python tools/run_fast_tests.py --fast -w 4` | Parallel execution of all non-hardware suites via `pytest-xdist`. |
| **Hardware Serial Suite** | `python tools/run_fast_tests.py --hardware` | Isolated serial execution of audio, ONNX GPU, and COM suites. |
| **Full Gate** | `python tools/run_fast_tests.py --all` | Runs fast parallel suite followed by serial hardware suite. |
| **Failed Tests Only** | `python tools/run_fast_tests.py --failed` | Re-executes only tests that failed in the last run (`--lf`). |

---

## 7. Context Architecture Navigation & Cross-References

- [INDEX.md](INDEX.md): Primary architectural index and context engineering ground truth.
- [docs/adr/](../adr/README.md): Architecture Decision Records (`ADR-001` through `ADR-006`).
- [docs/milestones/MILESTONES.md](../milestones/MILESTONES.md): Verified milestone deliverables and historical evidence register.
- [AGENTS.md](../../AGENTS.md): Developer-agent operational directives and binding invariants.
- [ROADMAP.md](../../ROADMAP.md): Future architectural milestones and tracked deliverables.
- [JARVIS_2.0_MASTER_ARCHITECTURAL_BLUEPRINT.txt](../../JARVIS_2.0_MASTER_ARCHITECTURAL_BLUEPRINT.txt): Deep technical specification and 8-pillar blueprint.
- [handoff.md](../../handoff.md): Historical milestone handoff pointer.
- [`.agents/skills/`](../../.agents/skills/): Specialized engineering skills (`systematic-debugging`, `test-driven-development`, `python-testing-patterns`, `python-profiling`, `council`, etc.).
