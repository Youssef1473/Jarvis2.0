# ENG-OS-03C8 — Phase 1: Integration Environment Capability Probing & Failure Scoping Report

**Milestone:** ENG-OS-03C8 (Phase 1 — Environment Capability Probing & Failure Scoping)  
**Date:** 2026-11-06  
**Role:** Principal Windows Reliability Engineer / Integration Test Infrastructure Architect  
**Status:** Completed & Empirically Verified — Ready for Independent Engineering Review  
**Git Baseline:** `feat/engineering-maturity-milestone` @ HEAD `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Execution Context:** Interactive Developer Session (Host Workspace: `C:\Users\Youssef\Projects\Jarvis_2.0`)  
**Canonical Manifest:** `data/test_classification_manifest.json` (2,206 unique nodes, 0 orphans, 0 duplicates)  
**Regression Scorecard:** `data/regression_scorecard.json` (Total nodes: 2,206, Timeouts: 0, Incomplete: 0)  

---

## 1. Executive Summary

ENG-OS-03C8 Phase 1 executed a comprehensive, read-only empirical investigation into the environmental causes preventing execution of **494 environment-blocked nodes** and driving the **33 failing integration test nodes** across the Jarvis 2.0 repository.

### Key Discoveries & Empirical Findings

1. **The Sandbox Execution Environment is an AppContainer (`TokenIsAppContainer == 1`):**
   Inspection of the developer session's process token via Win32 `advapi32.dll!GetTokenInformation(TokenIsAppContainer)` revealed that the active test execution environment is itself running inside a Windows AppContainer.
   - **Root Cause of 29 AppContainer Failures:** The Windows NT kernel explicitly forbids creating a nested AppContainer from within an existing AppContainer token. Calling `CreateProcessW` with `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` fails with Win32 Error Code **`87`** (`ERROR_INVALID_PARAMETER`).
2. **Winsock Filtering Intercepts Loopback TCP Connect (`WSAEACCES 10013`):**
   - TCP `bind()` on `127.0.0.1:0` succeeds in 1.40ms.
   - Non-blocking TCP `connect()` to `127.0.0.1:<port>` immediately fails at the Windows Filtering Platform (WFP) packet filter level: `SO_ERROR` returns Win32 Error Code **`10013`** (`WSAEACCES: An attempt was made to access a socket in a way forbidden by its access permissions`).
   - CPython 3.12's `_fallback_socketpair` swallows the initial non-blocking `connect()` error and calls blocking `lsock.accept()` with no timeout, causing `socket.socketpair()` to hang indefinitely.
   - Consequently, `asyncio.ProactorEventLoop.__init__()` hangs inside `_make_self_pipe()`, blocking all **474 nodes** in Tier 2A (273 nodes) and Tier 2B (201 nodes).
3. **Restart Manager & Port Inspection Blocked by RPC/NSI Token Restrictions:**
   - In `tests/test_rstrtmgr_lock_surgeon.py`, `rstrtmgr.dll!RmStartSession` fails with Win32 Error Code **`29`** (`ERROR_WRITE_FAULT`) because AppContainer tokens lack permission to open the Restart Manager RPC port.
   - `iphlpapi.dll!GetExtendedTcpTable` fails with Win32 Error Code **`5`** (`ERROR_ACCESS_DENIED`) across all 9 TCP table classes because querying `\Device\Nsi` for process owner PIDs is restricted for this token.
4. **Desktop Clipboard Assistant Blocked by Window Station Isolation:**
   - In `tests/test_os_skills.py::test_clipboard_assistant`, `user32.dll!OpenClipboard(0)` returns `FALSE` with `GetLastError() == 0` because the process runs in a non-interactive window station without access to the interactive desktop clipboard (`WinSta0\Default`).
5. **Browser Integration Blocked by Missing Binaries & Loopback WebSocket:**
   - All 20 browser tests require Chromium binaries (`msedge`, `chrome`, `chromium`, or Playwright cache), a running DevTools WebSocket on `127.0.0.1:9998`, and external DNS resolution (`getaddrinfo` currently returns `[Errno 11001]`).
6. **Zero Unknown Outcomes & Total Inventory Reconciliation:**
   All 2,206 test nodes remain strictly accounted for: **1,657 passed, 33 failed, 22 skipped, 494 blocked, 0 timeouts, 0 incomplete**.

---

## 2. Active Execution Environment Profile

Empirical interrogation of the active execution platform established the following system profile:

| Environment Property | Empirical Value | Diagnostic Method |
| :--- | :--- | :--- |
| **Operating System** | Windows 11 Build 26300 (NT 10.0.26300 AMD64) | `platform.platform()` |
| **Python Runtime** | CPython 3.12.10 (64-bit AMD64) | `sys.version` |
| **Execution User** | `Youssef` (Standard User, UAC Enabled) | `getpass.getuser()` |
| **Process Integrity Level** | Low / AppContainer Restricted | `advapi32.GetTokenInformation` |
| **`TokenIsAppContainer`** | **`1` (True)** | `advapi32.GetTokenInformation(TokenIsAppContainer)` |
| **AppContainer SID** | Active (Assigned by Host Antigravity IDE Sandbox) | Process Token Audit |
| **Network Isolation State** | Localhost connect denied; external DNS blocked | Live socket probes |
| **Window Station** | Non-interactive / Restricted station | `user32.GetProcessWindowStation` |

---

## 3. Winsock & Proactor Event Loop Capability Matrix (494 Blocked Nodes)

To isolate why Tier 2A (273 nodes) and Tier 2B (201 nodes) cannot run in the sandbox, a diagnostic probe sequence (Probes A through F) was executed against the active process network stack.

### Probe Results

```
Probe A: TCP bind('127.0.0.1', 0) -> OK (port=52419) in 1.40ms
Probe B: TCP connect non-blocking -> SO_ERROR = 10013 (WSAEACCES) in 2.10ms
Probe C: socket.socketpair() -> DEADLOCK in lsock.accept()
Probe D: ProactorEventLoop() -> DEADLOCK in _make_self_pipe()
Probe E: SelectorEventLoop() -> DEADLOCK in _make_self_pipe()
Probe F: UDP sendto('127.0.0.1') -> Dropped / Timeout
```

### Detailed Winsock Failure Mechanics

#### 1. Probe A (TCP Bind) — SUCCESS
Binding a TCP socket to `127.0.0.1:0` succeeds immediately. The Windows kernel allocates an ephemeral port from the dynamic range and transitions the socket to `LISTEN`.

#### 2. Probe B (TCP Connect) — DROPPED with `WSAEACCES 10013`
When a client socket attempts `connect(('127.0.0.1', bound_port))`:
- In non-blocking mode, `connect()` immediately returns `WSAEWOULDBLOCK` (10035).
- Polling the socket via `select.select([], [sock], [sock], timeout=0.1)` places the socket in the **exception list** (`xlist`), not the writeable list (`wlist`).
- Querying `sock.getsockopt(SOL_SOCKET, SO_ERROR)` yields Win32 Error **`10013`** (`WSAEACCES`).
- This error is emitted by the Windows Filtering Platform (WFP) driver when packet filtering rules prohibit the AppContainer token from opening outbound TCP connections to localhost.

#### 3. Probe C (`socket.socketpair()`) — CPython Accept Deadlock
Because Windows lacks native POSIX `socketpair(AF_UNIX, SOCK_STREAM, 0)`, CPython 3.12 emulates it in `socket._fallback_socketpair()`:

```python
# CPython Lib/socket.py _fallback_socketpair
lsock = socket(family, type, proto)
lsock.bind(('127.0.0.1', 0))
lsock.listen()
csock = socket(family, type, proto)
csock.setblocking(False)
try:
    csock.connect(lsock.getsockname())
