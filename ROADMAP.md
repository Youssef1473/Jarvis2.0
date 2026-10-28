# Hardening Roadmap & Milestone Status

## P0 — Close Confirmed Security Gaps (Completed & Verified)

1. [x] **PowerShell Sandboxing & SafetyGate:** Direct `execute_powershell()` gated with `SafetyGate`; verified with `BackgroundTaskManager` checks and non-elevated validation tests.
2. [x] **SSRF & Scheme Defense:** Fail-closed HTTP(S) URL validation across headless browser, deep-research, and CDP fetch paths. Rejects non-global addresses, internal schemes (`chrome://`, `brave://`), and arbitrary local file paths.
3. [x] **Path Traversal & Data Root Confinement:** Screenshot outputs and file writes confined to fixed data roots after canonical path resolution.
4. [x] **Structured Reference Escaping:** Untrusted source text escaped and wrapped in inert `<reference_data>` XML containers before entering prompt assemblies.
5. [x] **Visual Privacy Guard:** Zero-exposure policy automatically halts desktop screen perception when sensitive windows (banking, password managers, authenticators) are detected.
6. [x] **Execution-Boundary Security Authorization:** Enforced fail-closed authorization gate at direct `SkillRegistry.execute` boundary (`evaluate_tool_execution`), preventing unclassified mutating tool execution, tainted turn execution, and unseeded worker thread bypasses (`SEC-H2-02`).

## P1 — Lifecycle, Concurrency & Low-Latency Correctness (Completed & Verified)

1. [x] **Thread COM Lifecycle:** Paired COM initialization with `CoUninitialize()` in `finally` across watchdog, OS control, and audio workers.
2. [x] **Zero-Copy Shared Memory Audio IPC:** Implemented `ShmAudioRingTransport` over memory-mapped shared memory with atomic 32-byte header and sub-1us barge-in clear.
3. [x] **Nanosecond Turn Latency Profiler:** Implemented `PipelineLatencyProfiler` with P50/P95/P99 rolling window and live WebSocket telemetry broadcasting.
4. [x] **Preemptive Actuation Cancellation Bus:** Unified `jarvis.core.cancellation` and `jarvis.audio.cancellation` singletons with atomic re-entrancy protected flush callbacks.
5. [x] **Win32 Job Object Teardown:** Hardened subprocess lifecycle in `job_object_sandbox.py` with post-execution `proc.wait(timeout=1.0)` eliminating Exit Code 15 (`0x0000000F`).
6. [x] **ONNX Low-Latency CPU Tuning:** Configured `session.intra_op.allow_spinning="0"`, `intra_op_num_threads=2`, and `CPUExecutionProvider` to eliminate P-core spin contention.
7. [x] **Speculative TOCTOU Invalidation:** Linked `ContextOrchestrator` mutations to `speculative/engine.py` epoch advancing.
8. [x] **Zero-Allocation Audio Ring Buffer:** Champion `a003` optimization replacing slice allocations with `ctypes.memmove`, precomputed raised-cosine tables, and `read_into()` to guarantee 0 heap allocations during 100Hz WASAPI audio loops.
9. [x] **Streaming Parser Per-Call Buffer Isolation & Exactly-Once Dispatch:** Partitioned streaming token buffers by `call_id` in `StreamingToolCallParser` to eliminate state corruption across interleaved chunks, and eliminated duplicate callback dispatches when coordinator is attached.

## P2 — Integrations, Test Gates & Continuous Verification

1. [x] **MCP Client Transport Pool & Auto-Configuration:** Unified MCP JSON-RPC stdio transport and client pool under `jarvis/core/mcp_manager.py` with E-core Job Object sandboxing (`0xFF00`), Windows `shutil.which` resolution, env expansion (`${VAR}`), and 4-tier MCP suite (Everything, GitHub, Context7, Sequential Thinking).
2. [x] **Arena Competitive Optimization Framework:** Self-contained tournament state machine, 2,160 strategy permutations, Windows CLI execution, and 27 unit tests under `.agents/skills/arena/`.
3. [x] **Reproducible CI Environment & Test Stabilization:** Installed Playwright Chromium browser binary in CI workflow, mocked external HTTP/egress interactions in unit test suites, and hermetically isolated worker turn state across `pytest-xdist` runners.
4. [x] **Full Regression Baseline:** 1295+ unit tests passing with zero failures, zero errors, and clean isolation under `tools/run_fast_tests.py --fast`.
5. [x] **Continuous Real-Time Telemetry:** Implemented historical rolling P99 latency snapshots in `PipelineLatencyProfiler`, serialized through `WorkstationMetricsCollector` and `TelemetryConnectionManager`, with holographic canvas trendlines, dynamic Y-axis scaling, and disconnected/empty state handling in `hud.html`.
