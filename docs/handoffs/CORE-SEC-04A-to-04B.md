# Project Handoff: Jarvis 2.0 — Milestone Transition (CORE-SEC-04A to CORE-SEC-04B)

## Executive Summary & Current Status

- **Project:** Jarvis 2.0 (Local Sovereign AI Assistant)
- **Repository Location:** `C:\Users\Youssef\Projects\Jarvis_2.0`
- **Active Branch:** `main`
- **Current HEAD & Baseline Commit:** `7decd74484fe30e14083517a5b5fd7df5403f105` (`fix(security): persist document provenance and restore retrieval taint`)
- **Completed Milestone (Staged for Review):** `CORE-SEC-04A: UNC Confinement, URL Credential Protection & Browser Navigation Hardening`
- **Staged File Manifest (8 Files):**
  - `jarvis/core/security_boundaries.py`
  - `jarvis/skills/browser_cdp.py`
  - `jarvis/skills/dev_shell.py`
  - `jarvis/skills/document_ingest.py`
  - `jarvis/skills/os_control.py`
  - `jarvis/skills/photo_manager.py`
  - `tests/test_browser_cdp.py`
  - `tests/test_security_boundaries.py`
- **Unstaged Changes (1 File):** `.gitignore` (preserving `data/pytest_temp/` housekeeping rule)
- **Untracked Files:** `0` (clean working tree)
- **Commit / Push Status:** **Zero commits and zero remote pushes performed.** The 8 implementation and test files are staged for review only; no commit or push authorization exists.
- **Immediate Next Action for New Conversation:** Await user commit instructions for CORE-SEC-04A or explicit authorization to begin read-only planning for **CORE-SEC-04B: Win32 Job Object Containment for the Antigravity Sidecar**.

---

## A. Project Context

Jarvis 2.0 is an advanced, local-first sovereign AI assistant designed to pair high-performance local neural execution (faster-whisper CUDA transcription, Kokoro-82M neural TTS, Starlette/FastAPI Web & HUD interface) with deep desktop integration (Win32 UI Automation, silent browser automation via Chrome DevTools Protocol, process execution sandboxes, persistent SQLite WAL memory with hybrid vector/BM25 retrieval, and multi-tier cognitive routing).

Because the assistant operates with ambient desktop access, web ingestion, and tool-execution capabilities, security boundaries are enforced across all input and output channels:
- Multi-step tool mutation gating via `DualKeyApprovalManager` in `jarvis/core/security_boundaries.py`.
- Epistemic turn taint tracking via `TurnTaintTracker`.
- Fail-closed SSRF egress filtering via `validate_egress_url()` and socket-level IP pinning.
- Filesystem path confinement via `validate_confined_path()`.
- Windows process tree sandboxing via Win32 Job Objects.

### Verified Current Repository Facts (Inspected Live)
- **Working Tree Path:** `C:\Users\Youssef\Projects\Jarvis_2.0`
- **Branch:** `main` (confirmed via `git branch --show-current`)
- **HEAD Commit SHA:** `7decd74484fe30e14083517a5b5fd7df5403f105`
- **HEAD Commit Subject:** `fix(security): persist document provenance and restore retrieval taint`
- **Staged Diff Stats:** 8 files changed, 357 insertions(+), 28 deletions(-)
- **Unstaged Working Tree:** `.gitignore` modified (adding `data/pytest_temp/`, 1 insertion)
- **Formatting Hygiene:** `git diff --cached --check` and `git diff --check` both pass with exit code 0 (zero trailing whitespace, zero formatting anomalies).

---

## B. Development Protocol and Non-Negotiable Guardrails

All software-engineering agents working on Jarvis 2.0 must adhere strictly to these directives:

