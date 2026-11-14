# ENG-OS-03A1 — Independent Adversarial Execution Provenance Audit

**Auditor:** Independent Principal Security Engineer and Reliability Architect  
**Mode:** STRICTLY READ-ONLY  
**Date:** 2026-11-05  
**Target Subsystem:** `tools/execution_evidence.py`, `tools/lesson_candidate.py`, `tests/test_execution_evidence.py`  
**Reference Specification:** `docs/agent/ENG_OS_03A_EXECUTION_EVIDENCE.md`  
**Verdict:** **ACCEPTED WITH LIMITATIONS**

---

## 1. Executive Verdict & Answers to Primary Audit Questions

ENG-OS-03A introduces substantial testing infrastructure improvements to Jarvis 2.0: subprocess isolation with JUnit XML parsing, test definition SHA-256 invariance, test selector invariance, secret scrubbing via `redact_sensitive_text`, and unconditional preservation of the fail-closed permanent-memory promotion barrier (`PROMOTION_PROHIBITED`).

However, an independent adversarial audit reveals significant architectural gaps between the documented claims and empirical reality:

### Answer to Question 1: Can model-controlled or ordinary Python code forge `AUTHORITATIVE_LOCAL_EXECUTION`?
**YES (IN-PROCESS).**  
While `ExecutionEvidenceEnvelope.from_dict()` and `LessonCandidate.from_dict()` successfully downgrade imported external JSON to `IMPORTED_UNVERIFIED`, `ExecutionEvidenceEnvelope` is an ordinary, unfrozen Python `@dataclass`. Any Python code running in the same process can directly instantiate an envelope with `provenance_status="AUTHORITATIVE_LOCAL_EXECUTION"`, or mutate fields on an existing envelope in-place or via `dataclasses.replace()`. There is no private constructor, no observer HMAC token, and no cryptographic signature.

### Answer to Question 2: Does GREEN establish a real implementation change rather than merely a successful rerun?
**NO.**  
`validate_fix_green()` compares only the test file path, the test file SHA-256, and the executed test node IDs. It does **not** record or compare source-code file hashes, working-tree status, `git diff` outputs, `conftest.py`, `pytest.ini`, or environment variables. Consequently, a flaky or intermittent test that failed in RED and passes on a subsequent rerun without any code change is accepted by `validate_fix_green()` as a valid fix (`VALID_FIX_GREEN`).

### Answer to Question 3: Does trusted execution evidence survive serialization and a new process/session?
**NO (COLLAPSES ACROSS SESSIONS).**  
Because `from_dict()` unconditionally downgrades `provenance_status` to `IMPORTED_UNVERIFIED` to defend against JSON injection, saving a candidate with verified execution envelopes to disk and reloading it in a subsequent process or session causes `has_verified_reproduction()` and `has_verified_fix()` to permanently drop from `True` to `False`. The candidate completely loses its verified execution standing upon session restart.

---

## 2. Git Baseline and Preservation Manifest

All existing work has been strictly preserved. Zero commits, stashes, checkouts, or file modifications were made to code, tests, hooks, or candidates.

