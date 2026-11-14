# ENG-OS-03C3R2 — Exclusive Failure Taxonomy and Timeout Evidence Closure

**Audit ID:** ENG-OS-03C3R2  
**Date:** 2026-11-06 (Updated per ENG-OS-03C3R2-C)  
**Auditor:** Principal Test Infrastructure Architect & Reliability Engineer  
**Mode:** READ-ONLY EVIDENCE RECONCILIATION & CLOSURE  
**Scope:** Strictly Read-Only (Zero modifications to production source code, test suites, test classifier, supervisor, hooks, or databases)  
**Repository:** `C:/Users/Youssef/Projects/Jarvis_2.0`  
**Git Baseline:** Branch `feat/engineering-maturity-milestone` | HEAD `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Authoritative Artifacts:** `data/pytest_tmp/**/junit.xml`, `data/test_classification_manifest.json`, `data/regression_scorecard.json`

---

## 1. Executive Summary & Machine-Checkable Invariants

### Final Verdict: ACCEPTED (Evidence-Accurate Taxonomy & Inventory Closure)

This document establishes an internally consistent, evidence-accurate accounting of the repository's 2,198-test inventory. Every failed test node has a unique node identity, an explicit primary root cause, and verified secondary environmental factors. Crucially, in strict adherence to empirical evidence, the 54 nodes belonging to the three suites terminated by supervisor watchdogs are **not** described as individually proven timeouts; they are accounted for under the evidence-accurate classification `EXECUTION_INCOMPLETE_NODE_OUTCOME_UNKNOWN`.

### The Three Foundational Repository Invariants

1. **Failure Inventory Invariant:**

   $$\mathbf{29} \text{ (AppContainer-Related)} + \mathbf{112} \text{ (Host agy.exe Path Boundary)} + \mathbf{18} \text{ (Loopback Network Boundary)} + \mathbf{3} \text{ (Host Filesystem Boundaries)} + \mathbf{20} \text{ (Residual Assertions / Behavioral / Environment / Timing)} = \mathbf{182} \text{ Unique Failed Nodes}$$

2. **Interrupted Execution / Timeout Scope Invariant:**

   - **Confirmed File-Level Watchdog Timeouts:** **3 test files** (1 in Tier 3, 2 in Tier 4).

   - **Nodes Selected in Interrupted Files:** **54 manifest nodes** (45 in `test_job_object_sandbox_hardening.py`, 6 in `test_onnx_embeddings.py`, 3 in `test_semantic_endpointing.py`).

   - **Individual Test Outcomes for Those Nodes:** **`UNKNOWN`** (process terminated before JUnit XML generation).

   - **Individually Confirmed Timed-Out Test Nodes:** **`UNKNOWN`**.

   - **Accounting Classification:** **`EXECUTION_INCOMPLETE_NODE_OUTCOME_UNKNOWN`** (**54 nodes**).

   $$\mathbf{45} \text{ (Job Object Hardening)} + \mathbf{6} \text{ (ONNX Embeddings)} + \mathbf{3} \text{ (Semantic Endpointing)} = \mathbf{54} \text{ Incomplete Nodes across 3 Files}$$

3. **Observed Execution Inventory Invariant:**

   $$\mathbf{1,495} \text{ Passed} + \mathbf{182} \text{ Failed} + \mathbf{6} \text{ Skipped} + \mathbf{461} \text{ Environment Blocked} + \mathbf{54} \text{ Incomplete / Outcome Unknown} = \mathbf{2,198} \text{ Total Selected Nodes}$$


### Definitive Performance Metrics

- **Total Accounting Reconciliation Rate:** **100.0%** ($2,198 / 2,198$ selected nodes mapped with zero duplicates or omissions).

- **Overall Inventory Pass Rate:** **68.02%**  

  $$\text{Overall Pass Rate} = \frac{\text{Passed}}{\text{Total Selected}} = \frac{1,495}{2,198} \approx 68.02\%$$

- **Evaluated Test Pass Rate:** **88.83%**  

  $$\text{Evaluated Pass Rate} = \frac{\text{Passed}}{\text{Passed} + \text{Failed} + \text{Skipped}} = \frac{1,495}{1,495 + 182 + 6} = \frac{1,495}{1,683} \approx 88.83\%$$

  *(Scope: Evaluated among test nodes with verified terminal execution outcomes)*


---

## 2. Reconciliation of AppContainer and Filesystem Boundaries

### 2.1 Renaming to `AppContainer-Related Test Failures` (29 Nodes)

In strict accordance with evidence, the 29-node category is designated **`AppContainer-Related Test Failures`** (all originating from `tests/test_appcontainer_sandbox.py`). It preserves three distinct, mutually exclusive failure subtypes:

1. **`CreateProcessW` AppContainer Token Restrictions (27 nodes):** Windows rejects nested AppContainer creation within restricted tokens (`RuntimeError: SandboxedProcess: AppContainer launch failed: [WinError 87] The parameter is incorrect`).

2. **Profile SID Assertion Mismatch (1 node):** `TestAppContainerProfileLifecycle::test_profile_creation_reuse_and_cleanup` fails with `AssertionError: 'S-1-15-2-4219...' != 'S-1-15-2-1612...'`.

3. **Filesystem Access Failure Before AppContainer Launch (1 node):** `TestAppContainerExecutionAndContainment::test_execute_sandboxed_custom_skill_with_appcontainer` fails during skill promotion HMAC writing to `C:\Users\Youssef\.jarvis_sec\evolution_hmac.dat` with `[WinError 5] Access is denied`.


### 2.2 Disjoint Boundary Assignment (Zero Double-Counting)

Because `test_execute_sandboxed_custom_skill_with_appcontainer` belongs to `tests/test_appcontainer_sandbox.py`, it is assigned to **`AppContainer-Related Test Failures`** with a secondary diagnostic note (*Skill promotion failed before AppContainer launch due to host .jarvis_sec access denial*). It is **strictly excluded** from Category 4 (Host Filesystem Boundaries).


### 2.3 Explicit Classification of `test_lint_tool.py` and the 3 Host Filesystem Nodes

Category 4 contains exactly **3 nodes** representing host filesystem access restrictions completely unrelated to AppContainer execution:

1. `tests/test_lint_tool.py::TestLintToolResolution::test_resolve_untrusted_env_var_falls_through_to_next_tier`: Tests rejected `VIRTUAL_ENV` fallbacks. It sets `os.environ["VIRTUAL_ENV"] = "C:\\untrusted\\env"`. When resolving ruff, `os.path.exists("C:\\untrusted\\env\\Scripts\\ruff.exe")` triggers `PermissionError: [Errno 13] Permission denied` (POSIX style). Explicitly classified under **Category 4: Host Filesystem Boundary Isolation**.

2. `tests/test_flagship_features.py::test_project_manager`: Fails on unmocked traversal to `~/Downloads` (`[WinError 5] Access is denied`).

3. `tests/test_self_evolution.py::TestSelfEvolution::test_dynamic_skill_creation_and_hot_reload`: Fails creating drive root `C:\.jarvis_sec` (`[WinError 5] Access is denied`).


---

## 3. Reconciliation of the 20-Node Residual Category

### 3.1 Composite Breakdown of the 20 Residual Nodes

The 20 residual nodes do **not** represent uniform product bugs. Rather, they comprise a heterogeneous mixture of test assertion deviations, environmental OS/API restrictions, external DNS failures, clipboard contention, audio behavior, and infrastructure timing:

- **11 SSRF Test Assertion Deviations (`tests/test_browser_agent.py`):** The tests assert specific HTTP redirect or error message strings (e.g. `'169.254.169.254'`, `'Redirect loop detected'`). In the offline sandbox, DNS resolution fails first, raising `SecurityBoundaryViolation: DNS resolution failed`, which causes the string assertion to fail (`AssertionError`). Nature: `assertion-related`.

- **2 External DNS Resolution Aborts (`tests/test_plugin_integrations.py`, `tests/test_new_skills.py`):** Unmocked live network requests to `gemini-api-docs` and `example.com` aborted by the SSRF security boundary. Nature: `assertion-related`.

- **3 Win32 Restart Manager API Permission Restrictions (`tests/test_rstrtmgr_lock_surgeon.py`):** `rstrtmgr.RmStartSession` returns Win32 error 29 (`The system cannot write to the specified device` due to RPC/service restrictions in sandbox), and `inspect_port_holders` fails with `Failed to query TCP table size`. The production code safely catches this and returns `{"status": "error"}`. The unit test asserts `status == "unlocked"`, raising `AssertionError`. Nature: `environmental`.

- **2 TTS Streaming Session Restart Logic Defects (`tests/test_audio_streaming_pipeline.py`):** `start_streaming_session()` returns `False` during clean restart after worker join. Nature: `behavioral` (genuine product defect).

- **1 Windows Clipboard Lock Contention (`tests/test_os_skills.py`):** `test_clipboard_assistant` fails because an active host desktop application held the exclusive Windows clipboard lock (`OpenClipboard: Access is denied`). Nature: `environmental`.

- **1 Supervisor Output Drain Timing Flake (`tests/test_four_tier_runner.py`):** `test_timeout_during_rapid_output_generation` hit `AssertionError: 0 not greater than 0` under 4-worker parallel xdist CPU load. Nature: `infrastructure-related`.

$$\mathbf{11} + \mathbf{2} + \mathbf{3} + \mathbf{2} + \mathbf{1} + \mathbf{1} = \mathbf{20} \text{ Unique Nodes}$$


---

## 4. Corrected Timeout Scope & Interrupted Execution Evidence

### 4.1 Strict Distinction: Watchdog Timeouts vs Individual Test Outcomes

In `data/regression_scorecard.json`, the supervisor recorded:

- Tier 3: `1 file (test_job_object_sandbox_hardening) timed out at 30s due to uvicorn/asyncio.`

- Tier 4: `2 files (test_onnx_embeddings, test_semantic_endpointing) timed out at 30s due to asyncio.run().`


Because pytest was executed at the file level and terminated via Win32 Job Object upon reaching the 30-second watchdog deadline, execution was interrupted before pytest could serialize `<testcase>` records to JUnit XML:

1. **Confirmed Watchdog Timeouts:** Exactly **3 test files** timed out at the supervisor process boundary.

2. **Manifest Node Selection:** The 3 interrupted files contain exactly **54 selected test nodes** in `data/test_classification_manifest.json`.

3. **Individual Node Outcomes:** **`UNKNOWN`**. It cannot be empirically proven which specific node was executing when the watchdog fired, nor whether prior nodes in those files completed in memory before termination.

4. **Individually Confirmed Timed-Out Test Nodes:** **`UNKNOWN`** (0 confirmed at the individual node level).

5. **Accounting Label:** These 54 nodes are classified as **`EXECUTION_INCOMPLETE_NODE_OUTCOME_UNKNOWN`**. They must **not** be counted as confirmed individual test timeouts.


### 4.2 Complete Manifest Inventory of the 54 Incomplete Nodes

#### Tier 3: `tests/test_job_object_sandbox_hardening.py` (45 Nodes — Outcome Unknown)

1. `tests/test_job_object_sandbox_hardening.py::TestExecutePowershellRouting::test_cancelled_and_error_paths`
2. `tests/test_job_object_sandbox_hardening.py::TestExecutePowershellRouting::test_empty_output`
3. `tests/test_job_object_sandbox_hardening.py::TestExecutePowershellRouting::test_nonzero_exit_still_returns_stdout_and_stderr`
4. `tests/test_job_object_sandbox_hardening.py::TestExecutePowershellRouting::test_routes_through_job_object_with_expected_arguments`
5. `tests/test_job_object_sandbox_hardening.py::TestExecutePowershellRouting::test_timeout_message_is_unchanged`
6. `tests/test_job_object_sandbox_hardening.py::TestExecutePowershellRouting::test_tool_still_registered`
7. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_assignment_failure_fails_closed`
8. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_cancel_token_kills_job_promptly`
9. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_failed_exit_code`
10. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_orphan_after_normal_exit_is_reaped`
11. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_pre_cancelled_token_never_spawns`
12. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_resume_failure_terminates_job`
13. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_spawn_failure_is_reported_not_raised`
14. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_spawn_then_assign_then_resume`
15. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_struct_layouts_match_x64_win32`
16. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_tail_chars_limit`
17. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_timeout_kills_whole_tree_and_verifies_drain`
18. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_undrained_job_is_reported_not_hidden`
19. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxContainment::test_zero_affinity_disables_pinning`
20. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_access_log_redaction_oversized_query_beginning_and_end`
21. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_access_log_redaction_percent_encoded_parameters`
22. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_access_log_redaction_preserves_legitimate_query_params`
23. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_access_log_redaction_queries_exceeding_10mb_failsafe`
24. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_access_log_redaction_timing_at_response_start`
25. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_actual_uvicorn_access_log_output_captures_and_redacts`
26. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_background_task_cancellation_kills_grandchild`
27. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_background_task_failed_assignment_kills_child_fail_closed`
28. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_background_task_failed_job_creation_kills_child_fail_closed`
29. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_cancellation_kills_child_and_grandchild`
30. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_concurrent_independent_jobs`
31. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_end_to_end_live_uvicorn_percent_encoded_access_log_redaction` *(Proposed Tier 2B Override Node)*
32. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_execute_powershell_timeout_leaves_no_children`
33. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_failed_assignment_kills_child_before_running`
34. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_job_object_unavailable_fallback_in_dev_shell`
35. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_normal_completion_with_pipe_holding_grandchild`
36. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_orphan_after_exit_is_reaped`
37. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_pre_cancelled_token_never_spawns`
38. `tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_timeout_kills_grandchild_and_drains`
39. `tests/test_job_object_sandbox_hardening.py::TestRunProjectTestsContainment::test_does_not_fallback_for_unrelated_status`
40. `tests/test_job_object_sandbox_hardening.py::TestRunProjectTestsContainment::test_non_windows_fallback_behavior_preserved`
41. `tests/test_job_object_sandbox_hardening.py::TestRunProjectTestsContainment::test_tool_schema_excludes_uncontained_override`
42. `tests/test_job_object_sandbox_hardening.py::TestRunProjectTestsContainment::test_untrusted_model_tool_call_cannot_activate_containment_bypass`
43. `tests/test_job_object_sandbox_hardening.py::TestRunProjectTestsContainment::test_windows_explicit_opt_in_arg_allows_fallback_with_warning`
44. `tests/test_job_object_sandbox_hardening.py::TestRunProjectTestsContainment::test_windows_explicit_opt_in_env_allows_fallback_with_warning`
45. `tests/test_job_object_sandbox_hardening.py::TestRunProjectTestsContainment::test_windows_fails_closed_on_containment_failure_by_default`


