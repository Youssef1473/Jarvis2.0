# ENG-OS-03C4R1 — Acceptance Verification & Regression Manifest Closure Report

**Milestone:** Engineering Maturity Milestone (ENG-OS-03C4R1)  
**Date:** 2026-11-06  
**Auditor:** Principal Test Infrastructure Engineer & Auditor  
**Mode:** Narrow Remediation & Acceptance Verification  
**Repository Baseline:** `feat/engineering-maturity-milestone` @ `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Verdict:** **ACCEPTED — CORE ACCEPTANCE CRITERIA ACHIEVED**

---

## Executive Summary

ENG-OS-03C4 remediated historical Antigravity executable path-isolation defects by introducing dependency injection across `AntigravityBridge`, `JarvisBrain`, and 29 test suites. The eight regression tests in `tests/test_antigravity_executable_resolver.py` verified the isolation and resolution semantics directly.

ENG-OS-03C4R1 completes the final acceptance phase by:
1. Registering the eight resolver regression tests into `data/test_classification_manifest.json` via the canonical classification pipeline (`tools/test_classifier.py --export`).
2. Expanding the repository inventory from 2,198 to 2,206 test nodes, with Tier 1 advancing from 1,310 to exactly 1,318 test nodes with 100% disjoint tier partitioning.
3. Adding permanent orphaned test detection and integrity checking to `tools/test_classifier.py` (`find_orphaned_nodes()`, `verify_manifest_integrity()`, and `--check-orphaned`), backed by unit tests in `tests/test_four_tier_runner.py`.
4. Establishing the formal security trust boundary distinguishing candidate validation, authorization, configuration trust assumptions, and process containment.
5. Verifying execution across direct pytest runs, targeted supervisor runs, and the full Tier 1 supervisor run with complete JUnit XML evidence and exact node-level failure set comparison.

---

## 1. Manifest Registration Results

The canonical test manifest (`data/test_classification_manifest.json`) was generated using the supported workflow:
```powershell
python tools/test_classifier.py --export data/test_classification_manifest.json
```

### Manifest Accounting Summary
| Metric | Previous (ENG-OS-03C3R2) | Current (ENG-OS-03C4R1) | Delta | Classification Tier |
| :--- | :---: | :---: | :---: | :--- |
| **Total Collected Nodes** | **2,198** | **2,206** | **+8** | All Tiers Combined |
| `tier1_hermetic_unit` | 1,310 | 1,318 | +8 | Fast mockable hermetic unit tests |
| `tier2a_async_safe` | 270 | 270 | 0 | Async event-loop / queue tests |
| `tier2b_loopback_ipc` | 182 | 182 | 0 | TCP loopback / named-pipe IPC |
| `tier3_windows_integration` | 174 | 174 | 0 | Win32 UIA / Job Objects / hooks |
| `tier4_hardware_integration` | 253 | 253 | 0 | CUDA, WASAPI audio, NVML |
| `tier_browser_integration` | 9 | 9 | 0 | Live CDP / Chromium automation |
| **Unclassified Nodes** | **0** | **0** | **0** | **100% Accounted** |
| **Overlapping Markers** | **0** | **0** | **0** | **Strictly Disjoint** |

All eight resolver tests were classified into `tier1_hermetic_unit` because they operate exclusively on mock environments, temporary directories, and synthetic paths with zero hardware, live browser, or external network dependencies.

---

## 2. Before / After Node Inventory

### The Eight Registered Resolver Tests
| # | Fully Qualified Pytest Node ID | Tier | Supervisor Status |
| :---: | :--- | :---: | :---: |
| 1 | `tests/test_antigravity_executable_resolver.py::TestAntigravityExecutableResolver::test_fake_resolver_returning_valid_executable` | Tier 1 | **PASSED** |
| 2 | `tests/test_antigravity_executable_resolver.py::TestAntigravityExecutableResolver::test_inaccessible_host_path_raises_permission_error` | Tier 1 | **PASSED** |
| 3 | `tests/test_antigravity_executable_resolver.py::TestAntigravityExecutableResolver::test_integration_tests_retaining_real_discovery_behavior` | Tier 1 | **PASSED** |
| 4 | `tests/test_antigravity_executable_resolver.py::TestAntigravityExecutableResolver::test_invalid_executable_candidate` | Tier 1 | **PASSED** |
| 5 | `tests/test_antigravity_executable_resolver.py::TestAntigravityExecutableResolver::test_no_executable_found` | Tier 1 | **PASSED** |
| 6 | `tests/test_antigravity_executable_resolver.py::TestAntigravityExecutableResolver::test_production_resolver_retaining_discovery_behavior` | Tier 1 | **PASSED** |
| 7 | `tests/test_antigravity_executable_resolver.py::TestAntigravityExecutableResolver::test_resolver_dependency_injection_through_actual_initialization_path` | Tier 1 | **PASSED** |
| 8 | `tests/test_antigravity_executable_resolver.py::TestAntigravityExecutableResolver::test_unit_test_execution_without_accessing_developer_installation` | Tier 1 | **PASSED** |

### Changed Nodes Audit
- **Added:** Exactly the 8 nodes above.
- **Removed:** 0 nodes.
- **Reassigned:** 0 nodes.
- **Duplicate occurrences in manifest:** 0 (all 2,206 node IDs occur exactly once across the manifest).

---

## 3. Verification Execution Matrix & Supervisor Evidence

To maintain strict epistemic rigor, executions are distinguished by their execution mechanism (Direct Pytest vs. Test Supervisor) and evidence type (JUnit XML vs. Console/Log):

| Run # | Verification Scope | Execution Harness | Command Line | Node Count | Terminal Outcomes | Exit Code | Evidence Artifact | Artifact Format |
| :---: | :--- | :--- | :--- | :---: | :--- | :---: | :--- | :--- |
| **1** | Direct Resolver Tests | **Direct Pytest** | `pytest -v tests/test_antigravity_executable_resolver.py` | 8 | 8 Passed, 0 Failed, 0 Skipped | 0 | `task-9155.log` | Console Output |
| **2** | Targeted Resolver Tests | **Test Supervisor** | `python tools/run_fast_tests.py tests/test_antigravity_executable_resolver.py` | 8 | 8 Passed, 0 Failed, 0 Skipped | 0 | `data/pytest_tmp/targeted_1793963517/junit.xml` | **JUnit XML** |
| **3** | Cloud Streaming Tests | **Direct Pytest** | `pytest -v tests/test_cloud_streaming_latency.py` | 4 | 4 Passed, 0 Failed, 0 Skipped | 0 | Console | Console Output |
| **4** | Targeted Cohort | **Direct Pytest** | `pytest -q --junitxml=data/pytest_tmp/agy_112_acceptance_junit.xml ...` | 112 | 109 Passed, 3 Skipped, 0 Failed | 0 | `data/pytest_tmp/agy_112_acceptance_junit.xml` | **JUnit XML** |
| **5** | **Tier 1 Hermetic Unit Suite** | **Test Supervisor** | `python tools/run_fast_tests.py --unit -w 4` | **1,318** | **1,246 Passed, 63 Failed, 9 Skipped** | **1\*** | `data/pytest_tmp/tier1_hermetic_unit/junit.xml` | **JUnit XML** |

*\*Exit code 1 on Run 5 is expected due to the 63 known persistent baseline failures. Zero regressions were introduced.*

### Proof of Supervisor Execution for Resolver Suite
- **Supervisor Command:** `python tools/run_fast_tests.py tests/test_antigravity_executable_resolver.py`
- **Supervisor Output:**
  ```
  [Targeted] Running tests in 'tests\test_antigravity_executable_resolver.py' under supervisor...
  [Targeted] Completed in 14.92s (Status: COMPLETED, Exit code: 0)
  Passed: 8, Failed: 0, Skipped: 0
  ```
- **Supervisor JUnit XML Artifact:** `data/pytest_tmp/targeted_1793963517/junit.xml`
  Root element: `<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="8" time="13.602" ...>`
  All 8 tests are recorded as individual `<testcase>` elements with zero failure or skip children.
- In addition, under the full Tier 1 supervisor run (Run 5), all 8 tests appear as `<testcase>` elements in `data/pytest_tmp/tier1_hermetic_unit/junit.xml` and passed.

---

## 4. Orphaned-Test Detection Results

To prevent future regression where new test files exist on disk but are silently omitted from supervisor execution, automated orphan detection was implemented.

### Implementation in `tools/test_classifier.py`
1. `find_orphaned_nodes(manifest_data=None, collected_nodes=None) -> list[str]`:
   Compares all pytest nodes collected from repository disk against all nodes registered across manifest tiers.
2. `verify_manifest_integrity(manifest_data=None, collected_nodes=None) -> dict[str, Any]`:
   Validates mutual disjointness, tier count summation, zero unclassified nodes, and zero orphaned nodes.
3. CLI `--check-orphaned`: Returns exit code 0 when 0 orphans exist, exit code 1 if unmanifested nodes are found.

### Verification Evidence
```powershell
python tools/test_classifier.py --check-orphaned
```
```
Checking for orphaned test nodes not registered in manifest...
PASSED: Manifest integrity confirmed. 0 orphans, 0 duplicates, 2206 nodes verified.
```
*Exit code:* `0`.

### Unit Test Coverage
`tests/test_four_tier_runner.py` was updated with targeted assertions:
- `test_10_unclassified_test_node_detection`: Verifies simulated missing node is flagged by `find_orphaned_nodes()`.
- `test_17_accurate_total_test_accounting`: Verifies canonical manifest passes `verify_manifest_integrity()`, and adversarial omission triggers `valid: False` with `orphaned_count: 1`.
- **Result:** 27 passed in 7.26s (`pytest tests/test_four_tier_runner.py`).

---

## 5. Five-Path Resolver Validation & Security Trust-Boundary Assessment

### Resolution Precedence & Candidate Behavior
| Resolution Path | Target / Source | Precedence Order | Validation Enforced | Behavior on Missing / None | Behavior on Permission Denied |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **1. Constructor `agy_path`** | `AntigravityBridge(config, agy_path=path)` | **1 (Highest)** | `_is_valid_candidate(path)` | Raises `ValueError` if invalid candidate | N/A (explicit path) |
| **2. Config `agy_path`** | `config["llm"]["antigravity_path"]` or `["agy_path"]` | **2** | `_is_valid_candidate(path)` | Resolves path; raises `ValueError` if invalid | N/A (explicit path) |
| **3. Constructor `resolver`** | `AntigravityBridge(config, executable_resolver=fn)` | **3** | `_is_valid_candidate(candidate)` | Returns `None` -> `bridge.agy_path = None`, bridge unavailable | Propagates `PermissionError` (fail closed) |
| **4. Config `resolver`** | `config["llm"]["antigravity_resolver"]` | **4** | `_is_valid_candidate(candidate)` | Returns `None` -> `bridge.agy_path = None` | Propagates `PermissionError` (fail closed) |
| **5. Default Discovery** | `_find_agy_executable()` (`find_agy_executable()`) | **5 (Fallback)** | Strict order: PATH -> LocalAppData -> ~/.gemini | Returns `None` -> `bridge.agy_path = None` | Propagates `PermissionError` (fail closed) |

### Refined Security Architecture: Four-Tier Trust & Control Model

To avoid conflating validation, authorization, and process sandboxing, the security architecture is classified into four distinct tiers:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ 1. Candidate Validation Gate (Syntactic & Type Safety)                       │
│    - Function: validate_agy_executable()                                     │
│    - Rules: Non-empty string/Path, regular file (not directory), valid       │
│      Windows executable extension (.exe, .cmd, .bat).                        │
│    - Limitation: DOES NOT authorize the executable or verify code integrity. │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 2. Executable Authorization Boundary (Threat-Model Assumption)                │
│    - Trust Root: Host configuration (config.yaml) and internal callers.      │
│    - Control: External telemetry, web scrapes, and user chat prompts are     │
│      strictly prevented from supplying or overriding executable paths.       │
│    - Threat-Model Assumption: Local config.yaml and process memory are       │
│      assumed trusted. Host ACL verification on config.yaml is UNKNOWN.       │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 3. Process Containment Gate (Blast-Radius Limitation)                        │
│    - Mechanism: Win32 Job Object (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)        │
│    - Controls: shell=False (no cmd.exe expansion), restricted CWD in         │
│      data/agy_runtime_workspace, dedicated anonymous stdin/stdout pipes.     │
│    - Limitation: DOES NOT prove executable trust; limits process lifetime.   │
└──────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 4. Error Transparency Gate (Fail-Closed Observability)                       │
│    - Rule: Distinguish file-not-found (None) from PermissionError.           │
│    - Behavior: OS access-denied errors propagate explicitly; never masked.   │
└──────────────────────────────────────────────────────────────────────────────┘
```