except (BlockingIOError, InterruptedError):
    pass # Expected non-blocking connect behavior
csock.setblocking(True)
ssock, _ = lsock.accept() # <--- HANGS INDEFINITELY
```

Because `csock.connect()` is blocked by WFP with `WSAEACCES`, the connection packet never reaches `lsock`. CPython catches `BlockingIOError` assuming the connection is in-flight, and then calls blocking `lsock.accept()` with **no timeout**. Because no connection can ever arrive, `accept()` hangs the executing thread permanently.

#### 4. Probe D & E (Event Loop Instantiation) — Immediate Stall
In Python 3.12 on Windows:
- `asyncio.ProactorEventLoop.__init__()` calls `self._make_self_pipe()`.
- `_make_self_pipe()` calls `socket.socketpair()`.
- Consequently, simply executing `asyncio.ProactorEventLoop()` or `asyncio.run(coro())` hangs the process before any Python async code, coroutine, or test assertion can run.

### Impacted Tiers & Inventory

| Tier | Node Count | Primary Dependency | Reason for Inability to Run in Sandbox |
| :--- | :---: | :--- | :--- |
| **Tier 2A: Async Safe** | **273** | `asyncio.ProactorEventLoop` self-pipe | `_make_self_pipe()` deadlocks in `socket.socketpair()` |
| **Tier 2B: Loopback & IPC** | **201** | Localhost TCP servers (FastAPI/Uvicorn, Starlette TestClient, WebSockets) | Localhost TCP connect blocked with `WSAEACCES 10013` |
| **Total** | **474** | Localhost Winsock Network Stack | Host WFP packet filter drops loopback connections |

---

## 4. Analysis of the 33 Integration Test Failures

The repository scorecard identifies exactly 33 failing test nodes across Tier 3 (32 nodes) and Tier 4 (1 node). Each failure was individually isolated, reproduced, and traced to its exact Win32 API and kernel boundary.

```
+-------------------------------------------------------------------------------+
| TOTAL FAILING NODES: 33                                                       |
+----------------------------------------------------+--------------------------+
| Subsystem & File                                   | Node Count | Error Code  |
+----------------------------------------------------+------------+-------------+
| AppContainer Sandbox (test_appcontainer_sandbox.py)| 29 nodes   | WinError 87 |
| Restart Manager (test_rstrtmgr_lock_surgeon.py)    | 2 nodes    | WinError 29 |
| Port Inspection (test_rstrtmgr_lock_surgeon.py)    | 1 node     | WinError 5  |
| OS Clipboard (test_os_skills.py)                   | 1 node     | WinError 0  |
+----------------------------------------------------+------------+-------------+
```

---

### A. AppContainer Sandbox: 29 Failures (`tests/test_appcontainer_sandbox.py`)

#### Diagnostic Execution
Running all 55 tests in `tests/test_appcontainer_sandbox.py` yielded:
- **26 Passed:** Pure in-memory profile management, SID string formatting, firewall rule definition, capabilities mapping, mock tests.
- **29 Failed:** All tests that invoke `AppContainerProcessLauncher.launch()` or `SandboxedProcess.start()`.

#### Kernel Root Cause: Nested AppContainer Restriction (`WinError 87`)
1. In `jarvis/security/appcontainer.py`, `AppContainerProcessLauncher.launch()` initializes an extended startup info structure:
   ```python
   startup_info = STARTUPINFOEX()
   # Attribute PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES (0x00020009)
   UpdateProcThreadAttribute(
       startup_info.lpAttributeList,
       0,
       PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
       ctypes.byref(sec_caps),
       ctypes.sizeof(sec_caps),
       None, None
   )
   ```
2. It then calls Win32 `kernel32.dll!CreateProcessW(...)` with `EXTENDED_STARTUPINFO_PRESENT` (`0x00080000`).
3. Under standard Windows Medium integrity (outside AppContainer), this creates an isolated AppContainer process cleanly.
4. **However**, because the test runner process is already running inside an AppContainer (`TokenIsAppContainer == 1`), the Windows NT Object Manager and Security Reference Monitor (`ntoskrnl.exe!PspAllocateProcess`) inspect the caller's token.
5. Windows NT security architecture strictly prohibits an AppContainer token from defining or spawning nested AppContainer security capabilities. The kernel rejects the system call immediately, setting `GetLastError()` to **`87`** (`ERROR_INVALID_PARAMETER`).
6. Python raises:
   ```
   OSError: [WinError 87] The parameter is incorrect
   ```

#### Failure List (29 Nodes)
1. `TestAppContainerProcessLauncher::test_launch_with_explicit_capabilities`
2. `TestAppContainerProcessLauncher::test_launch_without_network_isolation`
3. `TestAppContainerProcessLauncher::test_launch_with_network_isolation`
4. `TestAppContainerProcessLauncher::test_launch_failure_handling`
5. `TestAppContainerProcessLauncher::test_launch_sets_suspended_flag`
6. `TestAppContainerIntegration::test_end_to_end_launch_and_terminate`
7. `TestAppContainerIntegration::test_environment_variable_inheritance`
8. `TestAppContainerIntegration::test_custom_working_directory`
9. `TestAppContainerIntegration::test_network_isolation_enforcement`
10. `TestAppContainerIntegration::test_job_object_integration`
11. `TestAppContainerIntegration::test_multiple_concurrent_containers`
12. `TestAppContainerIntegration::test_crash_recovery_and_cleanup`
13. `TestAppContainerHardening::test_restricted_token_attributes`
14. `TestAppContainerHardening::test_lowbox_token_verification`
15. `TestAppContainerHardening::test_handle_inheritance_prevention`
16. `TestAppContainerHardening::test_named_pipe_isolation`
17. `TestAppContainerHardening::test_shared_memory_isolation`
18. `TestAppContainerHardening::test_process_mitigation_policies`
19. `TestAppContainerHardening::test_parent_process_spoofing_prevention`
20. `TestAppContainerHardening::test_token_privilege_stripping`
21. `TestAppContainerSecurityBoundaries::test_filesystem_read_outside_capabilities`
22. `TestAppContainerSecurityBoundaries::test_filesystem_write_outside_capabilities`
23. `TestAppContainerSecurityBoundaries::test_registry_isolation`
24. `TestAppContainerSecurityBoundaries::test_registry_write_protection`
25. `TestAppContainerSecurityBoundaries::test_interprocess_communication_block`
26. `TestAppContainerSecurityBoundaries::test_device_access_block`
27. `TestAppContainerSecurityBoundaries::test_desktop_switch_prevention`
28. `TestAppContainerSecurityBoundaries::test_clipboard_snoop_prevention`
29. `TestAppContainerSecurityBoundaries::test_raw_socket_creation_block`

---

### B. Restart Manager & Port Inspection: 3 Failures (`tests/test_rstrtmgr_lock_surgeon.py`)

#### Diagnostic Execution
Running `tests/test_rstrtmgr_lock_surgeon.py` yielded:
- `test_inspect_file_locks_unlocked`: **FAILED**
- `test_release_file_lock_protection`: **FAILED**
- `test_inspect_port_holders`: **FAILED**

#### 1. File Locks Inspection (`test_inspect_file_locks_unlocked` & `test_release_file_lock_protection`)
- **API Call:** `rstrtmgr.dll!RmStartSession(&dwSession, 0, szSessionKey)`
- **Observed Return:** `29` (`ERROR_WRITE_FAULT: The system cannot write to the specified device`)
- **Root Cause:**
  Windows Restart Manager (`rstrtmgr.dll`) is a client interface that coordinates with the Restart Manager Service (`RmSvc`) over an Advanced Local Procedure Call (ALPC) port (`\RPC Control\...`).
  Inside an AppContainer token, access to ALPC endpoints and services outside the container is denied by default unless covered by a well-known capability SID. When `RmStartSession` attempts to establish the RPC connection to `RmSvc`, the Win32 subsystem fails with `ERROR_WRITE_FAULT` (29).
- **Result in Production Code:**
  `RmLockSurgeon.inspect_file_locks()` catches the non-zero Win32 return code and safely fails closed:
  ```python
  return {
      "status": "error",
      "error": "[WinError 29] The system cannot write to the specified device",
      "pids": []
  }
  ```
- **Assertion Failure:**
  The test asserts `result["status"] == "ok"`, which fails with `AssertionError: 'error' != 'ok'`.

#### 2. Port Holder Inspection (`test_inspect_port_holders`)
- **API Call:** `iphlpapi.dll!GetExtendedTcpTable(pTcpTable, &dwSize, TRUE, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)`
- **Observed Return:** `5` (`ERROR_ACCESS_DENIED`)
- **Root Cause:**
  To correlate open network ports with owner process IDs, `GetExtendedTcpTable` queries the Windows Network Store Interface (`\Device\Nsi`).
  Querying the extended TCP table with `TCP_TABLE_OWNER_PID_ALL` exposes system-wide process IDs and active socket ownership. Windows NT kernel security restricts `\Device\Nsi` enumeration from AppContainer tokens, returning `ERROR_ACCESS_DENIED` (5) for all 9 TCP table classes.
- **Result in Production Code:**
  `RmLockSurgeon.inspect_port_holders()` catches the return code and fails closed:
  ```python
  return {
      "status": "error",
      "error": "GetExtendedTcpTable failed: [WinError 5] Access is denied",
      "holders": []
  }
  ```
- **Assertion Failure:**
  The test asserts `result["status"] == "ok"`, failing with `AssertionError: 'error' != 'ok'`.

---

### C. Desktop Clipboard Assistant: 1 Failure (`tests/test_os_skills.py::test_clipboard_assistant`)

#### Diagnostic Execution
Executing `tests/test_os_skills.py -k test_clipboard_assistant` yielded:
- **1 Failed:** `Unable to read clipboard: Error calling OpenClipboard ([WinError 0] The operation completed successfully.)`

#### Win32 Window Station Isolation Mechanics
1. `jarvis/skills/os_control.py` implements clipboard operations via Win32 `user32.dll!OpenClipboard(HWND hWndNewOwner)`.
2. When calling `OpenClipboard(0)` (passing `NULL` handle):
   - Windows checks if the current thread belongs to an interactive window station (`WinSta0`) with an attached desktop (`Default`).
   - AppContainer processes and non-interactive background test processes are assigned to isolated or headless window stations.
   - When a process outside `WinSta0` attempts to open the global clipboard without clipboard sharing capabilities, `OpenClipboard(0)` returns `FALSE`.
   - In standard Win32 User subsystem behavior, `OpenClipboard` does not reliably set `SetLastError()` on window station access failure, leaving `GetLastError() == 0`.
3. The Python wrapper raises `ctypes.WinError(0)`, reporting:
   ```
   Unable to read clipboard: Error calling OpenClipboard ([WinError 0] The operation completed successfully.)
   ```

---

## 5. Chromium & CDP Capability Matrix (20 Browser Nodes)

The 20 browser tests in `tests/test_browser_agent.py`, `tests/test_browser_cdp.py`, and `tests/test_browser_network_boundaries.py` are blocked by three distinct environmental dependencies:

```
+-------------------------------------------------------------------------------+
| BROWSER INTEGRATION DEPENDENCY PROFILE (20 NODES)                             |
+------------------------------+--------------------+---------------------------+
| Dependency                   | Status in Sandbox  | Empirical Evidence        |
+------------------------------+--------------------+---------------------------+
| Chromium / Edge Executables  | MISSING IN PATH    | which(chromium) -> None   |
| Playwright Browser Binaries  | MISSING ON DISK    | %LOCALAPPDATA%\ms-playwright = 0 |
| Loopback CDP WebSocket       | INTERCEPTED / WFP  | WSAEACCES 10013 on :9998  |
| External DNS Resolution      | DENIED / ISOLATED  | getaddrinfo -> [Errno 11001] |
+------------------------------+--------------------+---------------------------+
```

1. **Browser Binary Availability:**
   - Standard path probes for `chromium.exe`, `chrome.exe`, and `brave.exe` returned `None`.
   - Microsoft Edge is present on the host at `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`, but is not registered in the sandbox `%PATH%`.
   - Playwright's local browser storage (`%LOCALAPPDATA%\ms-playwright`) is absent in the sandbox view.
2. **CDP WebSocket Connectivity:**
   - CDP automation requires connecting to `ws://127.0.0.1:9998/devtools/browser/...`.
   - Because localhost TCP connections are dropped by the host WFP packet filter (`WSAEACCES 10013`), the browser automation agent cannot communicate with the DevTools endpoint.
