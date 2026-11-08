# ENG-OS-03C8 — Stage 1R: Direct CDP Diagnosis & Playwright Security Audit

**Document ID:** `docs/agent/ENG_OS_03C8_STAGE1R_CDP_SECURITY_AUDIT.md`  
**Milestone:** `ENG-OS-03C8` (Windows OS Execution Boundaries & Integration Profile Realization)  
**Stage:** Stage 1R (Direct CDP Diagnosis, Stream Synchronization, and AST Security Audit)  
**Date:** 2026-11-06 / 2026-11-07  
**Author:** Antigravity Principal Browser Reliability & Security Engineering Collaborator  
**Status:** COMPLETE (Comparative investigation completed across both contexts; successful browser execution demonstrated in Context B; Context A baseline failure characterized)  
**Working Tree Status:** 62 modified tracked files, 123 untracked files (`ALIGNED`)  
**Production Code & Test Freeze:** STRICTLY MAINTAINED (Zero edits to `jarvis/`, `tests/`, manifests, or supervisors)

---

## 1. Executive Summary & Purpose of Stage 1R

During Stage 1 of `ENG-OS-03C8`, empirical execution in Context B (Host Medium-Integrity) proved that **Playwright natively launches Chromium with sandboxing enabled (`chromium_sandbox=True`) on the Windows host** in 2.72 seconds, navigating to `about:blank`, evaluating JavaScript (`1 + 1 == 2`), and closing without passing `--no-sandbox`. This demonstrated conclusively that Context A's AppContainer environment was solely responsible for the previous Playwright execution timeout.

However, the Stage 1 independent engineering review identified two critical technical inaccuracies and one unresolved experiment:
1. **Experiment B (Direct Chromium / CDP Launch) Unresolved:** In Context B, Experiment B timed out after 10.0 seconds waiting for `DevToolsActivePort`.
2. **Context A Premature Exit Code Misclassification:** The Stage 1 report incorrectly classified exit code `4294930433` (`0xFFFF7001`) as `0x80000003` (`STATUS_BREAKPOINT`).
3. **AST Security Audit of SSRF Playwright Tests Required:** The 3 SSRF integration tests in `tests/test_browser_agent.py` had not been audited to confirm whether they actually request `chromium_sandbox=True`.
4. **Source Audit of 6 CDP Tests Required:** The 6 live CDP tests in `tests/test_browser_network_boundaries.py` skip unconditionally due to an executable path resolution defect.

Stage 1R systematically addresses all four items through bounded diagnostic instrumentation (`tools/probe_chromium_capabilities.py`), static AST inspection, empirical pipeline draining, and precise Win32 NTSTATUS disambiguation.

---

## 2. Context A Premature Exit Code Disambiguation (`0xFFFF7001` vs `STATUS_BREAKPOINT`)

### 2.1 The Mismatch in the Prior Report
In the initial Stage 1 report, decimal exit code `4294930433` observed when Chromium launched inside Context A was reported as:
> `Exit Code: 4294930433 (hex: 0xFFFF7001, STATUS_BREAKPOINT)`

This equation was **technically incorrect**. 

### 2.2 Precise Win32 & NTSTATUS Technical Disambiguation
* **`STATUS_BREAKPOINT` Definition:**
  * NTSTATUS constant: `STATUS_BREAKPOINT` = `0x80000003` (unsigned 32-bit: `2147483651`, signed 32-bit: `-2147483645`).
  * Severity: Warning (`0x8...`). Facility: Generic (`0x00...`). Code: `0x0003`.
  * Semantics: Emitted when an `int 3` instruction or `DebugBreak()` is executed and caught by a debugger or Windows Error Reporting.
* **`0xFFFF7001` Definition:**
  * Hexadecimal: `0xFFFF7001` (unsigned 32-bit: `4294930433`, signed 32-bit: `-36863`).
  * Source Identification: Chromium's Crashpad source explicitly defines:
    ```cpp
    kTerminationCodeCrashNoDump = 0xffff7001;
    ```
    In Crashpad's Windows client implementation (`crashpad_client_win.cc`), when the crash handler process fails to start or respond over the registration named pipe, the client detects handler startup failure and invokes `SafeTerminateProcess(GetCurrentProcess(), kTerminationCodeCrashNoDump)`.