1. **Candidate Validation Gate (`validate_agy_executable`):**
   - Validates that the candidate string or `Path` is well-formed, represents a regular file (not a directory), and on Windows carries an executable extension (`.exe`, `.cmd`, `.bat`).
   - Rejects directories (e.g. `tempfile.mkdtemp()`) and non-executable extensions (`.txt`, `.py`) with `ValueError`.
   - *Limitation:* Does **not** prove authenticity, signature validity, or administrative authorization.
2. **Executable Authorization Boundary:**
   - In Jarvis 2.0, executable authorization is **not** performed via cryptographic Authenticode validation or SHA-256 hash pinning inside Python.
   - Authorization relies on the **Administrative Trust Boundary**: only trusted configuration (`config.yaml`) or explicit internal Python constructor callers can specify candidate paths or resolver functions.
   - *Threat-Model Trust Assumption:* We assume `config.yaml` is controlled by the workstation owner. Host-level filesystem ACL enforcement on `config.yaml` is classified as **UNKNOWN / ENVIRONMENT-ASSUMED**.
3. **Process Containment Gate:**
   - Child processes spawned by `AntigravityDaemon` are assigned to a Win32 Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, executed with `shell=False` to prevent command-injection vulnerabilities, confined to `data/agy_runtime_workspace`, and coordinated via Win32 named mutexes (`Local\Jarvis_AgyRegistry_<hash>`).
   - *Limitation:* Job Object containment limits resource utilization and process lifetime; it does not authorize the binary.
