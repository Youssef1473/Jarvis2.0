# Jarvis 2.0 — Project Map & Subsystem Architecture

This document provides a concise, ground-truth reference for developers and agents navigating the Jarvis 2.0 codebase.

---

## 1. Primary Entry Points

| Entry Point | Path | Role |
| :--- | :--- | :--- |
| **Daemon Entry Point** | [`run.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/run.py) | CLI bootstrap, service lifecycle, argument parser (`--stealth`, `--omniroute`, `--check-health`). |
| **Web HUD & Telemetry Server** | [`jarvis/ui/server.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/ui/server.py) | FastAPI / Starlette server for the Holographic Web HUD (port 8000) and WebSocket feeds. |
| **Antigravity Sidecar Worker** | [`jarvis/core/antigravity_bridge.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/antigravity_bridge.py) | Persistent warm `agy.exe` child process in `data/agy_runtime_workspace` using `--input-format stream-json`. |
| **Fast Test Runner** | [`tools/run_fast_tests.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/tools/run_fast_tests.py) | Two-tier test runner (fast parallel pytest suite + isolated serial hardware suite). |

---

## 2. Directory Layout & Subsystem Ownership

```
Jarvis_2.0/
├── jarvis/
│   ├── audio/                 # Audio I/O, WASAPI playback (`tts.py`), mic capture (`wake_word.py`), DSP, Kokoro TTS, Whisper STT, SHM transport
│   ├── context/               # Prompt assembly, context bounding, flow state mirroring
│   ├── conversation/          # LIFO ReasoningBookmarkStack for barge-in thought resumption
│   ├── core/                  # Brain, orchestrator, cognitive router, reflex engines, memory, security
│   │   ├── cognition/         # CognitiveRouter: tier classification, thinking budgets, failover
│   │   ├── evolution/         # Sandbox execution and AST validation for self-evolution
│   │   ├── memory/            # 4-tier SQLite WAL + sqlite-vec memory subsystem (mixins & actor)
│   │   └── pipeline/          # ToolOrchestrator, streaming tool parser, interim speech, profiler
│   ├── routing/               # ONNX intent router for fast CPU-side neural intent classification
│   ├── skills/                # 36 specialized tool modules registered with SkillRegistry
│   ├── speculative/           # SpeculativeEngine and EnvironmentEpochTracker for TOCTOU gating
│   └── ui/                    # Holographic HUD server, WebSocket broadcaster, desktop overlay
├── data/
│   ├── agy_runtime_workspace/ # Isolated runtime workspace for the persistent agy sidecar
│   └── prompts/               # Curated prompts loaded at runtime by ContextOrchestrator
├── tests/                     # Test suites (unit, integration, mock-isolated, concurrency)
├── tools/                     # Developer tools (test runner, context slicer)
└── docs/                      # Architectural maps, runbooks, and design documentation
```

---

## 3. Core Call Paths

### A. Full-Duplex Conversational Voice Turn
```
[User Speech]
       │
       ▼
1. Mic Stream Capture & Wake Word (`jarvis/audio/wake_word.py`)
       │ P-Core Pinning (0xFF) & MMCSS Pro Audio (`jarvis/core/win32_affinity.py`)
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
       │ Kokoro-82M ONNX latent style blend (`jarvis/audio/tts.py`)
       │ Zero-copy shared memory SPSC transport (`jarvis/audio/shm_transport.py`)
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
       │ DualKeyApprovalManager: requires explicit user confirmation for mutating operations
       │ validate_confined_path: enforces filesystem sandboxing
       │ validate_egress_url: blocks private IP / SSRF targets
       │ TurnTaintTracker: prevents tainted ambient telemetry from driving privileged actions
       ▼
3. SkillRegistry Invocation (`jarvis/skills/base.py`)
       │ Dispatches to target skill function in `jarvis/skills/<module>.py`
       ▼
4. Output Feedback to Brain Context
```

