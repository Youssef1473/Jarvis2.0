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

- **Validation Timestamp:** 2026-11-09T16:25:00+01:00 (CET)
- **Active Git Branch:** `fix/security-stabilization-p0`
- **Recorded HEAD Commit SHA:** `8c395d3f2aa32effb5236ea716a4cba0b4803f8b`
- **HEAD Commit Subject:** `chore: prepare independent engineering audit snapshot`
- **Milestone State:** **Phase 2 Security Stabilization: ACCEPTED & FROZEN (184/185 verified)**
- **Staged Modifications:** `0 files` (clean staging index)
- **Unstaged Tracked Modifications (17 files):**
  1. `docs/agent/PROJECT_STATE.md`
  2. `docs/agent/SESSION_LOG.md`
  3. `jarvis/core/security_boundaries.py`
  4. `jarvis/skills/base.py`
  5. `jarvis/skills/browser_cdp.py`
  6. `jarvis/skills/dev_shell.py`
  7. `jarvis/skills/email_manager.py`
  8. `jarvis/skills/job_object_sandbox.py`
  9. `jarvis/skills/os_control.py`
  10. `jarvis/skills/rstrtmgr_lock_surgeon.py`
  11. `jarvis/skills/system_maintenance.py`
  12. `tests/test_email_integration.py`
  13. `tests/test_flagship_features.py`
  14. `tests/test_job_object_sandbox.py`
  15. `tests/test_os_skills.py`
  16. `tests/test_p0_action_bound_authorization.py`
  17. `tests/test_tool_authorization_integrity.py`
- **Untracked Security Audit Suites & Artifacts:**
  1. `docs/agent/PHASE_2_FROZEN_SNAPSHOT_MANIFEST.md`
  2. `tests/test_filesystem_toctou_security_audit.py`
  3. `tests/test_gemini_dispatch_security_audit.py`
  4. `tests/test_p0_security_remediation.py`
  5. `tests/test_p1_stage6a_execution_security.py`
  6. `tests/test_p1_stage6b_addendum_security.py`
  7. `tests/test_p1_stage6b_mutation_security.py`
  8. `tests/test_p1_stage6c_approval_transparency.py`
  29. `jarvis/core/win32_affinity.py`
  30. `jarvis/skills/base.py`
  31. `jarvis/skills/windows_uia.py`
  32. `pytest.ini`
  33. `tests/test_antigravity_integration.py`
  34. `tests/test_antigravity_job_containment.py`
  35. `tests/test_audio_streaming_pipeline.py`
  36. `tests/test_background_tasks.py`
  37. `tests/test_bettany_voice_and_dsp.py`
  38. `tests/test_brain_rate_limit_resilience.py`
  39. `tests/test_browser_agent.py`
  40. `tests/test_browser_cdp.py`
  41. `tests/test_browser_network_boundaries.py`
  42. `tests/test_claude_token_exhaustion_fallback.py`
  43. `tests/test_closed_loop_and_core_preferences.py`
  44. `tests/test_cloud_streaming_latency.py`
  45. `tests/test_cognitive_twin_pipeline.py`
  46. `tests/test_council_batch_2.py`
  47. `tests/test_council_batch_3.py`
  48. `tests/test_curated_prompts_and_epistemic_gate.py`
  49. `tests/test_diagnostics.py`
  50. `tests/test_email_integration.py`
  51. `tests/test_episodic_reflection.py`
  52. `tests/test_everything_search_and_self_editing.py`
  53. `tests/test_flagship_features.py`
  54. `tests/test_gemini_session_reliability.py`
  55. `tests/test_github_copilot_bridge.py`
  56. `tests/test_hybrid_and_timers.py`
  57. `tests/test_interactive_persona.py`
  58. `tests/test_learned_habits.py`
  59. `tests/test_lint_tool.py`
  60. `tests/test_milestone4_cognitive_invariants.py`
  61. `tests/test_multi_llm_responsiveness.py`
  62. `tests/test_new_skills.py`
  63. `tests/test_oneshot_process_containment.py`
  64. `tests/test_os_skills.py`
  65. `tests/test_performance_pc_control.py`
  66. `tests/test_plugin_integrations.py`
  67. `tests/test_residual_security_boundaries.py`
  68. `tests/test_self_evolution.py`
  69. `tests/test_self_healing_and_shutdown_immunity.py`
  70. `tests/test_shutdown_lifecycle.py`
  71. `tests/test_voice_control_and_skills.py`
  72. `tests/test_windows_uia.py`
  73. `tools/run_fast_tests.py`
