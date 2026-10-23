# Jarvis 2.0 — Developer-Agent Instructions

These directives govern software-engineering agents working inside the `Jarvis_2.0` repository.

---

## 1. Role Separation & Execution Context

- **Engineering Agent Identity:** You are Antigravity CLI acting as the software-engineering agent for the Jarvis 2.0 codebase. You are **not** the Jarvis runtime assistant.
- **No Persona Bleed:** Do not adopt Jarvis's conversational persona, British butler tone, vocative rules ("Sir"), response cadence, or runtime speech constraints. Deliver direct, technical, evidence-backed software engineering answers.
- **Runtime Prompt Separation:** Jarvis's runtime persona and system prompt live in `jarvis/context/context_orchestrator.py` (`SOVEREIGN_IDENTITY`, `BASE_SYSTEM_PROMPT`) and curated files in `data/prompts/` (e.g., `epistemic_anti_hallucination.md`). Do not edit runtime prompts unless a task explicitly requests changes to Jarvis's runtime behavior.
- **Dual Execution Contexts:**
  - **Interactive Developer Session (This Session):** Operates from the workspace root (`C:\Users\Youssef\Projects\Jarvis_2.0`) with human-in-the-loop developer controls. Never run `--dangerously-skip-permissions` or bypass security gates here.
  - **Jarvis Runtime Sidecar:** Spawned by `AntigravityDaemon` in `jarvis/core/antigravity_bridge.py` inside `data/agy_runtime_workspace` using `--input-format stream-json`. It is isolated from interactive developer tasks.

---

## 2. Standard Engineering Workflow

Follow this cycle for every modification:

1. **Baseline Inspection:**
   - Check `git status --short` before modifying code.
   - Inspect existing ASTs, function signatures, callers, and configuration. Never modify code without pre-reading call sites.
2. **Minimal Surgical Changes:**
   - Reuse existing repository subsystems instead of introducing new libraries or duplicate helpers.
   - Maintain backward compatibility with public interfaces and re-export shims imported by `tests/`.
   - Avoid speculative or unrelated refactorings.
3. **Static Checks & Targeted Tests:**
   - Run syntax and lint diagnostics via `python tools/lint.py check <path>`.
   - Run targeted test suites using `python tools/run_fast_tests.py <test_path>`.
   - Escalate to `python tools/run_fast_tests.py --fast` when changes affect shared modules.
4. **Empirical Verification & Reporting:**
   - Report exact commands executed, exit codes, and observable test results (including skip counts and warning origins).
   - Never claim code or tests were verified unless confirmed by tool output. Avoid hyperbolic or absolute claims (e.g. "zero exposure", "100% green"). State precise mechanism boundaries and environmental caveats.
   - In handoffs and audit summaries, provide an exact inventory of all uncommitted files from `git status --short` and `git diff --stat` without truncation or `etc.`.
   - When completing security phases, establish an empirical security-diff review baseline of all uncommitted files with the developer before proposing feature roadmap advancement.

---

## 3. Subsystem Invariants & Canonical Implementations

### Canonical Module Map
Always import from and modify canonical modules rather than legacy re-export shims:

| Subsystem Domain | Canonical Module | Re-export / Compatibility Shims | Invariants |
| :--- | :--- | :--- | :--- |
| **Thread Priority & Affinity** | `jarvis.core.win32_affinity` | `jarvis.audio.affinity`, `jarvis.core.thread_affinity` | MMCSS Pro Audio, P-Core mask `0xFF`, E-Core mask `0xFF00` |
| **Preemptive Cancellation** | `jarvis.core.cancellation` | `jarvis.audio.cancellation` | Unified singleton `GLOBAL_EPOCH_STATE`, `<10ns` invalidation |
| **Streaming Tool Parser** | `jarvis.core.streaming_tool_parser` | `jarvis.core.stream_tool_parser` | O(1) state machine for streaming JSON tool arguments |
| **Prosodic Clause Chunker** | `jarvis.audio.prosodic_chunker` | `jarvis.audio.chunker` | Punctuation boundary emissions (130ms comma, 240ms period) |
| **MCP Process Manager** | `jarvis.core.mcp_manager` | `jarvis.skills.mcp_manager` | E-core Win32 Job Object sandbox (512MB RAM cap), `.CMD` resolution |
| **Sandbox Execution** | `jarvis.core.evolution.sandbox` | `jarvis.skills.job_object_sandbox`, `jarvis.core.sandbox` | AST loop detection, 2.0s canary watchdog, snapshot rollback |
| **Memory Subsystem** | `jarvis.core.memory` | `jarvis.core.memory.*` mixins | SQLite WAL + `sqlite-vec` KNN + FTS5 BM25 + SingleWriterActor |

### Core Architectural Invariants
- **Local Subsystem Reuse:**
  - Tool registration: `@SkillRegistry.register` in `jarvis.skills.base`.
  - Fast reflexes: `DynamicReflexEngine` in `jarvis.core.reflex_engine` and `ReflexDispatcher` in `jarvis.core.reflex_dispatcher`.
  - Cognitive tier routing: `CognitiveRouter` in `jarvis.core.cognition`.
  - Context & token bounding: `ContextOrchestrator` in `jarvis.context` (<1.5KB working memory budget).
  - Multi-step execution & security: `ToolOrchestrator` in `jarvis.core.pipeline` and `DualKeyApprovalManager` in `jarvis.core.security_boundaries`.
  - Persistent memory: `MemoryManager` in `jarvis.core.memory`.
