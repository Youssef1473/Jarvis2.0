"""Comprehensive diagnostic probe for Windows integration test environment capabilities.

Part of ENG-OS-03C8 Phase 2 (Controlled Execution-Context Comparison).
Executes bounded, read-only diagnostic probes across Winsock/asyncio, AppContainer process creation,
Restart Manager, Extended TCP tables, Desktop Clipboard, and Chromium/CDP browser preflight.
Outputs structured JSON and human-readable diagnostics. Never blocks or hangs indefinitely.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import errno
import getpass
import json
import os
import platform
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))


def get_token_info() -> dict[str, Any]:
    """Interrogates active process token for AppContainer, integrity level, elevation, and session."""
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
        "GetOpenClipboardWindow": None,
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
    info["WindowStationHandle"] = int(hwinsta) if hwinsta else None
    buf = ctypes.create_unicode_buffer(512)
    needed = wintypes.DWORD(0)
    UOI_NAME = 2
    if hwinsta and u32.GetUserObjectInformationW(hwinsta, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
        info["WindowStation"] = buf.value

    hdesk = u32.GetThreadDesktop(k32.GetCurrentThreadId())
    info["DesktopHandle"] = int(hdesk) if hdesk else None
    if hdesk and u32.GetUserObjectInformationW(hdesk, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
        info["Desktop"] = buf.value

    # Clipboard window holder
    info["GetOpenClipboardWindow"] = int(u32.GetOpenClipboardWindow())

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


def probe_winsock_and_asyncio() -> dict[str, Any]:
    """Probes Winsock loopback TCP and asyncio proactor event loop construction."""
    res: dict[str, Any] = {}

    # 1. TCP Bind
    t0 = time.perf_counter()
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        port = lsock.getsockname()[1]
        elapsed_bind = (time.perf_counter() - t0) * 1000
        res["tcp_bind"] = {"status": "SUCCESS", "port": port, "duration_ms": round(elapsed_bind, 2)}
    except Exception as e:
        elapsed_bind = (time.perf_counter() - t0) * 1000
        res["tcp_bind"] = {"status": "FAILED", "error": str(e), "duration_ms": round(elapsed_bind, 2)}
        lsock.close()
        return res

    # 2. Non-blocking connect
    csock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    csock.setblocking(False)
    t1 = time.perf_counter()
    conn_errno = 0
    try:
        csock.connect(("127.0.0.1", port))
    except BlockingIOError as e:
        conn_errno = e.errno
    except Exception as e:
        conn_errno = getattr(e, "errno", -1)
    elapsed_conn = (time.perf_counter() - t1) * 1000
    res["tcp_nonblocking_connect"] = {
        "errno": conn_errno,
        "errorcode": errno.errorcode.get(conn_errno, str(conn_errno)),
        "duration_ms": round(elapsed_conn, 2),
    }

    # 3. Select readiness and SO_ERROR
    t2 = time.perf_counter()
    r, w, x = select.select([csock], [csock], [csock], 0.2)
    elapsed_sel = (time.perf_counter() - t2) * 1000
    so_error = csock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    res["tcp_select_so_error"] = {
        "readable": bool(r),
        "writable": bool(w),
        "exception": bool(x),
        "so_error": so_error,
        "so_errorcode": errno.errorcode.get(so_error, str(so_error)),
        "duration_ms": round(elapsed_sel, 2),
    }
    csock.close()
    lsock.close()

    # 4. socket.socketpair() runtime default
    t3 = time.perf_counter()
    pair_result: Any = None
    pair_err: str | None = None

    def _pair_worker():
        nonlocal pair_result, pair_err
        try:
            p = socket.socketpair()
            pair_result = [p[0].fileno(), p[1].fileno()]
            p[0].close()
            p[1].close()
        except Exception as exc:
            pair_err = str(exc)

    t_pair = threading.Thread(target=_pair_worker, daemon=True)
    t_pair.start()
    t_pair.join(timeout=1.0)
    elapsed_pair = (time.perf_counter() - t3) * 1000
    res["socket_socketpair_default"] = {
        "timed_out_hung": t_pair.is_alive(),
        "result": pair_result,
        "error": pair_err,
        "duration_ms": round(elapsed_pair, 2),
    }

    # 5. ProactorEventLoop construction
    t4 = time.perf_counter()
    loop_result: Any = None
    loop_err: str | None = None

    def _loop_worker():
        nonlocal loop_result, loop_err
        try:
            loop = asyncio.ProactorEventLoop()
            loop_result = str(loop)
            loop.close()
        except Exception as exc:
            loop_err = str(exc)

    t_loop = threading.Thread(target=_loop_worker, daemon=True)
    t_loop.start()
    t_loop.join(timeout=1.0)
    elapsed_loop = (time.perf_counter() - t4) * 1000
    res["asyncio_proactor_construction"] = {
        "timed_out_hung": t_loop.is_alive(),
        "result": loop_result,
        "error": loop_err,
        "duration_ms": round(elapsed_loop, 2),
    }

    # 6. Minimal coroutine execution (only attempted if loop constructor did not hang)
    if not t_loop.is_alive() and loop_result:
        try:
            async def _dummy():
                return 42
            val = asyncio.run(_dummy())
            res["asyncio_run_coroutine"] = {"status": "SUCCESS", "return_value": val}
        except Exception as exc:
            res["asyncio_run_coroutine"] = {"status": "FAILED", "error": str(exc)}
    else:
        res["asyncio_run_coroutine"] = {"status": "SKIPPED_LOOP_HUNG", "reason": "ProactorEventLoop constructor hung on self-pipe"}

    return res


def probe_appcontainer_process_creation() -> dict[str, Any]:
    """Probes Windows AppContainer process creation and minimal CreateProcessW."""
    res: dict[str, Any] = {}
    if sys.platform != "win32":
        return {"supported": False, "reason": "Non-Windows platform"}

    from jarvis.core.evolution.appcontainer import (
        AppContainerConfig,
        AppContainerProcessLauncher,
        AppContainerProfileManager,
        AppContainerSecurityError,
        CREATE_NO_WINDOW,
        CREATE_SUSPENDED,
        EXTENDED_STARTUPINFO_PRESENT,
        PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
        PROCESS_INFORMATION,
        SECURITY_CAPABILITIES,
        STARTUPINFOEXW,
        advapi32,
        kernel32,
    )

    probe_profile = f"J_Probe_Compare_{os.getpid()}"
    cfg = AppContainerConfig(profile_name=probe_profile)

    # 1. Profile creation / SID derivation
    p_sid, sid_str, is_new = AppContainerProfileManager.get_or_create_profile(cfg)
    res["profile_sid"] = sid_str
    res["profile_ptr"] = p_sid
    res["is_new_profile"] = is_new

    # 2. Control test: CreateProcessW with empty attribute list
    attr_size_ctrl = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList(None, 0, 0, ctypes.byref(attr_size_ctrl))
    attr_buf_ctrl = ctypes.create_string_buffer(attr_size_ctrl.value)
    kernel32.InitializeProcThreadAttributeList(attr_buf_ctrl, 0, 0, ctypes.byref(attr_size_ctrl))

    si_ctrl = STARTUPINFOEXW()
    si_ctrl.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    si_ctrl.lpAttributeList = ctypes.cast(attr_buf_ctrl, ctypes.c_void_p)
    pi_ctrl = PROCESS_INFORMATION()
    flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW

    ok_ctrl = kernel32.CreateProcessW(
        None,
        "cmd.exe /c exit 0",
        None,
        None,
        False,
        flags,
        None,
        None,
        ctypes.byref(si_ctrl.StartupInfo),
        ctypes.byref(pi_ctrl),
    )
    err_ctrl = ctypes.get_last_error()
    if ok_ctrl:
        kernel32.TerminateProcess(pi_ctrl.hProcess, 0)
        kernel32.CloseHandle(pi_ctrl.hProcess)
        kernel32.CloseHandle(pi_ctrl.hThread)
    kernel32.DeleteProcThreadAttributeList(attr_buf_ctrl)
    res["control_createprocess_empty_attributes"] = {
        "success": bool(ok_ctrl),
        "last_error": err_ctrl,
    }

    # 3. Minimal CreateProcessW with PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES
    sec_caps = SECURITY_CAPABILITIES()
    sec_caps.AppContainerSid = ctypes.c_void_p(p_sid)
    sec_caps.Capabilities = None
    sec_caps.CapabilityCount = 0
    sec_caps.Reserved = 0

    attr_size_sec = ctypes.c_size_t(0)
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size_sec))
    attr_buf_sec = ctypes.create_string_buffer(attr_size_sec.value)
    kernel32.InitializeProcThreadAttributeList(attr_buf_sec, 1, 0, ctypes.byref(attr_size_sec))

    upd_ok = kernel32.UpdateProcThreadAttribute(
        attr_buf_sec,
        0,
        PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
        ctypes.byref(sec_caps),
        ctypes.sizeof(sec_caps),
        None,
        None,
    )
    upd_err = ctypes.get_last_error()

    si_sec = STARTUPINFOEXW()
    si_sec.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    si_sec.lpAttributeList = ctypes.cast(attr_buf_sec, ctypes.c_void_p)
    pi_sec = PROCESS_INFORMATION()

    t_cp = time.perf_counter()
    cp_ok = kernel32.CreateProcessW(
        None,
        "cmd.exe /c exit 0",
        None,
        None,
        False,
        flags,
        None,
        None,
        ctypes.byref(si_sec.StartupInfo),
        ctypes.byref(pi_sec),
    )
    cp_err = ctypes.get_last_error()
    elapsed_cp = (time.perf_counter() - t_cp) * 1000

    if cp_ok:
        kernel32.TerminateProcess(pi_sec.hProcess, 0)
        kernel32.CloseHandle(pi_sec.hProcess)
        kernel32.CloseHandle(pi_sec.hThread)
    kernel32.DeleteProcThreadAttributeList(attr_buf_sec)

    res["minimal_createprocess_with_security_capabilities"] = {
        "sizeof_security_capabilities": ctypes.sizeof(sec_caps),
        "attribute_list_size": attr_size_sec.value,
        "update_proc_thread_attribute_ok": bool(upd_ok),
        "update_last_error": upd_err,
        "create_process_ok": bool(cp_ok),
        "create_process_last_error": cp_err,
        "duration_ms": round(elapsed_cp, 2),
    }

    # 4. Production Launcher invocation
    prod_ok = False
    prod_err = None
    try:
        handle = AppContainerProcessLauncher.spawn_suspended_appcontainer(
            ["cmd.exe", "/c", "exit 0"],
            config=AppContainerConfig(profile_name=f"{probe_profile}_prod"),
        )
        prod_ok = True
        handle.cleanup()
    except Exception as e:
        prod_err = str(e)

    res["production_launcher_spawn"] = {
        "success": prod_ok,
        "error": prod_err,
    }

    # Cleanup test profile
    AppContainerProfileManager.free_sid(p_sid)
    AppContainerProfileManager.delete_profile(probe_profile)
    AppContainerProfileManager.delete_profile(f"{probe_profile}_prod")

    return res


def probe_restart_manager_and_tcp_table() -> dict[str, Any]:
    """Probes Restart Manager RmStartSession and IP Helper TCP table APIs."""
    res: dict[str, Any] = {}
    if sys.platform != "win32":
        return {"supported": False}

    rstrtmgr = ctypes.WinDLL("Rstrtmgr.dll", use_last_error=True)
    iphlpapi = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)

    # 1. RmStartSession
    session_handle = wintypes.DWORD(0)
    CCH_RM_SESSION_KEY = 32
    session_key = (wintypes.WCHAR * (CCH_RM_SESSION_KEY + 1))()
    t0 = time.perf_counter()
    rm_code = rstrtmgr.RmStartSession(ctypes.byref(session_handle), 0, session_key)
    elapsed_rm = (time.perf_counter() - t0) * 1000

    res["rm_start_session"] = {
        "return_dword": int(rm_code),
        "session_key_buffer_len": len(session_key),
        "session_key_sizeof_bytes": ctypes.sizeof(session_key),
        "duration_ms": round(elapsed_rm, 2),
    }
    if rm_code == 0:
        rstrtmgr.RmEndSession(session_handle)

    # 2. GetTcpTable (MIB-II table size probe)
    size_tcp = wintypes.DWORD(0)
    t1 = time.perf_counter()
    tcp_code = iphlpapi.GetTcpTable(None, ctypes.byref(size_tcp), True)
    elapsed_tcp = (time.perf_counter() - t1) * 1000
    res["get_tcp_table_size_probe"] = {
        "return_dword": int(tcp_code),
        "needed_size_bytes": size_tcp.value,
        "duration_ms": round(elapsed_tcp, 2),
    }

    # 3. GetExtendedTcpTable (Owner PID table across classes 0-8 for AF_INET)
    classes_res: dict[str, Any] = {}
    for tc in range(9):
        size_ext = wintypes.DWORD(0)
        ext_code = iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size_ext), True, 2, tc, 0)
        classes_res[f"class_{tc}"] = {
            "return_dword": int(ext_code),
            "needed_size_bytes": size_ext.value,
        }
    res["get_extended_tcp_table_classes"] = classes_res

    return res


def probe_clipboard() -> dict[str, Any]:
    """Probes Windows clipboard access, window station isolation, and holder windows."""
    res: dict[str, Any] = {}
    if sys.platform != "win32":
        return {"supported": False}

    u32 = ctypes.windll.user32

    # 1. Existing clipboard window holder
    hwnd_open = u32.GetOpenClipboardWindow()
    res["open_clipboard_window_hwnd"] = int(hwnd_open)

    # 2. OpenClipboard(0) with NULL HWND
    ok_null = u32.OpenClipboard(0)
    err_null = ctypes.get_last_error()
    if ok_null:
        u32.CloseClipboard()
    res["open_clipboard_null_hwnd"] = {
        "success": bool(ok_null),
        "last_error": err_null,
    }

    # 3. OpenClipboard(hwnd) with valid window created via CreateWindowExW
    hwnd = u32.CreateWindowExW(0, "STATIC", "J_Clip_Probe", 0, 0, 0, 0, 0, 0, 0, 0, 0)
    res["created_window_hwnd"] = int(hwnd) if hwnd else None
    if hwnd:
        ok_wnd = u32.OpenClipboard(hwnd)
        err_wnd = ctypes.get_last_error()
        if ok_wnd:
            u32.CloseClipboard()
        u32.DestroyWindow(hwnd)
        res["open_clipboard_with_hwnd"] = {
            "success": bool(ok_wnd),
            "last_error": err_wnd,
        }
    else:
        res["open_clipboard_with_hwnd"] = {
            "success": False,
            "error": "CreateWindowExW failed",
        }

    return res


def probe_browser_environment() -> dict[str, Any]:
    """Probes Chromium executables, Playwright caches, CDP loopback, and DNS."""
    res: dict[str, Any] = {}

    # 1. Search PATH
    res["path_search"] = {
        "chromium": shutil.which("chromium"),
        "chrome": shutil.which("chrome"),
        "msedge": shutil.which("msedge"),
        "brave": shutil.which("brave"),
    }

    # 2. Standard disk locations
    std_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    res["program_files_search"] = {p: os.path.exists(p) for p in std_paths}

    # 3. Playwright browser cache
    pw_dir = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright")
    pw_exists = os.path.exists(pw_dir)
    res["playwright_dir"] = {
        "path": pw_dir,
        "exists": pw_exists,
        "contents": os.listdir(pw_dir) if pw_exists else [],
    }

    # 4. Playwright chromium executable probe
    chrome_pw = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-1243\chrome-win64\chrome.exe")
    chrome_pw_exists = os.path.exists(chrome_pw)
    res["playwright_chromium_executable"] = {
        "path": chrome_pw,
        "exists": chrome_pw_exists,
    }

    if chrome_pw_exists:
        try:
            sub = subprocess.run([chrome_pw, "--version"], capture_output=True, text=True, timeout=2.0)
            res["playwright_chromium_launch"] = {
                "returncode": sub.returncode,
                "stdout": sub.stdout.strip(),
                "stderr": sub.stderr.strip()[:300],
            }
        except Exception as e:
            res["playwright_chromium_launch"] = {"error": str(e)}

    # 5. CDP loopback reachability (port 9998)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    cdp_err = None
    try:
        s.connect(("127.0.0.1", 9998))
        cdp_ok = True
    except Exception as exc:
        cdp_ok = False
        cdp_err = str(exc)
    finally:
        s.close()

    res["cdp_loopback_port_9998"] = {
        "reachable": cdp_ok,
        "error": cdp_err,
    }

    # 6. External DNS resolution
    try:
        dns_res = socket.getaddrinfo("google.com", 80)
        dns_ok = True
        dns_err = None
    except Exception as exc:
        dns_ok = False
        dns_err = str(exc)
    res["external_dns"] = {
        "resolves": dns_ok,
        "error": dns_err,
    }

    return res


def run_full_probe(context_label: str = "Context A (Sandbox)") -> dict[str, Any]:
    """Runs all bounded probes and returns full telemetry dictionary."""
    data: dict[str, Any] = {
        "context_label": context_label,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": {
            "platform": platform.platform(),
            "windows_version": platform.win32_ver() if sys.platform == "win32" else None,
            "architecture": platform.architecture(),
            "user": getpass.getuser(),
            "pid": os.getpid(),
        },
        "python": {
            "executable": sys.executable,
            "normalized_executable": os.path.normcase(os.path.normpath(sys.executable)),
            "version": sys.version,
            "version_info": list(sys.version_info[:3]),
            "machine": platform.machine(),
            "pointer_bits": struct.calcsize("P") * 8,
            "is_64bit_interpreter": bool(sys.maxsize > 2**32),
            "architecture": list(platform.architecture()),
            "prefix": sys.prefix,
        },
        "token_security": get_token_info(),
        "probe_winsock_asyncio": probe_winsock_and_asyncio(),
        "probe_appcontainer_process": probe_appcontainer_process_creation(),
        "probe_restart_manager": probe_restart_manager_and_tcp_table(),
        "probe_clipboard": probe_clipboard(),
        "probe_browser": probe_browser_environment(),
    }
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis 2.0 Integration Environment Diagnostic Probe")
    parser.add_argument("--context", default="Context A (Antigravity Sandbox)", help="Context label for this run")
    parser.add_argument("--json-out", default=None, help="Path to write JSON results")
    args = parser.parse_args()

    results = run_full_probe(context_label=args.context)
    json_text = json.dumps(results, indent=2)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json_text, encoding="utf-8")
        print(f"Probe results written to: {out_path}")

    print(json_text)


if __name__ == "__main__":
    main()