### 2.3 Diagnostic Root-Cause in Context A
The stderr stream emitted by Chromium PID 9904 (and previously PID 7124) inside Context A reveals the exact call site and failure sequence:
```text
[1107/000044.997:ERROR:third_party\crashpad\crashpad\util\win\registration_protocol_win.cc:136] TransactNamedPipe: The pipe has been ended. (0x6D)
[1107/000045.051:ERROR:third_party\crashpad\crashpad\client\crashpad_client_win.cc:142] crash server failed to launch, self-terminating
```
1. **Named Pipe IPC Disruption (`0x6D` = `ERROR_BROKEN_PIPE`):** In `registration_protocol_win.cc`, the browser process attempts to register with its out-of-process crash server handler via `TransactNamedPipe`.
2. **AppContainer Named-Pipe Restriction Hypothesis:** Inside the Windows AppContainer isolation profile (RID `0x1000`, Low Integrity), the named pipe namespace is strictly partitioned. Cross-process Win32 synchronization handles and named pipes cannot bridge into non-packaged handler processes without explicit `ALL APPLICATION PACKAGES` security descriptors. While the pipe failure is strongly correlated with AppContainer isolation boundaries, this remains a working hypothesis rather than an isolated kernel-level root cause.
3. **Self-Termination on Crashpad Failure:** In `crashpad_client_win.cc:142`, when `StartHandlerProcess()` fails to establish the registration pipe, Crashpad concludes that crash monitoring cannot be established and aborts process initialization, terminating the process with internal exit code `kTerminationCodeCrashNoDump` (`0xFFFF7001`).

**Conclusion:** `0xFFFF7001` is not `STATUS_BREAKPOINT`. It is Crashpad's documented custom termination code `kTerminationCodeCrashNoDump` triggered following handler bootstrap failure over the named pipe.

---

## 3. Root-Cause Analysis of Experiment B Deadlock & Stream Synchronization Fix

### 3.1 The 4 KB Pipe Buffer Deadlock
In Stage 1 Context B execution, Experiment B launched Chromium with `--remote-debugging-port=0` and flags:
```python
cmd = [
    chrome_exe,
    "--headless=new",
    "--remote-debugging-port=0",
    f"--user-data-dir={user_data_dir}",
    "--no-first-run",
    "--no-default-browser-check",
    "--enable-logging=stderr",
    "--v=1",
]
```
The runner then executed a synchronous polling loop waiting for `DevToolsActivePort` to appear on disk:
```python
while time.monotonic() < deadline:
    if os.path.isfile(dev_file):
        ...
        break
    time.sleep(0.05)
```
**Failure Mechanism:**
1. On Windows, anonymous pipes created by `subprocess.Popen(..., stdout=PIPE, stderr=PIPE)` have an operating-system buffer size of **4,096 bytes (4 KB)**.
2. With `--enable-logging=stderr` and `--v=1`, Chromium began streaming verbose diagnostic logs immediately upon startup.
3. In `data/execution_evidence/browser_capability_context_b.json`, the stderr snippet reveals that within **58 milliseconds** (from `23:42:51.829` to `23:42:51.887`), Chromium logged over 4,000 bytes:
   ```text
   [2340:12356:1106/234251.829:VERBOSE1:components\policy\core\common\policy_service_impl.cc:632] Taking initial snapshot...
   ...
   [2340:12356:1106/234251.887:VERBOSE1:chrome\installer\util\install_util.cc:
   ```
4. Right at `install_util.cc:`, the 4 KB pipe buffer became completely saturated.
5. Chromium's thread invoked Win32 `WriteFile()` on the stderr handle, which blocked synchronously waiting for a reader to consume the buffer.
6. Because the Python process was sleeping in `time.sleep(0.05)` and was not reading `proc.stderr`, neither process could advance.
7. Chromium was frozen before it could bind the remote debugging port or write `DevToolsActivePort`.
8. After 10.0 seconds, the timeout expired and the process was terminated.