#### Tier 4: `tests/test_onnx_embeddings.py` (6 Nodes — Outcome Unknown)

1. `tests/test_onnx_embeddings.py::TestOnnxEmbeddingEngine::test_asymmetric_query_prefix`
2. `tests/test_onnx_embeddings.py::TestOnnxEmbeddingEngine::test_async_embedding_event_loop` *(Proposed Tier 2A Override Node)*
3. `tests/test_onnx_embeddings.py::TestOnnxEmbeddingEngine::test_batch_encoding_and_determinism`
4. `tests/test_onnx_embeddings.py::TestOnnxEmbeddingEngine::test_native_384_dimension_and_unit_norm`
5. `tests/test_onnx_embeddings.py::TestOnnxEmbeddingEngine::test_offline_fallback_resilience`
6. `tests/test_onnx_embeddings.py::TestOnnxEmbeddingEngine::test_semantic_synonym_similarity_vs_orthogonal`


#### Tier 4: `tests/test_semantic_endpointing.py` (3 Nodes — Outcome Unknown)

1. `tests/test_semantic_endpointing.py::test_adaptive_tripwire_trigger` *(Proposed Tier 2A Override Node)*
2. `tests/test_semantic_endpointing.py::test_lock_free_conflation`
3. `tests/test_semantic_endpointing.py::test_syntactic_threshold_modulation`


