# ENG-OS-03C8 — Phase 2: Controlled Execution-Context Comparison & Remediation Architecture (Evidence-Corrected Baseline)

**Milestone:** ENG-OS-03C8 (Phase 2 — Controlled Comparison & Remediation Architecture — Corrected Audit Baseline)  
**Date:** 2026-11-06  
**Role:** Principal Windows Reliability Engineer / Integration Test Infrastructure Architect  
**Status:** Completed & Empirically Verified — Ready for Independent Engineering Review  
**Git Baseline:** `feat/engineering-maturity-milestone` @ HEAD `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Execution Context:** Interactive Developer Session (Host Workspace: `C:\Users\Youssef\Projects\Jarvis_2.0`)  
**Canonical Manifest:** `data/test_classification_manifest.json` (2,206 unique nodes, 0 orphans, 0 duplicates)  
**Regression Scorecard:** `data/regression_scorecard.json` (Total nodes: 2,206, Timeouts: 0, Incomplete: 0)  

---

## 1. Executive Summary & Epistemic Grounding

Phase 2 was re-audited and corrected in response to the independent engineering review:

1. **Context B Telemetry Remains Pending:**
   Context A (Antigravity sandbox) has been rigorously investigated and measured. Context B (ordinary Medium-integrity host process) has **not** yet been measured. Consequently, a controlled empirical comparison between the two environments is **pending external execution evidence**.
2. **Separation of Observed API Codes from Inferred Subsystem Causes:**
   Direct API return values (`RmStartSession` returning DWORD 29, `GetExtendedTcpTable` returning DWORD 5, `CreateProcessW` returning Win32 Error Code 87, `OpenClipboard` returning FALSE with last-error 0) are documented strictly as empirical observations, separated from internal kernel hypotheses (such as ALPC/RPC permissions, NSI driver paths, or NT token inheritance limits).
3. **Clipboard Contention Not Eliminated by `GetOpenClipboardWindow() == NULL`:**
   Microsoft documentation explicitly specifies that when a process opens the clipboard with a `NULL` window handle (`OpenClipboard(NULL)`), the clipboard is successfully opened but is not associated with any HWND; in that state, `GetOpenClipboardWindow()` returns `NULL` (0). Therefore, `GetOpenClipboardWindow() == 0` does **not** rule out clipboard contention by another process. Furthermore, `GetLastError() == 0` is non-diagnostic. The root cause of clipboard failure in Context A is classified as **Unresolved**, pending a controlled multi-process test using a verified HWND.
4. **AppContainer `CreateProcessW` Call Chain Isolated; Cause Unresolved:**
   Testing confirmed that `CreateProcessW` with `STARTUPINFOEXW` and an empty attribute list succeeds (`ok == True, last_error == 0`), whereas adding `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` fails with Win32 Error Code **`87`** (`ERROR_INVALID_PARAMETER`). While caller token context is a candidate cause, the complete native attribute setup (structure layout, SID validity, attribute sizes, and flags) must be compared in Context B before asserting an NT kernel nested-container prohibition.
5. **Browser Preflight & SSRF Environmental Dependencies Disentangled:**
   - **Chromium Executable Exists:** Playwright's Chromium binary exists on disk at `C:\Users\Youssef\AppData\Local\ms-playwright\chromium-1243\chrome-win64\chrome.exe`.
   - **Launch Failure Observed:** When spawned in Context A, Chromium self-terminates with return code `4294930433` and logs a Crashpad named pipe error (`TransactNamedPipe: The pipe has been ended. (0x6D)`). However, treating Crashpad failure as the proven cause of process termination is unconfirmed without complete process crash and exit diagnostics.
   - **CDP Loopback & DNS Blocked:** Localhost port 9998 connection fails with `WSAEACCES 10013`; external DNS queries fail with `[Errno 11001] getaddrinfo failed`.
   - **11 SSRF Nodes Retain Environmental Dependencies:** The 11 tests in `tests/test_browser_agent.py::TestBrowserAgentSSRFSuite` mock Playwright objects, but perform real network DNS lookups via `socket.getaddrinfo`. They are not hermetic and must retain their integration classification until environmental DNS dependencies are audited and resolved per node.
6. **Tier 1 & Runner Accounting Corrected:**
   - Tier 1 outcome: **1,228 passed and 9 skipped out of 1,237** (100% of executed Tier 1 tests passed).
   - Proactor self-pipe virtualization enables coroutine dispatch, but does **not** substitute for real localhost TCP/HTTP socket integration. Genuine server tests require an environment where localhost TCP succeeds.
   - Interactive desktop clipboard tests require a verified interactive desktop session, which generic non-interactive CI runners may not provide.
   - No unsupported 99% pass rate is projected or assumed.
7. **Repository Protection & Working Tree Grounding:**
   Protected production code, test files, test runner, supervisor, classifier, manifest, and retrieval evaluation files remain **strictly frozen**. Diagnostic tools (`tools/probe_environment_capabilities.py`), telemetry evidence (`data/execution_evidence/env_probe_context_a.json`), deliverable reports, and state continuity updates were added to the working tree.

---

## 2. Environment Comparison Matrix: Context A vs. Context B

> [!IMPORTANT]
> Context B has not yet been executed. In accordance with epistemic directives, **every Context B entry is explicitly categorized as `PENDING — EXPECTED, NOT OBSERVED`**. No inference is made from process launch expectations.

| Subsystem / Environment Property | Context A: Antigravity Sandbox (Observed Telemetry) | Context B: Host Medium-Integrity Process (Control Procedure) | Epistemic Status |
| :--- | :--- | :--- | :--- |
| **Operating System Platform** | Windows 11 Build 26300 (AMD64) | Windows 11 Build 26300 (Same host) | Host OS identical |
| **Python Runtime** | CPython 3.12.10 (AMD64) | CPython 3.12.10 (Same executable) | Runtime identical |
| **Process Token Container Status** | `TokenIsAppContainer == 1` | `TokenIsAppContainer == 0` | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **Token Integrity Level RID** | `0x1000` (`Low (AppContainer)`) | `0x2000` (`Medium`) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **Token Elevation State** | Standard User (Non-elevated) | Standard User (Non-elevated, `Elevation == False`) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **AppContainer SID** | `S-1-15-2-447416817-...` | None | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **Session ID** | Session 1 | Session 1 | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **Window Station / Desktop** | `WinSta0` / `Default` | `WinSta0` / `Default` | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **TCP Bind (`127.0.0.1:0`)** | **SUCCESS** (0.18ms, port 53672) | `SUCCESS` | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **TCP Connect (`127.0.0.1:<port>`)** | `WSAEWOULDBLOCK` -> `SO_ERROR: 10013 (WSAEACCES)` | `SUCCESS` (`so_error == 0`) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **`socket.socketpair()` Default** | **HUNG** (Thread alive at 1000ms timeout) | `SUCCESS` (Fileno pair returned) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **`ProactorEventLoop()` Construction** | **HUNG** (Thread alive at 1000ms timeout) | `SUCCESS` (Loop instance initialized) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **`CreateProcessW` (Empty Attr)** | **SUCCESS** (`ok == True, last_error == 0`) | `SUCCESS` (`ok == True`) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **`CreateProcessW` (`SECURITY_CAPS`)** | **FAILED** (`ok == False, last_error == 87`) | `SUCCESS` (`ok == True`) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **Production Launcher (`spawn_suspended`)**| **FAILED** (`WinError 87`) | `SUCCESS` (Process handle returned) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **Restart Manager (`RmStartSession`)** | **FAILED** (DWORD `29`, `ERROR_WRITE_FAULT`) | `0` (`ERROR_SUCCESS`) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **`GetTcpTable` (Size Probe)** | **FAILED** (DWORD `5`, `ERROR_ACCESS_DENIED`) | `122` (`ERROR_INSUFFICIENT_BUFFER`) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **`GetExtendedTcpTable` (Classes 0-8)** | **FAILED** (DWORD `5` across all 9 classes) | `122` / `0` | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **`GetOpenClipboardWindow()`** | **`0`** (NULL window handle) | `0` or foreground HWND | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **`OpenClipboard(0)`** | **FAILED** (`ok == False, last_error == 0`) | `SUCCESS` (`ok == True`) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **`OpenClipboard(valid_hwnd)`** | **FAILED** (`ok == False, last_error == 0`) | `SUCCESS` (`ok == True`) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **Chromium on Disk** | **CONFIRMED PRESENT** (`.../chromium-1243/.../chrome.exe`)| **CONFIRMED PRESENT** | Confirmed on host disk |
| **Chromium Process Launch** | **CRASHED** (Returncode `4294930433`, Pipe `0x6D`)| `SUCCESS` (Exit code 0 with `--version`) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **CDP Loopback (`127.0.0.1:9998`)** | **BLOCKED** (`WSAEACCES 10013`) | `SUCCESS` / Connection refused (port idle) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |
| **External DNS (`google.com:80`)** | **BLOCKED** (`[Errno 11001] getaddrinfo failed`) | `SUCCESS` (Resolves IP list) | Context A confirmed; **B: PENDING — EXPECTED, NOT OBSERVED** |

---

## 3. Context A Empirical Telemetry & Native Call Analysis

Context A was profiled using `tools/probe_environment_capabilities.py` running inside the Antigravity sandbox session. Raw output was persisted to `data/execution_evidence/env_probe_context_a.json`:

```json
{
  "context_label": "Context A (Antigravity Sandbox)",
  "timestamp": "2026-11-06T17:06:57Z",
  "host": {
    "platform": "Windows-11-10.0.26300-SP0",
    "architecture": ["64bit", "WindowsPE"],
    "user": "Youssef",
    "pid": 3392
  },
  "python": {
    "executable": "C:\\Users\\Youssef\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
    "version": "3.12.10 (tags/v3.12.10:0cc8128, Apr  8 2025, 12:21:36) [MSC v.1943 64 bit (AMD64)]"
  },
  "token_security": {
    "TokenIsAppContainer": 1,
    "AppContainerSid": "S-1-15-2-447416817-1030210281-2055737164-4064754270-4171815755-4016766877-1078211931",
    "IntegrityLevelRID": "0x1000",
    "IntegrityLevelName": "Low (AppContainer)",
    "SessionId": 1,
    "WindowStation": "WinSta0",
    "Desktop": "Default",
    "GetOpenClipboardWindow": 0,
    "WindowStationHandle": 460,
    "DesktopHandle": 484
  },
  "probe_winsock_asyncio": {
    "tcp_bind": {"status": "SUCCESS", "port": 53672, "duration_ms": 0.18},
    "tcp_nonblocking_connect": {"errno": 10035, "errorcode": "WSAEWOULDBLOCK", "duration_ms": 0.16},
    "tcp_select_so_error": {"readable": false, "writable": false, "exception": true, "so_error": 10013, "so_errorcode": "WSAEACCES", "duration_ms": 0.02},
    "socket_socketpair_default": {"timed_out_hung": true, "result": null, "duration_ms": 1003.07},
    "asyncio_proactor_construction": {"timed_out_hung": true, "result": null, "duration_ms": 1002.7},
    "asyncio_run_coroutine": {"status": "SKIPPED_LOOP_HUNG", "reason": "ProactorEventLoop constructor hung on self-pipe"}
  },
  "probe_appcontainer_process": {
    "control_createprocess_empty_attributes": {"success": true, "last_error": 0},
    "minimal_createprocess_with_security_capabilities": {
      "sizeof_security_capabilities": 24,
      "attribute_list_size": 48,
      "update_proc_thread_attribute_ok": true,
      "update_last_error": 122,
      "create_process_ok": false,
      "create_process_last_error": 87,
      "duration_ms": 0.68
    },
    "production_launcher_spawn": {"success": false, "error": "CreateProcessW in AppContainer failed: WinError 87"}
  },
  "probe_restart_manager": {
    "rm_start_session": {"return_dword": 29, "session_key_buffer_len": 33, "session_key_sizeof_bytes": 66, "duration_ms": 0.15},
    "get_tcp_table_size_probe": {"return_dword": 5, "needed_size_bytes": 0, "duration_ms": 0.14},
    "get_extended_tcp_table_classes": {
      "class_0": {"return_dword": 5, "needed_size_bytes": 0},
      "class_1": {"return_dword": 5, "needed_size_bytes": 0},
      "class_2": {"return_dword": 5, "needed_size_bytes": 0},
      "class_3": {"return_dword": 5, "needed_size_bytes": 0},
      "class_4": {"return_dword": 5, "needed_size_bytes": 0},
      "class_5": {"return_dword": 5, "needed_size_bytes": 0},
      "class_6": {"return_dword": 5, "needed_size_bytes": 0},
      "class_7": {"return_dword": 5, "needed_size_bytes": 0},
      "class_8": {"return_dword": 5, "needed_size_bytes": 0}
    }
  },
  "probe_clipboard": {
    "open_clipboard_window_hwnd": 0,
    "open_clipboard_null_hwnd": {"success": false, "last_error": 0},
    "created_window_hwnd": 6555730,
    "open_clipboard_with_hwnd": {"success": false, "last_error": 0}
  },
  "probe_browser": {
    "playwright_chromium_executable": {
      "path": "C:\\Users\\Youssef\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe",
      "exists": true
    },
    "playwright_chromium_launch": {
      "returncode": 4294930433,
      "stderr": "TransactNamedPipe: The pipe has been ended. (0x6D)... crash server failed to launch, self-terminating"
    },
    "cdp_loopback_port_9998": {"reachable": false, "error": "[WinError 10013] An attempt was made to access a socket in a way forbidden by its access permissions"},
    "external_dns": {"resolves": false, "error": "[Errno 11001] getaddrinfo failed"}
  }
}
```

### Native Call Chain Analysis for `CreateProcessW`
In Context A, process creation was analyzed across the exact native Win32 call sequence:
1. `kernel32.dll!InitializeProcThreadAttributeList(NULL, 1, 0, &attr_size)` -> returned required buffer size of 48 bytes on 64-bit Windows.
2. Allocated attribute buffer: `attr_buf = create_string_buffer(48)`.
3. `InitializeProcThreadAttributeList(attr_buf, 1, 0, &attr_size)` -> succeeded.
4. Initialized `SECURITY_CAPABILITIES` struct:
   - `sizeof(SECURITY_CAPABILITIES) == 24` bytes on AMD64 (`AppContainerSid` pointer [8B], `Capabilities` pointer [8B], `CapabilityCount` [4B], `Reserved` [4B]).
   - `AppContainerSid`: valid pointer returned by `userenv.dll!CreateAppContainerProfile` / `DeriveAppContainerSidFromAppContainerName` (SID string `S-1-15-2-447416817-...`).
   - `Capabilities`: `NULL`; `CapabilityCount`: `0`; `Reserved`: `0`.
5. `UpdateProcThreadAttribute(attr_buf, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, &sec_caps, 24, NULL, NULL)` -> returned `TRUE` (success).
6. Initialized `STARTUPINFOEXW` (`cb = sizeof(STARTUPINFOEXW)`, `lpAttributeList = attr_buf`).
7. Called `CreateProcessW(NULL, "cmd.exe /c exit 0", ..., EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW, ..., &si_ex.StartupInfo, &pi)`.
   - **Result:** `CreateProcessW` returned `FALSE`. Immediate `GetLastError()` was **`87`** (`ERROR_INVALID_PARAMETER`).
8. **Control Comparison in Same Process:** Called `CreateProcessW` with `STARTUPINFOEXW` and an empty attribute list (attr_count = 0) with identical flags.
   - **Result:** `CreateProcessW` returned `TRUE` (`last_error == 0`).
- **Conclusion:** The difference is isolated to passing `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`. Whether this failure is strictly due to the parent process possessing an AppContainer token (`TokenIsAppContainer == 1`) or an interaction with specific attribute construction parameters requires verification in Context B.

---

## 4. Context B Control Procedure & Acceptance Criteria

To obtain empirical Context B telemetry without violating sandbox isolation boundaries, the self-contained script `tools/probe_environment_capabilities.py` must be executed in a separate, ordinary host process.

### Exact Manual Invocation Command

```powershell
# Open an ordinary (non-elevated) PowerShell window outside Antigravity:
cd C:\Users\Youssef\Projects\Jarvis_2.0
python tools/probe_environment_capabilities.py --context "Context B (Host Medium-Integrity)" --json-out data/execution_evidence/env_probe_context_b.json
```

### Three Mandatory Acceptance Gates for Context B Evidence
Before Context B telemetry can be ingested and compared:
1. **Container State Gate:** `token_security.TokenIsAppContainer` must equal **`0`**.
2. **Integrity Level Gate:** `token_security.IntegrityLevelRID` must equal **`"0x2000"`** (`Medium`).
3. **Non-Elevation Gate:** Process must not be elevated (`UAC` token must not be `High` or `Administrator`).

Until an artifact satisfying these three criteria is generated, **Context B is classified as PENDING CONTROL EVIDENCE**.

---

## 5. Revised Cause Classification Register

Following the independent engineering review, each subsystem is classified under standardized confidence levels:

```
+---------------------------------------------------------------------------------------------------------+
| REVISED CAUSE CLASSIFICATION REGISTER                                                                   |
+--------------------------+---------------------+--------------------------------------------------------+
| Subsystem & Condition    | Confidence Category | Observable Basis & Required Next Evidence              |
+--------------------------+---------------------+--------------------------------------------------------+
| Winsock TCP Loopback     | Strongly Supported  | Connect returns WSAEACCES 10013. CPython socketpair()  |
|                          |                     | accept deadlock proven in-process. AppContainer        |
|                          |                     | network isolation is strongly supported; governing WFP |
|                          |                     | policy vs UWP filter unverified. Awaits Context B.     |
+--------------------------+---------------------+--------------------------------------------------------+
| Proactor socketpair()    | Strongly Supported  | Windows Proactor loop self-pipe invokes socketpair().  |
| Event Loop Stall         |                     | Construction hangs in-process. Awaits Context B.       |
+--------------------------+---------------------+--------------------------------------------------------+
| AppContainer Process     | Strongly Supported  | Empty attribute list succeeds; SECURITY_CAPABILITIES   |
| Creation (WinError 87)   | Failure; Cause      | path fails with error 87. Parent token context is      |
|                          | Unresolved          | candidate cause; native call chain comparison in       |
|                          |                     | Context B required to confirm root cause.              |
+--------------------------+---------------------+--------------------------------------------------------+
| Restart Manager (29)     | Cause Unresolved    | RmStartSession directly returns DWORD 29 (WRITE_FAULT).|
|                          |                     | Buffer layout verified. ALPC/RPC hypothesis unproven.  |
|                          |                     | Context B API comparison required.                     |
+--------------------------+---------------------+--------------------------------------------------------+
| TCP Table Access (5)     | Strongly Supported  | GetTcpTable and GetExtendedTcpTable both return DWORD  |
|                          | Access Denial;      | 5 on initial size probe. Token restriction strongly    |
|                          | NSI Origin          | supported; attributing to \Device\Nsi is plausible but |
|                          | Plausible           | requires ETW kernel tracing to confirm origin.         |
+--------------------------+---------------------+--------------------------------------------------------+
| Desktop Clipboard        | Cause Unresolved    | GetOpenClipboardWindow() == 0 does NOT eliminate       |
| Failure                  |                     | contention (OpenClipboard(NULL) returns NULL HWND).    |
|                          |                     | GetLastError() == 0 is non-diagnostic. Root cause     |
|                          |                     | unresolved pending controlled multi-process HWND test. |
+--------------------------+---------------------+--------------------------------------------------------+
| Chromium Launch Failure  | Failure Observed;   | Binary exists on disk. Process self-terminates with    |
|                          | Cause Unresolved    | returncode 4294930433 and Crashpad pipe error (0x6D).  |
|                          |                     | Crashpad causal relationship unconfirmed. Context B    |
|                          |                     | launch diagnostics required.                           |
+--------------------------+---------------------+--------------------------------------------------------+
| Browser CDP & DNS        | Blocked in          | Port 9998 blocked by WSAEACCES 10013; DNS blocked by   |
|                          | Context A           | [Errno 11001]. Awaits Context B comparison.            |
+--------------------------+---------------------+--------------------------------------------------------+
| Browser SSRF Mock Tests  | Classification      | 11 tests in TestBrowserAgentSSRFSuite mock Playwright  |
| (11 Nodes)               | Unresolved          | but execute real DNS resolution via socket.getaddrinfo.|
|                          |                     | Not hermetic. Retain Tier 4 / Browser classification   |
|                          |                     | pending per-node deterministic DNS audit.              |
+--------------------------+---------------------+--------------------------------------------------------+
```

---

## 6. Remediation Architecture & Two-Profile Design

The revised remediation architecture preserves least-privilege principles and maintains a clear epistemic boundary between hermetic unit testing and authoritative Windows integration verification:

```
+-----------------------------------------------------------------------------------------+
| TWO-PROFILE REVISED RUNNER ARCHITECTURE                                                 |
+-----------------------------------------------------------------------------------------+
|                                                                                         |
|   PROFILE A: Hermetic Sandbox Profile (Zero Privileges, Standard IDE AppContainer)      |
|   - Purpose: Fast (<60s) developer feedback with 0 network access and 0 permissions.     |
|   - Coverage:                                                                           |
|     * Tier 1: 1,228 passed, 9 skipped out of 1,237 nodes (100% of executed passed).     |
|     * Tier 3 Mocked: 26 in-memory AppContainer tests + 13 mocked Job Object tests.      |
|     * Tier 2A Coroutines: Async coroutines where in-process self-pipe virtualization     |
|       is provided for internal task wakeups.                                            |
|   - Real OS Integration Handling: Tests requiring real kernel integration (nested       |
|     AppContainers, Restart Manager, interactive clipboard, real TCP listeners) cleanly  |
|     skip with explicit informative skip notices via fail-safe preflight probes.         |
|     Zero false-negative assertion failures.                                             |
|                                                                                         |
|   PROFILE B: Authoritative Windows Integration Profile (Host Medium-Integrity / CI VM)  |
|   - Purpose: Authoritative full-stack regression verification.                          |
|   - Execution Context: Clean Medium-integrity Windows user token outside AppContainer. |
|   - Coverage: All 2,206 test nodes:                                                     |
|     * Real Win32 Job Object containment and process tree termination.                   |
|     * Real AppContainer profile creation, ACL granting, and process sandboxing.         |
|     * Real Restart Manager file-lock inspection and extended TCP tables.                |
|     * Real interactive desktop clipboard (requires verified interactive user desktop).  |
|     * Real Playwright Chromium browser launch, live CDP WebSocket, and SSRF guards.     |
|     * Real localhost TCP servers (FastAPI, Uvicorn, ASGI, pipe listeners).              |
|   - Measured Verification Contract: No pass rate is assumed or forecast. Results will  |
|     be established strictly through empirical execution evidence.                       |
|                                                                                         |
+-----------------------------------------------------------------------------------------+
```

### Critical Architectural Distinctions

1. **Virtualization vs. Real Network Sockets:**
   Providing an in-process socketpair self-pipe allows `asyncio.ProactorEventLoop` to construct its internal waking mechanism without touching the Winsock network stack. However, this does **not** substitute for real localhost TCP servers (Tier 2B). Genuine server tests (FastAPI, Uvicorn, ASGI, WebSockets) require an environment where localhost TCP connections actually complete.
2. **Interactive Desktop Requirement for Clipboard Tests:**
   A standard headless CI runner (e.g. GitHub Actions `windows-latest` running as a background service without an interactive desktop session) may fail clipboard access identically to Context A. Desktop clipboard tests require a verified interactive user session (`WinSta0\Default`) or an explicit `ENVIRONMENT_BLOCKED` classification on headless runners.
3. **SSRF Tests Retain Network Dependencies:**
   The 11 tests in `TestBrowserAgentSSRFSuite` perform real DNS resolution (`socket.getaddrinfo`). They must **not** be moved to Tier 1 without an explicit per-node audit and deterministic mocking of DNS lookups that preserves SSRF validation invariants.

---

## 7. Phased Implementation Roadmap (For Separately Authorized Phase)

Remediation is **not** implemented in this phase. When separately authorized, execution will proceed through the following phased sequence:

```
+-----------------------------------------------------------------------------------------+
| PHASED REMEDIATION IMPLEMENTATION PLAN                                                  |
+-------+----------------------------------+----------------------------------------------+
| Phase | Action Item                      | Technical Scope & Safety Invariants          |
+-------+----------------------------------+----------------------------------------------+
| Step 1| Context B Telemetry Ingestion    | Ingest and verify real Context B probe data  |
|       |                                  | (TokenIsAppContainer == 0, Medium integrity).|
+-------+----------------------------------+----------------------------------------------+
| Step 2| Per-Node SSRF Dependency Audit   | Inspect all 11 SSRF test nodes; identify DNS |
|       |                                  | calls; evaluate deterministic resolution.    |
+-------+----------------------------------+----------------------------------------------+
| Step 3| Capability Preflight Module      | Implement lightweight preflight helpers in   |
|       |                                  | jarvis/utils/environment_probe.py (<1ms).    |
+-------+----------------------------------+----------------------------------------------+
| Step 4| Integration Test Skip Guards     | Apply pytest.mark.skipif in:                 |
|       |                                  | - tests/test_appcontainer_sandbox.py (29)    |
|       |                                  | - tests/test_rstrtmgr_lock_surgeon.py (3)    |
|       |                                  | - tests/test_os_skills.py (1)                |
|       |                                  | to cleanly skip when host capability absent. |
+-------+----------------------------------+----------------------------------------------+
| Step 5| Runner Execution Profiles        | Add --profile sandbox and --profile host/ci  |
|       |                                  | dispatch to tools/run_fast_tests.py.         |
+-------+----------------------------------+----------------------------------------------+
```

---

## 8. Repository Invariants & Working-Tree Grounding

Repository protections and state continuity were maintained throughout Phase 2:
- **Protected Code Intact:** No modifications to production code, test files, test runner (`tools/run_fast_tests.py`), supervisor (`tools/test_supervisor.py`), classifier (`tools/test_classifier.py`), manifest (`data/test_classification_manifest.json`), or retrieval evaluation files.
- **Diagnostic Artifacts Added:** `tools/probe_environment_capabilities.py`, `data/execution_evidence/env_probe_context_a.json`, deliverable reports, and continuity ledger updates.
- **Git Working Tree State:** Branch `feat/engineering-maturity-milestone` @ HEAD `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db` (62 modified tracked files, 114 untracked items).
- **Verification Commands:**
  - `git diff --check`: Clean (0 whitespace errors).
  - `python tools/test_classifier.py --check-orphaned`: Confirmed (0 orphans, 0 duplicates, 2,206 nodes verified).
  - `python tools/continuity_manager.py verify-state`: **`ALIGNED`**.