1. **Milestone by Milestone:** Focus modification strictly on files, functions, and systems necessary to fulfill the active approved milestone.
2. **Pre-Flight Codebase Inspection:** Inspect actual ASTs, call sites, function signatures, and configuration before authoring or modifying code. Never apply speculative edits.
3. **Targeted Surgical Changes:** Favor minimal, backwards-compatible fixes over broad architectural refactorings. Reuse existing subsystems (`jarvis/core/security_boundaries.py`, `jarvis/skills/base.py`, etc.).
4. **Empirical Truth & Verification:** Never assert that code was tested or verified unless confirmed by real tool execution. Report exact commands, exit codes, execution times, skip counts, and warning origins.
5. **No Persona Bleed:** Act strictly as the Antigravity software-engineering agent. Do not adopt Jarvis's runtime butler persona ("Sir", speech cadences, etc.).
6. **Preserve Unrelated Changes:** Never discard, stash, reset, or overwrite pre-existing uncommitted modifications (specifically preserving `.gitignore`).
7. **Strict Git Gating:** **NEVER stage, unstage, commit, amend, reset, stash, push, or discard files without explicit, written authorization from the user.** Staging authorization does not authorize committing; commit authorization does not authorize pushing.
8. **Intellectual Integrity on Security Scope:** Distinguish verified vulnerabilities from theoretical risks and accepted scope limitations. Never claim "complete isolation" or "zero exposure" when a control only shields direct navigation initiation.
9. **Environment Awareness:** Ground all scripts and commands in Windows 11 / PowerShell 7. Avoid shell string interpolation or `shell=True`.

---

## C. Completed Milestone: CORE-SEC-04A

**Milestone Title:** `UNC Confinement, URL Credential Protection & Browser Navigation Hardening`  
**Baseline:** `7decd74484fe30e14083517a5b5fd7df5403f105`  
**Current State:** 8 files staged, `.gitignore` unstaged.

### Finding 1 — UNC & Device Namespace Path Confinement