### 4.3 Async Overrides & Execution Feasibility

The proposed Tier 2A overrides are retained:

- `tests/test_onnx_embeddings.py::TestOnnxEmbeddingEngine::test_async_embedding_event_loop` → **Tier 2A**

- `tests/test_semantic_endpointing.py::test_adaptive_tripwire_trigger` → **Tier 2A**


The remaining 5 ONNX nodes and 2 semantic nodes are structurally synchronous tensor and ring buffer tests that do not invoke `asyncio.run()`. They are **potentially independently executable** after node-level separation from the blocking async tests. However, in strict adherence to epistemic truthfulness: **hermetic execution has not been proven without an actual isolated run**, as they were interrupted by file-level timeouts in the historical test execution.


---

## 5. Clarification of `tests/test_cloud_streaming_latency.py` Status

- **Intended Architectural Tier:** **Tier 1 (Hermetic Unit Candidate)** (Contains zero async primitives and zero loopback networking).

- **Current Observed Environmental Dependency:** `AntigravityBridge._find_agy_executable()` scans host path `C:\Users\Youssef\AppData\Local\agy\bin\agy.exe` during `JarvisBrain.__init__` in `setUp()`.

- **Current Observed Test Outcome:** `FAILED (errors=4)` in 0.527s with `PermissionError: [WinError 5] Access is denied`.

- **Required Isolation Work:** Mock `AntigravityBridge` in `setUp()` or guard `candidate.exists()`.

- **Hermeticity Status:** **NOT YET ACHIEVED IN LIVE EXECUTION.** While structurally synchronous, it cannot execute cleanly in sandbox without mocking the host binary discovery. Preserved unchanged in this audit.


---

## 6. The Authoritative 182-Node Failure Inventory Table

Every single one of the 182 failed test nodes has exactly one row, a unique node ID, an explicit primary cause, and verified evidence:


| # | Original Tier | Test File | Fully Qualified Pytest Node ID | Exception / Assertion Type | Primary Failure Cause & Secondary Factors | Nature |

| :--- | :---: | :--- | :--- | :--- | :--- | :---: |