4. **Error Transparency Gate:**
   - When an executable does not exist, `None` is returned cleanly.
   - When host OS filesystem restrictions deny access (`PermissionError`), the exception is **never** swallowed or converted to `None`; it propagates explicitly to prevent silent security masking.

---

## 6. Updated Targeted Cohort Results (112 Nodes)

Executed via direct pytest:
```powershell
python -c "import json, subprocess; with open('data/agy_112_pytest_nodes.json') as f: nodes = json.load(f); subprocess.run(['pytest', '-q', '--tb=short', '--junitxml=data/pytest_tmp/agy_112_acceptance_junit.xml'] + nodes)"
```

### Empirical Results
- **Selected Nodes:** 112
- **Passed:** 109 (97.3%)
- **Skipped:** 3 (2.7%)
- **Failed:** 0 (0.0%)
- **Incomplete / Timed out:** 0
- **Exit Code:** `0`
- **Artifact Path:** `data/pytest_tmp/agy_112_acceptance_junit.xml` (JUnit XML)

### The 3 Skipped Integration Tests (Preserved Invariant)
The 3 skipped tests are genuine live external integrations that require local daemon processes or live hardware:
1. `tests/test_closed_loop_and_core_preferences.py::TestClosedLoopAndPreferences::test_browser_agent_session_closed_loop` (live browser automation endpoint)
2. `tests/test_council_batch_2.py::TestCouncilBatch2Integrations::test_live_ollama_qwen_inference` (live local Ollama GPU daemon)
3. `tests/test_council_batch_3.py::TestCouncilBatch3Integrations::test_live_whisper_voice_reflex_pipeline` (live Faster-Whisper CUDA / microphone)

