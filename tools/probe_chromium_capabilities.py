"""Comprehensive diagnostic probe for Chromium and Playwright capabilities (ENG-OS-03C8 Stage 1).

Executes bounded, isolated diagnostic investigations across:
1. Environment & Token Baseline Verification (Identity, AppContainer, Integrity, Session, Desktop).
2. Playwright & Chromium Binary Verification (Package version, expected vs actual revision).
3. Experiment A — Native Playwright Launch with explicit chromium_sandbox=True.
4. Experiment B — Direct Chromium Launch with --remote-debugging-port=0 and real CDP Target Session protocol.

Adheres strictly to Stage 1 guardrails:
- chromium_sandbox=True explicitly enforced (never relies on default False; never passes --no-sandbox).
- Hard wall-clock bounded execution via Win32 Job Objects with kill_on_close=True.
- Dedicated unique temporary --user-data-dir per experiment; zero pollution of user profiles.
- Genuine CDP target-session protocol via loopback WebSocket: Browser.getVersion -> Target.getTargets ->
  Target.attachToTarget (flatten=True) -> Page.enable -> Page.navigate(about:blank) -> Runtime.evaluate.
- Full Crashpad, stdout, stderr, command line, and process tree lifecycle telemetry.
"""
from __future__ import annotations

import argparse
import ast
import ctypes
import datetime
import getpass
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

# Import Win32 Job Object containment helpers if available
try:
    from jarvis.skills.job_object_sandbox import (
        JobHandleHolder,
        _resume_process,
        _spawn_suspended,
        _terminate_and_drain,
        assign_process_to_job,
        create_process_job_object,
        kernel32,
    )
    HAS_JOB_OBJECT = (kernel32 is not None)
except ImportError:
    HAS_JOB_OBJECT = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    import websockets.sync.client as wsp_client
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False


# ==============================================================================
# 1. Windows Token & Environment Interrogation
# ==============================================================================