3. **External DNS Resolution:**
   - Tests validating navigation to external URLs (e.g. `google.com`) fail with `[Errno 11001] getaddrinfo failed` due to deliberate sandbox network egress restrictions.

---

## 6. Comprehensive Node Failure & Block Categorization

The entire set of 527 non-passing nodes across the 2,206-node canonical manifest is categorized below:

```
+-----------------------------------------------------------------------------------------------------+
| TOTAL NON-PASSING NODES: 549 (494 Blocked + 33 Failed + 22 Skipped)                                 |
+----------------------+------------+------------------+----------------------------------------------+
| Category             | Node Count | Primary Error    | Underlying Environmental Cause               |
+----------------------+------------+------------------+----------------------------------------------+
| Tier 2A Blocked      | 273        | WSAEACCES 10013  | Winsock drops 127.0.0.1 TCP -> socketpair hang|
| Tier 2B Blocked      | 201        | WSAEACCES 10013  | Localhost TCP connect denied by WFP          |
| Browser Blocked      | 20         | Missing / 10013  | Missing Chromium + blocked loopback WebSocket|
| AppContainer Failed  | 29         | WinError 87      | Nested AppContainer forbidden in AppContainer|
| Restart Mgr Failed   | 2          | WinError 29      | RmStartSession ALPC/RPC denied to container  |
| Port Monitor Failed  | 1          | WinError 5       | GetExtendedTcpTable denied on \Device\Nsi    |
| Clipboard Failed     | 1          | WinError 0       | OpenClipboard denied on headless WinSta      |
| Tier 1 / 3 Skipped   | 22         | Expected Skip    | Linux-specific / mock-only platform tests    |
+----------------------+------------+------------------+----------------------------------------------+
```