| 1 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_antigravity_batch_generation` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 2 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_antigravity_batch_generation_mocked` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 3 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_antigravity_batch_generation_with_null_daemon` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 4 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_antigravity_bridge_fallback_when_daemon_fails` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 5 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_antigravity_daemon_adaptive_inactivity_watchdog` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 6 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_antigravity_daemon_lifecycle_mocked` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 7 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_antigravity_streaming_clause_extraction` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 8 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_brain_antigravity_provider_execution` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 9 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_brain_antigravity_provider_execution_mocked` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 10 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_bridge_binary_discovery_mocked` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 11 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_bridge_discovery_and_model_resolution` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 12 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_cascade_pool_building` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 13 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_code_task_momentum_multi_turn` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 14 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_hybrid_token_shield_routing` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 15 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_prompt_complexity_auto_tiering` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 16 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_specialist_consultation_routing` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 17 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_specialist_consultation_routing_mocked` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 18 | Tier 1 | `test_antigravity_integration.py` | `tests.test_antigravity_integration.TestAntigravityIntegration::test_voice_model_switching_commands` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 19 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerACLOwnership::test_preexisting_deny_ace_fails_closed` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 20 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerACLOwnership::test_preexisting_inadequate_readonly_ace_fails_closed` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 21 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerACLOwnership::test_reused_profile_preserves_preexisting_acl_grants` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 22 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerACLOwnership::test_temporary_profile_revokes_own_acl_grants` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 23 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerCapabilities::test_valid_capability_resolves_and_launches` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 24 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerCommunicator::test_communicate_child_closes_stdin_early_preserves_broken_pipe` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 25 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerCommunicator::test_communicate_failed_write_surfaces_error` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 26 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerCommunicator::test_communicate_partial_write_surfaces_error` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 27 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerExecutionAndContainment::test_all_standard_handles_valid_with_stdin_only_pipe_and_devnull` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 28 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerExecutionAndContainment::test_attribute_list_backing_lifetimes_and_single_cleanup` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 29 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerExecutionAndContainment::test_communicate_input_none_sends_eof_and_large_input_no_deadlock` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 30 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerExecutionAndContainment::test_cwd_outside_approved_grant_list_never_receives_acl_grant` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 31 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerExecutionAndContainment::test_execute_sandboxed_custom_skill_with_appcontainer` | `AssertionError` | **AppContainer-Related (.jarvis_sec HMAC write)**: Host .jarvis_sec HMAC signature write blocked outside workspace *(Secondary: Skill promotion failed before AppContainer launch)* | `environmental` |
| 32 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerExecutionAndContainment::test_live_windows_containment_denied_access` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 33 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerExecutionAndContainment::test_real_process_tree_termination_with_descendant_in_appcontainer` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 34 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerExecutionAndContainment::test_sandboxed_batch_stdin_stdout_pipeline` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 35 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerExecutionAndContainment::test_timeout_and_termination_in_appcontainer` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 36 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerFailClosedSecurity::test_fail_closed_on_job_assignment_failure` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 37 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerProfileLifecycle::test_existing_profile_reuse_does_not_delete_shared_profile` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 38 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerProfileLifecycle::test_profile_creation_reuse_and_cleanup` | `AssertionError` | **AppContainer-Related (Profile SID Mismatch)**: AppContainer profile SID string mismatch assertion *(Secondary: None)* | `assertion-related` |
| 39 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerTeardownLifecycle::test_sandboxed_process_reused_profile_preserves_preexisting_profile_and_acls` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 40 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerTeardownLifecycle::test_sandboxed_process_terminate_tree_invokes_wrapper_cleanup_exactly_once` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 41 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerTeardownLifecycle::test_sandboxed_process_timeout_invokes_wrapper_cleanup_cleanly` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 42 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerTerminationTruthfulness::test_job_handle_holder_query_failure_returns_none_not_zero` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 43 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerTerminationTruthfulness::test_terminate_tree_propagates_unsuccessful_drain_result` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 44 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerTerminationTruthfulness::test_termination_get_exit_code_failed_does_not_fabricate_returncode` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 45 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerTerminationTruthfulness::test_termination_still_active_reported_does_not_fabricate_returncode` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 46 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerTerminationTruthfulness::test_termination_wait_failed_does_not_fabricate_returncode` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 47 | Tier 1 | `test_appcontainer_sandbox.py` | `tests.test_appcontainer_sandbox.TestAppContainerTerminationTruthfulness::test_termination_wait_timeout_does_not_fabricate_returncode` | `AssertionError` | **AppContainer-Related (CreateProcessW WinError 87)**: Nested AppContainer token creation forbidden in restricted token *(Secondary: CreateProcessW PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)* | `environmental` |
| 48 | Tier 1 | `test_brain_rate_limit_resilience.py` | `tests.test_brain_rate_limit_resilience::test_deterministic_fallback_when_all_llms_fail` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 49 | Tier 1 | `test_brain_rate_limit_resilience.py` | `tests.test_brain_rate_limit_resilience::test_groq_candidate_models` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 50 | Tier 1 | `test_brain_rate_limit_resilience.py` | `tests.test_brain_rate_limit_resilience::test_post_tool_synthesis_429_resilience` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 51 | Tier 1 | `test_browser_agent.py` | `tests.test_browser_agent.TestBrowserAgentSSRFSuite::test_browse_and_read_webpage_and_screenshot_enforce_service_workers_block` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 52 | Tier 1 | `test_browser_agent.py` | `tests.test_browser_agent.TestBrowserAgentSSRFSuite::test_browse_and_read_webpage_fallback_successful_extraction` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 53 | Tier 1 | `test_browser_agent.py` | `tests.test_browser_agent.TestBrowserAgentSSRFSuite::test_browse_and_read_webpage_fallback_uses_hardened_opener` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 54 | Tier 1 | `test_browser_agent.py` | `tests.test_browser_agent.TestBrowserAgentSSRFSuite::test_browse_and_read_webpage_playwright_reports_ssrf_on_redirect` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 55 | Tier 1 | `test_browser_agent.py` | `tests.test_browser_agent.TestBrowserAgentSSRFSuite::test_capture_webpage_screenshot_navigates_to_safe_url` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 56 | Tier 1 | `test_browser_agent.py` | `tests.test_browser_agent.TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_aborts_multi_hop_redirect_chain_to_private` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 57 | Tier 1 | `test_browser_agent.py` | `tests.test_browser_agent.TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_blocks_https_to_http_downgrade` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 58 | Tier 1 | `test_browser_agent.py` | `tests.test_browser_agent.TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_fulfills_safe_public_response` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 59 | Tier 1 | `test_browser_agent.py` | `tests.test_browser_agent.TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_hop_limit_exceeded` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 60 | Tier 1 | `test_browser_agent.py` | `tests.test_browser_agent.TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_redirect_loop_detection` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 61 | Tier 1 | `test_browser_agent.py` | `tests.test_browser_agent.TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_relative_location_resolution` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 62 | Tier 1 | `test_browser_network_boundaries.py` | `tests.test_browser_network_boundaries.TestBrowserNetworkBoundaries::test_cdp_navigation_installs_network_guards` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 63 | Tier 1 | `test_browser_network_boundaries.py` | `tests.test_browser_network_boundaries.TestBrowserNetworkBoundaries::test_cdp_new_tab_guard_failure_fails_closed` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 64 | Tier 1 | `test_browser_network_boundaries.py` | `tests.test_browser_network_boundaries.TestBrowserNetworkBoundaries::test_existing_tab_guard_failure_aborts_navigation` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 65 | Tier 1 | `test_browser_network_boundaries.py` | `tests.test_browser_network_boundaries.TestBrowserNetworkBoundaries::test_head_method_rejected_falls_back_to_get_cleanly` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 66 | Tier 1 | `test_browser_network_boundaries.py` | `tests.test_browser_network_boundaries.TestBrowserNetworkBoundaries::test_legitimate_local_dev_redirect_allowed` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 67 | Tier 1 | `test_browser_network_boundaries.py` | `tests.test_browser_network_boundaries.TestBrowserNetworkBoundaries::test_max_hop_limit_exceeded_aborted` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 68 | Tier 1 | `test_browser_network_boundaries.py` | `tests.test_browser_network_boundaries.TestBrowserNetworkBoundaries::test_os_control_open_browser_tab_blocks_redirect_to_disallowed` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 69 | Tier 1 | `test_browser_network_boundaries.py` | `tests.test_browser_network_boundaries.TestBrowserNetworkBoundaries::test_redirect_loop_detected_and_aborted` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 70 | Tier 1 | `test_browser_network_boundaries.py` | `tests.test_browser_network_boundaries.TestBrowserNetworkBoundaries::test_redirect_to_cloud_metadata_rejected` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 71 | Tier 1 | `test_browser_network_boundaries.py` | `tests.test_browser_network_boundaries.TestBrowserNetworkBoundaries::test_redirect_to_private_rfc1918_rejected_from_remote` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 72 | Tier 1 | `test_browser_network_boundaries.py` | `tests.test_browser_network_boundaries.TestBrowserNetworkBoundaries::test_redirect_to_protected_localhost_port_rejected` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 73 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_brain_antigravity_query_switches_when_exhausted` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 74 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_brain_fails_over_to_claude_when_flash_exhausted` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 75 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_classify_prompt_tier_when_claude_exhausted` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 76 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_classify_prompt_tier_when_gemini_flash_exhausted` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 77 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_exhaustion_state_lifecycle` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 78 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_explicit_user_directive_resets_exhaustion` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 79 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_gemini_flash_unified_exhaustion_lifecycle` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 80 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_generate_response_dynamic_exhaustion_failover` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 81 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_get_candidate_cascade_priorities` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 82 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_get_candidate_cascade_when_gemini_flash_exhausted` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 83 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_is_task_complex_classification` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 84 | Tier 1 | `test_claude_token_exhaustion_fallback.py` | `tests.test_claude_token_exhaustion_fallback.TestClaudeTokenExhaustionFallback::test_is_token_exhaustion_error` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 85 | Tier 1 | `test_closed_loop_and_core_preferences.py` | `tests.test_closed_loop_and_core_preferences.TestClosedLoopAndCorePreferencesSuite::test_brain_system_instruction_injects_core_preferences` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 86 | Tier 1 | `test_cognitive_twin_pipeline.py` | `tests.test_cognitive_twin_pipeline.TestCognitiveTwinPipelineSuite::test_brain_envelope_injection_contains_live_telemetry` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 87 | Tier 1 | `test_curated_prompts_and_epistemic_gate.py` | `tests.test_curated_prompts_and_epistemic_gate.TestCuratedPromptsAndEpistemicGate::test_code_mode_system_instruction_contains_epistemic_gate` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 88 | Tier 1 | `test_curated_prompts_and_epistemic_gate.py` | `tests.test_curated_prompts_and_epistemic_gate.TestCuratedPromptsAndEpistemicGate::test_consult_specialist_injects_architect_curated_prompt` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 89 | Tier 1 | `test_curated_prompts_and_epistemic_gate.py` | `tests.test_curated_prompts_and_epistemic_gate.TestCuratedPromptsAndEpistemicGate::test_consult_specialist_injects_security_curated_prompt` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 90 | Tier 1 | `test_curated_prompts_and_epistemic_gate.py` | `tests.test_curated_prompts_and_epistemic_gate.TestCuratedPromptsAndEpistemicGate::test_curated_prompts_exist_on_disk` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 91 | Tier 1 | `test_curated_prompts_and_epistemic_gate.py` | `tests.test_curated_prompts_and_epistemic_gate.TestCuratedPromptsAndEpistemicGate::test_load_curated_prompt_helper` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 92 | Tier 1 | `test_curated_prompts_and_epistemic_gate.py` | `tests.test_curated_prompts_and_epistemic_gate.TestCuratedPromptsAndEpistemicGate::test_voice_mode_system_instruction_remains_lean` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 93 | Tier 1 | `test_diagnostics.py` | `tests.test_diagnostics.TestDiagnosticsSuite::test_brain_diagnostics_fast_reflex` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 94 | Tier 1 | `test_diagnostics.py` | `tests.test_diagnostics.TestDiagnosticsSuite::test_jarvis_doctor_fast_reflex` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 95 | Tier 1 | `test_email_integration.py` | `tests.test_email_integration::test_intent_routing_coverage` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 96 | Tier 1 | `test_episodic_reflection.py` | `tests.test_episodic_reflection.TestEpisodicReflectionSuite::test_auto_reflection_on_tool_failure_in_brain` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 97 | Tier 1 | `test_episodic_reflection.py` | `tests.test_episodic_reflection.TestEpisodicReflectionSuite::test_prompt_injection_of_episodic_lessons` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 98 | Tier 1 | `test_everything_search_and_self_editing.py` | `tests.test_everything_search_and_self_editing.TestEverythingSearchAndSelfEditing::test_brain_recognizes_self_editing_intent` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 99 | Tier 1 | `test_everything_search_and_self_editing.py` | `tests.test_everything_search_and_self_editing.TestEverythingSearchAndSelfEditing::test_brain_selects_everything_search_for_file_queries` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 100 | Tier 1 | `test_everything_search_and_self_editing.py` | `tests.test_everything_search_and_self_editing.TestEverythingSearchAndSelfEditing::test_brain_selects_full_code_tools_for_self_editing` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 101 | Tier 1 | `test_everything_search_and_self_editing.py` | `tests.test_everything_search_and_self_editing.TestEverythingSearchAndSelfEditing::test_everything_skill_registration` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 102 | Tier 1 | `test_everything_search_and_self_editing.py` | `tests.test_everything_search_and_self_editing.TestEverythingSearchAndSelfEditing::test_find_es_executable` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 103 | Tier 1 | `test_everything_search_and_self_editing.py` | `tests.test_everything_search_and_self_editing.TestEverythingSearchAndSelfEditing::test_search_desktop_files_fast_uses_everything` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 104 | Tier 1 | `test_everything_search_and_self_editing.py` | `tests.test_everything_search_and_self_editing.TestEverythingSearchAndSelfEditing::test_search_everything_empty_query` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 105 | Tier 1 | `test_everything_search_and_self_editing.py` | `tests.test_everything_search_and_self_editing.TestEverythingSearchAndSelfEditing::test_search_everything_mocked_success` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 106 | Tier 1 | `test_flagship_features.py` | `tests.test_flagship_features::test_intent_routing_coverage` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 107 | Tier 1 | `test_flagship_features.py` | `tests.test_flagship_features::test_project_manager` | `AssertionError` | **Host Filesystem Boundary (~/Downloads)**: Host user Downloads folder access blocked outside workspace *(Secondary: Path.home() / 'Downloads' unmocked traversal)* | `environmental` |
| 108 | Tier 1 | `test_four_tier_runner.py` | `tests.test_four_tier_runner.TestSupervisorAdversarialSuite::test_timeout_during_rapid_output_generation` | `AssertionError` | **Infrastructure Timing Flake (xdist CPU load)**: Supervisor process output drain assertion timing under parallel CPU load *(Secondary: AssertionError: 0 not greater than 0 under rapid stdout production)* | `infrastructure-related` |
| 109 | Tier 1 | `test_gemini_session_reliability.py` | `tests.test_gemini_session_reliability.TestGeminiSessionReliability::test_afc_explicitly_disabled_in_config` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 110 | Tier 1 | `test_gemini_session_reliability.py` | `tests.test_gemini_session_reliability.TestGeminiSessionReliability::test_independent_sessions_do_not_leak_state` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 111 | Tier 1 | `test_gemini_session_reliability.py` | `tests.test_gemini_session_reliability.TestGeminiSessionReliability::test_iteration_ceiling_stops_safely_and_invalidates_chat` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 112 | Tier 1 | `test_gemini_session_reliability.py` | `tests.test_gemini_session_reliability.TestGeminiSessionReliability::test_model_returns_function_call_in_final_allowed_iteration` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 113 | Tier 1 | `test_gemini_session_reliability.py` | `tests.test_gemini_session_reliability.TestGeminiSessionReliability::test_normal_tool_completion_below_ceiling_preserves_chat` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 114 | Tier 1 | `test_gemini_session_reliability.py` | `tests.test_gemini_session_reliability.TestGeminiSessionReliability::test_parallel_function_calls_in_single_turn_all_resolved` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 115 | Tier 1 | `test_gemini_session_reliability.py` | `tests.test_gemini_session_reliability.TestGeminiSessionReliability::test_streaming_error_mid_turn_resets_session_safely` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 116 | Tier 1 | `test_gemini_session_reliability.py` | `tests.test_gemini_session_reliability.TestGeminiSessionReliability::test_subsequent_request_succeeds_after_iteration_ceiling` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 117 | Tier 1 | `test_gemini_session_reliability.py` | `tests.test_gemini_session_reliability.TestGeminiSessionReliability::test_unified_runner_appends_closing_assistant_message_on_ceiling` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 118 | Tier 1 | `test_github_copilot_bridge.py` | `tests.test_github_copilot_bridge.TestAntigravityCopilotQuotaFallback::test_non_quota_antigravity_error_does_not_switch_provider` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 119 | Tier 1 | `test_github_copilot_bridge.py` | `tests.test_github_copilot_bridge.TestAntigravityCopilotQuotaFallback::test_quota_error_hands_off_to_copilot_without_executing_its_text_as_a_tool` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 120 | Tier 1 | `test_github_copilot_bridge.py` | `tests.test_github_copilot_bridge.TestAntigravityCopilotQuotaFallback::test_quota_signal_survives_later_candidate_error` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 121 | Tier 1 | `test_github_copilot_bridge.py` | `tests.test_github_copilot_bridge.TestAntigravityCopilotQuotaFallback::test_unknown_antigravity_quota_error_is_not_reported_as_success` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 122 | Tier 1 | `test_hybrid_and_timers.py` | `tests.test_hybrid_and_timers::test_hybrid_brain_initialization_and_dispatch` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 123 | Tier 1 | `test_interactive_persona.py` | `tests.test_interactive_persona.TestInteractivePersona::test_intent_groups_include_active_learning_and_commit_keywords` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 124 | Tier 1 | `test_interactive_persona.py` | `tests.test_interactive_persona.TestInteractivePersona::test_multi_turn_affirmative_follow_up_active_memory` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 125 | Tier 1 | `test_interactive_persona.py` | `tests.test_interactive_persona.TestInteractivePersona::test_multi_turn_affirmative_follow_up_co_engineering` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 126 | Tier 1 | `test_interactive_persona.py` | `tests.test_interactive_persona.TestInteractivePersona::test_system_prompt_contains_authentic_bettany_demeanor` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 127 | Tier 1 | `test_interactive_persona.py` | `tests.test_interactive_persona.TestInteractivePersona::test_system_prompt_contains_epistemic_humility_protocol` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 128 | Tier 1 | `test_interactive_persona.py` | `tests.test_interactive_persona.TestInteractivePersona::test_system_prompt_contains_socratic_co_engineering_protocol` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 129 | Tier 1 | `test_learned_habits.py` | `tests.test_learned_habits.TestAdaptationProgressionAndVerification::test_compound_habit_execution_in_brain` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 130 | Tier 1 | `test_learned_habits.py` | `tests.test_learned_habits.TestAdaptationProgressionAndVerification::test_dynamic_voyager_skill_discovery` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 131 | Tier 1 | `test_learned_habits.py` | `tests.test_learned_habits.TestAdaptationProgressionAndVerification::test_turn1_learning_to_turn2_reflex_flow` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 132 | Tier 1 | `test_learned_habits.py` | `tests.test_learned_habits.TestAdaptationProgressionAndVerification::test_verify_learned_habits_audit` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 133 | Tier 1 | `test_learned_habits.py` | `tests.test_learned_habits.TestAdaptationProgressionAndVerification::test_voice_trigger_for_habit_audit` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 134 | Tier 1 | `test_learned_habits.py` | `tests.test_learned_habits.TestFastBrowserUrlReflex::test_open_brave_tab_empty` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 135 | Tier 1 | `test_learned_habits.py` | `tests.test_learned_habits.TestFastBrowserUrlReflex::test_open_brave_tab_with_url` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 136 | Tier 1 | `test_learned_habits.py` | `tests.test_learned_habits.TestFastBrowserUrlReflex::test_open_chrome_url` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 137 | Tier 1 | `test_learned_habits.py` | `tests.test_learned_habits.TestFastBrowserUrlReflex::test_open_url_in_brave_direct` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 138 | Tier 1 | `test_lint_tool.py` | `tests.test_lint_tool.TestLintToolResolution::test_resolve_untrusted_env_var_falls_through_to_next_tier` | `AssertionError` | **Host Filesystem Boundary (C:\untrusted)**: Host path C:\untrusted access blocked outside workspace *(Secondary: Virtualenv resolution os.path.exists() Errno 13)* | `environmental` |
| 139 | Tier 1 | `test_milestone4_cognitive_invariants.py` | `tests.test_milestone4_cognitive_invariants.TestMilestone4CognitiveInvariants::test_gemini_provider_nested_json_tool_call_handling` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 140 | Tier 1 | `test_milestone4_cognitive_invariants.py` | `tests.test_milestone4_cognitive_invariants.TestMilestone4CognitiveInvariants::test_routing_cascade_brain_integration` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 141 | Tier 1 | `test_multi_llm_responsiveness.py` | `tests.test_multi_llm_responsiveness.TestHotStandbyDaemonPool::test_hot_standby_daemon_pool_lru` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 142 | Tier 1 | `test_new_skills.py` | `tests.test_new_skills::test_new_skills` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 143 | Tier 1 | `test_performance_pc_control.py` | `tests.test_performance_pc_control.TestPCControlAndPerformance::test_fast_reflex_brave_tab` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 144 | Tier 1 | `test_performance_pc_control.py` | `tests.test_performance_pc_control.TestPCControlAndPerformance::test_fast_reflex_system_vitals` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 145 | Tier 1 | `test_performance_pc_control.py` | `tests.test_performance_pc_control.TestPCControlAndPerformance::test_fast_reflex_volume` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 146 | Tier 1 | `test_performance_pc_control.py` | `tests.test_performance_pc_control.TestPCControlAndPerformance::test_hotkey_safety_guard` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 147 | Tier 1 | `test_performance_pc_control.py` | `tests.test_performance_pc_control.TestPCControlAndPerformance::test_is_fast_reflex_does_not_execute_skills` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 148 | Tier 1 | `test_performance_pc_control.py` | `tests.test_performance_pc_control.TestPCControlAndPerformance::test_os_control_skills_registered` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 149 | Tier 1 | `test_performance_pc_control.py` | `tests.test_performance_pc_control.TestPCControlAndPerformance::test_pass_through_for_questions_and_self_building` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 150 | Tier 1 | `test_performance_pc_control.py` | `tests.test_performance_pc_control.TestPCControlAndPerformance::test_self_knowledge_in_system_prompt` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 151 | Tier 1 | `test_plugin_integrations.py` | `tests.test_plugin_integrations.TestPluginIntegrations::test_gemini_docs_mcp_sse_parsing` | `AssertionError` | **Test Assertion / SSRF Guard Deviation**: Offline DNS resolution abort prevents expected HTTP redirect inspection *(Secondary: SSRF guard raises SecurityBoundaryViolation before test redirect check)* | `assertion-related` |
| 152 | Tier 1 | `test_residual_security_boundaries.py` | `tests.test_residual_security_boundaries.TestResidualSecurityBoundaries::test_orchestrator_stop_invokes_cdp_cleanup` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 153 | Tier 1 | `test_self_evolution.py` | `tests.test_self_evolution.TestSelfEvolution::test_dynamic_skill_creation_and_hot_reload` | `AssertionError` | **Host Filesystem Boundary (C:\.jarvis_sec)**: Host drive root .jarvis_sec access blocked outside workspace *(Secondary: Dynamic skill hot-reload HMAC storage unmocked write)* | `environmental` |
| 154 | Tier 1 | `test_self_evolution.py` | `tests.test_self_evolution.TestSelfEvolution::test_user_model_choice_for_self_building` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 155 | Tier 1 | `test_self_healing_and_shutdown_immunity.py` | `tests.test_self_healing_and_shutdown_immunity.TestSelfHealingAndShutdownImmunity::test_antigravity_streaming_subprocess_flags` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 156 | Tier 1 | `test_shutdown_lifecycle.py` | `tests.test_shutdown_lifecycle.TestShutdownLifecycle::test_brain_shutdown_lifecycle` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 157 | Tier 1 | `test_ui_approval_workflow.py` | `tests.test_ui_approval_workflow.TestUIApprovalWorkflow::test_end_to_end_ui_approval_grants_execution` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 158 | Tier 1 | `test_ui_approval_workflow.py` | `tests.test_ui_approval_workflow.TestUIApprovalWorkflow::test_end_to_end_ui_rejection_denies_execution` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 159 | Tier 1 | `test_ui_approval_workflow.py` | `tests.test_ui_approval_workflow.TestUIApprovalWorkflow::test_expired_grant_rejection` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 160 | Tier 1 | `test_ui_approval_workflow.py` | `tests.test_ui_approval_workflow.TestUIApprovalWorkflow::test_modified_payload_tampering_prevention` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 161 | Tier 1 | `test_ui_approval_workflow.py` | `tests.test_ui_approval_workflow.TestUIApprovalWorkflow::test_replay_prevention_rejects_second_execution` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 162 | Tier 1 | `test_ui_approval_workflow.py` | `tests.test_ui_approval_workflow.TestUIApprovalWorkflow::test_safe_tools_bypass_approval_without_post_execution_interference` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 163 | Tier 1 | `test_ui_approval_workflow.py` | `tests.test_ui_approval_workflow.TestUIApprovalWorkflow::test_websocket_approval_response_unblocks_tool` | `AssertionError` | **Loopback Network Boundary**: Loopback TCP socket connection blocked by sandbox boundary *(Secondary: urllib / socket connect to 127.0.0.1 (WSAEACCES WinError 10013))* | `environmental` |
| 164 | Tier 3 | `test_antigravity_job_containment.py` | `tests.test_antigravity_job_containment.TestAntigravityJobContainmentMocked::test_lru_pool_eviction_terminates_evicted_job` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 165 | Tier 3 | `test_oneshot_process_containment.py` | `tests.test_oneshot_process_containment.TestOneShotProcessContainment::test_16_overlapping_streaming_sandboxes_all_cancelled` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 166 | Tier 3 | `test_oneshot_process_containment.py` | `tests.test_oneshot_process_containment.TestOneShotProcessContainment::test_17_finishing_request_discards_only_its_own_sandbox` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 167 | Tier 3 | `test_oneshot_process_containment.py` | `tests.test_oneshot_process_containment.TestOneShotProcessContainment::test_18_repeated_cancellation_and_cleanup_are_idempotent` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 168 | Tier 3 | `test_oneshot_process_containment.py` | `tests.test_oneshot_process_containment.TestOneShotProcessContainment::test_19_failure_in_one_sandbox_does_not_prevent_other_terminations` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 169 | Tier 3 | `test_oneshot_process_containment.py` | `tests.test_oneshot_process_containment.TestOneShotProcessContainment::test_20_cancellation_during_startup_prevents_request_continuation` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 170 | Tier 3 | `test_oneshot_process_containment.py` | `tests.test_oneshot_process_containment.TestOneShotProcessContainment::test_21_cancellation_after_registration_targets_sandbox` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 171 | Tier 3 | `test_oneshot_process_containment.py` | `tests.test_oneshot_process_containment.TestOneShotProcessContainment::test_22_new_request_after_completed_cancellation_not_cancelled` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 172 | Tier 3 | `test_oneshot_process_containment.py` | `tests.test_oneshot_process_containment.TestOneShotProcessContainment::test_23_exceptions_and_repeated_cancellation_retain_cleanup_guarantees` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 173 | Tier 3 | `test_rstrtmgr_lock_surgeon.py` | `tests.test_rstrtmgr_lock_surgeon.TestRstrtmgrLockSurgeon::test_inspect_file_locks_unlocked` | `AssertionError` | **OS API Restriction (Restart Manager)**: Win32 Restart Manager RmStartSession restricted in sandbox *(Secondary: RmStartSession returns Win32 error 29 (RPC/service restricted))* | `environmental` |
| 174 | Tier 3 | `test_rstrtmgr_lock_surgeon.py` | `tests.test_rstrtmgr_lock_surgeon.TestRstrtmgrLockSurgeon::test_inspect_port_holders` | `AssertionError` | **OS API Restriction (Restart Manager)**: Win32 Restart Manager RmStartSession restricted in sandbox *(Secondary: RmStartSession returns Win32 error 29 (RPC/service restricted))* | `environmental` |
| 175 | Tier 3 | `test_rstrtmgr_lock_surgeon.py` | `tests.test_rstrtmgr_lock_surgeon.TestRstrtmgrLockSurgeon::test_release_file_lock_protection` | `AssertionError` | **OS API Restriction (Restart Manager)**: Win32 Restart Manager RmStartSession restricted in sandbox *(Secondary: RmStartSession returns Win32 error 29 (RPC/service restricted))* | `environmental` |
| 176 | Tier 4 | `test_audio_streaming_pipeline.py` | `tests.test_audio_streaming_pipeline.TestPlaybackReliabilityAndRingAllocation::test_shutdown_bounded_single_worker_and_fail_closed_on_stalled_shutdown` | `AssertionError` | **Product Logic Defect (TTS Streaming Restart)**: TTS start_streaming_session() returns False on clean restart *(Secondary: Post-worker-join state machine reconciliation logic failure)* | `behavioral` |
| 177 | Tier 4 | `test_audio_streaming_pipeline.py` | `tests.test_audio_streaming_pipeline.TestPlaybackReliabilityAndRingAllocation::test_stop_failure_abort_failure_close_succeeds_is_uncertain` | `AssertionError` | **Product Logic Defect (TTS Streaming Restart)**: TTS start_streaming_session() returns False on clean restart *(Secondary: Post-worker-join state machine reconciliation logic failure)* | `behavioral` |
| 178 | Tier 4 | `test_council_batch_2.py` | `tests.test_council_batch_2::test_intent_routing_coverage` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 179 | Tier 4 | `test_council_batch_3.py` | `tests.test_council_batch_3::test_dynamic_latency_telemetry` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 180 | Tier 4 | `test_os_skills.py` | `tests.test_os_skills::test_brain_intent_routing` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |
| 181 | Tier 4 | `test_os_skills.py` | `tests.test_os_skills::test_clipboard_assistant` | `AssertionError` | **OS Resource Contention (Windows Clipboard)**: Host desktop application holds exclusive Windows clipboard lock *(Secondary: OpenClipboard Win32 API access denied)* | `environmental` |
| 182 | Tier 4 | `test_voice_control_and_skills.py` | `tests.test_voice_control_and_skills.TestVoiceControlAndSkillsSuite::test_brain_fast_reflex_voice_profiles` | `AssertionError` | **Host Path Boundary (agy.exe)**: Host agy.exe discovery path access blocked outside workspace *(Secondary: AntigravityBridge._find_agy_executable() candidate.exists() check)* | `environmental` |