def get_token_info() -> dict[str, Any]:
    """Interrogates active process token for AppContainer, integrity level, elevation, and desktop."""
    info: dict[str, Any] = {
        "TokenType": None,
        "TokenTypeName": None,
        "ThreadImpersonationTokenPresent": None,
        "TokenIsAppContainer": None,
        "AppContainerSid": None,
        "IntegrityLevelRID": None,
        "IntegrityLevelName": None,
        "TokenIsElevated": None,
        "TokenElevationType": None,
        "TokenElevationTypeName": None,
        "HasLinkedToken": None,
        "HasAdminGroupSid": None,
        "AdminGroupAttributes": None,
        "SessionId": None,
        "WindowStation": None,
        "Desktop": None,
    }

    if sys.platform != "win32":
        return info

    adv = ctypes.windll.advapi32
    k32 = ctypes.windll.kernel32
    u32 = ctypes.windll.user32

    # Session ID
    sess = wintypes.DWORD(0)
    if k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sess)):
        info["SessionId"] = int(sess.value)

    # Window Station & Desktop
    hwinsta = u32.GetProcessWindowStation()
    buf = ctypes.create_unicode_buffer(512)
    needed = wintypes.DWORD(0)
    UOI_NAME = 2
    if hwinsta and u32.GetUserObjectInformationW(hwinsta, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
        info["WindowStation"] = buf.value

    hdesk = u32.GetThreadDesktop(k32.GetCurrentThreadId())
    if hdesk and u32.GetUserObjectInformationW(hdesk, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
        info["Desktop"] = buf.value

    # Thread impersonation check
    h_th_tok = wintypes.HANDLE()
    TOKEN_QUERY = 0x0008
    if adv.OpenThreadToken(wintypes.HANDLE(-2), TOKEN_QUERY, True, ctypes.byref(h_th_tok)):
        info["ThreadImpersonationTokenPresent"] = True
        k32.CloseHandle(h_th_tok)
    else:
        info["ThreadImpersonationTokenPresent"] = False

    # Process Primary Token inspection
    h_tok = wintypes.HANDLE()
    if adv.OpenProcessToken(wintypes.HANDLE(-1), TOKEN_QUERY, ctypes.byref(h_tok)):
        try:
            ret_len = wintypes.DWORD(0)

            # TokenType = 8 (1 = TokenPrimary, 2 = TokenImpersonation)
            tok_type = wintypes.DWORD(0)
            if adv.GetTokenInformation(h_tok, 8, ctypes.byref(tok_type), ctypes.sizeof(tok_type), ctypes.byref(ret_len)):
                info["TokenType"] = int(tok_type.value)
                info["TokenTypeName"] = "TokenPrimary" if tok_type.value == 1 else "TokenImpersonation"

            # TokenElevation = 20 (TOKEN_ELEVATION struct: DWORD TokenIsElevated)
            elev = wintypes.DWORD(0)
            if adv.GetTokenInformation(h_tok, 20, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(ret_len)):
                info["TokenIsElevated"] = int(elev.value)

            # TokenElevationType = 18 (1 = Default, 2 = Full, 3 = Limited) per MS Learn
            elev_type = wintypes.DWORD(0)
            if adv.GetTokenInformation(h_tok, 18, ctypes.byref(elev_type), ctypes.sizeof(elev_type), ctypes.byref(ret_len)):
                info["TokenElevationType"] = int(elev_type.value)
                type_map = {
                    1: "Default (Token has no linked token)",
                    2: "Full (Elevated token)",
                    3: "Limited (Limited token)",
                }
                info["TokenElevationTypeName"] = type_map.get(elev_type.value, f"Unknown ({elev_type.value})")

            # TokenLinkedToken = 19
            h_linked = wintypes.HANDLE()
            ret_len = wintypes.DWORD(0)
            has_linked = adv.GetTokenInformation(h_tok, 19, ctypes.byref(h_linked), ctypes.sizeof(h_linked), ctypes.byref(ret_len))
            info["HasLinkedToken"] = bool(has_linked and h_linked.value)
            if has_linked and h_linked.value:
                k32.CloseHandle(h_linked)

            # TokenGroups = 2 (Inspect for Administrators group SID S-1-5-32-544)
            ret_len = wintypes.DWORD(0)
            adv.GetTokenInformation(h_tok, 2, None, 0, ctypes.byref(ret_len))
            if ret_len.value > 0:
                buf_grp = ctypes.create_string_buffer(ret_len.value)
                if adv.GetTokenInformation(h_tok, 2, buf_grp, ret_len.value, ctypes.byref(ret_len)):
                    class SID_AND_ATTRIBUTES(ctypes.Structure):
                        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]
                    count = ctypes.cast(buf_grp, ctypes.POINTER(wintypes.DWORD)).contents.value
                    groups_ptr = ctypes.addressof(buf_grp) + 8  # 64-bit alignment padding
                    has_admin = False
                    admin_attrs = None
                    for i in range(count):
                        item = SID_AND_ATTRIBUTES.from_address(groups_ptr + i * ctypes.sizeof(SID_AND_ATTRIBUTES))
                        sid_str = wintypes.LPWSTR()
                        if adv.ConvertSidToStringSidW(ctypes.c_void_p(item.Sid), ctypes.byref(sid_str)):
                            if sid_str.value == "S-1-5-32-544":
                                has_admin = True
                                attr_val = item.Attributes
                                flags = []
                                if attr_val & 0x00000010:
                                    flags.append("SE_GROUP_USE_FOR_DENY_ONLY")
                                if attr_val & 0x00000004:
                                    flags.append("SE_GROUP_ENABLED")
                                if attr_val & 0x00000001:
                                    flags.append("SE_GROUP_MANDATORY")
                                admin_attrs = f"0x{attr_val:08X} ({', '.join(flags) if flags else 'NONE'})"
                            k32.LocalFree(sid_str)
                    info["HasAdminGroupSid"] = has_admin
                    info["AdminGroupAttributes"] = admin_attrs

            # TokenIsAppContainer = 29
            is_app = wintypes.DWORD(0)
            ret_len = wintypes.DWORD(0)
            if adv.GetTokenInformation(h_tok, 29, ctypes.byref(is_app), ctypes.sizeof(is_app), ctypes.byref(ret_len)):
                info["TokenIsAppContainer"] = int(is_app.value)

            # TokenAppContainerSid = 31
            buf_sid = ctypes.create_string_buffer(256)
            if adv.GetTokenInformation(h_tok, 31, buf_sid, ctypes.sizeof(buf_sid), ctypes.byref(ret_len)):
                p_sid = ctypes.cast(buf_sid, ctypes.POINTER(ctypes.c_void_p)).contents.value
                if p_sid:
                    sid_w = wintypes.LPWSTR()
                    if adv.ConvertSidToStringSidW(ctypes.c_void_p(p_sid), ctypes.byref(sid_w)):
                        info["AppContainerSid"] = sid_w.value
                        k32.LocalFree(sid_w)

            # TokenIntegrityLevel = 25
            buf_il = ctypes.create_string_buffer(256)
            if adv.GetTokenInformation(h_tok, 25, buf_il, ctypes.sizeof(buf_il), ctypes.byref(ret_len)):
                p_il_sid = ctypes.cast(buf_il, ctypes.POINTER(ctypes.c_void_p)).contents.value
                if p_il_sid:
                    count_ptr = adv.GetSidSubAuthorityCount(ctypes.c_void_p(p_il_sid))
                    if count_ptr:
                        c = ctypes.cast(count_ptr, ctypes.POINTER(wintypes.BYTE)).contents.value
                        p_rid = adv.GetSidSubAuthority(ctypes.c_void_p(p_il_sid), c - 1)
                        if p_rid:
                            rid = ctypes.cast(p_rid, ctypes.POINTER(wintypes.DWORD)).contents.value
                            info["IntegrityLevelRID"] = f"0x{rid:04X}"
                            name_map = {
                                0x0000: "Untrusted",
                                0x1000: "Low (AppContainer)",
                                0x2000: "Medium",
                                0x3000: "High (Administrator)",
                                0x4000: "System",
                            }
                            info["IntegrityLevelName"] = name_map.get(rid, f"Unknown (0x{rid:04X})")

        finally:
            k32.CloseHandle(h_tok)

    return info


# ==============================================================================
# 2. Binary Identification & Discovery
# ==============================================================================

def identify_browser_binaries() -> dict[str, Any]:
    """Identifies and validates installed Playwright package and Chromium binary paths."""
    res: dict[str, Any] = {
        "playwright_package_version": None,
        "expected_revision": None,
        "expected_browser_version": None,
        "chromium_executable_path": None,
        "chromium_executable_exists": False,
        "chromium_executable_size_bytes": None,
        "manifest_path": None,
        "manifest_exists": False,
        "driver_node_path": None,
        "driver_node_exists": False,
        "browsers_json_path": None,
        "browsers_json_exists": False,
    }

    # Playwright version
    try:
        res["playwright_package_version"] = importlib.metadata.version("playwright")
    except Exception as e:  # noqa: BLE001
        res["playwright_package_version"] = f"Error: {e}"

    # Locate driver browsers.json
    try:
        import playwright
        pw_pkg_dir = Path(playwright.__file__).parent
        bjson = pw_pkg_dir / "driver" / "package" / "browsers.json"
        res["browsers_json_path"] = str(bjson)
        res["browsers_json_exists"] = bjson.is_file()
        if bjson.is_file():
            with open(bjson, "r", encoding="utf-8") as f:
                bdata = json.load(f)
                for item in bdata.get("browsers", []):
                    if item.get("name") == "chromium":
                        res["expected_revision"] = item.get("revision")
                        res["expected_browser_version"] = item.get("browserVersion")
                        break

        node_exe = pw_pkg_dir / "driver" / "node.exe"
        res["driver_node_path"] = str(node_exe)
        res["driver_node_exists"] = node_exe.is_file()
    except Exception as e:  # noqa: BLE001
        res["driver_resolution_error"] = str(e)

    # Resolve expected Playwright chromium executable
    rev = res["expected_revision"] or "1243"
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        cand = Path(local_app_data) / "ms-playwright" / f"chromium-{rev}" / "chrome-win64" / "chrome.exe"
        if not cand.is_file():
            # Check chrome-win alternative
            cand_alt = Path(local_app_data) / "ms-playwright" / f"chromium-{rev}" / "chrome-win" / "chrome.exe"
            if cand_alt.is_file():
                cand = cand_alt

        res["chromium_executable_path"] = str(cand)
        res["chromium_executable_exists"] = cand.is_file()
        if cand.is_file():
            res["chromium_executable_size_bytes"] = cand.stat().st_size
            parent_dir = cand.parent
            # Check manifest file
            ver = res["expected_browser_version"] or "153.0.8010.12"
            manifest = parent_dir / f"{ver}.manifest"
            res["manifest_path"] = str(manifest)
            res["manifest_exists"] = manifest.is_file()

    return res


# ==============================================================================
# 3. Experiment A: Native Playwright Launch (Worker & Supervisor)
# ==============================================================================

def execute_worker_playwright(user_data_dir: str, timeout_sec: float) -> dict[str, Any]:
    """In-worker routine for Experiment A: Native Playwright launch with chromium_sandbox=True."""
    t0 = time.perf_counter()
    res: dict[str, Any] = {
        "status": "INIT",
        "chromium_sandbox_requested": True,
        "executable_path": None,
        "browser_version": None,
        "observed_cmdline": None,
        "no_sandbox_flag_detected": None,
        "navigation_success": False,
        "page_title": None,
        "eval_result": None,
        "crashpad_messages": [],
        "duration_sec": 0.0,
        "error": None,
    }

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        res["status"] = "IMPORT_FAILED"
        res["error"] = f"Failed to import playwright.sync_api: {e}"
        res["duration_sec"] = time.perf_counter() - t0
        return res

    try:
        with sync_playwright() as pw:
            res["executable_path"] = pw.chromium.executable_path

            # Explicitly enforce chromium_sandbox=True per Stage 1 security guardrail
            browser = pw.chromium.launch(
                headless=True,
                chromium_sandbox=True,
                timeout=int(timeout_sec * 1000),
            )
            try:
                res["browser_version"] = browser.version

                # Inspect command line via psutil if available
                if HAS_PSUTIL:
                    try:
                        cur_proc = psutil.Process()
                        for child in cur_proc.children(recursive=True):
                            if "chrome" in child.name().lower():
                                cmdline = child.cmdline()
                                res["observed_cmdline"] = cmdline
                                res["no_sandbox_flag_detected"] = any("--no-sandbox" in arg for arg in cmdline)
                                break
                    except Exception as exc:  # noqa: BLE001
                        res["psutil_inspection_error"] = str(exc)

                # Context & page interaction
                context = browser.new_context()
                try:
                    page = context.new_page()
                    page.goto("about:blank", timeout=int(timeout_sec * 1000))
                    res["navigation_success"] = True
                    res["page_title"] = page.title()
                    res["eval_result"] = page.evaluate("1 + 1")
                    res["status"] = "PASSED"
                finally:
                    context.close()
            finally:
                browser.close()

    except Exception as exc:  # noqa: BLE001
        res["status"] = "EXCEPTION"
        res["error"] = f"{type(exc).__name__}: {exc}"

    res["duration_sec"] = round(time.perf_counter() - t0, 3)
    return res


# ==============================================================================
# 4. Experiment B: Direct Chromium Launch + Real CDP (Worker & Supervisor)
# ==============================================================================

def execute_worker_cdp(chrome_exe: str, user_data_dir: str, timeout_sec: float) -> dict[str, Any]:
    """In-worker routine for Experiment B: Direct Chromium launch with --remote-debugging-port=0."""
    t0 = time.perf_counter()
    res: dict[str, Any] = {
        "status": "INIT",
        "launch_command": [],
        "user_data_dir": user_data_dir,
        "process_created": False,
        "pid": None,
        "exit_code": None,
        "devtools_active_port_created": False,
        "devtools_stream_detected": False,
        "psutil_listener_detected": False,
        "allocated_port": None,
        "ws_path": None,
        "ws_url": None,
        "is_loopback": None,
        "cdp_version_response": None,
        "cdp_targets_count": None,
        "page_target_id": None,
        "session_id": None,
        "page_enable_response": None,
        "page_navigate_response": None,
        "runtime_eval_title": None,
        "runtime_eval_calc": None,
        "playwright_cdp_interop": None,
        "crashpad_messages": [],
        "stderr_snippet": "",
        "stdout_snippet": "",
        "duration_sec": 0.0,
        "error": None,
    }

    if not os.path.isfile(chrome_exe):
        res["status"] = "BINARY_NOT_FOUND"
        res["error"] = f"Chromium binary not found at: {chrome_exe}"
        res["duration_sec"] = time.perf_counter() - t0
        return res

    cmd = [
        chrome_exe,
        "--headless=new",
        "--remote-debugging-port=0",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    res["launch_command"] = cmd

    proc = None
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    discovered_ws_from_stream: str | None = None
    discovered_port_from_stream: int | None = None
    stream_discovery_event = threading.Event()

    def _drain_stdout(pipe: Any) -> None:
        try:
            while line := pipe.readline():
                stdout_lines.append(line)
        except Exception:  # noqa: BLE001, S110
            pass
        finally:
            try:
                pipe.close()
            except Exception:  # noqa: BLE001, S110
                pass

    def _drain_stderr(pipe: Any) -> None:
        nonlocal discovered_ws_from_stream, discovered_port_from_stream
        try:
            while line := pipe.readline():
                stderr_lines.append(line)
                m = re.search(r"DevTools listening on\s+(ws://(?:127\.0\.0\.1|localhost):(\d+)(/[^\s\r\n]*))", line)
                if m:
                    discovered_ws_from_stream = m.group(1)
                    discovered_port_from_stream = int(m.group(2))
                    stream_discovery_event.set()
        except Exception:  # noqa: BLE001, S110
            pass
        finally:
            try:
                pipe.close()
            except Exception:  # noqa: BLE001, S110
                pass

    t_out = None
    t_err = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
        )
        res["process_created"] = True
        res["pid"] = proc.pid

        # Start background drainers immediately to prevent Windows pipe buffer deadlock
        t_out = threading.Thread(target=_drain_stdout, args=(proc.stdout,), daemon=True)
        t_err = threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True)
        t_out.start()
        t_err.start()

        # Wait for DevTools endpoint via stream regex, DevToolsActivePort, or psutil
        dev_file = os.path.join(user_data_dir, "DevToolsActivePort")
        deadline = time.monotonic() + min(timeout_sec, 12.0)
        port_found = False

        while time.monotonic() < deadline:
            if proc.poll() is not None:
                res["exit_code"] = proc.returncode
                res["status"] = "PROCESS_EXITED_PREMATURELY"
                break

            # 1. Check stderr stream regex
            if stream_discovery_event.is_set() and discovered_ws_from_stream:
                res["devtools_stream_detected"] = True
                res["ws_url"] = discovered_ws_from_stream
                res["allocated_port"] = discovered_port_from_stream
                port_found = True

            # 2. Check DevToolsActivePort file
            if os.path.isfile(dev_file):
                try:
                    with open(dev_file, "r", encoding="utf-8") as f:
                        lines = [l.strip() for l in f if l.strip()]
                    if len(lines) >= 2:
                        res["allocated_port"] = int(lines[0])
                        res["ws_path"] = lines[1]
                        res["devtools_active_port_created"] = True
                        if not res["ws_url"]:
                            res["ws_url"] = f"ws://127.0.0.1:{lines[0]}{lines[1]}"
                        port_found = True
                except (OSError, ValueError):
                    pass

            # 3. Check psutil listener
            if HAS_PSUTIL and not res.get("psutil_listener_detected"):
                try:
                    p_obj = psutil.Process(proc.pid)
                    conns = p_obj.net_connections(kind="tcp4")
                    listeners = [
                        c.laddr.port
                        for c in conns
                        if c.status == psutil.CONN_LISTEN and c.laddr.ip in ("127.0.0.1", "0.0.0.0")
                    ]
                    if listeners:
                        res["psutil_listener_detected"] = True
                        if not res["allocated_port"]:
                            res["allocated_port"] = listeners[0]
                except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):  # noqa: BLE001, S110
                    pass

            if port_found:
                break
            time.sleep(0.05)

        if not port_found:
            if res["status"] == "INIT":
                res["status"] = "DEVTOOLS_ENDPOINT_DISCOVERY_TIMEOUT"
                res["error"] = "Neither DevToolsActivePort file nor stderr stream endpoint appeared within deadline."
        else:
            # Endpoint found! Construct WebSocket URL
            res["is_loopback"] = True
            ws_url = res["ws_url"]
            port = res["allocated_port"]

            # Genuine WebSocket handshake and CDP protocol exchange
            if not HAS_WEBSOCKETS:
                res["status"] = "WEBSOCKETS_NOT_AVAILABLE"
                res["error"] = "websockets library not installed in Python environment."
            elif not ws_url:
                res["status"] = "WS_URL_NOT_RESOLVED"
                res["error"] = "Port was detected but ws_url could not be constructed."
            else:
                try:
                    with wsp_client.connect(ws_url, open_timeout=5.0) as ws:
                        def _recv_id(expected_id: int, timeout: float = 5.0) -> dict[str, Any]:
                            d = time.monotonic() + timeout
                            while time.monotonic() < d:
                                try:
                                    raw = ws.recv(timeout=max(0.1, d - time.monotonic()))
                                    if isinstance(raw, (str, bytes, bytearray)):
                                        data = json.loads(raw)
                                        if data.get("id") == expected_id:
                                            return data
                                except (TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError):
                                    continue
                            return {}

                        # 1. Browser.getVersion
                        ws.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
                        ver_reply = _recv_id(1)
                        res["cdp_version_response"] = ver_reply.get("result", {})

                        # 2. Target.getTargets
                        ws.send(json.dumps({"id": 2, "method": "Target.getTargets"}))
                        tgts_reply = _recv_id(2)
                        target_infos = tgts_reply.get("result", {}).get("targetInfos", [])
                        res["cdp_targets_count"] = len(target_infos)

                        # 3. Find or create page target
                        page_target_id = None
                        for t in target_infos:
                            if t.get("type") == "page":
                                page_target_id = t.get("targetId")
                                break
                        if not page_target_id:
                            ws.send(json.dumps({"id": 3, "method": "Target.createTarget", "params": {"url": "about:blank"}}))
                            create_reply = _recv_id(3)
                            page_target_id = create_reply.get("result", {}).get("targetId")

                        res["page_target_id"] = page_target_id

                        if page_target_id:
                            # 4. Attach with Target.attachToTarget and flatten=True
                            ws.send(json.dumps({
                                "id": 4,
                                "method": "Target.attachToTarget",
                                "params": {"targetId": page_target_id, "flatten": True},
                            }))
                            attach_reply = _recv_id(4)
                            session_id = attach_reply.get("result", {}).get("sessionId")
                            res["session_id"] = session_id

                            if session_id:
                                # 5. Page.enable
                                ws.send(json.dumps({"id": 5, "sessionId": session_id, "method": "Page.enable"}))
                                p_en_reply = _recv_id(5)
                                res["page_enable_response"] = p_en_reply.get("result", {})

                                # 6. Page.navigate to about:blank
                                ws.send(json.dumps({
                                    "id": 6,
                                    "sessionId": session_id,
                                    "method": "Page.navigate",
                                    "params": {"url": "about:blank"},
                                }))
                                p_nav_reply = _recv_id(6)
                                res["page_navigate_response"] = p_nav_reply.get("result", {})

                                # 7. Runtime.evaluate document.title
                                ws.send(json.dumps({
                                    "id": 7,
                                    "sessionId": session_id,
                                    "method": "Runtime.evaluate",
                                    "params": {"expression": "document.title", "returnByValue": True},
                                }))
                                eval_reply = _recv_id(7)
                                res["runtime_eval_title"] = eval_reply.get("result", {}).get("result", {}).get("value")

                                # 8. Runtime.evaluate 21 * 2
                                ws.send(json.dumps({
                                    "id": 8,
                                    "sessionId": session_id,
                                    "method": "Runtime.evaluate",
                                    "params": {"expression": "21 * 2", "returnByValue": True},
                                }))
                                calc_reply = _recv_id(8)
                                res["runtime_eval_calc"] = calc_reply.get("result", {}).get("result", {}).get("value")

                                if res["runtime_eval_calc"] == 42:
                                    res["status"] = "PASSED"
                                else:
                                    res["status"] = "EVAL_MISMATCH"
                                    res["error"] = f"Expected 42 from Runtime.evaluate, got {res['runtime_eval_calc']}"

                except Exception as ws_err:  # noqa: BLE001
                    res["status"] = "CDP_COMMUNICATION_ERROR"
                    res["error"] = f"{type(ws_err).__name__}: {ws_err}"

            # Additional check: Playwright connect_over_cdp
            if port:
                try:
                    from playwright.sync_api import sync_playwright
                    with sync_playwright() as pw:
                        cdp_endpoint = f"http://127.0.0.1:{port}"
                        cdp_browser = pw.chromium.connect_over_cdp(cdp_endpoint, timeout=5000)
                        res["playwright_cdp_interop"] = {
                            "connected": True,
                            "version": cdp_browser.version,
                            "contexts_count": len(cdp_browser.contexts),
                        }
                        cdp_browser.close()
                except Exception as pw_cdp_err:  # noqa: BLE001
                    res["playwright_cdp_interop"] = {
                        "connected": False,
                        "error": f"{type(pw_cdp_err).__name__}: {pw_cdp_err}",
                    }

    except Exception as exc:  # noqa: BLE001
        res["status"] = "PROCESS_LAUNCH_EXCEPTION"
        res["error"] = f"{type(exc).__name__}: {exc}"

    finally:
        if proc:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
            except OSError:
                pass

            res["exit_code"] = proc.returncode

        if t_out and t_out.is_alive():
            t_out.join(timeout=1.0)
        if t_err and t_err.is_alive():
            t_err.join(timeout=1.0)

        out_text = "".join(stdout_lines)
        err_text = "".join(stderr_lines)
        res["stdout_snippet"] = out_text.strip()[:1000]
        res["stderr_snippet"] = err_text.strip()[:2000]

        # Parse Crashpad messages
        for line in err_text.splitlines():
            if any(k in line.lower() for k in ("crashpad", "pipe", "crash server")):
                res["crashpad_messages"].append(line.strip())

    res["duration_sec"] = round(time.perf_counter() - t0, 3)
    return res