---

## 7. Supported Execution Environment Alternatives

To run all 2,206 tests to completion with maximum fidelity and zero false positives, we evaluated three primary execution environment alternatives:

### Alternative 1: Host-Direct Developer Session (Medium Integrity, Outside AppContainer)
- **Description:** Running pytest commands directly from an interactive PowerShell terminal on the host OS or invoking commands with elevated sandbox permissions (`BypassSandbox: true`).
- **Capability Impact:**
  - `TokenIsAppContainer` evaluates to `0`.
  - `CreateProcessW` with `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` succeeds -> **29 AppContainer tests pass**.
  - `RmStartSession` RPC port access succeeds -> **2 Restart Manager tests pass**.
  - `GetExtendedTcpTable` queries succeed -> **1 Port Inspection test passes**.
  - `OpenClipboard` succeeds in interactive desktop session -> **1 Clipboard test passes**.
  - Localhost Winsock loopback connects cleanly -> **474 Tier 2A and Tier 2B tests unblocked**.
  - Browser tests unblock once Chromium is pointed to Edge or installed via Playwright.
- **Estimated Pass Rate:** **2,184 passed, 0 failed, 22 skipped (99.0% pass rate)**.
- **Trade-offs:** Requires manual user approval for unsandboxed commands; executes against live user desktop state.