- **Active Git Branch:** `feat/engineering-maturity-milestone`
- **Recorded HEAD Commit SHA:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`
- **Working Tree State:** Clean staging index; 17 tracked modified files (exact baseline).
- **Untracked Inventory:** 71 files (aligned with `docs/agent/PROJECT_STATE.md`).

### A. Candidate Artifacts (7 files)
| Candidate Path | SHA-256 Digest |
|---|---|
| `data/learning_candidates/LCAND-060af81e3416.json` | `ae58837e00fd30d5b72fb84710be999376662ca9e4cf2feafef8d909ce67762d` |
| `data/learning_candidates/LCAND-1572b93ec9cc.json` | `90a1f8535af0b51142915956a85444c9498b3bf68d4fdd740c5154ba59f1fa7c` |
| `data/learning_candidates/LCAND-2d10293ac866.json` | `3e3d86cf411f35d9d737cf1927b6bbd5f016f3ff6574b556d96d99967fa04480` |
| `data/learning_candidates/LCAND-480b6011ed6d.json` | `117f906a6598e2191716ba6bf9a47d5060fe986766a30779cc18bacc3c219d4d` |
| `data/learning_candidates/LCAND-4ad4da892e64.json` | `a3757f3e9f6217bb48b39431b0d39536eacb0fb74d0eff3fcea9a94409592610` |
| `data/learning_candidates/LCAND-b9b3a4380d77.json` | `9ab72645310a7e3b6f664d2190fc5e380600154773c92ec646ea89fb25a8e6e0` |
| `data/learning_candidates/LCAND-c42c5d28f6ba.json` | `9af8211f045ca0bc0e44140f274e53b33b15ca446465b55b94ef349163f4e136` |

### B. Event Artifacts (8 files)
| Event Path | SHA-256 Digest |
|---|---|
| `data/learning_events/061fbc2e59c9ceebf1f11d89.json` | `ebc30d95f10b2b26d64a7a570a1e93c270ba23dc5e9b02d2ec2840182ec2a90d` |
| `data/learning_events/20f3ee2a48c69f4598473840.json` | `f68f5d40a99920f673a88b577d8c04630b86f59365e1769eafc551c61fbdcd35` |
| `data/learning_events/78a29e18e72a7f5b67e09fbb.json` | `ef355393337d6f60d82c1cca31772148c7af3edf3fcd98800f4fe0ee75d68c56` |
| `data/learning_events/8a0d872346812c3ab2b8b7f5.json` | `402e60103be634d2a72b95c4498e24275f56fb8a3161e4fe551e80038d61c2b4` |
| `data/learning_events/8a327621fede38c2e56d44ab.json` | `f9bc8548b414478081b5e2b66c3431f582725b02eae13c370a41b207929a70c4` |
| `data/learning_events/b07f38879bb8559e7220307f.json` | `bc876bbc8aacedf5e5c0852e6414a02ec7669f01052ecda4accaf3dc0d066f2f` |
| `data/learning_events/c1d96b1722129b50906a3723.json` | `c98dfc1da3b279709ff9a4b779488ca5f5918e6ee99c196b3a8cc4a970576359` |
| `data/learning_events/cb2ec53a55a829b3cadc3fc2.json` | `bdbee7bed8c700d2e01850febe1e92dfce64ec82f124e40b95393657283110ae` |

### C. Frozen Context Retrieval Files (7 files)
| File Path | SHA-256 Digest | Status |
|---|---|---|
| `docs/agent/LESSONS.md` | `bf11bc69ef8730bb03b40450c18aa04ab16442cf2d7cb5386ee2f342f3ffe670` | **BIT-FOR-BIT UNTOUCHED** |
| `tests/eval/eval_holdout_registry.md` | `275044cb12c9466361174208a5c07a7ff501da023e44dac041f05e5401cec5eb` | **BIT-FOR-BIT UNTOUCHED** |
| `tests/eval/eval_retrieval_holdout.json` | `0f2171a087ec5df9ff5f960e04cf1b63dd67be0b6f8ef7b236a836fecb0545b1` | **BIT-FOR-BIT UNTOUCHED** |
| `tests/eval/eval_retrieval_scenarios.json` | `3cf434392533eca8647acb8367df7a3a14c0e55ac831e8e5876e484cd776ba60` | **BIT-FOR-BIT UNTOUCHED** |
| `tests/eval/eval_scenarios.json` | `3f669f1e3ced93580038000197d7f6d9a0c6d52c0a1f9897fda773f0d7fa8c50` | **BIT-FOR-BIT UNTOUCHED** |
| `tools/context_router.py` | `dbe262e53633473c800828ccc9a1caf2f7ed7030c9a5efafb514cd004f35401e` | **BIT-FOR-BIT UNTOUCHED** |
| `tools/evaluate_lesson_retrieval.py` | `3cbdf0b60a65ccc8cd267f6ac5aa4352ea2474fe78954686f3625f08e37c1ee8` | **BIT-FOR-BIT UNTOUCHED** |

---

## 3. Audit A — Provenance Authenticity

### Detailed Answers to the 10 Specific Inquiries

1. **Can an ordinary caller directly instantiate an envelope with `AUTHORITATIVE_LOCAL_EXECUTION`?**  
   **YES.** `ExecutionEvidenceEnvelope` has a public Python constructor. Any caller in the Python environment can instantiate it directly with `provenance_status="AUTHORITATIVE_LOCAL_EXECUTION"`. When passed to `RedGreenVerificationEngine.validate_baseline_red()`, it is validated as `True, "VALID_BASELINE_RED"`.

2. **Can a caller pass a fabricated subprocess result into any collector API?**  
   **NO.** `ExecutionEvidenceCollector.capture_test_execution()` invokes `subprocess.run()` directly and does not take a mock `CompletedProcess` argument. However, a caller can pass arbitrary pytest flags via `extra_args`.

3. **Can `dataclasses.replace()` or ordinary object mutation alter authoritative fields?**  
   **YES.** `ExecutionEvidenceEnvelope` is an unfrozen `@dataclass`. Any field (e.g. `exit_code`, `tests_failed`, `test_outcomes`) can be mutated in-place or copied via `dataclasses.replace()`, retaining `provenance_status="AUTHORITATIVE_LOCAL_EXECUTION"`.

4. **Can an imported envelope be modified and supplied with a recomputed SHA-256 digest?**  
   **YES.** `compute_content_digest()` is a deterministic SHA-256 hash method without a key. Anyone can modify a dictionary and recompute a matching digest. Furthermore, `compute_content_digest()` is not stored in any field or verified upon deserialization.

5. **Is `envelope_signature` a true MAC/signature or merely another digest?**  
   **DOES NOT EXIST.** Inspection of `tools/execution_evidence.py` confirms that `envelope_signature` is **not present** as a dataclass field or method. The claim in `docs/agent/ENG_OS_03A_EXECUTION_EVIDENCE.md` that an observer HMAC (`OBSERVER-AUTH:<hex16>`) is attached is inaccurate.

6. **If a key exists, who owns it, where is it stored, and what code can access it?**  
   **NO KEY EXISTS.** There is no secret key, symmetric HMAC key, or asymmetric key pair in the codebase.

7. **Can candidate JSON or a model-generated payload set trusted provenance?**  
   **NO (VIA DESERIALIZATION).** When imported through `LessonCandidate.from_dict()`, any envelope claiming `AUTHORITATIVE_LOCAL_EXECUTION` is stripped and overwritten with `IMPORTED_UNVERIFIED`.

8. **Does collector authority depend solely on calling a public Python method?**  
   **YES.** Within the Python process, authority is simply a string field (`provenance_status`) set by `collector.capture_test_execution()` or by direct constructor call.

9. **Does the threat model exclude arbitrary local code execution?**  
   **YES.** The threat model explicitly states that it does not defend against untrusted code executing within the same Python process. However, because agents generate code, this boundary is critical.

10. **What precisely distinguishes an actual collector-created execution from an ordinary Python-constructed object?**  
    **NOTHING.** In-memory instances are structurally and typographically identical.

---

## 4. Audit B — RED-GREEN Implementation Identity

### Empirical Probe Matrix Across 10 Scenarios

The audit evaluated `RedGreenVerificationEngine.validate_fix_green()` across 10 situations in isolated temporary fixtures:

| # | Scenario | Modification Applied | Verification Engine Outcome | Epistemic Assessment |
|---|---|---|---|---|
| **1** | **Identical RED and GREEN Implementation** | Zero source code change; exact same test rerun | `True VALID_FIX_GREEN` | **CRITICAL DEFECT:** Passing a rerun of an identical source state is accepted as a verified fix. |
| **2** | **Changed Implementation (Valid Fix)** | `app.py` modified to fix failure; test unchanged | `True VALID_FIX_GREEN` | **EXPECTED BEHAVIOR:** Legitimate fix validated. |
| **3** | **Changed Test Definition** | Test file assertions mutated (different SHA-256) | `False Test definition mutated` | **PROVENANCE DEFENSE:** Correctly rejected (`TEST_DEFINITION_MUTATED`). |
| **4** | **Changed Pytest Configuration** | `pytest.ini` altered between runs | `True VALID_FIX_GREEN` | **LIMITATION:** Pytest config changes are unrecorded and uncompared. |
| **5** | **Changed Fixture Implementation** | `conftest.py` helper modified between runs | `True VALID_FIX_GREEN` | **LIMITATION:** Fixture changes are unrecorded and uncompared. |
| **6** | **Changed Dependency Version** | `test_runner_version` altered between runs | `True VALID_FIX_GREEN` | **LIMITATION:** Dependency differences between RED and GREEN are not compared. |
| **7** | **Changed Test Selector** | Selector narrowed or altered | `False Test selection mismatch` | **PROVENANCE DEFENSE:** Correctly rejected (`TEST_SELECTION_MISMATCH`). |
| **8** | **Flaky Test Re-run** | Intermittent failure passing on attempt 2 | `True VALID_FIX_GREEN` | **CRITICAL DEFECT:** Flaky tests produce false-positive fix verifications. |
| **9** | **Unrelated Source Changes Only** | Unrelated file modified; test still passes | `True VALID_FIX_GREEN` | **LIMITATION:** The engine does not verify that the modified code touches affected subsystems. |
| **10** | **Missing RED Outcomes Record** | `red_envelope.test_outcomes = {}` | `True VALID_FIX_GREEN` | **DEFECT:** If RED outcomes map is empty, loop executes 0 times and passes without validating that failing test was resolved. |

### Minimum Trustworthy Comparison Model Recommendation
To establish that GREEN represents an actual bugfix:
1. **Source State Fingerprint:** The envelope must capture the Git working-tree status, or a cryptographic hash of all modified files relative to HEAD.
2. **Implementation Non-Identity:** `validate_fix_green()` must assert `green_env.source_state_digest != red_env.source_state_digest` (guaranteeing that code actually changed).
3. **Fixture & Config Invariance:** SHA-256 of active `conftest.py` and `pytest.ini` must match between RED and GREEN.
4. **Mandatory Non-Empty Node Verification:** `validate_fix_green()` must fail closed if `red_envelope.test_outcomes` is empty or contains zero failures.

---

## 5. Audit C — Persisted Evidence and Session Restart

### Investigation of `from_dict()` and Session Boundaries

A core requirement of ENG-OS continuous engineering intelligence is that verified candidates survive across sessions.

An isolated probe simulated candidate serialization and cross-process deserialization:
```python
# In-memory candidate before persistence:
cand.has_verified_reproduction() == True
cand.has_verified_fix() == True
cand.evidence_provenance_status == "FIX_VERIFIED"

