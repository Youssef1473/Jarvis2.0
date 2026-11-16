# Jarvis 2.0 — Dynamic Project State & Engineering Baseline

> [!IMPORTANT]
> **State Validation Contract:** A fresh engineering session must **never** blindly trust this document without independent empirical verification.
> Upon session startup, the agent must execute:
> ```powershell
> git branch --show-current
> git rev-parse HEAD
> git status --short --untracked-files=all
> ```
> The agent must compare live command output against Section 1 below. Any divergence in branch, HEAD commit, or working-tree file manifest invalidates the recorded baseline. Reconcile differences explicitly and **never** silently overwrite, discard, stash, or reset pre-existing user modifications.

---

## 1. Last Verified Git Baseline

- **Validation Timestamp:** 2026-11-16T19:25:00+01:00 (CET)
- **Active Git Branch:** `feat/rtx3050-4gb-vram-arbiter-and-pressure-shedding`
- **Recorded HEAD Commit SHA:** `95a4817`
- **HEAD Commit Subject:** `Merge pull request #60 from Youssef1473/feat/autonomous-appcontainer-patch-verify-loop`
- **Milestone State:** **Track C Completed: RTX 3050 4GB VRAM Arbiter, DXGI WDDM Budget Tracking & Pressure Shedding (Plan 043 Verified); 2,502 Tests Accounted with 0 Drift & 0 Orphans; Repository-Wide 0 Ruff / 0 AST-Grep Diagnostics**
- **Active Milestone:** **Track C: RTX 3050 4GB VRAM Arbiter & Pressure Shedding (Plan 043 Completed)**
- **Staged Modifications:** `0 files` (clean staging index)
- **Unstaged Tracked Modifications:**
  - `data/test_classification_manifest.json` (test manifest accounting updated to 2,502 nodes)
  - `jarvis/skills/nvml_dxgi_vram_arbiter.py` (DXGI 1.4 WDDM budget tracking, 4-tier watermarks, pre-flight admission, staged shedding)
  - `jarvis/audio/stt.py` (wired request_vram_budget and dynamic compute_type adaptation)
  - `jarvis/audio/tts.py` (wired kokoro_tts session shedding hook and pre-flight admission)
  - `jarvis/ui/metrics.py` (additive VRAM headroom, budget, and pressure tier vitals for Zero-CLS HUD)
  - `tests/test_nvml_vram_arbiter.py` (15 hermetic unit, mathematical headroom, and fallback tests)
  - `docs/agent/PROJECT_STATE.md`
  - `docs/agent/SESSION_LOG.md`

- **Untracked Items (8 items):**
  1. `data/agy_112_nodes.json` (Diagnostic node map)
  2. `data/agy_112_pytest_nodes.json` (Remediation test inventory)
  3. `data/execution_evidence/` (Audit evidence directory)
  4. `data/learning_candidates/` (Candidate learning records)
  5. `data/learning_events/` (Persisted learning events)
  6. `data/regression_scorecard.json` (Test regression metrics)
  7. `data/supplemental_test_manifest.json` (Auxiliary test map)
  8. `scratch/` (Temporary scratch directory)

## 2. Current Engineering Focus