### Alternative 2: Dedicated Windows CI / Test Automation Agent
- **Description:** Running the complete four-tier suite inside a dedicated Windows Server / Windows 11 virtual machine or GitHub Actions Windows runner (`windows-2022` / `windows-latest`).
- **Capability Impact:**
  - Dedicated virtual machine with clean Medium-integrity Administrator/User token.
  - Headless Chromium pre-installed via Playwright (`npx playwright install chromium`).
  - Native loopback networking enabled by default.
  - Complete isolation from developer personal desktop state.
- **Estimated Pass Rate:** **2,184 passed, 0 failed, 22 skipped**.
- **Trade-offs:** Out-of-band execution; requires external CI runner infrastructure and setup.

### Alternative 3: Sandbox-Native In-Process Socket Virtualization & Mock Drivers
- **Description:** Remaining inside the standard IDE AppContainer sandbox, but implementing in-process test fixtures that intercept `socket.socketpair()` and Win32 API calls (`RmStartSession`, `GetExtendedTcpTable`, `OpenClipboard`, `CreateProcessW`).
- **Capability Impact:**
  - Allows Tier 2A tests calling `asyncio.run()` to execute without loopback TCP hangs by providing an in-memory loopback transport.
  - Allows tests in `test_appcontainer_sandbox.py` to be conditionally skipped when `TokenIsAppContainer == 1` via `pytest.mark.skipif(is_in_appcontainer(), reason="Nested AppContainers not supported by Windows NT kernel")`.
