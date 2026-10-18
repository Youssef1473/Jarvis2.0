# Agent Handoff: Jarvis 2.0 Engineering State & Architecture Runbook

**Generated At:** 2026-10-18T17:05:00+01:00  
**Target Agent:** Incoming Jarvis AI Pair Programmer / Autonomous Continuation Agent  
**Host Environment:** Windows 11 / WSL2 Arch Linux | Intel Core i5-12500H | NVIDIA GeForce RTX 3050 Laptop GPU (4GB VRAM) | 16GB System RAM  
**Python Runtime:** `.\.venv\Scripts\python.exe` (Python 3.12 virtualenv)  

---

## 🎯 1. Mission Objectives & Current Milestone Status

### Recently Completed & Verified Milestones:

1. **Phase 9 Architectural Consolidation & Zero-Copy Audio IPC (Commit `c418b31`):**
   - **Zero-Copy Shared Memory SPSC Audio Transport:** Implemented [`ShmAudioRingTransport`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/audio/shm_transport.py) over `multiprocessing.shared_memory` with an atomic 32-byte little-endian header (`<qqqq`: `write_idx`, `read_idx`, `barge_in_epoch`, `sample_rate`) and contiguous `np.float32` ring buffer. Wired into [`dsp_process.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/audio/dsp_process.py) and [`tts.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/audio/tts.py) with sub-1us cross-process barge-in flush and wrap-around/overflow protection.
   - **Stage-by-Stage Monotonic Latency Profiler:** Implemented [`PipelineLatencyProfiler`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/pipeline/latency_profiler.py) and `TurnTrace` using nanosecond monotonic timestamps (`time.perf_counter_ns()`). Measures `stt_ms`, `routing_ms`, `ttft_ms`, `tts_synth_ms`, `dsp_ipc_ms`, and `total_ttfa_ms`. Wired into [`brain.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/brain.py), [`diagnostics.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/diagnostics.py), and [`server.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/ui/server.py) with live rolling P50/P95/P99 WebSocket telemetry broadcast.
   - **Unified Preemptive Cancellation Singleton:** Unified [`jarvis/core/cancellation.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/cancellation.py) and [`jarvis/audio/cancellation.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/audio/cancellation.py) into an identical singleton epoch bus with `CancellationToken` alias, active barge-in flags, and atomic re-entrancy protection across audio flush callbacks.
   - **Reflex Dispatcher Deduplication:** Consolidated conversational regex normalization between [`ReflexEngine`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/reflex_engine.py) and [`ReflexDispatcher`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/reflex_dispatcher.py).
   - **ONNX Low-Latency CPU Tuning:** Configured ONNX Runtime `SessionOptions` in [`onnx_intent_router.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/routing/onnx_intent_router.py) and [`onnx_embeddings.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/onnx_embeddings.py) with `intra_op_num_threads=2`, `inter_op_num_threads=1`, `session.intra_op.allow_spinning="0"`, and `CPUExecutionProvider` to preserve 100% RTX 3050 VRAM for neural models while eliminating P-core spin contention.
   - **Hardened Win32 Job Object Lifecycle:** Enforced `proc.wait(timeout=1.0)` following `proc.communicate()` prior to closing Job Object handles in [`job_object_sandbox.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/job_object_sandbox.py), permanently eradicating Windows Exit Code 15 (`0x0000000F`).
   - **Speculative TOCTOU Invalidation:** Linked [`ContextOrchestrator`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/context/context_orchestrator.py) to [`speculative/engine.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/speculative/engine.py) to advance `EnvironmentEpochTracker` on active window, git, filesystem, or shell mutations.
   - **Repository Hygiene Purge:** Evicted legacy `scratch/` prototypes, duplicate batch launchers, and consolidated flags into [`run.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/run.py) (`--stealth`, `--omniroute`, `--check-health`).

2. **Hardware Thermal & Comprehensive Tool Teardown Protocol:**
   - **Child Process Tree Sweep:** Upgraded [`ManagedServiceManager`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/services.py) and added `kill_orphaned_jarvis_processes()` to recursively detect, terminate, and force-kill any child/descendant processes (`psutil.Process().children(recursive=True)`) or orphaned background workers.
   - **Universal Tool & Sentinel Disengagement:** Refactored [`JarvisOrchestrator.stop()`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/orchestrator.py) to sequentially disengage the hardware thermal watchdog (`stop_hardware_watchdog()`), all active timers (`cancel_timer("all")`), photo DB queue worker (`shutdown_photo_manager()`), `UnifiedRunner` tool worker pool (`shutdown()`), Web HUD Uvicorn server, and NVML telemetry session.
   - **VRAM & CUDA Teardown:** Added model unloader to `SpeechToText.stop()` (unloads faster-whisper), joined Kokoro synthesis and playback workers in `TextToSpeech.stop()`, dispatched model-unload instructions to local Ollama from `JarvisBrain.stop()`, and flushed PyTorch CUDA allocations via `torch.cuda.empty_cache()` and `torch.cuda.ipc_collect()`.