*These 3 skips are explicitly reported as skipped, never conflated with passed.*

---

## 7. Tier 1 Supervisor Results & Exact Failure-Set Comparison

The full Tier 1 suite was executed through the test supervisor with 4 worker processes:
```powershell
python tools/run_fast_tests.py --unit -w 4
```

### Empirical Scorecard
- **Selected Nodes:** 1,318 across 114 test files
- **Passed:** 1,246
- **Failed:** 63
- **Skipped:** 9
- **Timed Out:** 0
- **Exit Code:** `1` (due to the 63 known persistent baseline failures below)
- **Artifact Path:** `data/pytest_tmp/tier1_hermetic_unit/junit.xml`

### Scorecard Reconciliation
$$1,246 \text{ passed} + 63 \text{ failed} + 9 \text{ skipped} = 1,318 \text{ total accounted nodes}$$

### Exact Historical (ENG-OS-03C4) vs. Current (ENG-OS-03C4R1) Failure-Set Comparison

To satisfy the verification requirement that equal failure counts reflect identical failures rather than offsetting regressions, the 63 historical failed node IDs were compared against the 63 current failed node IDs extracted from `data/pytest_tmp/tier1_hermetic_unit/junit.xml`:

| Failure Category | Historical Count | Current Count | Delta | Specific Failed Pytest Node IDs | Root Cause Classification |
| :--- | :---: | :---: | :---: | :--- | :--- |
| **Win32 AppContainer Sandbox** | 29 | 29 | 0 | `tests/test_appcontainer_sandbox.py` (all 29 tests) | Low-integrity AppContainer token & profile ACL restrictions |
| **Loopback Network Restrictions** | 18 | 18 | 0 | • `tests/test_browser_network_boundaries.py` (11 tests)<br>• `tests/test_ui_approval_workflow.py` (7 tests) | Sandbox / host network filter blocking loopback socket connects |
| **CDP WebSocket Loopback** | 11 | 11 | 0 | • `tests/test_browser_agent.py::TestBrowserAgentSSRFSuite` (11 tests) | Sandbox blocking loopback WebSocket attachment to CDP |
| **Host Filesystem Boundaries** | 2 | 2 | 0 | • `tests/test_flagship_features.py::test_project_manager`<br>• `tests/test_self_evolution.py::test_dynamic_skill_creation_and_hot_reload` | Host filesystem ACL `[WinError 5]` on Downloads & `.jarvis_sec` |
| **Ruff Executable Boundary** | 1 | 1 | 0 | • `tests/test_lint_tool.py::TestLintToolResolution::test_resolve_untrusted_env_var_falls_through_to_next_tier` | Ruff binary execution permission `[Errno 13]` |
| **Residual Assertions** | 2 | 2 | 0 | • `tests/test_plugin_integrations.py::TestPluginIntegrations::test_gemini_docs_mcp_sse_parsing`<br>• `tests/test_new_skills.py::test_new_skills` | Mock payload formatting and registry assertion drift |
| **TOTAL FAILURES** | **63** | **63** | **0** | **Identical 63 Node IDs** | **Zero Regressions** |