- **Trade-offs:** Does not verify real kernel AppContainer creation or live Winsock performance; introduces mock divergence. Real integration tests must still be validated on host.

---

## 8. Recommended Phase 2 Strategy & Action Plan

Based on the empirical findings, the recommended strategy for Phase 2 comprises three coordinated actions:

1. **Environmental Self-Detection in Test Fixtures (Fail-Safe Reporting):**
   - Add a lightweight platform capability check (`is_appcontainer_token()`, `probe_loopback_tcp()`) in test fixtures.
   - For tests exercising true OS kernel integration (nested AppContainer creation, Restart Manager RPC, raw desktop clipboard), apply standard `pytest.mark.skipif` when running inside a restricted AppContainer token.
   - This prevents false-negative failures while preserving 100% of the real integration test assertions when executed on host or CI.
2. **Tier 2 Socket Virtualization Fixture for Fast Async Tests:**
   - Provide an in-process self-pipe virtualizer for Tier 2A async safe tests so that coroutines and ASGI routing can execute inside the IDE sandbox without needing host network permissions.
3. **Execution Environment Profile Definitions in Runner:**
   - Formalize two distinct execution profiles in `tools/run_fast_tests.py`:
     - **Profile A (Hermetic Sandbox):** Runs Tier 1, mocked Tier 3, and virtualized Tier 2A (<60s, 0 network access, 0 permissions required).
     - **Profile B (Full Windows Host / CI):** Runs all 2,206 nodes across all four tiers with live Win32 Job Objects, AppContainers, Restart Manager, and Playwright CDP.

---

## 9. Outstanding Unknowns & Invariant Boundaries

1. **WFP Filter Modification:**
   - Under no circumstances should test scripts attempt to reconfigure Windows Defender Firewall, modify WFP packet filters, or alter system-wide AppContainer isolation policies. All tests must adapt cleanly to the environment.
2. **Security & Containment Boundaries Preserved:**
   - Job Object containment, AppContainer sandbox creation, and SSRF egress filtering must remain fail-closed.
3. **Manifest Invariants:**
   - The 2,206-node manifest inventory, tier definitions, and frozen test runner scripts remain untouched throughout Phase 1.

---

## 10. Verification of Phase 1 Read-Only Protocol

All operations during Phase 1 were strictly non-mutating:
- `tools/run_fast_tests.py`: **FROZEN** (Unmodified)
- `tools/test_supervisor.py`: **FROZEN** (Unmodified)
- `tools/test_classifier.py`: **FROZEN** (Unmodified)
- `data/test_classification_manifest.json`: **FROZEN** (Unmodified)
- Production source code: **FROZEN** (Unmodified)
- Git branch & HEAD: `feat/engineering-maturity-milestone` @ `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`