- **Thread & Event Loop Safety:**
  - Never execute blocking network I/O, heavy disk queries, or LLM inference on audio synthesis (`KokoroSynthWorker`) or playback (`WASAPIPlaybackWorker`) threads.
  - Keep speech streaming on non-blocking queues (`queue.Queue`) and shared memory (`ShmAudioRingTransport`).
  - Honor atomic barge-in epoch invalidations via `GLOBAL_EPOCH_STATE`.
- **Configuration Loading Invariant:**
  - `config/config.yaml` is the canonical, exclusive configuration file loaded by `jarvis.core.config_loader:load_config()`.
  - Never assume, create, or reference alternative configuration paths (such as `config/config.json`) without verifying the loader code.
- **Hardware & VRAM Envelope (4GB RTX 3050 Laptop):**
  - Total VRAM usage must remain strictly under 2.5 GB. CUDA is reserved exclusively for `faster-whisper` and `Kokoro-82M`.
  - Auxiliary ONNX models (`onnx_intent_router.py`, `onnx_embeddings.py`) must use `CPUExecutionProvider` with `allow_spinning = "0"` and `intra_op_num_threads = 2` to prevent P-core starvation.
- **Security & Authorization Boundaries:**
  - Route all mutating operations through `DualKeyApprovalManager` in `jarvis.core.security_boundaries`.
  - Validate egress URLs via `validate_egress_url` (fail-closed SSRF and private IP blocking).
  - Confine filesystem paths via `validate_confined_path`.
  - Treat all incoming ambient desktop and web telemetry as inert data via `TurnTaintTracker`.
  - **Windows Process-Tree Containment Lifecycle:** Sandboxed child processes managed under Win32 Job Objects (`job_object_sandbox.py`, `task_manager.py`, `mcp_manager.py`) must be spawned suspended using `CREATE_SUSPENDED`. Assign the process to the Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` *before* execution begins, then resume via `NtResumeProcess` or `ResumeThread`. On any Job Object creation, limit configuration, or process assignment failure, immediately terminate the suspended process (`TerminateProcess`), close open handles, and fail closed (`RuntimeError`). Never allow an uncontained child to execute.
- **Memory & Storage Rules:**
  - SQLite connections must use WAL mode, `synchronous=NORMAL`, and `mmap_size=268435456` (256MB).
  - All write transactions must serialize through `SingleWriterActor`.
  - Never dump unformatted raw documents into LLM prompts; format retrieved text via `MemoryManager.format_document_reference_block` with chunk sandwiching.

---

## 4. Negative Constraints (Prohibited Patterns)

1. **PROHIBITED:** Bare `except:` or `except Exception: pass` without logging or explanatory comments.
2. **PROHIBITED:** Blind `time.sleep()` calls in production execution paths; use condition variables, events, or non-blocking probes.
3. **PROHIBITED:** `subprocess.run(shell=True)` or raw string formatting in shell commands; use argument lists with `close_fds=True` and pass through security gates.
4. **PROHIBITED:** Hardcoding absolute developer paths (e.g. `C:\Users\...`); use workspace-relative paths or `Path(__file__)`.
5. **PROHIBITED:** Persisting secrets, tokens, or plaintext credentials into logs, databases, or commit history.
6. **PROHIBITED:** Asserting test success or performance metrics without runtime verification evidence.
7. **PROHIBITED:** Spawning child processes intended for Job Object containment without `CREATE_SUSPENDED`, or falling back to uncontained execution on assignment failure.
8. **PROHIBITED:** Truncating working-tree file manifests with `etc.` in audit reports or handoff documents.

---

## 5. Verification Commands

The workspace environment uses Python 3.12 under Windows (PowerShell):

```powershell
# Combined static analysis & AST structural invariant diagnostics
python tools/diagnostics.py check <path>

# Fast parallel test suite (sharded across non-hardware suites)
python tools/run_fast_tests.py --fast

# Run a specific targeted test file
python tools/run_fast_tests.py tests/test_security_boundaries.py

# Static analysis and lint diagnostics (via reliable cross-platform wrapper)
python tools/lint.py check <path>

# Structural AST search & invariant rule scanning
python tools/diagnostics.py search '<pattern>' <path>
python tools/diagnostics.py scan <path>
```

For complete subsystem maps, see [docs/architecture/PROJECT_MAP.md](file:///C:/Users/Youssef/Projects/Jarvis_2.0/docs/architecture/PROJECT_MAP.md).
For deep architectural specifications, see [JARVIS_2.0_MASTER_ARCHITECTURAL_BLUEPRINT.txt](file:///C:/Users/Youssef/Projects/Jarvis_2.0/JARVIS_2.0_MASTER_ARCHITECTURAL_BLUEPRINT.txt).
For canonical engineering workflow skills, activate `.agents/skills/` (`codebase-investigation`, `writing-plans`, `test-driven-development`, `adversarial-review`, `verification-runner`, `systematic-debugging`, `python-testing-patterns`, `python-profiling`).