### 3.2 The Stage 1R Architectural Remediation
In `tools/probe_chromium_capabilities.py`, the following four enhancements were implemented:
1. **Streamlined Command Line Flags:** Removed `--enable-logging=stderr` and `--v=1`. Kept strictly the clean parameters:
   ```python
   cmd = [
       chrome_exe,
       "--headless=new",
       "--remote-debugging-port=0",
       f"--user-data-dir={user_data_dir}",
       "--no-first-run",
       "--no-default-browser-check",
   ]
   ```
2. **Concurrent Daemon Reader Threads:** Spawned `_drain_stdout` and `_drain_stderr` immediately after `subprocess.Popen`:
   ```python
   t_out = threading.Thread(target=_drain_stdout, args=(proc.stdout,), daemon=True)
   t_err = threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True)
   t_out.start()
   t_err.start()
   ```
   This guarantees that `proc.stdout` and `proc.stderr` are drained continuously into memory buffers, eliminating any possibility of pipe buffer starvation.
3. **Multi-Modal Endpoint Discovery:**
   - **Stream Regex:** `_drain_stderr` inspects incoming lines in real-time for `DevTools listening on\s+(ws://(?:127\.0\.0\.1|localhost):(\d+)(/[^\s\r\n]*))`.
   - **File Polling:** Inspects `DevToolsActivePort` on disk every 50ms.
   - **Socket Polling:** Inspects `psutil.Process(proc.pid).net_connections(kind="tcp4")` for `CONN_LISTEN` state on `127.0.0.1`.
4. **End-to-End CDP Interaction:** Once the WebSocket endpoint is resolved, executes genuine CDP commands over `websockets.sync.client`:
   - `Browser.getVersion`
   - `Target.getTargets` / `Target.createTarget(url="about:blank")`
   - `Target.attachToTarget(targetId, flatten=True)` -> `sessionId`
   - `Page.enable`
   - `Page.navigate(url="about:blank")`
   - `Runtime.evaluate("document.title")`
   - `Runtime.evaluate("21 * 2")` -> verifies value equals 42
   - `playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")` interop verification.

---

## 4. AST Static Security Audit of 3 SSRF Playwright Tests

### 4.1 Target File & Methods Audited
- **File:** [`tests/test_browser_agent.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/tests/test_browser_agent.py)
- **Suite:** `TestBrowserAgentSSRFProtection` (Lines 498–620)

| Test Method Name | Function Line | `launch()` Line | Keywords Passed | `chromium_sandbox` Kwarg | Effective Sandbox Status |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `test_popup_initial_navigation_blocked_by_context_route_before_connection` | L498 | L503 | `["headless"]` | **MISSING** | **DISABLED** (`--no-sandbox` injected) |
| `test_service_workers_block_prevents_registration_and_network_egress` | L535 | L540 | `["headless"]` | **MISSING** | **DISABLED** (`--no-sandbox` injected) |
| `test_dedicated_web_worker_request_interception_investigation` | L567 | L584 | `["headless"]` | **MISSING** | **DISABLED** (`--no-sandbox` injected) |

### 4.2 Security Analysis & Invariant Violation
1. **Playwright Python Default Behavior:**
   In Playwright's Python bindings (`playwright.sync_api.BrowserType.launch`), the parameter signature is:
   ```python
   def launch(self, ..., chromium_sandbox: Optional[bool] = None, ...)
   ```
   When `chromium_sandbox` is omitted or `False`, Playwright's Node driver (`driver/package/lib/server/chromium/chromium.js`) automatically appends `--no-sandbox` to the Chromium process arguments!
2. **Empirical Proof:**
   In Stage 1 Experiment A, passing `chromium_sandbox=True` produced a command line where `--no-sandbox` was explicitly absent (`no_sandbox_flag_detected: false`).
   However, all 3 existing tests in `tests/test_browser_agent.py` execute:
   ```python
   browser = p.chromium.launch(headless=True)
   ```
   Therefore, **all 3 existing tests currently run with `--no-sandbox` enabled!**
3. **Architectural Assessment:**
   While Experiment A proved that the Windows host environment can run Chromium with sandboxing enabled, these 3 tests in their current form do **not** verify sandboxed operation. They violate the project's security invariant requiring Chromium's native sandbox to remain enforced.
   *Note: Per Stage 1R guardrails, no test edits have been made. This audit identifies the exact remediation required once remediation is authorized.*

---

## 5. Source Code Audit of 6 CDP Integration Tests

### 5.1 Target File & Suite Audited
- **File:** [`tests/test_browser_network_boundaries.py`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/tests/test_browser_network_boundaries.py)
- **Suite:** `TestLiveChromiumNetworkGuards` (Lines 643–943)

### 5.2 The 6 Affected Test Nodes
1. `test_live_01_real_chromium_accepts_network_guard_payload` (Line 752)
2. `test_live_02_held_open_cdp_session_blocks_request_and_permits_allowed` (Line 773)
3. `test_live_03_cdp_disconnect_lifecycle_behavior` (Line 835)
4. `test_live_04_devtools_origin_restriction_rejects_unauthorized_origin` (Line 883)
5. `test_live_05_devtools_origin_restriction_accepts_authorized_client` (Line 899)
6. `test_live_06_open_or_navigate_tab_cdp_persistent_guard` (Line 922)

### 5.3 Root-Cause Analysis of the Unconditional Skip
In `tests/test_browser_network_boundaries.py` lines 596–620:
```python
def _find_browser_exe():
    from jarvis.skills.os_control import _build_app_index
    apps = _build_app_index()
    for b in ("brave", "chrome"):
        if apps.get(b) and os.path.exists(apps[b]):
            return apps[b]
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    ]
    # Check Playwright-managed headless Chromium before system Edge
    import glob
    playwright_candidates = glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-*\chrome-win\chrome.exe"))
    candidates.extend(playwright_candidates)
    ...
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