- **Exact Untracked File Inventory (158 files via git status --short --untracked-files=all):
  1. .agents/rules/audio_threading.md
  2. .agents/rules/persistence_sqlite.md
  3. .agents/rules/security_boundaries.md
  4. .agents/skills/agent-continuity/SKILL.md
  5. .agents/skills/agent-evaluation/SKILL.md
  6. data/agy_112_nodes.json
  7. data/agy_112_pytest_nodes.json
  8. data/execution_evidence/browser_capability_context_a.json
  9. data/execution_evidence/browser_capability_context_b.json
  10. data/execution_evidence/env_probe_context_a.json
  11. data/execution_evidence/env_probe_context_b.json
  12. data/execution_evidence/evidence_registry.db
  13. data/execution_evidence/stage2_group1_playwright_ssrf_junit.xml
  14. data/execution_evidence/stage2_group2_cdp_guards_junit.xml
  15. data/execution_evidence/stage2_resolver_regression_junit.xml
  16. data/execution_evidence/stage2_shim_regression_junit.xml
  17. data/learning_candidates/LCAND-060af81e3416.json
  18. data/learning_candidates/LCAND-1572b93ec9cc.json
  19. data/learning_candidates/LCAND-2d10293ac866.json
  20. data/learning_candidates/LCAND-480b6011ed6d.json
  21. data/learning_candidates/LCAND-4ad4da892e64.json
  22. data/learning_candidates/LCAND-b9b3a4380d77.json
  23. data/learning_candidates/LCAND-c42c5d28f6ba.json
  24. data/learning_candidates/candidates_registry.db
  25. data/learning_events/061fbc2e59c9ceebf1f11d89.json
  26. data/learning_events/20f3ee2a48c69f4598473840.json
  27. data/learning_events/78a29e18e72a7f5b67e09fbb.json
  28. data/learning_events/8a0d872346812c3ab2b8b7f5.json
  29. data/learning_events/8a327621fede38c2e56d44ab.json
  30. data/learning_events/b07f38879bb8559e7220307f.json
  31. data/learning_events/c1d96b1722129b50906a3723.json
  32. data/learning_events/cb2ec53a55a829b3cadc3fc2.json
  33. data/regression_scorecard.json
  34. data/supplemental_test_manifest.json
  35. data/test_classification_manifest.json
  36. docs/adr/ADR-001-decision-memo.md
  37. docs/adr/ADR-001-sqlite-single-writer-wal.md
  38. docs/adr/ADR-002-win32-job-object-containment.md
  39. docs/adr/ADR-003-epoch-based-audio-cancellation.md
  40. docs/adr/ADR-004-dual-key-tool-authorization-turn-taint.md
  41. docs/adr/ADR-005-win32-hardware-core-affinity.md
  42. docs/adr/ADR-006-egress-validation-ip-pinning.md
  43. docs/adr/README.md
  44. docs/agent/ENG_OS_03A1_PROVENANCE_AUDIT.md
  45. docs/agent/ENG_OS_03A2_PROVENANCE_REMEDIATION.md
  46. docs/agent/ENG_OS_03A3_TRUST_AUDIT.md
  47. docs/agent/ENG_OS_03A_EXECUTION_EVIDENCE.md
  48. docs/agent/ENG_OS_03B1_CRASH_CONSISTENCY_AUDIT.md
  49. docs/agent/ENG_OS_03B2R_REMEDIATION.md
  50. docs/agent/ENG_OS_03B2_AUTHORITATIVE_PERSISTENCE.md
  51. docs/agent/ENG_OS_03B3_FINAL_ACCEPTANCE_AUDIT.md
  52. docs/agent/ENG_OS_03B_PERSISTENCE_REMEDIATION.md
  53. docs/agent/ENG_OS_03C2R_RUNNER_DISPATCH_AUDIT.md
  54. docs/agent/ENG_OS_03C2_ACCEPTANCE_AUDIT.md
  55. docs/agent/ENG_OS_03C3R1_FINAL_RECONCILIATION.md
  56. docs/agent/ENG_OS_03C3R2_FAILURE_TAXONOMY_CLOSURE.md
  57. docs/agent/ENG_OS_03C3R_CLASSIFICATION_CLOSURE.md
  58. docs/agent/ENG_OS_03C3_SCORECARD_AUDIT.md
  59. docs/agent/ENG_OS_03C4R1_FINAL_ACCEPTANCE.md
  60. docs/agent/ENG_OS_03C4R2_CLOSURE.md
  61. docs/agent/ENG_OS_03C4R_ACCEPTANCE_AUDIT.md
  62. docs/agent/ENG_OS_03C4_TEST_HERMETICITY_REPORT.md
  63. docs/agent/ENG_OS_03C5_TTS_LIFECYCLE_REPORT.md
  64. docs/agent/ENG_OS_03C6R1_FINAL_HERMETICITY_ACCEPTANCE.md
  65. docs/agent/ENG_OS_03C6R2_FINAL_EXECUTION_ACCOUNTING.md
  66. docs/agent/ENG_OS_03C6R_CLASSIFICATION_INTEGRITY_REPORT.md
  67. docs/agent/ENG_OS_03C6_HERMETIC_TIER1_REPORT.md
  68. docs/agent/ENG_OS_03C7_PHASE1_EXECUTION_PROFILE.md
  69. docs/agent/ENG_OS_03C7_PHASE2_RECOVERY_REPORT.md
  70. docs/agent/ENG_OS_03C8_CONTEXT_B_COMPARISON_REPORT.md
  71. docs/agent/ENG_OS_03C8_PHASE1_ENVIRONMENT_CAPABILITY_REPORT.md
  72. docs/agent/ENG_OS_03C8_PHASE2_CONTROLLED_COMPARISON_AND_ARCHITECTURE.md
  73. docs/agent/ENG_OS_03C8_PHASE3_DUAL_PROFILE_ARCHITECTURE.md
  74. docs/agent/ENG_OS_03C8_STAGE1R_CDP_SECURITY_AUDIT.md
  75. docs/agent/ENG_OS_03C8_STAGE1_CHROMIUM_CAPABILITY_REPORT.md
  76. docs/agent/ENG_OS_03C8_STAGE2R_EXECUTION_PROVENANCE_AUDIT.md
  77. docs/agent/ENG_OS_03C8_STAGE2_BROWSER_TEST_ENABLEMENT_REPORT.md
  78. docs/agent/ENG_OS_03C9_PHASE_1P_AUTHORIZATION_SEMANTICS_AUDIT.md
  79. docs/agent/ENG_OS_03C9_PHASE_1Q_AUTHORIZATION_CLOSURE.md
  80. docs/agent/ENG_OS_03C9_PHASE_1R_SNAPSHOT_AND_RECOVERY_CLOSURE.md
  81. docs/agent/ENG_OS_03C9_PHASE_1S_VERIFICATION_TO_EXECUTION_AUDIT.md
  82. docs/agent/ENG_OS_03C9_PHASE_1T_B1_ISOLATION_ARCHITECTURE.md
  83. docs/agent/ENG_OS_03C9_PHASE_1U_BOOTSTRAP_LOCK_AND_TRUST_FREEZE.md
  84. docs/agent/ENG_OS_03C_FEASIBILITY_ASSESSMENT.md
  85. docs/agent/ENG_OS_03C_IMPLEMENTATION.md
  86. docs/agent/ENG_OS_03_FEASIBILITY_ASSESSMENT.md
  87. docs/agent/EVALUATION_GUIDE.md
  88. docs/agent/JARVIS_2_0_PHASE_1A_STARTUP_HARDENING.md
  89. docs/agent/JARVIS_2_0_PHASE_1B_PROVIDER_ROUTING.md
  90. docs/agent/JARVIS_2_0_PHASE_2_ACOUSTIC_LOOP.md
  91. docs/agent/JARVIS_2_0_PHASE_3_END_TO_END_INTERACTION.md
  92. docs/agent/JARVIS_2_0_PRODUCT_READINESS_ASSESSMENT.md
  93. docs/agent/LESSONS.md
  94. docs/agent/PHASE_3D2A_INDEPENDENT_AUDIT.md
  95. docs/agent/PHASE_3D2B_REMEDIATION.md
  96. docs/agent/PHASE_3D2C_INDEPENDENT_ACCEPTANCE_AUDIT.md
  97. docs/agent/PHASE_3D2D_REMEDIATION.md
  98. docs/agent/PHASE_3D2E_FINAL_CLOSURE_AUDIT.md
  99. docs/agent/PHASE_3D2_ASSESSMENT.md
  100. docs/agent/PROJECT_STATE.md
  101. docs/agent/SESSION_LOG.md
  102. docs/architecture/INDEX.md
  103. docs/architecture/SQLITE_WRITER_OWNERSHIP_DESIGN.md
  104. docs/handoffs/historical/2026-10-29-PR5-appcontainer-sandbox.md
  105. docs/milestones/MILESTONES.md
  106. docs/reports/benchmark_sqlite_wal_results.json
  107. docs/reports/benchmark_sqlite_wal_serialization.md
  108. docs/reports/benchmark_sqlite_wal_validation_round.json
  109. docs/reports/benchmark_sqlite_wal_validation_round.md
  110. docs/reports/sqlite_writer_ownership_audit.md
  111. tests/eval/eval_holdout_registry.md
  112. tests/eval/eval_retrieval_holdout.json
  113. tests/eval/eval_retrieval_scenarios.json
  114. tests/eval/eval_scenarios.json
  115. tests/test_acoustic_loop.py
  116. tests/test_antigravity_executable_resolver.py
  117. tests/test_browser_executable_resolver.py
  118. tests/test_context_router.py
  119. tests/test_continuity_and_lifecycle.py
  120. tests/test_execution_evidence.py
  121. tests/test_failure_collector.py
  122. tests/test_four_tier_runner.py
  123. tests/test_hook_integration.py
  124. tests/test_lesson_candidate.py
  125. tests/test_memory_payload_minimization.py
  126. tests/test_persistence_concurrency.py
  127. tests/test_provider_routing_failover.py
  128. tests/test_pytest_fallback_shim.py
  129. tools/benchmark_context_router.py
  130. tools/benchmark_failure_collector.py
  131. tools/benchmark_sqlite_wal_serialization.py
  132. tools/benchmark_sqlite_wal_validation.py
  133. tools/context_router.py
  134. tools/continuity_manager.py
  135. tools/evaluate_lesson_retrieval.py
  136. tools/execution_evidence.py
  137. tools/execution_profile_dispatcher.py
  138. tools/failure_collector.py
  139. tools/lesson_candidate.py
  140. tools/probe_audio_devices.py
  141. tools/probe_chromium_capabilities.py
  142. tools/probe_environment_capabilities.py
  143. tools/probe_overlapped_named_pipe.py
  144. tools/probe_peek_named_pipe.py
  145. tools/probe_token_info.py
  146. tools/snapshot_test_supervisor.py
  147. tools/test_classifier.py
  148. tools/test_drainer_remediation.py
  149. tools/test_permit_authorization.py
  150. tools/test_profile_isolation.py
  151. tools/test_snapshot_integrity.py
  152. tools/test_supervisor.py
  153. tools/test_worker_bootstrap.py
  154. tools/validate_context_b_gates.py
  155. tools/verify_acoustic_loop.py
  156. tools/verify_consecutive_turns.py
  157. tools/verify_matrix_live.py
  158. tools/verify_runner_dispatch.py

## 2. Current Engineering Focus

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