---


## 7. Authoritative Final Regression Scorecard


```

===================================================================================================================================

                                        JARVIS 2.0 AUTHORITATIVE REGRESSION SCORECARD

===================================================================================================================================

Tier                          Status         Selected   Pass   Fail   Skip  Watchdog(Files) Incomplete(Nodes) Blocked  Duration

-----------------------------------------------------------------------------------------------------------------------------------

Tier 1: Hermetic Unit         FAILED             1310   1141    163      6                0                 0       0   272.70s

Tier 2A: Async Safe           ENV_BLOCKED         270      0      0      0                0                 0     270     0.00s

Tier 2B: Loopback & IPC       ENV_BLOCKED         182      0      0      0                0                 0     182     0.00s

Tier 3: Windows Integration   INCOMPLETE          174    117     12      0                1                45       0   184.22s

Tier 4: Hardware Integration  INCOMPLETE          253    237      7      0                2                 9       0   418.65s

Browser Integration           ENV_BLOCKED           9      0      0      0                0                 0       9     0.00s

-----------------------------------------------------------------------------------------------------------------------------------

TOTALS (Selected Test Nodes)  FAILED             2198   1495    182      6                3                54     461   875.57s

===================================================================================================================================

```


### Invariant Verification Summary

1. **Unique Failed Nodes:** Exactly **182** ($112 + 29 + 18 + 13 + 3 + 3 + 2 + 1 + 1 = 182$).

2. **Execution Incomplete Nodes:** Exactly **54** across 3 files ($45 + 6 + 3 = 54$; individual node outcomes `UNKNOWN`).

3. **Discovered Inventory Accounting:** Exactly **2,198** ($1,495 + 182 + 6 + 54 + 461 = 2,198$, 100.0% reconciliation).

4. **Overall Inventory Pass Rate:** **68.02%** ($1,495 / 2,198$).

5. **Evaluated Test Pass Rate:** **88.83%** ($1,495 / 1,683$).


---

## 8. Audit Integrity & Constraints Preserved

- **Zero Code Mutations:** No production code, unit tests, configuration files, test fixtures, or hooks were modified during this audit.

- **Preservation of Artifacts:** All 29 raw JUnit XML files in `data/pytest_tmp/` and existing scratch scripts remain intact.

- **Git Cleanliness:** Zero commits, pushes, stashes, or branch checkouts were performed.