- **Active Milestone:** `Track C: RTX 3050 4GB VRAM Arbiter & Pressure Shedding (Plan 043 Completed)`
- **Completed Work:**
  - **Track C (Plan 043 COMPLETED & VERIFIED): RTX 3050 4GB VRAM Arbiter, DXGI WDDM Budget Tracking & Pressure Shedding:**
    - **NVML & DXGI 1.4 VRAM Arbiter (`jarvis/skills/nvml_dxgi_vram_arbiter.py`):** Implemented COM-based DXGI 1.4 `IDXGIAdapter3::QueryVideoMemoryInfo` with adapter enumeration (`VendorId == 0x10de`), calibrated 4-tier pressure watermarks (`NOMINAL >= 1200 MiB`, `ELEVATED 700-1199 MiB`, `CRITICAL 350-699 MiB`, `OOM_GUARD < 350 MiB`), effective headroom calculation `min(DXGI_Budget - DXGI_Usage, NVML_Free)`, pre-flight admission `request_vram_budget`, and staged pressure shedding with thread-safe hook registry.
    - **Audio Subsystem Consumers (`jarvis/audio/stt.py`, `jarvis/audio/tts.py`):** Wired Faster-Whisper dynamic compute type adaptation (`float16` $\to$ `int8_float16` $\to$ `cpu`) via `request_vram_budget`, registered Kokoro session cache shedding hook (`_flush_kokoro_gpu_cache`), and gated Kokoro GPU provider admission.
    - **Zero-CLS Workstation Metrics (`jarvis/ui/metrics.py`):** Extended `WorkstationMetricsCollector` to expose `vram_headroom_mb`, `vram_pressure_tier`, `vram_budget_mb`, `shed_events_count`, `vh`, and `vpt` in HUD WebSocket telemetry without breaking existing schema keys.
    - **Hermetic Test Suite (`tests/test_nvml_vram_arbiter.py`):** Expanded suite to 15 tests covering effective headroom math, all 4 watermark tier transitions, Stage 1/2 shedding execution, fail-closed `OOM_GUARD` rejection, cross-platform Linux/non-NVIDIA graceful fallback, COM failure resiliency, and additive HUD metrics.
    - **Test Accounting & Classifier:** Expanded test manifest from 2,492 to 2,502 collected nodes (1,522 Tier 1 nodes) with 0 orphans, 0 duplicates, and 100% classification compliance.
  - **Track B (Plan 042 COMPLETED & VERIFIED): Autonomous AppContainer Code-Patch & Sandboxed Verify Loop:**
    - **AST Code Surgeon (`jarvis/skills/ast_code_surgeon.py`):** Added `FORBIDDEN_PATCH_MODULES` and path traversal safety checks (`validate_target_patch_path`), built ephemeral shadow staging (`create_shadow_patch`), created 3-stage AppContainer / Job Object verification runner (`verify_patch_in_appcontainer`), and added `DualKeyApprovalManager` gated promotion (`promote_verified_patch`).
    - **Self-Evolution Skills (`jarvis/skills/self_evolution.py`):** Registered `propose_and_verify_code_patch` with deterministic shadow cleanup.
    - **Hermetic Test Suite (`tests/test_autonomous_patch_verify.py`):** Implemented 10 tests verifying path traversal boundaries, syntax error rejection, AST security checks, unified diff generation without live file mutation, Ruff lint failure detection, test failure detection, end-to-end green verification, and human approval gating.
    - **Test Accounting & Classifier:** Expanded test manifest from 2,482 to 2,492 collected nodes with 0 orphans and 100% classification compliance.
  - **Track A (Plan 041 COMPLETED & VERIFIED): Multi-Workspace HUD & Quarantined Fact Promotion:**
    - **Backend Routes & Mixin (`jarvis/ui/server.py`, `jarvis/ui/routes/memory.py`):** Added `POST /api/workspaces/select` alias, exposed `quarantined_count` on `GET /api/workspaces`, and implemented authenticated `GET /api/memory/quarantined` (paginated list with category/search filters), `POST /api/memory/quarantined/promote` (single-writer transactional promotion with vector sync), and `POST /api/memory/quarantined/purge` (defense-in-depth quarantined purge).
    - **Zero-CLS Holographic HUD Integration (`jarvis/ui/templates/hud.html`):** Added fixed-width `#workspaceQuarantinedBadge` to the top-bar pill and a dedicated interactive Quarantined Memory Review drawer with batch promote/purge controls and real-time telemetry updates.
    - **Hermetic Test Suite (`tests/test_hud_workspace_and_quarantine.py`):** Authored 10 isolated test cases covering listing, switching, `403` unscoped/archived/invalid-slug rejection, pagination/filtering, promotion, purge `facts`/`facts_vec` parity, and Zero-CLS HTML invariants.
    - **Test Accounting & Classifier:** Expanded test manifest from 2,472 to 2,482 collected nodes with 0 orphans and 100% classification compliance.
  - **CI Hardening & Repository-Wide Static Analysis Regression Lock:**
    - Hardened `.github/workflows/ci.yml` (`security-gate` job) with a blocking `Verify Zero Static Lint & AST-Grep Diagnostics (Repository-Wide)` step.
    - Executes `python tools/lint.py check jarvis/ tools/ tests/` and `python tools/diagnostics.py check jarvis/ tools/` on every push and pull request, permanently locking in the 0-diagnostic baseline across the entire repository (`jarvis/`, `tools/`, and `tests/`).
  - **`/improve` Modernization & Refactoring Queue — Batch 8 (Plans 035–040 COMPLETED & SEALED):**
    - **Plan 035 (Tools Subsystem Zero-Ruff Closure — PR #54, commit `abd37c5`):** Cleared all 32 Ruff diagnostics across `tools/`, expanding CI gate to `jarvis/ tools/`.
    - **Plan 036 (Security & Sandbox Test Suites Hardening — PR #55, commit `15c30f9`):** Cleared all 147 Ruff diagnostics across 10 security test suites in `tests/`, replacing `B017` blind exception assertions with concrete security exceptions.
    - **Plans 037–039 (Memory, Audio, HUD, Brain Provider, Lifecycle Test Suites — PR #56, commit `27b1cab`):** Cleared 150 Ruff diagnostics across 29 test files in `tests/`.
    - **Plan 040 (Productivity & Context Test Suites Closure — PR #57, commit `e64d71c`):** Cleared all remaining 178 Ruff diagnostics across 51 auxiliary test files in `tests/`, driving the entire repository to 100% 0-Ruff clean status.
    - **Full Roadmap Closure:** All 40 implementation plans in `plans/README.md` (`001`–`040`) marked `VERIFIED DONE` with 0 open `TODO` or `BLOCKED` items.
    - **Total Diagnostics Eliminated Across All 8 Batches:** Over **1,200 diagnostics** eliminated from `jarvis/`, `tools/`, and `tests/`.
    - **Subsystems at 0 Ruff Diagnostics:** The **ENTIRE** Jarvis 2.0 repository (`jarvis/`, `tools/`, `tests/`).
    - **Test Accounting Invariant:** 2,472 collected nodes accounted for (0 drift) via `test_17_accurate_total_test_accounting` and `tools/test_classifier.py --check-orphaned`.
    - **Database Parity & Integrity:** `data/jarvis_memory.db` verified clean with `PRAGMA foreign_key_check` = 0 violations, `facts` = 238 rows (234 quarantined unscoped, 4 ws_default), and `facts_vec` = 238 rows.
  - **`/improve` Modernization & Refactoring Queue — Batch 6 (Plans 025–029 COMPLETED & SEALED):**
    - **Plan 025 (Evolution Sandbox Containment Hardening — PR #44, `5bd01ef`, merged `49d9392`):** Eliminates all 33 Ruff diagnostics in `jarvis/core/evolution/sandbox.py`, adding `ClassVar` annotations for blocked AST call sets, fixing re-raise syntax (`TRY201`), and typing AppContainer/Job Object containment and canary validation exceptions.
    - **Plan 026 (MCP Manager Lifecycle Hardening — PR #45, `fbe3a56`, merged `1e9428b`):** Eliminates all 22 Ruff diagnostics in `jarvis/core/mcp_manager.py`, merging string prefix checks (`PIE810`) and typing stdio/SSE process lifecycle and JSON-RPC transport exceptions.
    - **Plan 027 (Task Manager & OpenAI Brain Error Transparency — PR #45, `e9ceb27`, merged `1e9428b`):** Eliminates all 38 Ruff diagnostics across `jarvis/core/task_manager.py` (19) and `jarvis/core/brain_openai.py` (19), typing background worker Job Object termination and multi-provider LLM initializations.
    - **Plan 028 (Antigravity Bridge IPC Hardening — PR #46, `f8f8218`, merged `5f53b2f`):** Eliminates all 89 Ruff diagnostics in `jarvis/core/antigravity_bridge.py`, optimizing dict iteration (`PERF102`), simplifying integer casts (`RUF046`), and typing Win32 named mutex and streaming JSON IPC errors.
    - **Plan 029 (Data and Media Skills Typed Error Reporting — PR #47, `b41c2c1` + `3ca5dbe`, merged `a3016ad`):** Eliminates all 37 Ruff diagnostics across `jarvis/skills/email_manager.py` (16), `jarvis/skills/document_ingest.py` (11), and `jarvis/skills/photo_manager.py` (10), replacing silent swallows with typed error handling and protecting against unhandled external API exceptions.
    - **Full Roadmap Closure:** All 29 implementation plans in `plans/README.md` (`001`–`029`) marked `VERIFIED DONE` with 0 open `TODO` or `BLOCKED` items.
    - **Total Batch 6 Diagnostics Eliminated:** **219** (`334` $\to$ `~115` remaining across `jarvis/`).
    - **Subsystems at 0 Ruff Diagnostics:** `jarvis/audio/`, `jarvis/memory/`, `jarvis/ui/`, `jarvis/routing/`, `jarvis/context/`, `jarvis/core/pipeline/`, `jarvis/core/evolution/`, `jarvis/core/mcp_manager.py`, `jarvis/core/task_manager.py`, `jarvis/core/brain_openai.py`, `jarvis/core/antigravity_bridge.py`, `jarvis/core/win32_affinity.py`, `jarvis/core/services.py`, `jarvis/core/health_check.py`, `jarvis/skills/os_control.py`, `jarvis/skills/job_object_sandbox.py`, `jarvis/skills/system_watchdog.py`, `jarvis/skills/screen_vision.py`, `jarvis/skills/email_manager.py`, `jarvis/skills/document_ingest.py`, and `jarvis/skills/photo_manager.py`.
    - **Test Accounting Invariant:** 2,472 collected nodes accounted for (0 drift) via `test_17_accurate_total_test_accounting` and `tools/test_classifier.py --check-orphaned`.
    - **Database Parity & Integrity:** `data/jarvis_memory.db` verified clean with `PRAGMA integrity_check` = `ok`, `PRAGMA foreign_key_check` = 0 violations, `facts` = 238 rows (234 quarantined unscoped, 4 ws_default), and `facts_vec` = 238 rows.
  - **`/improve` Modernization & Refactoring Queue — Batch 5 (Plans 020–024 COMPLETED & SEALED):**
    - **Plan 020 (Context Pipeline Error Transparency — PR #40, `dd12589`, merged `575b7b4`):** Eliminates all 39 Ruff diagnostics across `jarvis/context/` (`pipe_listener.py`, `flow_state_mirror.py`, `context_orchestrator.py`, `fs_watcher.py`), achieving package-wide 0-Ruff clean status with typed IPC exception handling and collapsing redundant nested `if` statements (`SIM102`).
    - **Plan 021 (Win32 Thread Affinity Typed Exceptions — PR #41, `7657735`, merged `c0560e2`):** Eliminates all 8 `BLE001` diagnostics in `jarvis/core/win32_affinity.py` with typed ctypes exception handling (`(OSError, RuntimeError, AttributeError)`) across thread affinity masks, MMCSS scheduling, and COM STA initialization.
    - **Plan 022 (Pipeline Orchestration Error Transparency — PR #41, `6df3ecb`, merged `c0560e2`):** Eliminates all 23 Ruff diagnostics in `jarvis/core/pipeline/tool_orchestrator.py` and `jarvis/core/pipeline/dual_track.py` (including `TRY401` logging fixes) while preserving cooperative `asyncio.CancelledError` propagation and single-use tool invariants.
    - **Plan 023 (Core Services & Health Check Hardening — PR #42, `131087b`, merged `b125988`):** Eliminates all 31 Ruff diagnostics in `jarvis/core/services.py` and `jarvis/core/health_check.py`, providing structured logging and typed exception tuples (`psutil.Error`, `PortAudioError`, `sqlite3.Error`, OS/subprocess/network exceptions) for daemon supervision and health probes.
    - **Plan 024 (Watchdog & Vision Skills Error Reporting — PR #42, `bc92899`, merged `b125988`):** Eliminates all 22 Ruff diagnostics across `jarvis/skills/system_watchdog.py` and `jarvis/skills/screen_vision.py`, removing silent passes, simplifying nested conditionals (`SIM102`), and typing Gemini/UIA/Win32 exceptions while preserving user fallback responses.
    - **Full Roadmap Closure:** All 24 implementation plans in `plans/README.md` (`001`–`024`) marked `VERIFIED DONE` with 0 open `TODO` or `BLOCKED` items.
    - **Subsystems at 0 Ruff Diagnostics:** `jarvis/audio/`, `jarvis/memory/`, `jarvis/ui/`, `jarvis/routing/`, `jarvis/context/`, `jarvis/core/pipeline/`, `jarvis/core/win32_affinity.py`, `jarvis/core/services.py`, `jarvis/core/health_check.py`, `jarvis/skills/os_control.py`, `jarvis/skills/job_object_sandbox.py`, `jarvis/skills/system_watchdog.py`, and `jarvis/skills/screen_vision.py`.
    - **Test Accounting Invariant:** 2,472 collected nodes accounted for (0 drift) via `test_17_accurate_total_test_accounting` and `tools/test_classifier.py --check-orphaned`.
    - **Database Parity & Integrity:** `data/jarvis_memory.db` verified clean with `PRAGMA integrity_check` = `ok`, `PRAGMA foreign_key_check` = 0 violations, `facts` = 238 rows (234 quarantined unscoped, 4 ws_default), and `facts_vec` = 238 rows.
  - **`/improve` Modernization & Refactoring Queue — Batch 4 (Plans 015–019 COMPLETED & SEALED):**
    - **Plan 015 (HUD Audio Latency Telemetry Gauges — PR #36, `051baec`, merged `e170ebd`):** Wires live $L_1$/$L_2$ barge-in interruption latencies (with `var(--holo-crimson)` threshold highlighting above 50.0 ms) and TTS TTFB/RTF metrics into `renderTelemetryFrame()` inside `jarvis/ui/templates/hud.html` while preserving Zero-CLS layout stability.
    - **Plan 016 (UI Error Transparency & Type Safety — PR #37, `7124d80`, merged `b09ac0f`):** Eliminates all 26 Ruff diagnostics across `jarvis/ui/` (`hud.py`, `tray.py`, `win32_overlay.py`), adding `ClassVar` type safety to `JarvisHUD.STATES` and replacing blind exception catches with typed error logging.
    - **Plan 017 (Routing ONNX & Prewarm Hardening — PR #37, `9e0cebc`, merged `b09ac0f`):** Eliminates all 3 Ruff `BLE001` diagnostics across `jarvis/routing/` (`cascade.py`, `onnx_intent_router.py`), bringing `jarvis/routing/` to 100% clean Ruff status with typed ONNX Runtime and speculative pre-warm error handling.
    - **Plan 018 (E2E Harness JSON Scorecard Export — PR #38, `a9c0511`, merged `eb5f8be`):** Implements `--export-json <path>` in `tools/verify_consecutive_turns.py` and wires automated CI artifact upload (`phase3-consecutive-turns-scorecard`) in `.github/workflows/ci.yml`.
    - **Plan 019 (Win32 Job Object Sandbox Hardening — PR #38, `a33cc37`, merged `eb5f8be`):** Eliminates all 39 Ruff diagnostics in `jarvis/skills/job_object_sandbox.py`, fixing `__exit__` parameter typing (`PYI036`) and hardening Win32 Job Object process containment.
    - **Full Roadmap Closure:** All 19 implementation plans in `plans/README.md` (`001`–`019`) marked `VERIFIED DONE` with 0 open `TODO` or `BLOCKED` items.
    - **Test Accounting Invariant:** 2,472 collected nodes accounted for (0 drift) via `test_17_accurate_total_test_accounting`.
    - **Database Parity & Integrity:** `data/jarvis_memory.db` verified clean with `PRAGMA integrity_check` = `ok`, `PRAGMA foreign_key_check` = 0 violations, `facts` = 238 rows (234 quarantined unscoped, 4 ws_default), and `facts_vec` = 238 rows.
  - **`/improve` Modernization & Refactoring Queue — Batch 3 (Plans 010–014 COMPLETED & SEALED):**
    - **Plan 010 (CI Phase 3 Consecutive-Turns E2E Smoke Gate — PR #31, `14af094`, merged `042dd35`):** Wires `python tools/verify_consecutive_turns.py --headless` into `.github/workflows/ci.yml` under `Test Governance & Security Regression Gate`, executing the full 4-turn Phase 3 sequence (workspace isolation, single-use tool execution, audio barge-in latency budget, 6-phase teardown) on every pull request in under 2 seconds without test manifest drift (`2,472` nodes intact).
    - **Plan 011 (Memory Pool Teardown Hardening & Connection Logging — PR #32, `8a854ef`, merged `bf75693`):** Hardened connection teardown in `jarvis/memory/ro_pool.py` and `jarvis/core/memory/sqlite_base.py`. Added comprehensive error logging during connection closure, protected against interpreter shutdown module teardown (`ImportError` on `sys.meta_path`), and eliminated silent WAL frame pinning and connection lock leak hazards.
    - **Plan 012 (Audio Error Transparency & ClassVar Type Safety — PR #32, `b9fc6c9`, merged `bf75693`):** Eliminated all 54 Ruff diagnostics across `jarvis/audio/` (`hotkey_listener.py`, `dsp_tap.py`, `audio_bridge.py`, `playback.py`, `stt.py`, `wake_word.py`). Annotated mutable class-level attributes with `typing.ClassVar` (`RUF012`) and replaced blind `except Exception: pass` catches (`BLE001`, `S110`) with typed audio exceptions, surfacing real driver/endpoint errors before live hardware testing.
    - **Plan 013 (Live Audio Hardware Barge-In Validation — PR #33, `274ccc8`, merged `7446239`):** Implemented host hardware `--live-audio` execution path in `tools/verify_consecutive_turns.py` against `KokoroSynthWorker` and `WASAPIPlaybackWorker`. Empirically verified physical Windows 11 WASAPI barge-in interruption latency at $L_1 = 1.95$ ms and $L_2 = 0.00$ ms with TTFB = $20.6$ ms and RTF = $0.3500$ (>25x faster than the 50.0 ms SLA), utilizing instant PortAudio buffer abort (`st.abort()`).
    - **Plan 014 (Skills Subsystem Typed Error Reporting — PR #34, `2a86bd0`, `e828dab`, merged `177c6b6`):** Resolved all 96 Ruff diagnostics across `jarvis/skills/os_control.py` (69 `BLE001`, 20 `S110`, 3 `S112`, 1 `PLW0602`, 1 `PLW1510`, 1 `SIM102`, 1 `RUF059`). Replaced blind exception catches with narrow Win32/OS/subprocess/psutil exception handling and structured diagnostic logging. Handled headless runner `_COMError` from `pycaw.pycaw.AudioUtilities.GetSpeakers()` with seamless fallback to Win32 virtual keys, preserving all `@SkillRegistry.register` return type contracts and passing Phase 2 security regression.
    - **Full Plans Closure:** All 14 implementation plans in `plans/README.md` (`001`–`014`) marked `VERIFIED DONE` with 0 open `TODO` or `BLOCKED` items.
    - **Test Accounting Invariant:** 2,472 collected nodes accounted for (0 drift) via `test_17_accurate_total_test_accounting`.
    - **Database Parity & Integrity:** `data/jarvis_memory.db` verified clean with `PRAGMA integrity_check` = `ok`, `PRAGMA foreign_key_check` = 0 violations, `facts` = 238 rows (234 quarantined unscoped, 4 ws_default), and `facts_vec` = 238 rows.
  - **`/improve` Phase 3 Multimodal Live Verification Harness (Plan 009 COMPLETED & SEALED):**
    - **Plan 009 (Phase 3 Consecutive Turns E2E Harness — PR #29, `4bb6d4c`, merged `4d5e802`):** Modernized `tools/verify_consecutive_turns.py` (511 lines) implementing automated 4-turn Phase 3 multimodal live verification harness:
      - **Turn 1 (Memory Grounding & Workspace Isolation):** Seeds canary fact `ORCHID-47` under `ws_default`, asserts retrieval via `get_all_memory_nodes`, verifies fail-closed `PermissionError` on `'unscoped'` partition, asserts 0 disclosure to `'ws_other'`, and validates `/api/workspaces` route handler with fail-closed HTTP 403 enforcement.
      - **Turn 2 (System Vitals Tool Execution):** Dispatches read-only `system vitals` via fast deterministic reflex (`_try_fast_reflex` / `get_system_vitals`), asserts clean `TurnTaintTracker` (`is_tainted == False`), and verifies single-use tool execution without duplicates mid-turn.
      - **Turn 3 (Voice Barge-In & Latency Budget):** Simulates speech synthesis onset and barge-in interruption via `AudioTelemetry` and `DspProcessTelemetry`, sampled non-blockingly via `WorkstationMetricsCollector.sample_metrics_sync()`, asserting lock-free $L_1 < 50.0$ ms and $L_2 < 50.0$ ms. Empirical run: $L_1 = 12.50$ ms, $L_2 = 8.20$ ms, TTFB = $18.5$ ms, RTF = $0.4200$.
      - **Turn 4 (Clean Departure & 6-Phase Teardown):** Validates departure directive via `is_shutdown_command("Goodbye, Jarvis")`, executes `OrchestratorShutdownCoordinator.shutdown()`, stops memory `SingleWriterActor`, drains write locks, and cleans up temp SQLite sandbox without touching production DB.
    - **Windows AppContainer Loopback Constraint (`asyncio.ProactorEventLoop`):** Documented platform constraint on Windows: `asyncio.ProactorEventLoop` initializes an internal self-pipe over loopback TCP (`127.0.0.1`), which stalls (`WSAEACCES 10013`) when loopback is blocked inside standard terminal sandboxing. E2E tests executing Starlette route handlers with `asyncio.run()` require loopback access (`BypassSandbox: true`).
    - **Plans 001–009 Full Queue Closure:** All 9 implementation plans in `plans/README.md` marked `VERIFIED DONE` with 0 open `TODO` or `BLOCKED` items.
    - **Static Diagnostics & Hygiene:** 0 Ruff diagnostics on all modernized modules, 0 AST-Grep structural invariant violations across the entire repository.
    - **Test Accounting Invariant:** 2,472 collected nodes accounted for (0 drift) via `test_17_accurate_total_test_accounting`.
    - **Database Parity & Integrity:** `data/jarvis_memory.db` verified clean with `PRAGMA integrity_check` = `ok`, `PRAGMA foreign_key_check` = 0 violations, `facts` = 238 rows (234 quarantined unscoped, 4 ws_default), and `facts_vec` = 238 rows.
  - **`/improve` Modernization & Refactoring Queue — Batch 2 (Plans 005–008 COMPLETED & SEALED):**
    - **Plan 005 (Core F821 & BLE001 Cleanup — PR #24, `4ec563f`):** Resolved 6 live `F821` undefined-name diagnostics (`sqlite3` in `jarvis/core/memory/temporal_graph.py` and `Optional` in `jarvis/desktop/uia_delta_worker.py`), narrowed blind `BLE001` catches, and resolved `RUF059` unused variables across `jarvis/core/` and `jarvis/desktop/`.
    - **Plan 006 (SleepConsolidator SingleWriterActor & Workspace Gating — PR #25, `ed74210`):** Migrated `SleepConsolidator` direct SQLite writes to `SingleWriterActor`, resolving `PROJECT_STATE.md §5.3` debt; added fail-closed `active_workspace_id` parameter to `Heartbeat` and `SleepConsolidator` preventing consolidation of un-scoped historical facts; resolved 37 Ruff findings in `heartbeat.py`.
    - **Plan 007 (HUD Multi-Workspace Switcher — PR #26, `eeeafae`):** Added fail-closed authenticated workspace listing, partition switching via `SingleWriterActor`, and per-partition fact/vector count metrics in `jarvis/ui/routes/memory.py` and the Zero-CLS HUD (`hud.html`) with strict protection of quarantined un-scoped rows.
    - **Plan 008 (Audio L1/L2 Latency Telemetry — PR #27, `ce0de95`):** Exposed lock-free $L_1$/$L_2$ barge-in interruption latency, TTS TTFB, and RTF metrics in `AudioTelemetry` (`jarvis/audio/tts.py`) and `DspProcessTelemetry` (`jarvis/audio/dsp_process.py`), surfaced non-blockingly via `WorkstationMetricsCollector.sample_metrics_sync()` into UI WebSocket telemetry without blocking real-time audio threads.
    - **Batch 2 Plans Closure:** All 4 implementation plans in Batch 2 (`005`, `006`, `007`, `008`) marked `VERIFIED DONE` in `plans/README.md` with 0 open `TODO` or `BLOCKED` items.
    - **Static Diagnostics & Hygiene:** 0 Ruff diagnostics on all modernized modules, 0 AST-Grep structural invariant violations across the entire repository.
    - **Test Accounting Invariant:** 2,472 collected nodes accounted for (0 drift) via `test_17_accurate_total_test_accounting`.
    - **Database Parity & Integrity:** `data/jarvis_memory.db` verified clean with `PRAGMA integrity_check` = `ok`, `PRAGMA foreign_key_check` = 0 violations, `facts` = 238 rows (234 quarantined unscoped, 4 ws_default), and `facts_vec` = 238 rows.
  - **`/improve` Modernization & Refactoring Queue — Batch 1 (Plans 001–004 COMPLETED & SEALED):**
    - **Plan 001 (Audio DSP 64-bit Win32 ABI — PR #20, `0f35e95`):** Hardened 64-bit Win32 kernel32 event ABI (`SetEvent`, `ResetEvent`, `CloseHandle`) in `jarvis/audio/dsp_process.py`, eliminating 64-bit handle truncation hazard (`0xc0000008`) and logging teardown errors.
    - **Plan 002 (Provider Error Handling — PR #21, `6612990`):** Replaced blind exception catches with typed error handling and diagnostic telemetry across `openrouter.py`, `gemini.py`, and `http2_pool.py`.
    - **Plan 003 (Browser SSRF Error Transparency — PR #21, `ccdf42e`):** Separated `SecurityBoundaryViolation` SSRF policy denials from network timeouts in `jarvis/skills/browser_agent.py`, preserving audit failure transparency.
    - **Plan 004 (Browser CDP Architectural Decomposition — PR #22, `729d226`):** Decomposed `jarvis/skills/browser_cdp.py` from 1,949 lines down to 1,011 lines (-48.1% footprint). Extracted `cdp_transport.py` (661 lines), `cdp_tabs.py` (279 lines), and `cdp_page_extractor.py` (205 lines). Dynamic late-bound symbol resolution (`_get_browser_cdp_symbol`) preserves 100% mock-patch transparency. Enforced fail-closed CDP port authorization and guaranteed tab session cleanup on aborted navigations.
    - **Full Plans Closure:** All 4 implementation plans in `plans/README.md` marked `DONE`.
    - **Static Diagnostics & Hygiene:** 0 Ruff diagnostics on all modernized modules, 0 AST-Grep structural invariant violations across the entire repository.
    - **Test Accounting Invariant:** 2,472 collected nodes accounted for (0 drift) via `test_17_accurate_total_test_accounting`.
    - **Database Parity & Integrity:** `data/jarvis_memory.db` verified clean with `PRAGMA integrity_check` = `ok`, `PRAGMA foreign_key_check` = 0 violations, `facts` = 236 rows (234 quarantined unscoped, 2 ws_default), and `facts_vec` = 236 rows.
  - **Untangle-Codebase — `jarvis/vision/` Architectural Decomposition & Win32 ABI Hardening (COMPLETED & SEALED):**
    - **Refactor Commit SHA (`9efc7de`):** `refactor(vision): centralize 64-bit Win32 ABI, RAII desktop handles, and vision module boundaries`
    - **Centralized 64-bit Win32 ABI (`win32_vision_abi.py`, 456 lines):** Declared explicit 64-bit `ctypes` ABI signatures (`argtypes`/`restype`) for `user32`, `kernel32`, `dwmapi`, and `shcore`. Implemented `scoped_desktop_access()` RAII context manager restoring `orig_hdesk` before `CloseDesktop` to eliminate `ERROR_BUSY` (`170`) leaks. Provided dynamic late-bound resolvers for `dwmapi`, `user32`, and `kernel32` so `mock.patch("ctypes.windll...")` targets remain 100% interceptable.
    - **Hook Daemon Hardening (`window_hook_daemon.py`, 255 lines):** Replaced 32-bit `DWORD` hook handle declarations with explicit 64-bit `HWINEVENTHOOK` (`wintypes.HANDLE`) to eliminate `ERROR_INVALID_HANDLE` (`0x6`) on `UnhookWinEvent`. Narrowed event loop exception handling from blind catches to explicit `queue.Empty`, retained persistent `_hook_proc` reference, and declared sorted `__all__`.
    - **Screen Capture Pipeline (`screen_capture.py`, 66 lines):** Replaced unmanaged desktop switching with `scoped_desktop_access()`, eliminating permanent desktop handle leaks. Delegated DPI awareness to `enable_per_monitor_v2_dpi()`, preserved `ScreenPerception` public API, and declared sorted `__all__`.
    - **Privacy Guard Hardening (`privacy_guard.py`, 273 lines):** Routed `DwmGetWindowAttribute`, `GetWindowRect`, window title, and process lookup through `win32_vision_abi.py`. Preserved public exports (`BoundingRect`, `ZOrderPrivacyGuard`, and `is_window_dwm_cloaked`), eliminated blind exception swallowing, and declared sorted `__all__`.
    - **Passive dHash Background Monitor (`dhash_monitor.py`, 288 lines):** Removed inline `class LASTINPUTINFO` and circular fallback loops. Retained module-level `_get_window_title_and_proc(hwnd)` delegating to `win32_vision_abi` so `@patch("jarvis.vision.dhash_monitor._get_window_title_and_proc")` remains 100% transparent. Narrowed exception handling to specific OS/runtime tuples and declared sorted `__all__`.
    - **Set-of-Mark Visual Grounding (`som_grounding.py`, 580 lines):** Routed window geometry, DPI awareness, and window title queries to `win32_vision_abi.py`. Preserved all public data classes and compatibility aliases (`MarkElement`, `MarkedUIElement`, `MarkRegistry`, `SetOfMarkAnnotator`, `is_window_privacy_blocked`, `enable_per_monitor_v2_dpi`, `UIElementCandidate`, `INTERACTIVE_CONTROL_TYPES`). Replaced 20 blind `except Exception:` blocks with narrow runtime/OS exception tuples and declared sorted `__all__`.
    - **Static Hygiene:** 0 Ruff diagnostics across all 6 vision files (`tools/lint.py`) and 0 AST structural invariant violations (`tools/diagnostics.py`).
    - **Empirical Regression Verification:** Targeted 7-suite vision tests passed with **42 passed, 0 failed in 2.03s (Exit code 0)**; Tier 1 Fast Test Suite passed with **1,492 passed, 0 failed, 10 skipped in 146.52s (Exit code 0)**; Post-convergence suite passed with **10 passed, 0 failed in 3.11s (Exit code 0)**.
    - **Test Manifest Accounting:** Verified exact 2,472 collected node manifest accounting invariant via `test_17_accurate_total_test_accounting` (0 node drift).
  - **Untangle-Codebase — `jarvis/skills/dev_shell.py` Architectural Decomposition (COMPLETED & SEALED):**
    - **Refactor Commit SHA (`bc5e2a5`):** `refactor(skills): decompose dev_shell.py into confinement, fs operations, code intelligence, and background tasks`
    - **Hardening Commit SHA (`4c52948`):** `test(sandbox): align job_object_sandbox_hardening with DualKey auth and re-export server query constants`
    - **Line Reduction & Footprint:** Reduced `jarvis/skills/dev_shell.py` from 1,671 lines to 268 lines (-1,403 lines, -83.9% footprint).
    - **Win32 NTDLL/Kernel32 ABI Confinement:** Extracted 64-bit ctypes bindings (`NtCreateFile`, `GetFinalPathNameByHandleW`, `FILE_OPEN`, `SYNCHRONIZE`), directory handle confinement, TOCTOU mitigation, and handle-bound file opening to `jarvis/skills/fs_confinement.py` (579 lines).
    - **File Operations:** Extracted `@SkillRegistry` file tools (`list_directory`, `read_file`, `read_multiple_files`, `write_file`, `patch_file`, `search_codebase`) to `jarvis/skills/fs_operations.py` (438 lines).
    - **Code Intelligence & Diagnostics:** Extracted `run_project_tests`, `git_diff`, `git_status`, `query_codebase_graph`, `ast_search_codebase`, and `check_code_diagnostics` to `jarvis/skills/code_intelligence.py` (449 lines) with dynamic `_get_dev_shell_symbol` resolution and `(RuntimeError, OSError)` fallback exception handling.
    - **Background Tasks:** Extracted async background job lifecycle management (`run_background_task`, `run_command_in_background`, `list_background_tasks`, `get_background_task_output`, `cancel_background_task`) to `jarvis/skills/background_tasks.py` (110 lines).
    - **Mock Transparency & Re-exports:** Updated `jarvis/skills/job_object_sandbox.py` to evaluate `is_mocked_run` before auth checks, preserved all `dev_shell` re-exports in `__all__` (including `subprocess`), and re-exported query string sizing constants on `jarvis.ui.server`.
    - **Static Hygiene:** 0 Ruff diagnostics across all 5 skills files (`tools/lint.py`) and 0 AST structural invariant violations (`tools/diagnostics.py`).
    - **Empirical Regression Verification:** Targeted dev_shell test suite passed with **115 passed, 0 failed in 18.98s (Exit code 0)**; Tier 1 Fast Test Suite passed with **1,492 passed, 0 failed, 10 skipped in 138.44s (Exit code 0)**; Sandbox hardening suite passed with **32 passed, 13 skipped in 8.24s (Exit code 0)**.
    - **Test Manifest Accounting:** Verified exact 2,472 collected node manifest accounting invariant via `test_17_accurate_total_test_accounting` (0 node drift).
  - **Untangle-Codebase — `jarvis/core/orchestrator.py` Architectural Decomposition (COMPLETED & SEALED):**
    - **Commit SHA (`7b09bd2`):** `refactor(orchestrator): extract speech capture, lifecycle shutdown, and intent acknowledgments`
    - **Line Reduction & Complexity:** Reduced `jarvis/core/orchestrator.py` from 1,003 lines to 707 lines (-296 lines, -29.5% footprint).
    - **Speech Capture & Dynamic Silence:** Extracted VAD pause scaling, dynamic silence calculation, RMS hesitation damping, and Push-to-Talk frame buffering into `AdaptiveSpeechCapture` (`jarvis/audio/speech_capture.py`, 241 lines).
    - **Lifecycle Shutdown & 64-bit ABI Hardening:** Extracted 6-phase shutdown sequence to `jarvis/core/orchestrator_lifecycle.py` (`OrchestratorShutdownCoordinator`, 153 lines) and fixed 64-bit Win32 HANDLE ABI in `trim_process_memory()`, setting `kernel32.GetCurrentProcess.restype = wintypes.HANDLE` and `psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]`, eliminating `STATUS_INVALID_HANDLE` (`0xc0000008`).
    - **Cognitive Routing & Topic Extraction:** Extracted departure/shutdown directive detection, immediate intent audio feedback, and topic phrase extraction to `jarvis/core/cognition/intent_acknowledgments.py` (168 lines).
    - **Backward Compatibility & Patch Invariants:** Preserved all 10 module-scope patch targets in `jarvis.core.orchestrator` (`HotkeyListener`, `JarvisBrain`, `JarvisHUD`, `JarvisHeartbeat`, `MemoryManager`, `SafetyGate`, `SoundEffects`, `SpeechToText`, `TextToSpeech`, `WakeWordDetector`) and thin delegation methods for 100% backward compatibility.
    - **Static Hygiene:** 0 Ruff diagnostics across all 4 files (`tools/lint.py`) and 0 AST structural invariant violations (`tools/diagnostics.py`).
    - **Empirical Regression Verification:** Targeted orchestrator, audio & Win32 suites passed with **77 passed, 0 failed in 27.29s (Exit code 0)**; Tier 1 Fast Test Suite passed with **1,492 passed, 0 failed, 10 skipped in 150.20s (Exit code 0)**.
    - **Test Manifest Accounting:** Verified exact 2,472 collected node manifest accounting invariant via `test_17_accurate_total_test_accounting` (0 node drift).
  - **Untangle-Codebase — `jarvis/ui/server.py` Architectural Decomposition (COMPLETED & SEALED):**
    - **Commit SHA (`368572e`):** `refactor(ui): decompose server.py into security, metrics, telemetry, and domain route modules`
    - **Line Reduction & Complexity:** Reduced `jarvis/ui/server.py` from 2,431 lines to 699 lines (-1,732 lines, -71.2% footprint).
    - **Security & Token Isolation:** Extracted token lifecycle, CSP nonces, origin/host validation, and access log redaction to `jarvis/ui/security.py` (570 lines) with dynamic session token resolution (`resolve_active_session_token(request)`).
    - **Hardware Metrics & WebSocket Fanout:** Extracted NVML/psutil metrics to `jarvis/ui/metrics.py` (`WorkstationMetricsCollector`, 273 lines) and connection management to `jarvis/ui/telemetry.py` (`TelemetryConnectionManager`, 147 lines).
    - **Domain Route Mixins:** Modularized route handlers under `jarvis/ui/routes/` (`__init__.py`, `approval.py`, `calendar.py`, `memory.py`, `media.py`, `command.py`) and extracted `jarvis/ui/templates/unlock.html` (84 lines).
    - **Static Hygiene:** 0 Ruff diagnostics and 0 AST structural invariant violations across all 11 UI files (`tools/lint.py` & `tools/diagnostics.py` exited 0).
    - **Empirical Regression Verification:** Tier 1 Fast Test Suite passed with **1,492 passed, 0 failed, 10 skipped in 216.84s (Exit code 0)**. Tier 2 UI, WebSocket & Loopback IPC suites passed with **85 passed, 0 failed in 16.23s (Exit code 0)**.
    - **Test Manifest Accounting:** Verified exact 2,472 collected node manifest accounting invariant via `test_17_accurate_total_test_accounting` (0 node drift).
  - **Untangle-Codebase — `jarvis/core/brain.py` Architectural Decomposition (COMPLETED & SEALED):**
    - **Commit SHA (`d648243`):** `refactor(brain): decompose intent routing, turn state, skill loading, and provider lifecycle`
    - **Line Reduction & Complexity:** Reduced `jarvis/core/brain.py` from 1,498 lines to 874 lines (-624 lines, -41.6% footprint).
    - **Cognitive Routing & Pools:** Consolidated candidate model pools (`GEMINI_CANDIDATE_MODELS`, `GROQ_CANDIDATE_MODELS`, `ANTIGRAVITY_CANDIDATE_MODELS`, `OPENROUTER_CANDIDATE_MODELS`), tool classification sets, and query intent analysis into `CognitiveRouter` (`jarvis/core/cognition/cognitive_router.py`) with ClassVar re-exports on `JarvisBrain` preserving downstream public imports and test compatibility.
    - **Turn State Isolation:** Encapsulated turn-scoped mutable state into `TurnContext` dataclass with backward-compatible `None` fallback across all execution paths.
    - **Lazy Skill Auto-Registration:** Replaced 38 eager top-level skill imports in `brain.py` with idempotent, thread-safe `SkillRegistry.ensure_skills_loaded()` inside `JarvisBrain.__init__()`, preventing import side-effects and COM STA/MTA threading collisions while ensuring 100% test registry population.
    - **Speech & Provider Subsystem Delegation:** Relocated clause-based speech chunking to `jarvis/core/pipeline/interim_bridge.py` and Ollama VRAM eviction to `OllamaProvider.unload_model()`.
    - **Static Hygiene:** Eliminated all 38 Ruff (`RUF012`, `S110`, `BLE001`, `SIM102`, `SIM103`) diagnostics across all 7 refactored files; `tools/lint.py check` exited 0; `tools/diagnostics.py scan` exited 0.
    - **Empirical Regression Verification:** Tier 1 Fast Test Suite passed with **1,492 passed, 0 failed, 0 errors, 10 skipped in 141.89s (Exit code 0)**.
    - **Test Manifest Accounting:** Verified exact 2,472 collected node manifest accounting invariant via `test_17_accurate_total_test_accounting` (0 node drift).
  - **Option A (Workstream Verification, Triage & Atomic Commits — COMPLETED & CLOSED):**
    - **Commit 1 (`2eb2451`):** `feat(providers): enforce truth-in-reporting error policies and atomic bridge registry`
      - Packaged truth-in-reporting error handling in `brain_gemini.py` (eliminating false-success generic greetings on empty stream/candidates, malformed responses, thought-only parts, safety blocks).
      - Hardened fail-closed auth boundaries (401/403 abort), provider quota cascading, and mid-turn tool outcome preservation (`_format_action_result()`) in `brain_antigravity.py`.
      - Packaged atomic multi-lease registry swapping in `antigravity_bridge.py`.
      - Landed Workstream 1 audit suites (`test_antigravity_registry_atomicity.py`, `test_provider_routing_audit.py`) and Workstream 2 audit suite (`test_context_orchestration_audit.py`).
      - Verified 122 passed, 3 skipped, 32 subtests passed in 12.13s (Exit code 0).
    - **Commit 2 (`292d3d3`):** `ci(governance): add merge_group security gate and harden test supervisor accounting`
      - Added CI security gate with `merge_group` trigger in `.github/workflows/ci.yml`.
      - Fixed action hash token consumption in `jarvis/skills/dev_shell.py` for background tasks.
      - Restored `import jarvis.skills.dev_shell` in `tests/test_dev_skills.py` for complete tool registration.
      - Hardened `tools/test_supervisor.py` and `tools/run_fast_tests.py` with supervisor tempdir fallback, cache_dir isolation, explicit `errors: int` field in `TierScorecard`, and failure classification on teardown/collection errors.
      - Landed regression test `tests/test_four_tier_runner.py::test_18_teardown_errors_fail_suite_and_aggregate_scorecard`.
      - Verified 29 passed across `tests/test_four_tier_runner.py` and `tests/test_dev_skills.py` (Exit code 0).
    - **Commit 3 (`7d61206`):** `test(security): align Sub-Batch 3A-3D suites with single-use action-bound permits and fail-closed checks`
      - Remediated 20 security and unit test suites across Sub-Batches 3A-3D with single-use action-bound permits, CDP context IDs, and contained mocks with negative fail-closed checks.
      - Corrected `tests/test_document_provenance.py` line 407 to align with `close_application` Level 2 critical privilege authorization gate.
      - Re-exported canonical test manifest `data/test_classification_manifest.json` via `tools/test_classifier.py --export`, reconciling 2,472 collected nodes (Tier 1: 1,502; Tier 2A: 273; Tier 2B: 201; Tier 3: 225; Tier 4: 251; Browser: 20; 0 orphans, 0 unclassified).
      - Verified `test_17_accurate_total_test_accounting` passed 100%. Verified 238 passed in full run across all 21 test files (Exit code 0).
    - **Commit 4 (`0d12bdd`):** `docs(skills,governance): add Jarvis engineering skills and reliability milestone reports`
      - Staged and committed 5 custom Jarvis engineering skills in `.agents/skills/` (`jarvis-repo-orientation`, `jarvis-safe-implementation`, `jarvis-security-boundary-review`, `jarvis-test-governance`, `jarvis-windows-native-interop`).
      - Staged and committed milestone governance plan `docs/milestones/RELIABILITY_AND_ENGINEERING_GOVERNANCE_PLAN.md` and failure taxonomy reports in `docs/reports/` (`TIER_1_47_FAILURES_ANALYSIS_MATRIX.md`, WAL benchmark records, `tier1_47_failures_matrix.json`).
    - **Tracked Working Tree State:** Reconciled to exactly 0 modified tracked files (100% clean baseline).
  - **Batch 1 (Governance & Manifest):** Canonical 2,374 test nodes classified in `data/test_classification_manifest.json`; zero orphans verified; `--security` test runner integrated with 13 suites (**195 passed, 0 failed, 1 skipped**).
  - **Batch 2 (Failure Analysis):** Reconciled all 47 failures into 4 distinct remediation sub-batches with 1-to-1 node ID mapping (`docs/reports/TIER_1_47_FAILURES_ANALYSIS_MATRIX.md`).
  - **Sub-Batch 3A (Assertion Mismatches):** 4 negative security tests updated to expect `Critical Authorization Gate` (100% passed).
  - **Sub-Batch 3B (Batch Tool Permits):** 17 positive-path tests remediated across 6 files using single-use action-bound permits and contained batch mocks with negative fail-closed checks (100% passed).
  - **Sub-Batch 3C (Process & OS Control Permits):** 15 tests remediated across 7 files using action-bound permits, CDP context IDs, and contained mocks with negative fail-closed checks (100% passed).
  - **Sub-Batch 3D (Provider Routing & Formatting):** 11 tests remediated across 7 files (100% passed).
  - **Batch 4 — Workstream 1 (Provider Routing & Distinct Error Policy Audit & Final Closeout):**
    - Traced full call chains across `brain.py`, `brain_antigravity.py`, `brain_gemini.py`, `antigravity_bridge.py`, and `github_bridge.py`.
    - Hardened fail-closed authentication boundaries: 401/403 in Antigravity or Gemini halts execution immediately without retrying candidates or cascading to other providers.
    - Implemented quota cascading: Antigravity quota -> Gemini API -> Copilot CLI.
    - Suppressed provider failover mid-turn: if a tool has already been executed in the turn (`had_tool_call`), errors return the completed action result without provider switching.
    - Implemented truthful mid-turn tool outcome semantics: `_format_action_result()` distinguishes between successful execution, failed/halted execution, and authorization rejection.
    - Hermetic SQLite test isolation in `tests/test_provider_routing_audit.py`: isolated temporary database per instance, explicit close in `tearDown()`, zero writes to `data/jarvis_memory.db`.
    - Truth-in-reporting direct Gemini response handling in `jarvis/core/brain_gemini.py`: completely eliminated false-success generic greetings (`"At your service, Sir."`) on empty stream, empty candidates, malformed responses, thought-only parts, and safety blocks. Session invalidated on all failure/blocked paths to protect against chat history corruption.
    - Expanded `tests/test_provider_routing_audit.py` to 28 comprehensive audit tests (all 28 passed in 3.92s).
    - Updated `tests/test_provider_routing_failover.py` test 08 to expect truthful failure notice (14 passed in 4.45s).
    - Integrated `tests/test_provider_routing_audit.py` into the canonical security regression suite (14 suites).
    - Refreshed test classification manifest to 2,410 collected test nodes (1,441 in Tier 1 with 0 orphans).
    - Antigravity multi-lease registry atomicity audit: eliminated non-atomic `shutil.copyfile` fallback, strictly enforced atomic directory entry swap (`tmp_lock.replace` / `ReplaceFileW`) with pure fail-closed behavior preserving prior valid `.agy_lock`.
    - Hardened Win32 `ReplaceFileW` partial-failure error code handling: code 1175 preserves old file and unlinks staging `tmp_lock`; codes 1176 and 1177 strictly preserve `tmp_lock` (`preserve_tmp = True`) as the sole valid recovery artifact; all other errors clean staging file.
    - Preserved ACLs and durability separation: staging file is synchronously flushed via `FlushFileBuffers` before replacement; `ReplaceFileW` is called with exact `dwReplaceFlags=0` (removing unsupported `0x1` write-through flag and strictly omitting merge/ACL error ignore flags `0x2` and `0x4`).
    - Authored and expanded dedicated audit suite `tests/test_antigravity_registry_atomicity.py` to 8 tests covering interrupted write fault-injection, concurrent readers, staging file cleanup, fail-closed registration, Win32 error codes 1175/1176/1177, and ACL preservation (8/8 passed in 0.56s).
    - Accounted for exact single security skip: `tests/test_p0_action_bound_authorization.py:664` (`test_ai_generated_argument_cannot_authorize_unmanaged_browser` skipped due to ProactorEventLoop self-pipe Winsock WSAEACCES 10013 loopback sandbox restriction).
    - Verified full 14-suite security regression suite: **223 passed, 0 failed, 1 skipped in 55.33s (Exit code 0)**.
    - Verified full Tier 1 fast test suite across 4 workers: **1,432 passed, 0 failed, 9 skipped out of 1,441 collected nodes in 103.56s (100% pass rate, 0 failures, Exit code 0)**.
    - Accounted for all 9 Tier 1 skips individually: 3 live Antigravity integration tests (`JARVIS_RUN_LIVE_INTEGRATION=1`) and 6 opt-in MCP plugin smoke tests (`JARVIS_RUN_MCP_SMOKE=1`).
    - Verified Ruff static analysis: all modified files clean (0 net-new errors).
    - AST-Grep structural invariants: 0 violations (Exit code 0).
    - Git Whitespace Hygiene: `git diff --check` -> 0 violations (Exit code 0).
    - Preserved Phase 2 baseline commit `c59c0165314106f10689f5cbcda456957e9fe73f` with zero unauthorized git mutations.
  - **Batch 4 — Workstream 2 (Context Orchestration & Prompt Envelope Safety — FORMALLY ACCEPTED & CLOSED):**
    - Traced full context assembly across `assemble_system_prompt()` (Stage 1 static) and `build_ephemeral_user_envelope()` (Stage 2 dynamic turn tail).
    - Resolved DEFECT-CTX-01 (+20B separator/suffix overrun) and DEFECT-CTX-02 (multi-byte UTF-8 character boundary split): implemented `_safe_truncate_text_to_bytes()` and `_truncate_xml_block()` with byte-exact ceiling (<1536 bytes) and entity boundary protection (`&...;`, `<...>`).
    - Enforced strict context retention priority: Live Telemetry (Priority 1, dropped first) -> Recalled Memory (Priority 2, dropped second) -> Episodic Lessons (Priority 3, highest auxiliary context) -> User Utterance (Priority 4, absolute preservation without silent truncation).
    - Verified nested XML integrity: empirical trace proved production Stage 2 envelopes (`<episodic_lessons>`, `<jit_recalled_memory>`, `<live_telemetry_envelope>`) contain strictly escaped flat text; validated complete envelopes across 8 budget tiers via `xml.etree.ElementTree` with 100% parse success.
    - Documented architectural boundary: `_truncate_xml_block()` is an outer-tag-preserving block trimmer for flat escaped envelopes; multi-tier nested envelopes (`<ambient_untrusted_telemetry>`) are assembled in Stage 1 and enforce their own self-closing fallback in `FlowStateMirror`.
    - Authored dedicated 37-test adversarial audit suite `tests/test_context_orchestration_audit.py` (37/37 passed in 1.72s).
    - Registered all 37 audit tests in `data/test_classification_manifest.json` under `tier1_hermetic_unit`: updated Tier 1 count from 1,441 to 1,478; updated total collected from 2,410 to 2,447 (0 orphans, 0 duplicates verified).
    - Verified full Tier 1 Hermetic Unit Suite: **1,469 passed, 0 failed, 9 skipped out of 1,478 collected nodes in 113.92s (Exit code 0, 100% green)**.
    - Verified Phase 2 Security Regression Suite: **223 passed, 0 failed, 1 skipped in 48.45s (Exit code 0)**.
    - Verified `test_four_tier_runner.py::test_17_accurate_total_test_accounting`: **27/27 passed in 8.27s (Exit code 0)**.
  - **Batch 4 — Workstream 3 (Memory Authorization, Provenance & Context Trust — FORMALLY ACCEPTED & CLOSED):**
    - Traced full memory lifecycle across ingestion, persistence, retrieval, distillation, vector indexing, and model context envelope disclosure.
    - Implemented retrieval-time taint restoration (W3-01): untrusted remembered facts (`source_provenance != 'trusted_internal'`) and episodic reflection lessons restore `TurnTaintTracker` compromise state before model inference, guaranteeing Level 1 mutation protection against indirect prompt injection across conversational turns.
    - Preserved independent human approval precedence (W3-02): Level 2 critical operations (`CRITICAL_PRIVILEGE_TOOLS` or `risk_level >= 2`) evaluate unconditional authorization gates prior to taint gates, preventing bypass while ensuring fail-closed containment.
    - Implemented dual-boundary credential scrubbing (W3-04): recursive sanitization of structured `tool_calls` payloads in `InteractionMemory.log_interaction()` before persistence; real-time scrubbing of historical conversations in `ContextOrchestrator.assemble_system_prompt()`; real-time scrubbing of document summaries and raw-content fallback snippets in `DocumentVault.format_document_reference_block()`.
    - Preserved archival contract: original cleartext in `documents.raw_content` database column remains 100% byte-for-byte unmutated for cryptographic hash validation and exact citations.
    - Authored dedicated 20-node adversarial audit suite `tests/test_memory_authorization_audit.py`: 17 passed, 3 strict XFAIL (`test_06` workspace isolation, `test_07` session isolation, `test_11` epistemic conflict resolution).
    - Preserved three deferred architectural requirements as strict expected failures (`@pytest.mark.xfail(strict=True)`), tracking documented technical debt without false-positive inflation.
  - **Batch 4 — Regression Governance Remediation (FORMALLY ACCEPTED & CLOSED):**
    - Removed non-hermetic class-level database modification-time assertion in `tests/test_memory_authorization_audit.py`; replaced with per-instance temporary directory path isolation invariant.
    - Corrected test runner error accounting in `tools/run_fast_tests.py`: suites reporting teardown or collection errors (`error_count > 0`) or non-zero exit codes are classified as `FAILED`; summary output displays `Errors: {result.error_count}`; `TierScorecard` retains errors in explicit `errors: int` field; aggregate failure decision fails on any error.
    - Added regression test `tests/test_four_tier_runner.py::test_18_teardown_errors_fail_suite_and_aggregate_scorecard` proving teardown errors fail individual suites and aggregate scorecards.
    - Corrected `tests/test_document_provenance.py::test_12_level1_mutation_blocked_after_untrusted_retrieval` to evaluate `launch_or_focus_application` (Level 1 risk, high-privilege, non-critical) against Dual-Key taint gates, proving retrieval taint blocks execution without dispatcher invocation.
    - Reclassified `test_13_subsequent_async_tool_call_observes_retrieval_taint` into `tier2b_loopback_ipc` due to Windows ProactorEventLoop TCP self-pipe requirements.
    - Updated canonical manifest `data/test_classification_manifest.json` to 2,468 collected nodes (Tier 1: 1,499; Tier 2A: 272; Tier 2B: 202; Tier 3: 224; Tier 4: 251; Browser: 20; 0 orphans, 0 duplicates).
    - Final Canonical Tier 1 Suite (`tools/run_fast_tests.py --fast`): **1,487 passed, 0 failed, 0 errors, 12 skipped (3 XFAIL + 9 environment skips) out of 1,499 collected nodes in 114.84s (Exit code 0)**.
    - Final Canonical Security Suite (`tools/run_fast_tests.py --security`): **223 passed, 0 failed, 0 errors, 1 skipped across 14 suites in 46.14s (Exit code 0)**.
    - Final Four-Tier Runner Suite: **28/28 passed in 10.50s (Exit code 0)**.
    - Final Document Provenance Suite (non-13 nodes): **13/13 passed in 4.5s (Exit code 0)**.
- **Batch 4 Status:** **WORKSTREAMS 1, 2, AND 3 FORMALLY ACCEPTED, FROZEN & CLOSED**.
- **Forward Architecture Backlog for Subsequent Milestones:**
  1. *Workspace & Multi-Session Isolation (W3-03):* Tracked by executable XFAIL tests `test_06` and `test_07`. Scope: multi-tenant directory boundary enforcement and ephemeral session key memory isolation.
  2. *Epistemic Authority & Conflict Resolution (W3-05):* Tracked by executable XFAIL test `test_11`. Scope: supersession hierarchies, temporal decay, and contradictory historical fact qualification.
  3. *Tier 2B Host IPC Loopback Verification:* Execute `test_13` on host Windows OS where loopback TCP sockets (`127.0.0.1`) are permitted.
  4. *Test Fixture Containment Refinement:* Transition path containment assertions to `Path(resolved_db).is_relative_to(Path(temp_dir))`.

- **Active Milestone:** `JARVIS-2.0 — End-to-End Product Readiness` (Status: **PHASES 1A, 1B, & 2 COMPLETED & VERIFIED; PHASE 3 READY**)
- **Previous Milestone:** `ENG-OS-03C9: Windows Integration Profile Realization & Supervisor Architecture` (Status: **FORMALLY CLOSED & ACCEPTED UNDER B1 OPERATIONAL TRUST MODEL**)
- **Completed Phases:**
  - `Phase 1A: Resilient Startup & Antigravity Discovery`:
    - Resolved `find_agy_executable()` WinError 5 access denied crash on `%LOCALAPPDATA%\agy\bin\agy.exe`.
    - Added non-crashing path inspector with 4-way contract (`VALID`, `NOT_FOUND`, `INACCESSIBLE`, `INVALID`).
    - Decoupled `JarvisBrain` from mandatory Antigravity CLI presence; made bridge optional and resilient.
    - Implemented structured `get_provider_status()` reporting availability across providers without conflating CLI sessions and `GEMINI_API_KEY`.
    - Verified 11/11 hermetic unit tests passing (`tests/test_antigravity_executable_resolver.py`).
    - Verified live startup, <5ms reflex latency, and clean shutdown without exceptions.
  - `Phase 1B: Provider Routing & Live Gemini Connectivity`:
    - Established explicit 4-tier provider hierarchy: Primary Antigravity -> Secondary Gemini API (`gemini-3.8-flash` with dynamic candidate rotation) -> Deterministic reflexes (<10ms) -> Explanatory grounding.
    - Verified direct live Gemini API query with real arithmetic (`15 * 6 = 90`) in 2.29s, live multi-turn context retention (`Bettany99`), and dynamic candidate rotation (503/timeout cascade).
    - Hardened API client with `types.HttpOptions(timeout=10000)` and thinking token suppression.
    - Closed all three reviewer audit items:
      1. Turn idempotency ledger lifecycle strictly initialized and turn-scoped in-memory with side-effect replay guards (`test_11_no_duplicate_tool_action_mid_turn_failover`).
      2. 25-second turn deadline bounded as best-effort; enforces remaining budget >= 10.0s before attempting subsequent candidate retries (matching Google API's 10s minimum deadline).
      3. Pruned `JarvisBrain.GEMINI_CANDIDATE_MODELS` to the 3 reachable distinct models (`gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.1-flash-lite`).
    - Verified 14/14 hermetic unit tests passing (`tests/test_provider_routing_failover.py`).
  - `Phase 2: Acoustic Loop & Live Sensory Verification`:
    - Diagnosed Kokoro TTS initialization: confirmed ONNX tensor shapes (`tokens` [1, seq_len], `style` [1, 256], `speed` [1] -> `audio` [audio_len]) and identified environment dependency (`scipy 1.17.1` in project venv).
    - Hardened `apply_cinematic_jarvis_dsp()` and audio playback with graceful standard playback fallback on unplugged hardware endpoints (WDM-KS error 6).
    - Verified complete 5-stage acoustic sensory pipeline live (`tools/verify_acoustic_loop.py`):
      1. Audio capture / wake word detection: openWakeWord evaluated "Hey Jarvis" with 0.9984 confidence (threshold 0.55).
      2. Speech recognition: faster-whisper transcribed query with 100% accuracy.
      3. Reasoning & reflex dispatch: executed in 4.66-4.92ms locally without tokens.
      4. Neural TTS synthesis: Kokoro-82M synthesized 119,296 samples (4.97s audio) in Bettany voice blend (50% fable, 30% george, 20% daniel) with warm RTF 0.4708 < 1.0 (outpacing physical speech playback).
      5. Cognitive uplink acoustic loop: verified mathematical reasoning query, Gemini fallback handling, and spoken audio synthesis.
    - Verified 6/6 hermetic unit tests passing (`tests/test_acoustic_loop.py`).
    - Updated live health diagnostics: `[SPEECH SYNTHESIS] : OK (Kokoro-82M ONNX verified (Bettany blend, RTF < 0.70))`.
    - Resolved `sqlite-vec` `vec0` virtual table upsert bug: `vec0` does not implement virtual table conflict resolution for `INSERT OR REPLACE`, raising `UNIQUE constraint failed on <table_vec> primary key` when re-indexing existing facts/documents/lessons/edges. Replaced with atomic `DELETE FROM <table_vec> WHERE <id_col> = ?` followed by `INSERT INTO <table_vec>` across `facts_manager.py`, `document_vault.py`, `interaction_memory.py`, and `temporal_graph.py`.
- **Next Phase:** `Phase 3: Live End-to-End Multimodal Interaction & Co-Engineering Workflows`:
  - Turn 1: Synthetic memory grounding test (`ORCHID-47`) with verifiable local SQLite retrieval trace and prompt envelope injection.
  - Turn 2: Authorized tool execution (`get_system_vitals`) with zero mid-turn duplication.
  - Turn 3: Spoken voice interruption with dual latency reporting ($L_1$ speech onset to stop; $L_2$ wake event to stop, target $<50\text{ ms}$) under elevated playback threshold ($0.70$).
  - Turn 4: Clean conversational departure ("Goodbye, Jarvis") terminating application cleanly without invoking Windows OS shutdown.
- **Execution Constraints:**
  - Zero candidate node execution: Gate 2 strictly unauthorized (0/507 candidate test nodes executed; zero pytest collections).
  - Exact 71-file tracked modified baseline preserved.
  - Zero modifications to `docs/agent/LESSONS.md` and `data/learning_events`.
  - Zero git mutations (`commit`, `push`, `reset`, `clean`, `checkout`).

## 3. Verified Test Health

### A. Executed & Passing Suites (Dynamic Ground Truth)
- `tests/test_memory_payload_minimization.py`: **2 passed in 4.03s** under test supervisor (Exit code: 0) verifying prompt envelope minimization down to 157 bytes (shedding unrelated HUD errors, test runner instructions, and episodic lessons for conversational memory queries) and live Gemini grounding.
- `tests/test_shutdown_lifecycle.py`: **8 passed in 13.36s** under test supervisor (Exit code: 0) verifying 7-stage graceful teardown, synchronous farewell completion, explicit SQLite SingleWriterActor close, audio queue draining, and main thread unblocking via `_thread.interrupt_main()`/`sys.exit(0)` without `os._exit()`.
- `tests/test_acoustic_loop.py`: **6 passed in 14.03s** under test supervisor (Exit code: 0) covering Kokoro Bettany voice style blending, MCU cinematic DSP (110Hz HPF + 7.5ms micro-reflection), clause chunking, Hann window smoothing, SPSC frame ring buffer backpressure, and end-to-end sensory loop simulation.
- `tests/test_bettany_voice_and_dsp.py`: **8 passed in 7.95s** under test supervisor (Exit code: 0) verifying DSP, Bettany voice blend synthesis, and voice control skill invocation with clean turn context reset.
- `tests/test_voice_control_and_skills.py`: **8 passed in 9.47s** under test supervisor (Exit code: 0) verifying voice command routing, skill execution, and turn-taint boundary protection.
- `tests/test_provider_routing_failover.py`: **14 passed in 14.30s** under test supervisor (Exit code: 0) covering all Phase 1B provider routing, failover, candidate cascade, tool deduplication, turn idempotency ledger, candidate model reachability, bounded turn deadline, and health check state reconciliation.
- `tests/test_antigravity_executable_resolver.py`: **11 passed in 12.11s** under test supervisor (Exit code: 0) covering all Phase 1A verification cases hermetically with zero host access.
- `Tier 1: Hermetic Unit Suite` (1,237 Nodes across 113 Files via Supervisor): **1,228 passed, 0 failed, 9 skipped in 95.28s** (Exit code: 0; `data/pytest_tmp/tier1_hermetic_unit/junit.xml`). 100.0% pass rate among runnable unit tests! Zero failures, zero errors, zero timeouts!
- `tools/verify_runner_dispatch.py`: **8 passed in 0.060s** (Exit code: 0; split-file dispatch and Win32 CLI safety verification).
- `tests/test_flagship_features.py`: **1 passed** (`test_project_manager` with `os.startfile` patch).
- `tests/test_self_evolution.py`: **3 passed in 4.05s** (`TestSelfEvolution` with DPAPI HMAC key and CI process isolation).
- `tests/test_lint_tool.py`: **22 passed in 1.11s** (includes `test_resolve_untrusted_env_var_falls_through_to_next_tier` with `Path.is_file` mock).
- `tests/test_plugin_integrations.py`: **6 passed in 1.03s** (includes `test_gemini_docs_mcp_sse_parsing` with destination mock).
- `tests/test_new_skills.py`: **1 passed in 1.47s** (step 4 webpage reading isolated via `patch.dict`).
- `tests/test_audio_streaming_pipeline.py`: **26 passed in 1.76s** directly (Exit code: 0) and **26 passed in 2.83s** under test supervisor (Exit code: 0).
- Targeted 112-node remediation cohort (`data/agy_112_pytest_nodes.json`): **109 passed, 3 skipped, 0 failed in 109.04s** (Exit code: 0).
- `tests/test_cloud_streaming_latency.py`: **4 passed in 3.77s** (Exit code: 0). Hermetic execution without accessing host `agy.exe`.
- `tests/test_four_tier_runner.py`: **27 passed in 6.54s** (Exit code: 0).
- `tests/test_milestone2_memory_hardening.py`: **10 passed in 1.58s** (Exit code: 0).
- `tests/test_milestone3_audio_invariants.py`: **6 passed in 2.11s** (Exit code: 0).

### B. Tier 1 Hermetic Unit Suite (1,237 Nodes via Supervisor)
- Command: `python tools/run_fast_tests.py --unit -w 4`
- Outcomes: **1,228 passed, 0 failed, 9 skipped in 95.28s** (Status: `COMPLETED`, Exit code: 0; `data/pytest_tmp/tier1_hermetic_unit/junit.xml`).
- Delta: 100% elimination of all 63 historical Tier 1 failures. Runnable pass rate: **100.0%**.
- Skips (9 nodes): 3 live Antigravity integration tests + 6 live MCP smoke tests.

### C. Test Classification Manifest Inventory (2,206 Unique Nodes)
- Total Classified Nodes: **2,206**.
- Operational Tier Breakdown:
  - `tier1_hermetic_unit`: **1,237 tests** (56.1%)
  - `tier2a_async_safe`: **273 tests** (12.4%)
  - `tier2b_loopback_ipc`: **201 tests** (9.1%)
  - `tier3_windows_integration`: **224 tests** (10.2%)
  - `tier4_hardware_integration`: **251 tests** (11.4%)
  - `tier_browser_integration`: **20 tests** (0.9%)
- Orphan Detection & Disjointness Audit: `python tools/test_classifier.py --check-orphaned` verified **0 orphans, 0 duplicates, 0 unclassified, 2,206 nodes verified**.

### D. Whole-Repository Test Regression Scorecard (Empirical Ground Truth: `data/regression_scorecard.json`)
```
==================================================================================================
                         JARVIS 2.0 FOUR-TIER TEST REGRESSION SCORECARD
==================================================================================================
Tier                         Status            Total   Pass   Fail   Skip TimeOut Blocked  Duration
--------------------------------------------------------------------------------------------------
Tier 1: Hermetic Unit        COMPLETED          1237   1228      0      9       0       0   112.99s
Tier 2B: Loopback & IPC      ENVIRONMENT_BLOCKED    201      0      0      0       0     201     0.00s
Tier 2A: Async Safe          ENVIRONMENT_BLOCKED    273      0      0      0       0     273     0.00s
Tier 3: Windows Integration  FAILED              224    179     32     13       0       0    39.83s
Tier 4: Hardware Integration FAILED              251    250      1      0       0       0    79.48s
Browser Integration          ENVIRONMENT_BLOCKED     20      0      0      0       0      20     0.00s
--------------------------------------------------------------------------------------------------
TOTALS                                          2206   1657     33     22       0     494   238.46s
==================================================================================================
```
- Invariant: Zero watchdog timeouts, zero unclassified nodes, zero incomplete outcomes ($1657 + 33 + 22 + 494 = 2,206$).

### E. Failing Test Suites & Environmental Boundaries
- `tests/test_milestone1_security_hardening.py`: **14 passed, 2 failed in 2.70s** (Exit code: 1).
  - Failed tests: `test_hmac_signed_packet_accepted` and `test_token_authenticated_packet_accepted`.
  - Environmental Boundary: Local loopback socket communication (`127.0.0.1:39415`) failed inside standard sandbox (`BypassSandbox: false`). Raw loopback UDP timed out and raw loopback TCP failed with `[WinError 10013]`. Classified into `tier2b_loopback_ipc`.

### E. Unexecuted Suites (Historical Baseline)
- `tests/test_milestone4_cognitive_invariants.py`
- `tests/test_milestone5_windows_hardware.py`
- `tests/test_milestone6_residual_hardening.py`
- *Historical record (2026-11-02 on Host OS outside sandbox):* All 83 milestone tests passed (Exit code: 0) as recorded in `docs/milestones/MILESTONES.md`.

---

## 4. Architectural Invariants Under Guard

All engineering work must comply with canonical subsystem rules and Architecture Decision Records (ADRs). ADRs currently hold `PROPOSED` status and represent documented technical proposals awaiting formal developer acceptance:

1. **Persistence & Concurrency Serialization:**
   - Canonical Rule: `.agents/rules/persistence_sqlite.md`
   - Decision Record: [`docs/adr/ADR-001-sqlite-single-writer-wal.md`](../adr/ADR-001-sqlite-single-writer-wal.md) (`PROPOSED`)
   - Invariant: Core database write mutations serialize strictly through `SingleWriterActor` with `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`.
2. **Win32 Job Object Process Containment:**
   - Decision Record: [`docs/adr/ADR-002-win32-job-object-containment.md`](../adr/ADR-002-win32-job-object-containment.md) (`PROPOSED`)
   - Invariant: Child processes must be spawned suspended (`CREATE_SUSPENDED`), assigned to a Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, and only then resumed. Fail closed on any failure.
3. **Preemptive Audio Barge-In Cancellation:**
   - Canonical Rule: `.agents/rules/audio_threading.md`
   - Decision Record: [`docs/adr/ADR-003-epoch-based-audio-cancellation.md`](../adr/ADR-003-epoch-based-audio-cancellation.md) (`PROPOSED`)
   - Invariant: Audio playback and synthesis threads must never execute blocking disk/network I/O or LLM inference. Stale work invalidates cooperatively via `GLOBAL_EPOCH_STATE`.
4. **Mutating Tool Authorization & Provenance Tracking:**
   - Canonical Rule: `.agents/rules/security_boundaries.md`
   - Decision Record: [`docs/adr/ADR-004-dual-key-tool-authorization-turn-taint.md`](../adr/ADR-004-dual-key-tool-authorization-turn-taint.md) (`PROPOSED`)
   - Invariant: All mutating tool operations route through `DualKeyApprovalManager`. Telemetry and external data are marked untrusted via `TurnTaintTracker`.
5. **Windows Hardware Affinity & MMCSS Priority:**
   - Canonical Rule: `.agents/rules/audio_threading.md`
   - Decision Record: [`docs/adr/ADR-005-win32-hardware-core-affinity.md`](../adr/ADR-005-win32-hardware-core-affinity.md) (`PROPOSED`)
   - Invariant: WASAPI playback pinned to Logical CPUs 0–1 (`0x0003`) with MMCSS Pro Audio; wake-word pinned via physical P-core mask (`0x0055`); compute on P-cores (`0x00FC`); background on E-cores (`0xFF00`) with EcoQoS. Overlap on Logical CPU 0 represents affinity partitioning, not exclusive hardware isolation.
6. **Network Egress Validation & SSRF Defense:**
   - Canonical Rule: `.agents/rules/security_boundaries.md`
   - Decision Record: [`docs/adr/ADR-006-egress-validation-ip-pinning.md`](../adr/ADR-006-egress-validation-ip-pinning.md) (`PROPOSED`)
   - Invariant: Outbound URLs must be pre-validated with `validate_egress_url` and pinned at socket connect to prevent DNS rebinding.

---

## 5. Blockers & Technical Debt

1. **Environmental Sandbox Loopback TCP/UDP Dropping:**
   - On Windows, loopback network packets (`127.0.0.1`) are dropped inside the standard sandbox (`BypassSandbox: false`).
   - Impact A: UDP datagrams dropped -> `tests/test_milestone1_security_hardening.py` listener tests fail (14 passed, 2 failed).
   - Impact B: TCP loopback dropped -> `socket.socketpair()` emulates socketpair on Windows using TCP localhost (`_fallback_socketpair`), causing `lsock.accept()` to block indefinitely. Consequently, any test instantiating `asyncio.ProactorEventLoop` (`_make_self_pipe()`), Starlette `TestClient`, or ASGI servers freezes inside the sandbox.
   - Impact C: In `pytest -n 4`, when all 4 worker processes grab async/network test modules, all 4 deadlock in `lsock.accept()`, stalling test execution indefinitely at ~17%.
   - Status: Root causes empirically proven via stack trace and thread probe. Requires either in-process mock socket fixtures for sandbox or host execution for ASGI/network suites.
2. **Six Tracked F821 Undefined Name Diagnostics (RESOLVED in Plan 005 — PR #24, `4ec563f`):**
   - 5 occurrences in `jarvis/core/memory/temporal_graph.py`: added `import sqlite3`.
   - 1 occurrence in `jarvis/desktop/uia_delta_worker.py`: added `from typing import Optional`.
   - Status: Completely resolved; verified 0 F821 diagnostics repository-wide.
3. **SQLite Writer Ownership vs. Reader Connection Debt:**
   - Direct Write Bypass Paths (2 remaining modules): `photo_manager.py` (3 connections) and `reflex_macro_compiler.py` (6 connections) open uncoordinated direct connections and execute write transactions bypassing `SingleWriterActor`. (`sleep_consolidator.py` was migrated to `SingleWriterActor` in Plan 006 — PR #25, `ed74210`).
   - Direct Read Connection Ownership (1 module): `habit_graph.py` opens direct connection for reads, while writes serialize properly through `SingleWriterActor`. Under SQLite WAL mode, readers do not block writers; direct reads are technically valid under WAL concurrency, though connection pooling would be architectural best practice.
   - Status: Migration design fully specified in [`docs/architecture/SQLITE_WRITER_OWNERSHIP_DESIGN.md`](../architecture/SQLITE_WRITER_OWNERSHIP_DESIGN.md); remaining paths tracked for future maintenance.
4. **Master Blueprint Commit Staleness:**
   - Line 5 of `JARVIS_2.0_MASTER_ARCHITECTURAL_BLUEPRINT.txt` references historical commit `0697eb51...`.
   - Status: Clarified as historical baseline.
5. **Candidate CAS In-Place Replacement Crash Consistency Gap (ENG-OS-03B1 Finding — RESOLVED in ENG-OS-03B2):**
   - Remediation: Implemented Option A (Database-Authoritative Projection Model). SQLite `candidate_records.payload_json` is the canonical source of truth. File projections (`{candidate_id}.json`) are written via atomic staged replacement with `os.fsync()` strictly **after** SQLite `COMMIT`. Missing, stale, or corrupt projections self-heal deterministically via `reconcile_candidate_projections()`. Verified across 11 crash injection boundaries in `tests/test_persistence_concurrency.py` with zero data loss.

---

## 6. Immediate Next Action

- **Milestone Status:**
  - `ENG-OS-01: Engineering Agent Reliability & Persistent Intelligence` formally closed with verdict **ACCEPTED WITH LIMITATIONS**.
  - `ENG-OS-02: Adaptive Engineering Intelligence & Continuous Learning` formally closed with verdict **ACCEPTED WITH LIMITATIONS**.
  - `ENG-OS-03B1: Independent Crash Consistency & Transactional Persistence Audit` formally completed with verdict **ACCEPTED WITH LIMITATIONS** (`docs/agent/ENG_OS_03B1_CRASH_CONSISTENCY_AUDIT.md`).
  - `ENG-OS-03B2: Authoritative SQLite Persistence & Crash-Safe Candidate Recovery` formally completed and empirically verified (`docs/agent/ENG_OS_03B2_AUTHORITATIVE_PERSISTENCE.md`).
  - `ENG-OS-03B3: Independent Authoritative Persistence Acceptance Audit` formally completed with verdict **ACCEPTED WITH LIMITATIONS** (`docs/agent/ENG_OS_03B3_FINAL_ACCEPTANCE_AUDIT.md`).
  - `ENG-OS-03B2R: Receipt Canonical Payload & Safe Candidate Bootstrap` formally completed with verdict **ACCEPTED WITH LIMITATIONS** (`docs/agent/ENG_OS_03B2R_REMEDIATION.md`). Milestone `ENG-OS-03B` formally closed.
  - `ENG-OS-03C1: Test Infrastructure Modernization & Four-Tier Runner Architecture` formally implemented (`tools/test_classifier.py`, `tools/test_supervisor.py`, `data/test_classification_manifest.json`).
  - `ENG-OS-03C2: Independent Tier Execution & Supervisor Acceptance` formally completed with verdict **ACCEPTED WITH LIMITATIONS** (`docs/agent/ENG_OS_03C2_ACCEPTANCE_AUDIT.md`).
  - `ENG-OS-03C3: Independent Regression Scorecard Integrity & Test Classification Audit` formally completed with verdict **ACCEPTED WITH LIMITATIONS** (`docs/agent/ENG_OS_03C3_SCORECARD_AUDIT.md`).
  - `ENG-OS-03C3R: Node-Level Classification Evidence Closure` formally completed with verdict **ACCEPTED WITH LIMITATIONS** (`docs/agent/ENG_OS_03C3R_CLASSIFICATION_CLOSURE.md`).
  - `ENG-OS-03C3R1: Final Node-Level Accounting and Classification Reconciliation` formally completed with verdict **ACCEPTED** (`docs/agent/ENG_OS_03C3R1_FINAL_RECONCILIATION.md`).
  - `ENG-OS-03C3R2: Exclusive Failure Taxonomy and Timeout Evidence Closure` formally completed with verdict **ACCEPTED** (`docs/agent/ENG_OS_03C3R2_FAILURE_TAXONOMY_CLOSURE.md`).
  - `ENG-OS-03C3R2-C: Evidence Terminology and Final Scorecard Correction` formally completed with verdict **ACCEPTED** (`docs/agent/ENG_OS_03C3R2_FAILURE_TAXONOMY_CLOSURE.md`).
- `ENG-OS-03C4: Test Hermeticity & External Dependency Isolation` formally completed and empirically verified (`docs/agent/ENG_OS_03C4_TEST_HERMETICITY_REPORT.md`).
- `ENG-OS-03C4R: Independent Test Hermeticity Remediation Acceptance Audit` formally completed with verdict **ACCEPTED WITH LIMITATIONS** (`docs/agent/ENG_OS_03C4R_ACCEPTANCE_AUDIT.md`).
- `ENG-OS-03C4R1: Regression Manifest Registration & Final Verification` formally completed with verdict **ACCEPTED WITH LIMITATIONS** (`docs/agent/ENG_OS_03C4R1_FINAL_ACCEPTANCE.md`).
- `ENG-OS-03C4R2: Final Evidence and Documentation Closure` formally closed with verdict **ACCEPTED** (`docs/agent/ENG_OS_03C4R2_CLOSURE.md`).
- `ENG-OS-03C5: TTS Streaming Lifecycle & Restart Reliability` formally completed and empirically verified (`docs/agent/ENG_OS_03C5_TTS_LIFECYCLE_REPORT.md`).
- `ENG-OS-03C6: Hermetic Tier 1 Recovery & Classification Normalization` formally completed and empirically verified (`docs/agent/ENG_OS_03C6_HERMETIC_TIER1_REPORT.md`).
- `ENG-OS-03C6R: Tier 1 Hermeticity Audit & AppContainer Classification` formally completed with verdict **ACCEPTED WITH LIMITATIONS** (`docs/agent/ENG_OS_03C6R_CLASSIFICATION_INTEGRITY_REPORT.md`).
- `ENG-OS-03C6R1: Final Hermeticity Acceptance & Self-Evolution Split` formally completed with verdict **ACCEPTED WITH LIMITATIONS** (`docs/agent/ENG_OS_03C6R1_FINAL_HERMETICITY_ACCEPTANCE.md`).
- `ENG-OS-03C6R2: Final Execution Accounting & Timeout Evidence Closure` formally completed with verdict **ACCEPTED** (`docs/agent/ENG_OS_03C6R2_FINAL_EXECUTION_ACCOUNTING.md`). Milestone `ENG-OS-03C6` formally closed.
- `ENG-OS-03C2R: Test Runner Split-File Dispatch Integrity Audit & Win32 CLI Hardening` formally completed with verdict **ACCEPTED WITHOUT LIMITATIONS** (`docs/agent/ENG_OS_03C2R_RUNNER_DISPATCH_AUDIT.md`). Milestone `ENG-OS-03C2R` formally closed.
- `ENG-OS-03C7 Phase 1: Windows Integration Execution Reliability & Timeout Granularity Profile` formally completed (`docs/agent/ENG_OS_03C7_PHASE1_EXECUTION_PROFILE.md`).
- `ENG-OS-03C7: Windows Integration Execution Reliability & Timeout Granularity` formally closed with verdict **ACCEPTED** (`docs/agent/ENG_OS_03C7_PHASE2_RECOVERY_REPORT.md`).
- `ENG-OS-03C8 Phase 1: Integration Environment Capability Probing & Failure Scoping` formally completed and empirically verified (`docs/agent/ENG_OS_03C8_PHASE1_ENVIRONMENT_CAPABILITY_REPORT.md`).
- `ENG-OS-03C8 Phase 2: Controlled Execution-Context Comparison & Remediation Architecture` formally completed; evidence audit corrected per independent engineering review (`docs/agent/ENG_OS_03C8_PHASE2_CONTROLLED_COMPARISON_AND_ARCHITECTURE.md`).
- `ENG-OS-03C8 Context B Ingestion & Controlled Comparison:` Raw host telemetry ingested from `data/execution_evidence/env_probe_context_b.json`, validated across all 5 gates with verdict **ACCEPTED** (`tools/validate_context_b_gates.py` exit code 0). Comparative report `docs/agent/ENG_OS_03C8_CONTEXT_B_COMPARISON_REPORT.md` confirms 518 of 527 blocked/failing nodes are sandbox environment artifacts.
- `ENG-OS-03C8 Phase 3S Dual-Profile Architecture Precision Audit:` Architectural deliverable refined (`docs/agent/ENG_OS_03C8_PHASE3_DUAL_PROFILE_ARCHITECTURE.md`). Integrated all five final precision refinements: 526-candidate population accounting, controlled SSRF test infrastructure, real Chromium CDP endpoint verification, scoped production Job Object containment, and immutable execution receipts via EvidenceStore. Complete per-node mapping of all 527 investigated nodes preserved.
- `ENG-OS-03C8 Stage 1: Bounded Chromium & Playwright Capability Investigation:` Stage 1 authorized, executed, and compared across both contexts. Implemented `tools/probe_chromium_capabilities.py`. Context A (AppContainer): Experiment A timed out (15.0s event loop deadlock); Experiment B exited in 0.51s (`STATUS_BREAKPOINT 0x80000003` from Crashpad `0x6D` named pipe error). Context B (Host Medium-Integrity): Experiment A passed in 2.72s with `chromium_sandbox=True` (zero `--no-sandbox`), establishing that native Playwright works on host Windows; Experiment B timed out at 10.0s waiting for `DevToolsActivePort`, diagnosing Chrome for Testing flag requirements (`--remote-allow-origins=*`, `--remote-debugging-address=127.0.0.1`) and `chrome-win64` path resolution in `tests/test_browser_network_boundaries.py`. Full comparative findings documented in `docs/agent/ENG_OS_03C8_STAGE1_CHROMIUM_CAPABILITY_REPORT.md`. All 9 browser nodes remain strictly frozen (`CONDITIONALLY_BLOCKED`).
- **Next Action:** Await independent engineering review of the Stage 1 report and authorization for subsequent stages. Do not modify production code or test runners until authorized.
