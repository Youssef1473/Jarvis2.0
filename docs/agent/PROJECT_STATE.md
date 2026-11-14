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

- **Validation Timestamp:** 2026-11-14T09:50:00+01:00 (CET)
- **Active Git Branch:** `feat/reliability-engineering-governance`
- **Recorded HEAD Commit SHA:** `7b09bd2fe0ed61e029935a702896d4832ddb50e7`
- **HEAD Commit Subject:** `refactor(orchestrator): extract speech capture, lifecycle shutdown, and intent acknowledgments`
- **Milestone State:** **Core Triad (brain.py, server.py, orchestrator.py) Untangle-Codebase: COMPLETED & SEALED**
- **Active Milestone:** **Reliability & Engineering Governance (Branch Merge-Readiness Review)**
- **Staged Modifications:** `0 files` (clean staging index)
- **Unstaged Tracked Modifications (0 files):**
  - None (clean tracked working tree)

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

- **Active Milestone:** `Reliability & Engineering Governance` (`feat/reliability-engineering-governance`)
- **Completed Work:**
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
2. **Six Tracked F821 Undefined Name Diagnostics:**
   - 5 occurrences in `jarvis/core/memory/temporal_graph.py` (lines 44, 107, 136, 194, 226): type annotations `conn: sqlite3.Connection` missing `import sqlite3`.
   - 1 occurrence in `jarvis/desktop/uia_delta_worker.py` (line 45): `Optional["UIADeltaWorker"]` missing `from typing import Optional`.
   - Status: Documented in Technical Findings; resolution deferred to an authorized codebase hygiene pass.
3. **SQLite Writer Ownership vs. Reader Connection Debt:**
   - Direct Write Bypass Paths (3 modules): `photo_manager.py` (3 connections), `reflex_macro_compiler.py` (6 connections), and `sleep_consolidator.py` (1 batch write connection with `uri=True`) open uncoordinated direct connections and execute write transactions bypassing `SingleWriterActor`.
   - Direct Read Connection Ownership (1 module): `habit_graph.py` opens direct connection for reads, while writes serialize properly through `SingleWriterActor`. Under SQLite WAL mode, readers do not block writers; direct reads are technically valid under WAL concurrency, though connection pooling would be architectural best practice.
   - Status: Migration design fully specified in [`docs/architecture/SQLITE_WRITER_OWNERSHIP_DESIGN.md`](../architecture/SQLITE_WRITER_OWNERSHIP_DESIGN.md); implementation deferred until authorized.
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