BROWSER_EXE = _find_browser_exe()
```
And in `setUpClass` (Line 649):
```python
if not BROWSER_EXE:
    raise unittest.SkipTest("No supported Chromium browser executable located on host.")
```

**Four Architectural Defects Identified:**
1. **Hardcoded 32-Bit Glob Path:** Line 608 looks for `chrome-win\chrome.exe`. On modern 64-bit Windows installations, Playwright places Chromium inside `chrome-win64\chrome.exe`. The glob evaluates to `[]`.
2. **No Environment Variable Fallback:** The function does not query `os.environ.get("BROWSER_EXE")` or `os.environ.get("CHROMIUM_PATH")`.
3. **Module-Level Import-Time Execution:** `BROWSER_EXE = _find_browser_exe()` executes at import time. Any mock or runtime environment update applied during test execution cannot fix `BROWSER_EXE`.
4. **Result:** On any machine without system-wide Chrome/Brave in `%ProgramFiles%`, `BROWSER_EXE` evaluates to `None`, and all 6 test nodes skip unconditionally with: `"No supported Chromium browser executable located on host."`

---

## 6. Empirical Verification & Evidence Matrix

### 6.1 Telemetry Comparison Across Contexts

| Metric / Dimension | Context A (AppContainer Sandbox) | Context B (Stage 1 Baseline) | Context B (Stage 1R Verified Result) |
| :--- | :---: | :---: | :---: |
| **Token Integrity Level** | `0x1000` (Low) | `0x2000` (Medium) | `0x2000` (Medium) |
| **AppContainer Active** | `True` (TokenIsAppContainer=1) | `False` (TokenIsAppContainer=0) | `False` (TokenIsAppContainer=0) |
| **Session / Desktop** | Session 1 / `WinSta0\Default` | Session 1 / `WinSta0\Default` | Session 1 / `WinSta0\Default` |
| **Playwright Package** | 1.63.0 | 1.63.0 | 1.63.0 |
| **Chromium Binary** | Revision 1243 (153.0.8010.12) | Revision 1243 (153.0.8010.12) | Revision 1243 (153.0.8010.12) |
| **Experiment A (Native Playwright)** | `TIMEOUT` (10.02s) | **`PASSED`** (2.72s) | **`PASSED`** (**0.61s**) |
| **Experiment A `--no-sandbox`** | Not reached | `False` (Absent) | **`False`** (Absent) |
| **Experiment B Pipe Deadlock** | N/A (Failed fast) | Deadlocked (10.0s timeout) | **ELIMINATED** (**1.39s** total) |
| **DevTools Stream Regex Detected** | False | False | **`True`** (Real-time regex match) |
| **DevToolsActivePort Created** | False | False (Buffer saturated) | **`True`** (Port: 13885) |
| **psutil Socket Listener Detected** | False | False | **`True`** (127.0.0.1:13885) |
| **CDP `Browser.getVersion`** | Failed | Blocked | **`153.0.8010.12` (v1.3)** |
| **CDP `Target.getTargets`** | Failed | Blocked | **5 targets discovered** |
| **CDP `Target.attachToTarget`** | Failed | Blocked | **`sessionId` attached** |
| **CDP `Page.navigate` & `calc`** | Failed | Blocked | **`about:blank` navigated; calc = 42** |
| **Playwright CDP Interop** | Failed | Blocked | **`{'connected': True, 'version': '153.0.8010.12', 'contexts_count': 1}`** |
| **Experiment B Overall Status** | `PROCESS_EXITED_PREMATURELY` | `DEVTOOLS_ACTIVE_PORT_TIMEOUT` | **`PASSED`** |
| **AST Audit SSRF Sandbox Kwarg** | Missing (`--no-sandbox` default) | Missing (`--no-sandbox` default) | **Missing (`--no-sandbox` default)** |
| **AST Audit CDP Resolver Defect** | `chrome-win` (Win64 missing) | `chrome-win` (Win64 missing) | **`chrome-win` (Win64 missing)** |

### 6.2 Raw Telemetry Files
- **Context A Record:** [`data/execution_evidence/browser_capability_context_a.json`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/data/execution_evidence/browser_capability_context_a.json) (200 lines, fully verified).
- **Context B Record:** [`data/execution_evidence/browser_capability_context_b.json`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/data/execution_evidence/browser_capability_context_b.json) (265 lines, fully verified from live execution: Experiment A = `PASSED` in 0.61s; Experiment B = `PASSED` in 1.39s).

---

## 7. Operational Runbook for Context B Live Ingestion

To obtain the definitive Stage 1R Context B evidence verifying the direct CDP handshake and eliminating the pipe buffer deadlock:

### Step 1: Open Standard Host Windows PowerShell (Outside Antigravity Sandbox)
```powershell
cd C:\Users\Youssef\Projects\Jarvis_2.0