- **Risk Mitigated:** Outbound Windows SMB authentication probing and NetNTLM hash leakage triggered when Windows path canonicalization functions (`os.path.realpath`, `Path.resolve`, `os.path.abspath`, `os.stat`) encounter UNC paths (`\\server\share`, `//server/share`), device namespaces (`\\.\PhysicalDrive0`, `//./COM1`), or extended UNC paths (`\\?\UNC\...`).
- **Implementation:**
  - Added [`is_prohibited_network_or_device_path()`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/security_boundaries.py) to [`jarvis/core/security_boundaries.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/security_boundaries.py):
    - Pure string-prefix analysis that evaluates paths *before* any filesystem or canonicalization call.
    - Rejects leading double slashes (`\\`, `//`, `\/`, `/\`), split-drive network shares, NT object manager namespaces (`\??\...`, `/??/...`), extended prefixes (`\\?\`, `//?/`, `\\.\`, `//./`), DOS device names with or without colons (`CON:`, `NUL:`, `PRN:`, `AUX:`, `COM1:`, `LPT1:`), and URI schemes masquerading as paths (`smb://`, `cifs://`, `nfs://`, `file://`, `http://`, etc.).
  - Hardened [`validate_confined_path()`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/security_boundaries.py):
    - Validates both `target_path` and `allowed_root_dir` against `is_prohibited_network_or_device_path()`, raising `SecurityBoundaryViolation` prior to calling `os.path.realpath`.
    - Normalizes DOS device stems by stripping trailing colons before checking `WINDOWS_RESERVED_DEVICE_NAMES`.
  - Hardened Consumers:
    - [`save_photo()`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/photo_manager.py) in [`jarvis/skills/photo_manager.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/photo_manager.py): Validates inputs against `PHOTOS_DIR.parent` / `WORKSPACE_DIR` via `validate_confined_path()`, returning `"Security boundary violation: ..."` and blocking UNC inputs before `Path.expanduser().resolve()`.
    - [`_resolve_workspace_path()`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/dev_shell.py) in [`jarvis/skills/dev_shell.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/dev_shell.py): Uses `validate_confined_path(target, str(WORKSPACE_DIR))`, converting `SecurityBoundaryViolation` to `ValueError` to preserve API compatibility while rejecting UNC shares before `Path.resolve()`.
    - [`extract_text_from_pdf()`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/document_ingest.py) in [`jarvis/skills/document_ingest.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/document_ingest.py): Validates inputs against `is_prohibited_network_or_device_path()` and enforces confinement across approved staging candidate roots (`data/`, workspace root, and system temporary directories via `tempfile.gettempdir()`).

### Finding 2 — URL User-Info Credential Protection

- **Risk Mitigated:** Embedded credentials in URLs (e.g. `http://user:password@host/api`) leaking into application logs, error traces, exceptions, browser history, or downstream services.
- **Implementation:**
  - Updated [`resolve_and_validate_destination()`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/security_boundaries.py) in [`jarvis/core/security_boundaries.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/security_boundaries.py):
    - Rejects URLs where `parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or "")`, raising `SecurityBoundaryViolation("URLs with embedded user-info credentials are prohibited.")`.
    - Sanitized hostname and port parsing exception handling so malformed user-info or token fragments are never echoed in exception strings.
  - Updated [`sanitize_browser_url()`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/browser_cdp.py) in [`jarvis/skills/browser_cdp.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/browser_cdp.py):
    - Rejects embedded user-info credentials immediately, falling back to `"about:blank"` and emitting a static warning that never records the user-info tokens or passwords.

### Finding 3 — Browser Navigation Hardening & Fallback Protection

- **Risk Mitigated:** Bypassing browser SSRF validation by targeting fallback non-CDP subprocess execution paths, or accessing sensitive local background daemons via browser navigation.
- **Implementation:**
  - Updated [`open_browser_tab()`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/os_control.py) in [`jarvis/skills/os_control.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/os_control.py):
    - Validates `target_url` via `sanitize_browser_url()` at the function entry point.
    - If rejected (`validated_url == "about:blank"` while `target_url` is not `about:blank`), immediately returns `"Navigation blocked: Destination URL was rejected by browser security policy, Sir."`.
    - Guarantees unsafe destinations (`file:`, `javascript:`, RFC 1918 private subnets, credentials, shielded ports) never reach the CDP fast path, Chromium IPC dispatch (`subprocess.Popen([_browser_exe, target_url])`), direct executable launch (`subprocess.Popen([browser_exe, target_url])`), shortcut execution (`os.startfile`), or `webbrowser.open_new_tab()`.
  - Expanded [`get_blocked_localhost_ports()`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/browser_cdp.py) in [`jarvis/skills/browser_cdp.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/browser_cdp.py):
    - Shielded background ports: HUD `20129`, CDP `9222`, Ollama `11434`, Docker HTTP `2375`, Docker HTTPS `2376`, PostgreSQL `5432`, Redis `6379`, Kubernetes API `6443`.
  - Updated [`open_or_navigate_tab_cdp()`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/browser_cdp.py) in [`jarvis/skills/browser_cdp.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/browser_cdp.py):
    - Explicitly returns `"Navigation blocked: Destination URL was rejected by browser security policy, Sir."` rather than silently opening a blank tab on rejected inputs.

---

## D. CORE-SEC-04A Test Evidence

*(Note: These are historical test execution results verified and reported during the CORE-SEC-04A milestone; they were not re-executed during this documentation task.)*

### 1. Targeted Unit Suites
- `python tools/run_fast_tests.py tests/test_security_boundaries.py`
  - **Result:** `94 passed, 1 warning in 1.73s (Exit code: 0)`
  - Proven: 22 distinct UNC/device path formats rejected before `os.path.realpath`, `os.path.abspath`, or `os.stat`; photo_manager, document_ingest, and dev_shell reject UNC; URLs with credentials rejected without leaking secrets; legitimate workspace, relative, and staging paths permitted.
- `python tools/run_fast_tests.py tests/test_browser_cdp.py`
  - **Result:** `18 passed, 1 warning in 0.76s (Exit code: 0)`
  - Proven: User-info credentials rejected without logging; background daemon ports shielded; explicit rejection returned; pre-navigation validation protects both CDP and subprocess fallback paths.
- `python tools/run_fast_tests.py tests/test_photo_manager.py` — `5 passed (Exit code: 0)`
- `python tools/run_fast_tests.py tests/test_os_skills.py` — `8 passed (Exit code: 0)`
- `python tools/run_fast_tests.py tests/test_dev_skills.py` — `1 passed (Exit code: 0)`
- `python tools/run_fast_tests.py tests/test_document_provenance.py` — `14 passed (Exit code: 0)`
- `python tools/run_fast_tests.py tests/test_document_ingest.py` — `1 passed (Exit code: 0)`

### 2. Parallel Test Suite & Environmental Test-Runner Caveat
- **Command with Accessible Base Temp:**
  ```powershell
  python -c "import subprocess, sys; from tools.run_fast_tests import HARDWARE_SERIAL_TESTS, get_pytest_cmd; cmd = get_pytest_cmd() + ['-n', '4', '--dist', 'loadfile', '-q', '--basetemp=data/temp_pytest']; [cmd.append(f'--ignore={hw}') for hw in HARDWARE_SERIAL_TESTS]; res = subprocess.run(cmd); sys.exit(res.returncode)"
  ```
  - **Result:** `1025 passed, 16 skipped, 1 warning, 156 subtests passed in 93.27s (Exit code: 0)`
- **Ordinary `python tools/run_fast_tests.py --fast -w 2 -v` Invocation:**
  - **Exit Code:** `3`
  - **Failure Origin:** `INTERNALERROR> PermissionError: [WinError 5] Access is denied: '\\\\?\\C:\\Users\\Youssef\\Projects\\Jarvis_2.0\\data\\pytest_temp'`
  - **Root Cause Classification:** `pytest.ini:7` hardcodes `addopts = --basetemp=data/pytest_temp`. On this host environment, the physical directory `data/pytest_temp` has an NTFS ACL restriction (`Access is denied` even to `icacls`, `takeown`, and `Remove-Item`). When `pytest-xdist` attempts `shutil.rmtree` on `data/pytest_temp` during session initialization, it fails before executing tests.
  - **Status:** Documented as an **environmental test-runner / configuration defect**, not an application code regression. Passing `--basetemp=data/temp_pytest` bypasses the corrupt host directory and runs all 1,025 tests to completion.

### 3. Alternate-IP Verification (Port 8080 Unshielded Retest)
To decouple hostname evaluation from the blocked-port list, alternate representations were tested on unshielded port `8080`:
- Dotted-decimal RFC 1918 (`192.168.1.1:8080`, `10.0.0.1:8080`, `172.16.0.1:8080`): Blocked via explicit non-global address classification.
- Alternate RFC 1918 (integer `3232235777`, hex `0xc0a80101`, octal `0300.0250.0001.0001`, shortened `10.1`): Blocked via fail-closed DNS resolution (`[Errno 11001] getaddrinfo failed`).
- Localhost on unshielded developer ports (`127.0.0.1:8080`, `localhost:8080`): Explicitly permitted per documented project policy.
- Localhost with trailing dot (`localhost.:8080`): Blocked because it fails the explicit `'localhost'` string literal match and resolves to loopback `::1` without loopback permission.

---

## E. Browser Security Scope Limitation

> [!IMPORTANT]
> - **In-Scope Protection (Direct Navigation Initiation):** CORE-SEC-04A strictly secures the destination URL passed to Jarvis's browser navigation entry points (`open_browser_tab`, `open_or_navigate_tab_cdp`). Jarvis will never directly open tabs targeting dangerous URI schemes, internal private networks, cloud metadata services, user-info credentials, or shielded daemon ports.
> - **Acknowledged Limitation (In-Browser Redirects & Client-Side Requests):** CORE-SEC-04A is **not** a complete browser egress sandbox. If Jarvis navigates to a legitimate public website (e.g. `https://example.com`), that website may issue an HTTP 302 redirect to a local or private address (`http://127.0.0.1:20129`), or execute in-page client-side scripts (`fetch("http://127.0.0.1:11434")`). Because Jarvis does not operate an in-line proxy or a persistent CDP `Fetch.enable` network-interception loop across all user browser tabs, Chromium's internal network stack will process redirects and DOM requests according to its own browser policies (CORS, Private Network Access).
> - Complete browser-level egress enforcement is deferred to a dedicated, explicitly authorized future milestone.

---

## F. CORE-SEC-04A Acceptance & Authorization History

- **Acceptance Verdict:** The user reviewed the verification evidence and approved CORE-SEC-04A with the documented browser redirect limitation.
- **Staging Authorization:** The user explicitly authorized staging the 8 implementation and test files while keeping `.gitignore` unstaged.
- **Current Live State:**
  ```text
   M .gitignore
  M  jarvis/core/security_boundaries.py
  M  jarvis/skills/browser_cdp.py
  M  jarvis/skills/dev_shell.py
  M  jarvis/skills/document_ingest.py
  M  jarvis/skills/os_control.py
  M  jarvis/skills/photo_manager.py
  M  tests/test_browser_cdp.py
  M  tests/test_security_boundaries.py
  ```
- **Commit / Push Status:** **No commit or push authorization was granted.** Do not commit or push these staged files without explicit user direction.

---

## G. Next Milestone: CORE-SEC-04B

### Title
**Win32 Job Object Containment for the Antigravity Sidecar**

### Objective
Inspect and harden the process lifecycle of the Antigravity CLI persistent sidecar (`agy.exe`) spawned by `AntigravityDaemon` in `jarvis/core/antigravity_bridge.py`, ensuring leak-free Win32 Job Object containment and guaranteed child-process termination on Windows.

### Historical Context & Audit Findings
*(Note: These are prior audit findings and must be independently verified against the current codebase.)*
- Location: `jarvis/core/antigravity_bridge.py` (around lines 266–350).
- Sidecar Spawn Flags: Currently spawns `agy.exe` with `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW`.
- Permissions: Launches using `--dangerously-skip-permissions` inside `data/agy_runtime_workspace` for zero-overhead background completions.
- Termination Gap: In `AntigravityDaemon.stop()`, the parent process is terminated via `proc.terminate()` / `proc.kill()`, but descendant processes spawned by `agy.exe` (such as CLI subprocesses or compiler tools) are not contained in a Win32 Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
- Pre-existing Subsystem Reference: The codebase already contains an established Win32 Job Object implementation pattern in [`jarvis/skills/job_object_sandbox.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/skills/job_object_sandbox.py) and [`jarvis/core/evolution/sandbox.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/evolution/sandbox.py).

### Required Workflow for CORE-SEC-04B
1. **Pre-flight Check:** Verify branch `main`, HEAD commit SHA, and staged/unstaged git status.
2. **Read-Only Code Audit:**
   - Inspect `jarvis/core/antigravity_bridge.py` (`AntigravityDaemon._spawn_daemon`, `stop`, `execute_turn`).
   - Inspect existing Job Object abstractions in `jarvis/skills/job_object_sandbox.py` and `job_object_sandbox.py` re-exports.
   - Trace callers and test suites (`tests/test_antigravity_bridge.py`, `tests/test_job_object_sandbox_hardening.py`).
3. **Map Process Lifecycle & Failure Modes:**
   - Creation ordering: In Windows, assigning a running process to a Job Object has a race condition where child processes created before assignment escape containment. Mandatory pattern: spawn process suspended (`CREATE_SUSPENDED`), assign to Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, then resume process thread (`ResumeThread`).
   - Evaluate whether `agy.exe` requires suspended creation or if standard Job Object binding fits the persistent daemon model.
   - Map normal shutdown, timeout expiration, unexpected crash, and exception handling paths.
4. **Design Plan & Tests:**
   - Draft a scoped implementation plan and review with the user.
   - Author isolated reproduction/mock tests verifying Job Object assignment and child-tree termination.
5. **Implement & Verify:**
   - Apply minimal changes strictly to `jarvis/core/antigravity_bridge.py` and dedicated test files.
   - Verify with targeted test runs and parallel test suite execution.
   - Require explicit staging authorization before staging.

---

## H. Other Security Findings Awaiting Work

### CORE-SEC-04C: Document-Insertion Delimiter Breakout
- **Vulnerability:** In `jarvis/core/memory/document_vault.py` (specifically `format_document_reference_block()`), retrieved document titles, summaries, and content chunks are formatted into `<reference_data>` and `<document>` XML blocks without escaping synthetic closing tags (e.g. `</reference_data>`, `</document>`).
- **Potential Consequence:** An untrusted document stored in memory could contain adversarial closing tags to break out of the context reference block and inject system-level instructions into the LLM prompt.
- **Planned Direction:** Introduce a context-escaping helper that neutralizes delimiters or formats content safely while preserving chunk sandwiching and provenance metadata.
- **Status:** Deferred until CORE-SEC-04B is complete and explicitly authorized.

---

## I. Earlier Security Work Summary (Git Ancestry)

The following security milestones have been completed and committed to `main`:

| Milestone | Commit SHA | Message | Key Changes |
| :--- | :--- | :--- | :--- |
| **CORE-FIX-01A** | `80be0b0` | `fix(pipeline): repair async tool fallback signature` | Fixed asynchronous fallback method signature in pipeline orchestrator. |
| **CORE-SEC-02A** | `d855754` | `fix(security): harden browser SSRF egress controls` | Closed DNS rebinding race with socket connection pinning; blocked private IP ranges and cloud metadata. |
| **CORE-SEC-02B** | `a2877c3` | `fix(security): enforce approval gates and async cancellation safety` | Gated PowerShell and dangerous tool invocations via `DualKeyApprovalManager`; hardened atomic cancellation. |
| **CORE-SEC-03A** | `f23f0b0` | `fix(security): taint direct ingestion and repair webpage archiving` | Marked ambient web/desktop telemetry with `TurnTaintTracker`; fixed webpage archiving content capture. |
| **CORE-SEC-03B** | `7decd74` | `fix(security): persist document provenance and restore retrieval taint` | Stored `source_provenance` in SQLite document vault; restored turn taint upon retrieving untrusted docs. |

---

## J. Exact Startup Instructions for the New Conversation

```markdown
You are continuing software-engineering work on the Jarvis 2.0 repository. Read this handoff document completely before taking any action.

### Immediate Starting Directives:
1. Ground your state by running git inspection commands:
   git status --short
   git branch --show-current
   git log -3 --oneline
   git diff --cached --stat
2. Confirm that branch is 'main', HEAD is 7decd74, and the eight CORE-SEC-04A files are staged while .gitignore remains unstaged.
3. CORE-SEC-04A is completed and accepted for staging review, with browser egress limitations explicitly documented. Await user instructions regarding commit authorization for CORE-SEC-04A.
4. The next technical milestone is CORE-SEC-04B: Win32 Job Object Containment for the Antigravity Sidecar.
5. When authorized to begin CORE-SEC-04B, start with a strictly READ-ONLY audit of jarvis/core/antigravity_bridge.py and existing Job Object patterns in the codebase. Present a scoped implementation plan and failure-mode analysis.
6. Do NOT modify code, stage, commit, push, or begin unrelated tasks without explicit user authorization.
```