# Saved to JSON and loaded in simulated new process:
cand_reloaded = LessonCandidate.from_dict(json.loads(json_str))
cand_reloaded.has_verified_reproduction() == False
cand_reloaded.has_verified_fix() == False
cand_reloaded.evidence_provenance_status == "IMPORTED_UNVERIFIED"
```

### Answers to Audit C Questions:
1. **Can an authoritative envelope be serialized and later reloaded?** Yes, it serializes to valid JSON.
2. **Does reloading permanently downgrade it?** **YES.** `LessonCandidate.from_dict()` strictly downgrades all envelopes to `IMPORTED_UNVERIFIED`.
3. **Can a trusted component revalidate the persisted evidence?** **NO.** There is no revalidation API or cryptographic mechanism to verify that a serialized envelope was genuine.
4. **Does the revalidation method authenticate the original execution observation?** No revalidation method exists.
5. **Are envelope hashes rechecked after loading?** No. `compute_content_digest()` is never called during deserialization.
6. **Are artifact paths confined and content hashes verified on load?** No.
7. **Can evidence files be replaced after the original execution?** Yes, files in `data/execution_evidence/` or `data/learning_candidates/` can be edited on disk without detection by the candidate loader.
8. **Does candidate deserialization silently discard trusted execution history?** It retains the data fields but strips authoritative trust.
9. **Does `has_verified_reproduction()` remain true after restart?** **NO, it becomes `False`.**
10. **Does `has_verified_fix()` remain true after restart?** **NO, it becomes `False`.**

---

## 6. Audit D — RED-GREEN Semantic Correctness

1. **Failure Symptom Consistency:**  
   In `validate_baseline_red()`, the symptom consistency check splits the expected signal into 4-letter words and checks if *any single word* appears anywhere in stdout, stderr, or signatures. This is too coarse: a generic word like `error` or `failed` triggers a false-positive symptom match.
2. **Causal Gap vs. Test Outcome:**  
   Passing a test only proves that the test script exited with code 0 under the given runner. An agent can construct an artificial test that checks an arbitrary condition (e.g. checking an environment variable) to generate a synthetic RED-GREEN pair without addressing the root cause. True causal verification requires human review of the test logic.
3. **Fail-Closed Positive Verifications:**  
   Skips (`SKIPPED`), errors (`ERROR`), collection failures, abnormal process terminations, and timeout aborts are correctly rejected as valid reproductions or fixes.

---

## 7. Audit E — Collector Execution Boundary

1. **Path Confinement:**  
   `validate_confined_path(target_path, allowed_root=self.workspace_root)` is enforced before test execution. Traversal (`../../`) and paths outside the workspace fail closed.
2. **Process Sandboxing:**  
   Tests are executed via `subprocess.run(cmd, shell=False, cwd=str(self.workspace_root))` with an explicit timeout. Shell metacharacters are not interpreted by the OS shell.
3. **Command Whitelist Documentation Discrepancy:**  
   `docs/agent/ENG_OS_03A_EXECUTION_EVIDENCE.md` claimed `validate_test_command` enforced a command whitelist. In reality, no `validate_test_command` function exists in `tools/execution_evidence.py`. The collector directly invokes the resolved pytest binary.
4. **Extra Arguments Surface:**  
   The collector accepts `extra_args: list[str]`. While `shell=False` prevents command injection, pytest arguments (such as `-o` or `-p`) can be supplied.

---

## 8. Audit F — Regression and Verification

Targeted test execution results were independently verified via live runs:

```powershell
python tools/run_fast_tests.py tests/test_execution_evidence.py         # 22 passed in 16.99s (Exit code: 0)
python tools/run_fast_tests.py tests/test_continuity_and_lifecycle.py  # 28 passed in 0.64s  (Exit code: 0)
python tools/run_fast_tests.py tests/test_lesson_candidate.py          # 78 passed in 0.73s  (Exit code: 0)
python tools/run_fast_tests.py tests/test_failure_collector.py         # 41 passed in 0.20s  (Exit code: 0)
python tools/run_fast_tests.py tests/test_hook_integration.py          #  7 passed in 2.32s  (Exit code: 0)
python tools/run_fast_tests.py tests/test_context_router.py            # 60 passed in 0.29s  (Exit code: 0)
# Total targeted tests: 236 passed out of 236 (100% pass rate)
```

- **Frozen Context Retrieval Benchmark:** 38/38 scenarios passed with 100% precision and 0.0000 irrelevant injection.
- **Repository-Wide Test Limitation:** Running the full fast suite (`--fast`) stalls at ~26% due to Windows loopback socket drops inside the standard sandbox (previously documented technical limitation).

---

## 9. Findings Ranked by Severity

| ID | Severity | Subsystem | Finding Summary |
|---|---|---|---|
| **AUD-03A-01** | **CRITICAL** | `lesson_candidate.py` | **Cross-Session Provenance Collapse:** Deserialization unconditionally downgrades envelopes to `IMPORTED_UNVERIFIED`. Upon session restart, `has_verified_reproduction()` and `has_verified_fix()` become `False`, discarding verification standing across sessions. |
| **AUD-03A-02** | **CRITICAL** | `execution_evidence.py` | **Flaky / Rerun Fix Vulnerability:** `validate_fix_green()` does not fingerprint or compare implementation source code. An identical source state or flaky test rerun passing on attempt 2 is accepted as a valid fix. |
| **AUD-03A-03** | **HIGH** | `execution_evidence.py` | **In-Process Provenance Forgery:** `ExecutionEvidenceEnvelope` is an unfrozen dataclass with no private constructor or HMAC token. Any in-process code can instantiate or mutate an authoritative envelope directly. |
| **AUD-03A-04** | **MEDIUM** | `execution_evidence.py` | **Empty RED Outcomes Bypass:** If `red_envelope.test_outcomes` is empty, `validate_fix_green()`'s resolution loop is skipped, returning `VALID_FIX_GREEN`. |
| **AUD-03A-05** | **MEDIUM** | Documentation | **Documentation Discrepancy:** `ENG_OS_03A_EXECUTION_EVIDENCE.md` claimed `envelope_signature`, `observer_identity`, `envelope_content_hash`, and `validate_test_command` exist. None of these exist in code. |
| **AUD-03A-06** | **LOW** | `execution_evidence.py` | **Coarse Symptom Keyword Overlap:** Single 4-letter word overlap satisfies failure consistency check in `validate_baseline_red()`. |

---

## 10. Required Remediation & Acceptance Criteria

To achieve full verification maturity in `ENG-OS-03B`:
1. **Durable Session Provenance:** Implement local cryptographic HMAC or token signing using a local machine secret (or SQLite session execution registry) so that envelopes recorded during valid local runs can be securely re-authenticated upon deserialization.
2. **Implementation Diff Verification:** Require `ExecutionEvidenceEnvelope` to record source-code fingerprints (e.g. hash of modified repository source files or working-tree diff). Assert that GREEN source state differs from RED source state.
3. **Empty Outcomes Guard:** Require `len([k for k, v in red_envelope.test_outcomes.items() if v in ('FAILED', 'ERROR')]) > 0` in `validate_fix_green()`.
4. **Dataclass Hardening:** Set `frozen=True` on `ExecutionEvidenceEnvelope` to prevent in-place tampering.
5. **Documentation Alignment:** Reconcile `docs/agent/ENG_OS_03A_EXECUTION_EVIDENCE.md` with actual dataclass fields.

### Final Acceptance Verdict
**ACCEPTED WITH LIMITATIONS.**  
The execution evidence and RED-GREEN pairing mechanism is sound for active, single-session empirical testing and provides robust test definition invariance. However, the identified limitations regarding cross-session persistence, implementation source tracking, and in-process forgery must be carried forward into ENG-OS-03B.