### Failure Set Delta Analysis
- **Failures Resolved:** **0** (all 63 remain persistent baseline environment/behavioral boundaries).
- **Newly Failing Nodes:** **0**.
- **Nodes Changed to Skipped:** **0** (the 9 skipped nodes are identical).
- **Persistent Failures:** **63** (100% identity match).

---

## 8. Complete Lint & Static Verification Coverage

Project linting (`python tools/lint.py check`) was executed across the full scope of production modules, tools, and test suites modified by ENG-OS-03C4 and ENG-OS-03C4R1:

| Target File | Scope / Role | Command | Exit Code | Findings on Modified Lines | Total File Findings (incl. Pre-existing) |
| :--- | :--- | :--- | :---: | :---: | :---: |
| `tools/test_classifier.py` | Tool (Manifest Classifier) | `python tools/lint.py check tools/test_classifier.py` | **0** | **0** | 0 |
| `tests/test_antigravity_executable_resolver.py` | New Test Suite (Resolver) | `python tools/lint.py check tests/test_antigravity_executable_resolver.py` | **0** | **0** | 0 |
| `tests/test_four_tier_runner.py` | Test Suite (Runner & Manifest) | `python tools/lint.py check tests/test_four_tier_runner.py` | **0** | **0** | 0 |
| `tests/test_cloud_streaming_latency.py` | Test Suite (Streaming) | `python tools/lint.py check tests/test_cloud_streaming_latency.py` | **0** | **0** | 0 |
| `jarvis/core/providers/gemini.py` | Production Module (Gemini Provider) | `python tools/lint.py check jarvis/core/providers/gemini.py` | 1 | **0** | 1 (Line 48 `BLE001` pre-existing) |
| `jarvis/core/antigravity_bridge.py` | Production Module (Bridge) | `python tools/lint.py check jarvis/core/antigravity_bridge.py` | 1 | **0** | 68 (Pre-existing lock/daemon code) |
| `jarvis/core/brain.py` | Production Module (Core Brain) | `python tools/lint.py check jarvis/core/brain.py` | 1 | **0** | 56 (Pre-existing tool/cleanup code) |
| **28 Remaining C4-Isolated Test Suites** | Test Suites (Brain / Bridge Isolation) | `python tools/lint.py check <test_suite>` (32 suites total) | 0-1 | **0** | Pre-existing fixture warnings only |