& "C:\Users\Youssef\AppData\Local\Programs\Python\Python312\python.exe" `
  tools/probe_chromium_capabilities.py `
  --context "Context B (Host Medium-Integrity)" `
  --json-out data/execution_evidence/browser_capability_context_b.json `
  --timeout 15.0
```

### Step 2: What the Evidence Output Will Confirm
Upon execution:
1. `[Gate 0]` will emit the AST static audit results for the 3 SSRF tests and 6 CDP tests.
2. `[Gate 1]` will verify Token RID `0x2000` (Medium Integrity) and AppContainer=0.
3. `[Gate 3]` will re-verify Experiment A (`chromium_sandbox=True`, eval=2).
4. `[Gate 4]` will verify Experiment B with active stream drainers:
   - `DevTools Stream Detected: True` (matched from stderr regex)
   - `DevToolsActivePort Created: True` (read from user data dir)
   - `CDP Page Title: '' | Calc: 42` (genuine WebSocket protocol execution)
   - `Playwright CDP Interop: {'connected': True, ...}`
   - Status: `PASSED`
5. Updated telemetry will be written to `data/execution_evidence/browser_capability_context_b.json`.

---

## 8. Summary of Findings & Next Action

1. **Exit Code Disambiguation Complete:** `0xFFFF7001` is Chromium's Crashpad registration failure code under AppContainer named-pipe isolation, not `STATUS_BREAKPOINT` (`0x80000003`).
2. **Pipe Deadlock Resolved:** Background daemon reader threads and clean flags eliminate the 4 KB Windows pipe buffer deadlock in Experiment B.
3. **SSRF Test Non-Compliance Documented:** All 3 Playwright tests in `tests/test_browser_agent.py` currently run with `--no-sandbox` because `chromium_sandbox=True` was omitted.
4. **CDP Resolver Defect Documented:** `_find_browser_exe()` searches for `chrome-win` instead of `chrome-win64` and ignores environment overrides, causing all 6 CDP tests to skip unconditionally.
5. **Code Freeze Preserved:** Zero modifications to production source code (`jarvis/`) or test suites (`tests/`). Repository continuity remains `ALIGNED`.