# ==============================================================================
# 5. Isolated Subprocess Runner under Win32 Job Object Containment
# ==============================================================================

def run_isolated_experiment(
    worker_flag: str,
    extra_args: list[str],
    timeout_sec: float,
    temp_dir: str,
) -> dict[str, Any]:
    """Runs a worker routine in an isolated child process governed by a Win32 Job Object."""
    result_json_file = os.path.join(temp_dir, f"worker_result_{uuid.uuid4().hex[:8]}.json")
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        worker_flag,
        "--result-file",
        result_json_file,
        "--timeout",
        str(timeout_sec),
    ] + extra_args

    t_start = time.monotonic()
    h_job = None
    holder = None
    proc = None
    timed_out = False

    try:
        if sys.platform == "win32" and HAS_JOB_OBJECT:
            h_job = create_process_job_object(kill_on_close=True)
            holder = JobHandleHolder(h_job)
            proc = _spawn_suspended(cmd, cwd=str(WORKSPACE_ROOT))
            if not assign_process_to_job(h_job, proc):
                try:
                    proc.kill()
                except OSError:
                    pass
                raise RuntimeError("Failed to assign child process to Job Object.")
            _resume_process(proc)
        else:
            proc = subprocess.Popen(
                cmd,
                cwd=str(WORKSPACE_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
            )

        deadline = t_start + timeout_sec
        while True:
            ret = proc.poll()
            if ret is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)

        duration = round(time.monotonic() - t_start, 3)

        if timed_out:
            if holder and h_job and HAS_JOB_OBJECT:
                _terminate_and_drain(h_job, exit_code=124, drain_timeout=2.0)
            else:
                try:
                    proc.kill()
                except OSError:
                    pass

            out, err = "", ""
            if proc.stdout:
                out = proc.stdout.read()
            if proc.stderr:
                err = proc.stderr.read()

            return {
                "status": "TIMEOUT",
                "exit_code": 124,
                "duration_sec": duration,
                "error": f"Experiment exceeded bounded deadline of {timeout_sec:.1f}s.",
                "stdout_snippet": out.strip()[:500],
                "stderr_snippet": err.strip()[:500],
            }

        # Child completed normally
        out, err = "", ""
        if proc.stdout:
            out = proc.stdout.read()
        if proc.stderr:
            err = proc.stderr.read()

        if os.path.isfile(result_json_file):
            try:
                with open(result_json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["runner_duration_sec"] = duration
                data["runner_exit_code"] = proc.returncode
                return data
            except Exception as e:  # noqa: BLE001
                return {
                    "status": "JSON_PARSE_ERROR",
                    "exit_code": proc.returncode,
                    "duration_sec": duration,
                    "error": f"Failed to parse worker output: {e}",
                    "stdout_snippet": out.strip()[:500],
                    "stderr_snippet": err.strip()[:500],
                }

        return {
            "status": "WORKER_FAILED_NO_OUTPUT",
            "exit_code": proc.returncode,
            "duration_sec": duration,
            "stdout_snippet": out.strip()[:500],
            "stderr_snippet": err.strip()[:500],
        }

    finally:
        if holder and not holder.is_closed:
            holder.drain(timeout=1.0)
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


# ==============================================================================
# 6. Static AST Audit of Browser Test Suites (SSRF & CDP Tests)
# ==============================================================================

def audit_browser_test_suites() -> dict[str, Any]:
    """Performs AST static inspection of the 3 SSRF tests and 6 CDP integration tests."""
    audit_data: dict[str, Any] = {
        "ssrf_tests": {
            "file": "tests/test_browser_agent.py",
            "exists": False,
            "target_tests": [],
        },
        "cdp_tests": {
            "file": "tests/test_browser_network_boundaries.py",
            "exists": False,
            "resolver_function": "_find_browser_exe",
            "glob_patterns": [],
            "supports_win64_glob": False,
            "has_environ_override": False,
            "module_level_eval": True,
            "skip_reason_literal": None,
            "target_tests": [],
        },
    }

    agent_path = WORKSPACE_ROOT / "tests" / "test_browser_agent.py"
    if agent_path.is_file():
        audit_data["ssrf_tests"]["exists"] = True
        try:
            tree = ast.parse(agent_path.read_text(encoding="utf-8"))
            target_fn_names = {
                "test_popup_initial_navigation_blocked_by_context_route_before_connection",
                "test_service_workers_block_prevents_registration_and_network_egress",
                "test_dedicated_web_worker_request_interception_investigation",
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name in target_fn_names:
                    fn_info: dict[str, Any] = {
                        "name": node.name,
                        "line": node.lineno,
                        "launch_calls": [],
                        "has_chromium_sandbox_kwarg": False,
                        "effective_sandbox": "DISABLED (--no-sandbox passed by Playwright default)",
                    }
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            is_launch = False
                            if isinstance(sub.func, ast.Attribute) and sub.func.attr == "launch":
                                is_launch = True
                            if is_launch:
                                kw_names = [kw.arg for kw in sub.keywords if kw.arg]
                                has_cs = "chromium_sandbox" in kw_names
                                cs_val = None
                                if has_cs:
                                    for kw in sub.keywords:
                                        if kw.arg == "chromium_sandbox" and isinstance(kw.value, ast.Constant):
                                            cs_val = kw.value.value
                                fn_info["launch_calls"].append({
                                    "line": sub.lineno,
                                    "keywords": kw_names,
                                    "chromium_sandbox_passed": has_cs,
                                    "chromium_sandbox_value": cs_val,
                                })
                                if has_cs and cs_val is True:
                                    fn_info["has_chromium_sandbox_kwarg"] = True
                                    fn_info["effective_sandbox"] = "ENABLED (Explicit chromium_sandbox=True)"
                    audit_data["ssrf_tests"]["target_tests"].append(fn_info)
        except Exception as e:  # noqa: BLE001
            audit_data["ssrf_tests"]["parse_error"] = str(e)

    boundaries_path = WORKSPACE_ROOT / "tests" / "test_browser_network_boundaries.py"
    if boundaries_path.is_file():
        audit_data["cdp_tests"]["exists"] = True
        try:
            tree = ast.parse(boundaries_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_find_browser_exe":
                    audit_data["cdp_tests"]["resolver_line"] = node.lineno
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            if "chrome-win" in sub.value:
                                audit_data["cdp_tests"]["glob_patterns"].append(sub.value)
                                if "chrome-win64" in sub.value or "chrome-win*" in sub.value:
                                    audit_data["cdp_tests"]["supports_win64_glob"] = True
                            if "BROWSER_EXE" in sub.value:
                                audit_data["cdp_tests"]["has_environ_override"] = True
                        if isinstance(sub, ast.Attribute) and sub.attr in ("environ", "getenv"):
                            audit_data["cdp_tests"]["has_environ_override"] = True

                if isinstance(node, ast.ClassDef) and node.name == "TestLiveChromiumNetworkGuards":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name.startswith("test_live_"):
                            audit_data["cdp_tests"]["target_tests"].append({
                                "name": item.name,
                                "line": item.lineno,
                            })
                        if isinstance(item, ast.FunctionDef) and item.name == "setUpClass":
                            for sub in ast.walk(item):
                                if (
                                    isinstance(sub, ast.Raise)
                                    and sub.exc
                                    and isinstance(sub.exc, ast.Call)
                                    and isinstance(sub.exc.func, ast.Attribute)
                                    and sub.exc.func.attr == "SkipTest"
                                    and sub.exc.args
                                    and isinstance(sub.exc.args[0], ast.Constant)
                                ):
                                    audit_data["cdp_tests"]["skip_reason_literal"] = sub.exc.args[0].value
        except Exception as e:  # noqa: BLE001
            audit_data["cdp_tests"]["parse_error"] = str(e)

    return audit_data


# ==============================================================================
# 7. Main Orchestrator
# ==============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="ENG-OS-03C8 Stage 1: Chromium & Playwright Capability Probe")
    parser.add_argument("--context", type=str, default="Context A (AppContainer Sandbox)", help="Context label")
    parser.add_argument("--json-out", type=str, default=None, help="Output JSON path")
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-experiment timeout in seconds")
    parser.add_argument("--experiment", choices=["all", "playwright", "cdp"], default="all", help="Experiment selection")

    # Worker dispatch flags (internal)
    parser.add_argument("--worker-playwright", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-cdp", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--result-file", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--user-data-dir", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--chrome-exe", type=str, default=None, help=argparse.SUPPRESS)

    args = parser.parse_args()

    # --- Internal Worker Dispatch ---
    if args.worker_playwright:
        tmp_dir = args.user_data_dir or tempfile.mkdtemp(prefix="pw_probe_")
        res = execute_worker_playwright(tmp_dir, args.timeout)
        if args.result_file:
            with open(args.result_file, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2)
        else:
            print(json.dumps(res, indent=2))
        return 0 if res.get("status") == "PASSED" else 1

    if args.worker_cdp:
        tmp_dir = args.user_data_dir or tempfile.mkdtemp(prefix="cdp_probe_")
        chrome = args.chrome_exe or ""
        res = execute_worker_cdp(chrome, tmp_dir, args.timeout)
        if args.result_file:
            with open(args.result_file, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2)
        else:
            print(json.dumps(res, indent=2))
        return 0 if res.get("status") == "PASSED" else 1

    # --- Main Orchestration Mode ---
    print("=" * 80)
    print(" ENG-OS-03C8 STAGE 1: CHROMIUM & PLAYWRIGHT CAPABILITY INVESTIGATION")
    print("=" * 80)
    print(f"Context Label:  {args.context}")
    print(f"Timeout Budget: {args.timeout:.1f}s per experiment")
    print(f"Timestamp:      {datetime.datetime.now(datetime.UTC).isoformat()}")

    # 0. AST Static Audit
    print("\n[Gate 0] AST Static Audit of Browser Test Suites...")
    ast_audit = audit_browser_test_suites()
    ssrf_tests = ast_audit["ssrf_tests"]["target_tests"]
    print(f"  SSRF Tests Audited: {len(ssrf_tests)} in {ast_audit['ssrf_tests']['file']}")
    for st in ssrf_tests:
        print(f"    - {st['name']} (L{st['line']}): Sandbox: {st['effective_sandbox']}")
    cdp_info = ast_audit["cdp_tests"]
    print(f"  CDP Tests Audited:  {len(cdp_info['target_tests'])} in {cdp_info['file']}")
    print(f"    - Resolver: {cdp_info['resolver_function']} | Supports win64 glob: {cdp_info['supports_win64_glob']} | Env override: {cdp_info['has_environ_override']}")
    print(f"    - Skip condition in setUpClass: '{cdp_info['skip_reason_literal']}'")

    # 1. Environment & Token Interrogation
    print("\n[Gate 1] Collecting process token and environment baseline...")
    tok = get_token_info()
    host_info = {
        "platform": platform.platform(),
        "windows_version": platform.win32_ver() if sys.platform == "win32" else [],
        "architecture": platform.architecture(),
        "user": getpass.getuser(),
        "pid": os.getpid(),
    }
    python_info = {
        "executable": sys.executable,
        "version": sys.version,
        "pointer_bits": 64 if sys.maxsize > 2**32 else 32,
    }
    print(f"  Token: AppContainer={tok['TokenIsAppContainer']} | RID={tok['IntegrityLevelRID']} ({tok['IntegrityLevelName']}) | Elevated={tok['TokenIsElevated']}")
    print(f"  Session: {tok['SessionId']} | WindowStation: '{tok['WindowStation']}' | Desktop: '{tok['Desktop']}'")
    print(f"  Python: {python_info['executable']} ({python_info['pointer_bits']}-bit)")

    # 2. Binary Identification
    print("\n[Gate 2] Verifying Playwright package and Chromium binary...")
    bins = identify_browser_binaries()
    print(f"  Playwright Package: {bins['playwright_package_version']}")
    print(f"  Chromium Revision:  {bins['expected_revision']} (version {bins['expected_browser_version']})")
    print(f"  Chromium Executable: {bins['chromium_executable_path']} (Exists: {bins['chromium_executable_exists']})")
    print(f"  Manifest:            {bins['manifest_path']} (Exists: {bins['manifest_exists']})")

    suite_tmp = Path(WORKSPACE_ROOT) / "data" / "pytest_tmp" / f"stage1_{uuid.uuid4().hex[:8]}"
    suite_tmp.mkdir(parents=True, exist_ok=True)

    exp_a_result: dict[str, Any] = {}
    exp_b_result: dict[str, Any] = {}

    try:
        # 3. Experiment A: Native Playwright Launch
        if args.experiment in ("all", "playwright"):
            print("\n[Gate 3] Executing Experiment A: Native Playwright Launch (chromium_sandbox=True)...")
            pw_tmp = suite_tmp / "exp_a_user_data"
            pw_tmp.mkdir(parents=True, exist_ok=True)
            exp_a_result = run_isolated_experiment(
                "--worker-playwright",
                ["--user-data-dir", str(pw_tmp)],
                args.timeout,
                str(suite_tmp),
            )
            print(f"  Status:   {exp_a_result.get('status')}")
            print(f"  Duration: {exp_a_result.get('duration_sec', 0.0):.2f}s")
            if exp_a_result.get("error"):
                print(f"  Error:    {exp_a_result.get('error')}")
            if exp_a_result.get("no_sandbox_flag_detected") is not None:
                print(f"  --no-sandbox Flag Detected: {exp_a_result.get('no_sandbox_flag_detected')}")
            if exp_a_result.get("page_title"):
                print(f"  Page Title: '{exp_a_result.get('page_title')}' | Eval: {exp_a_result.get('eval_result')}")

        # 4. Experiment B: Direct Chromium Launch + Real CDP
        if args.experiment in ("all", "cdp"):
            print("\n[Gate 4] Executing Experiment B: Direct Chromium Launch + Real CDP Protocol...")
            cdp_tmp = suite_tmp / "exp_b_user_data"
            cdp_tmp.mkdir(parents=True, exist_ok=True)
            chrome_exe = bins.get("chromium_executable_path") or ""
            exp_b_result = run_isolated_experiment(
                "--worker-cdp",
                ["--user-data-dir", str(cdp_tmp), "--chrome-exe", chrome_exe],
                args.timeout,
                str(suite_tmp),
            )
            print(f"  Status:   {exp_b_result.get('status')}")
            print(f"  Duration: {exp_b_result.get('duration_sec', 0.0):.2f}s")
            print(f"  DevToolsActivePort Created: {exp_b_result.get('devtools_active_port_created')}")
            print(f"  DevTools Stream Detected:   {exp_b_result.get('devtools_stream_detected')}")
            print(f"  psutil Listener Detected:   {exp_b_result.get('psutil_listener_detected')}")
            if exp_b_result.get("allocated_port"):
                print(f"  Port: {exp_b_result.get('allocated_port')} | Endpoint: {exp_b_result.get('ws_url')}")
            if exp_b_result.get("exit_code") is not None:
                print(f"  Exit Code: {exp_b_result.get('exit_code')} (hex: 0x{exp_b_result.get('exit_code') & 0xFFFFFFFF:08X})")
            if exp_b_result.get("crashpad_messages"):
                print(f"  Crashpad Messages: {len(exp_b_result['crashpad_messages'])} recorded")
                for cm in exp_b_result["crashpad_messages"][:3]:
                    print(f"    - {cm}")
            if exp_b_result.get("runtime_eval_title") is not None:
                print(f"  CDP Page Title: '{exp_b_result.get('runtime_eval_title')}' | Calc: {exp_b_result.get('runtime_eval_calc')}")
            if exp_b_result.get("playwright_cdp_interop"):
                print(f"  Playwright CDP Interop: {exp_b_result.get('playwright_cdp_interop')}")
            if exp_b_result.get("error"):
                print(f"  Error:    {exp_b_result.get('error')}")

    finally:
        shutil.rmtree(suite_tmp, ignore_errors=True)

    # 5. Compile Master Evidence Record
    evidence = {
        "context_label": args.context,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "host": host_info,
        "python": python_info,
        "token": tok,
        "binaries": bins,
        "ast_audit": ast_audit,
        "experiment_a_playwright": exp_a_result,
        "experiment_b_cdp": exp_b_result,
    }

    if args.json_out:
        out_p = Path(args.json_out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(evidence, f, indent=2)
        print(f"\nProbe results written to: {out_p}")

    print("\n" + "=" * 80)
    print(" SUMMARY ASSESSMENT")
    print("=" * 80)
    print(f"Context:      {args.context}")
    print(f"Experiment A: {exp_a_result.get('status', 'SKIPPED')}")
    print(f"Experiment B: {exp_b_result.get('status', 'SKIPPED')}")
    print("=" * 80)

    return 0


if __name__ == "__main__":
    sys.exit(main())