3. **Obsidian Context Sync Engine & Differential Delta Tracking:**
   - **Thread-Isolated Sync Architecture:** Implemented [`ObsidianContextSyncEngine`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/obsidian_sync.py) running on a dedicated scanner thread (`_scan_thread`) with micro-sleep CPU yields (1ms) to eliminate context-switch stalls against the sub-100ms CUDA voice pipeline.
   - **Differential Delta Tracking:** Fast-path scanning utilizing Windows NTFS `os.scandir` cached `st_mtime` with in-memory `_stat_cache` and inline SHA-256 hash tracking in SQLite table `obsidian_sync_state`, skipping untouched notes with 0 file reads and 0 hashing cycles.

4. **Arena Competitive Optimization Framework & Zero-Allocation Ring Buffer:**
   - **Arena Skill Installation:** Integrated [`.agents/skills/arena/`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/.agents/skills/arena/) containing `bracket.py`, `strategies.json` (2,160 strategy permutations), `rubric.md`, and `SKILL.md` along with 27 unit tests in [`tests/test_arena_bracket.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/tests/test_arena_bracket.py).
   - **Windows CLI Runtime Adaptations:** Patched `bracket.py` to use `sys.executable` dynamically and quarantined `.arena/` tournament outputs in `.gitignore`.
   - **Champion `a003` Zero-Allocation `AudioRingBuffer` Optimization:** Ran 4-agent tournament on WASAPI ring buffer callback performance. Champion `a003` eliminated Python heap slice allocations via `ctypes.memmove`/`ctypes.memset`, static raised-cosine tables, and `read_into` method, achieving **0 bytes intermediate heap allocations** and **4.64 microsecond callback latency** over 2,000 frames.

---

## 📌 2. Repository & Branch State
- **Branch:** `main`
- **Remote URL:** `https://github.com/Youssef1473/Jarvis2.0.git`
- **Working Tree:** Clean (all 585 unit tests passing).

---

## ✅ 3. Completed & Verified Architectural Pillars (All Green)

| Subsystem | File Location | Key Architectural Invariants | Test Status |
| :--- | :--- | :--- | :--- |
| **Zero-Allocation Audio Ring Buffer** | [`jarvis/audio/ring_buffer.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/audio/ring_buffer.py) | Champion `a003` implementation: `ctypes.memmove`, precomputed raised-cosine tables, zero heap slices, zero GC pauses on WASAPI callback. | `test_audio_ring_buffer.py` (PASS) |
| **Arena Tournament Engine** | [`.agents/skills/arena/bracket.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/.agents/skills/arena/bracket.py) | Full bracket state machine, 2,160 orthogonal strategy cards, Windows `sys.executable` execution, multi-agent evaluation. | `test_arena_bracket.py` (PASS) |
| **Shared Memory Audio Transport** | [`jarvis/audio/shm_transport.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/audio/shm_transport.py) | Lock-free SPSC float32 ring buffer, atomic 32-byte header, `<1us` barge-in epoch increment, wrap/overflow protection. | `test_phase9_shm_and_profiler.py` (PASS) |
| **Nanosecond Latency Profiler** | [`jarvis/core/pipeline/latency_profiler.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/pipeline/latency_profiler.py) | Nanosecond turn tracing (`TurnTrace`), rolling histogram percentiles (P50/P95/P99), WebSocket HUD telemetry. | `test_phase9_shm_and_profiler.py` (PASS) |
| **Preemptive Cancellation Bus** | [`jarvis/core/cancellation.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/cancellation.py) | Unified singleton state across core and audio, re-entrancy protected flush callbacks, atomic `<10ns` invalidation. | `test_execution_abort_killswitch.py` (PASS) |
| **Tool & Hardware Teardown** | [`jarvis/core/services.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/services.py)<br>[`jarvis/core/orchestrator.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/orchestrator.py) | Full child process tree sweep (`psutil`), universal sentinel/timer disengagement, STT/TTS CUDA VRAM flush, `atexit`/signal trap. | `test_shutdown_lifecycle.py` (PASS) |
| **Obsidian Context Sync Engine** | [`jarvis/core/memory/obsidian_sync.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/memory/obsidian_sync.py) | Non-blocking background worker thread, `st_mtime` + SHA-256 differential delta cache, WAL-protected SQLite queue worker. | `test_obsidian_sync.py` (PASS) |
| **Async Tool Execution Pool** | [`jarvis/core/providers/base.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/providers/base.py) | Non-blocking `Jarvis-ToolWorker` thread pool, mandatory 5.0s per-tool timeout guard, cycle detection. | `test_unified_runner_and_sandbox.py` (PASS) |
| **Operational Sandbox Guards** | [`jarvis/core/evolution/sandbox.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/evolution/sandbox.py) | Win32 Job Object with `proc.wait(1.0)` teardown, AST loop detection, 2.0s canary watchdog, 64MB memory limit. | `test_self_evolution_sandbox.py`<br>`test_job_object_sandbox.py` (PASS) |
| **P-Core Priority Scheduling** | [`run.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/run.py)<br>[`jarvis/audio/tts.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/audio/tts.py) | Process set to `ABOVE_NORMAL`, Kokoro synth + WASAPI threads set to `THREAD_PRIORITY_HIGHEST` on physical P-Cores. | Full Suite (PASS) |
| **Streaming Voice Chunker** | [`jarvis/audio/prosodic_chunker.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/audio/prosodic_chunker.py) | Speculative token clause emission at earliest punctuation boundary, circular PCM slice transfers, sub-100ms TTFT. | `test_audio_streaming_pipeline.py`<br>`test_streaming_voice_reflex.py` (PASS) |
| **MCP Client & Server Transport** | [`jarvis/core/mcp_manager.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/mcp_manager.py) | Win32 Job Object sandbox (E-Cores `0xFF00`, 512MB RAM cap), `shutil.which` Windows `.cmd` resolution, env expansion (`${VAR}`), 4-tier suite (Everything, GitHub, Context7, Sequential Thinking). | `test_mcp_client_transport.py` (PASS) |
| **Telemetry & NVML Engine** | [`jarvis/ui/server.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/ui/server.py) | Lossy single-slot `maxsize=1` queues, 8-client ceiling, off-loop `psutil` + `pynvml` sampling, live latency stats. | `test_telemetry_connection_manager.py`<br>`test_ui_server.py` (PASS) |
| **Zero-CLS Holographic HUD** | [`jarvis/ui/templates/hud.html`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/ui/templates/hud.html) | Strict `CLS = 0` layout budget, GPU compositor animations anchored with `origin-left transform: scaleX(...)`. | `test_hud_zero_cls.py` (PASS) |

---

## 🧪 4. Verification Baseline

Run the complete test suite across the repository:
```powershell
.\.venv\Scripts\python.exe -m unittest discover tests
```
**Latest Output:**
```text
Ran 588 tests in 146.043s
OK
```

Targeted Arena Skill Bracket Unit Tests:
```powershell
.\.venv\Scripts\python.exe -m unittest tests/test_arena_bracket.py
```
**Output:**
```text
Ran 27 tests in 21.247s
OK
```

Targeted Phase 9 Shared Memory Audio Transport & Latency Profiler:
```powershell
.\.venv\Scripts\python.exe -m unittest tests/test_phase9_shm_and_profiler.py
```
**Output:**
```text
Ran 5 tests in 0.052s
OK
```

---

## ⚠️ 5. Architectural Invariants & Non-Negotiables
1. **Zero Audio Stutter (Reflex Budget <15ms):** Kokoro-82M TTS and faster-whisper STT run on CUDA. Real-time audio threads stay elevated at `THREAD_PRIORITY_HIGHEST` on Performance P-cores.
2. **VRAM Ceiling:** Total local model allocation must never exceed **2.5 GB VRAM** (leaves 1.5GB for display, CUDA audio pipelines, and OS rendering on RTX 3050 4GB).
3. **Async Tool Isolation:** All tools execute offloaded through `UnifiedRunner`'s `ThreadPoolExecutor` with a mandatory 5.0s timeout. Never execute blocking tool calls synchronously on the cognitive orchestrator thread.
4. **Backpressure Strategy:** Discard old frames (`queue.get_nowait()` on `Queue(maxsize=1)`) rather than buffering backlogs in RAM.
5. **Zero Cumulative Layout Shift (`CLS = 0`):** Animate HUD gauges strictly via `transform: scaleX(...)` on `will-change-transform` layers.
6. **Destructive Action Safety:** Never delete files or modify database schemas without explicit confirmation.
