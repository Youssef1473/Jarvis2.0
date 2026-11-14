# Phase 2 — Security Stabilization: Frozen Snapshot Manifest

- **Milestone:** Phase 2 Security Stabilization (Audited Scope)
- **Status:** **ACCEPTED & FROZEN**
- **Timestamp:** 2026-11-09T16:22:00+01:00
- **Active Git Branch:** `fix/security-stabilization-p0`
- **Baseline Git HEAD SHA:** `8c395d3f2aa32effb5236ea716a4cba0b4803f8b`
- **Baseline Commit Subject:** `chore: prepare independent engineering audit snapshot`
- **Git Index State:** `0 staged changes` (working tree remains strictly uncommitted)

---

## 1. Tracked Modified Files (15 Files)

| Path | Primary Architectural Security Responsibility |
| :--- | :--- |
| `jarvis/core/security_boundaries.py` | `DualKeyApprovalManager` mandatory context & script digest schema enforcement, process handle lifecycle revocation |
| `jarvis/skills/base.py` | Strict parameter binding and validation in tool registration shims |
| `jarvis/skills/browser_cdp.py` | CDP `uniqueContextId` binding, execution-context verification, script digest validation |
| `jarvis/skills/dev_shell.py` | Handle-relative `NtCreateFile` (`RootDirectory`), `FILE_OPEN_REPARSE_POINT`, handle-bound disposition rollback |
| `jarvis/skills/email_manager.py` | Strict schema generation, removal of `**kwargs` catch-all, attachment byte immutability |
| `jarvis/skills/job_object_sandbox.py` | Win32 Job Object process containment, teardown wait timeouts, exit code 15 prevention |
| `jarvis/skills/os_control.py` | `ProcessHandleRegistry` synchronized lease management (`.lease`), active monotonic expiry, deferred closure |
| `jarvis/skills/rstrtmgr_lock_surgeon.py` | Retained handle termination via lease, elimination of `OpenProcess` PID fallback |
| `jarvis/skills/system_maintenance.py` | Clean import reorganization and lint hygiene |
| `tests/test_email_integration.py` | Direct argument binding assertions, strict schema generation checks |
| `tests/test_flagship_features.py` | Flagship skill integration regression tests |
| `tests/test_job_object_sandbox.py` | Win32 Job Object containment tests |
| `tests/test_os_skills.py` | OS skill automation regression checks |
| `tests/test_p0_action_bound_authorization.py` | Action hash verification, capability monotonic expiry, sandbox filtering |
| `tests/test_tool_authorization_integrity.py` | Direct tool invocation authorization gating |

---

## 2. Untracked Security Audit Test Suites (7 Suites)

| Test Suite Path | Test Count | Focus Area |
| :--- | :--- | :--- |
| `tests/test_filesystem_toctou_security_audit.py` | 12 | Handle-relative directory confinement, TOCTOU junction attacks, disposition rollback |
| `tests/test_gemini_dispatch_security_audit.py` | 5 | Central authorization gateway, automatic callable execution disabled, pre-dispatch reject |
| `tests/test_p0_security_remediation.py` | 27 | P0 security boundary enforcement, SSRF egress validation, path confinement |
| `tests/test_p1_stage6a_execution_security.py` | 13 | Process identity binding, creation timestamp verification, PID recycling defense |
| `tests/test_p1_stage6b_mutation_security.py` | 21 | Destination preimage verification, non-overwrite file creation defense |
| `tests/test_p1_stage6b_addendum_security.py` | 32 | Handle leases, barrier concurrency, CDP `unique_context_id`, leaf reparse point rejection |
| `tests/test_p1_stage6c_approval_transparency.py` | 16 | Strict email schema, parameter validation, attachment previews, human approval transparency |

---

## 3. Dynamic Verification Evidence Summary

- **Combined 12-Suite Run:** `184 passed, 1 skipped in 44.28s` (Exit code: 0).
- **Supplemental Authorization Suite:** `11 passed in 0.14s` (Exit code: 0).
- **AST Structural Diagnostics:** `0 violations` (Exit code: 0).
- **Ruff Linter Delta:** `-23 findings` across modified files (206 baseline -> 183 current), 0 per-file increases.
- **Skipped Test:** `test_ai_generated_argument_cannot_authorize_unmanaged_browser` (host sandbox loopback socket filter).