### C. Persistent Memory Subsystem (`jarvis/core/memory/`)
The `MemoryManager` class combines focused mixins into a single cohesive facade:
- **`SqliteBaseMixin`** ([`sqlite_base.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/sqlite_base.py)): Manages WAL mode, 256MB `mmap_size`, read-only connection pooling, and serialized writes via `SingleWriterActor`.
- **`TemporalGraphMixin`** ([`temporal_graph.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/temporal_graph.py)): Bi-temporal knowledge graph edges and Markov habit transitions.
- **`FactsManagerMixin`** ([`facts_manager.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/facts_manager.py)): Atomic facts and core user profile preferences (Tier 1 & Tier 2).
- **`TasksCalendarMixin`** ([`tasks_calendar.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/tasks_calendar.py)): Tasks, deadlines, calendar events.
- **`DocumentVaultMixin`** ([`document_vault.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/document_vault.py)): JIT document vaults, chunk sandwiching, attention-boundary formatting (Tier 3).
- **`InteractionMemoryMixin`** ([`interaction_memory.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/interaction_memory.py)): Conversation turn logs and episodic failure lessons (Tier 4).
- **`VectorMemoryEngine`** ([`vector_engine.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/vector_engine.py)): Native `sqlite-vec` KNN virtual tables fused with FTS5 BM25 via Reciprocal Rank Fusion (RRF).

---

## 4. Canonical Modules vs. Compatibility Shims

When authoring new features or refactoring, always import from the canonical module:

| Domain | Canonical Module | Compatibility Shims | Purpose |
| :--- | :--- | :--- | :--- |
| **Win32 Affinity & Scheduling** | `jarvis.core.win32_affinity` | `jarvis.audio.affinity`<br>`jarvis.core.thread_affinity` | MMCSS Pro Audio, P-core (0xFF) / E-core (0xFF00) bitmasks |
| **Preemptive Cancellation** | `jarvis.core.cancellation` | `jarvis.audio.cancellation` | Unified singleton `GLOBAL_EPOCH_STATE` for instant barge-in |
| **Streaming Tool Parser** | `jarvis.core.streaming_tool_parser` | `jarvis.core.stream_tool_parser` | Incremental streaming JSON tool tokenizer |
| **Prosodic Clause Chunker** | `jarvis.audio.prosodic_chunker` | `jarvis.audio.chunker` | Natural punctuation pause chunking for Kokoro TTS |
| **MCP Process Manager** | `jarvis.core.mcp_manager` | `jarvis.skills.mcp_manager` | Win32 Job Object sandbox for external MCP servers |
| **Sandbox Execution** | `jarvis.core.evolution.sandbox` | `jarvis.skills.job_object_sandbox`<br>`jarvis.core.sandbox` | AST loop detection, canary watchdog, snapshot rollback |

---

## 5. Test Infrastructure & Verification

Tests live in the [`tests/`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/tests/) directory and are managed by [`tools/run_fast_tests.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/tools/run_fast_tests.py):

| Test Mode | Command | Scope |
| :--- | :--- | :--- |
| **Targeted Test** | `python tools/run_fast_tests.py tests/test_<name>.py` | Runs a single test file directly with pytest. |
| **Fast Parallel Suite** | `python tools/run_fast_tests.py --fast -w 4` | Parallel execution of all non-hardware suites via `pytest-xdist`. |
| **Hardware Serial Suite** | `python tools/run_fast_tests.py --hardware` | Isolated serial execution of audio, ONNX GPU, and COM suites. |
| **Full Gate** | `python tools/run_fast_tests.py --all` | Runs fast parallel suite followed by serial hardware suite. |
| **Failed Tests Only** | `python tools/run_fast_tests.py --failed` | Re-executes only tests that failed in the last run (`--lf`). |

---

## 6. Detailed Architectural References

- [AGENTS.md](file:///C:/Users/Youssef/Projects/Jarvis_2.0/AGENTS.md): Developer-agent operational directives and invariants.
- [JARVIS_2.0_MASTER_ARCHITECTURAL_BLUEPRINT.txt](file:///C:/Users/Youssef/Projects/Jarvis_2.0/JARVIS_2.0_MASTER_ARCHITECTURAL_BLUEPRINT.txt): Deep technical specification and 8-pillar audit.
- [handoff.md](file:///C:/Users/Youssef/Projects/Jarvis_2.0/handoff.md): Point-in-time architectural milestones and historical audit snapshot.
- [`.agents/skills/`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/.agents/skills/): Specialized engineering skills (`systematic-debugging`, `test-driven-development`, `python-testing-patterns`, `python-profiling`, `council`, etc.).