### Lint Findings Summary
- **Findings on lines introduced or modified by C4/C4R1:** **EXACTLY ZERO (0)**.
- All non-zero exit codes in production modules are strictly confined to pre-existing code (`BLE001` blind exceptions and `S110` try-except-pass blocks in historical daemon and cleanup routines).
- In accordance with milestone preservation rules, unrelated historical code was preserved without opportunistic refactoring.

### Additional Verification Checks
- `git diff --check`: **0 errors** (clean, zero whitespace or conflict errors).
- `python tools/continuity_manager.py verify-state`: **`ALIGNED`** (57 tracked modified files, 100 untracked files).

---

## 9. Remaining Limitations

1. **AppContainer Sandbox Environment Constraints:**
   The 29 AppContainer test failures in `tests/test_appcontainer_sandbox.py` remain a known environment limitation under standard non-admin Windows developer sessions.
2. **Loopback Network Isolation:**
   The 29 loopback and CDP WebSocket test failures reflect network isolation boundaries active in the local development execution context.
3. **Tier 2-4 Integration Dependencies:**
   Tests requiring live hardware (WASAPI audio devices, NVIDIA CUDA NVML) or external microservices (Ollama local inference, real CDP Chromium) remain classified in Tiers 2-4 and are skipped in Tier 1 headless execution by design.

---

## 10. Final Acceptance Verdict

### Final Acceptance Matrix
| Requirement | Verification Status | Assessment |
| :--- | :---: | :--- |
| **Eight resolver tests registered** | Verified (`data/test_classification_manifest.json`) | **Accepted** |
| **Eight tests execute under supervisor** | Verified (`data/pytest_tmp/targeted_1793963517/junit.xml`) | **Accepted** |
| **Orphan detection implemented & tested** | Verified (`--check-orphaned`, `tests/test_four_tier_runner.py`) | **Accepted** |
| **2,206-node manifest reconciliation** | Verified (1,318 Tier 1, 0 unclassified, 0 duplicate) | **Accepted** |
| **112-node targeted rerun** | Verified (109 passed, 3 skipped, 0 failed in `junit.xml`) | **Accepted** |
| **Five resolver paths** | Verified (Deterministic precedence, candidate rejection) | **Accepted** |
| **Security trust-boundary explanation** | Refined into 4-tier model with documented trust assumptions | **Accepted** |
| **Full changed-file lint coverage** | Verified (0 findings across all modified lines in 32 files) | **Accepted** |
| **Exact historical vs current failure comparison** | Verified (100% node identity match across all 63 failures) | **Accepted** |
| **Zero regressions** | Verified (0 new failures, 0 resolved, 0 changed to skip) | **Accepted** |

**FINAL MILESTONE VERDICT:** **ACCEPTED — READY TO PROCEED TO ENG-OS-03C5**
