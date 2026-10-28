"""Native Model Context Protocol (MCP) Client and Job-Sandboxed Subprocess Governor for Jarvis 2.0.

Provides stateless HTTP transport (MCP-Protocol-Version: 2026-07-28) and
E-Core Job-Sandboxed stdio transport with asynchronous stderr draining and taint tracking.
"""
import collections
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from collections.abc import Iterable

from jarvis.core.security_boundaries import (
    TurnTaintTracker,
    build_safe_url_opener,
    resolve_and_validate_destination,
)
from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Core.MCP")

MCP_PROTOCOL_VERSION = "2026-07-28"
DEFAULT_TIMEOUT_SEC = 5.0
DEFAULT_TOOL_CACHE_TTL_SEC = 60.0
STDIO_MEMORY_LIMIT_MB = 512
STDIO_AFFINITY_MASK = 0xFF00

MCP_BASE_ENV_ALLOWLIST: frozenset[str] = frozenset({
    # Windows system runtime & executables
    "SYSTEMROOT",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "SYSTEMDRIVE",
    # Ephemeral temp directories
    "TEMP",
    "TMP",
    # User / host execution profile
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERNAME",
    # CPU / architecture indicators
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "OS",
})

CONSEQUENTIAL_MCP_PATTERNS = (
    "evaluate_script",
    "execute_script",
    "eval_script",
    "exec_command",
    "execute_command",
    "run_command",
    "delete",
    "destroy",
    "drop",
    "purge",
    "wipe",
    "unlink",
    "format_disk",
    "shutdown",
    "reboot",
    "terminate",
    "kill",
)


def _setup_win32_job_object(proc_handle: int, max_memory_mb: int = STDIO_MEMORY_LIMIT_MB) -> int | None:
    """Configures a 64-bit Windows Job Object to constrain stdio MCP servers to E-Cores and 512MB RAM."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
        JOB_OBJECT_LIMIT_AFFINITY = 0x00000010
        JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
        JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]

        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]

        kernel32.GetProcessAffinityMask.restype = wintypes.BOOL
        kernel32.GetProcessAffinityMask.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
        ]

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []

        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            logger.warning("Failed to create Win32 Job Object for MCP server (error %s)", ctypes.get_last_error())
            return None

        ext_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limit_flags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_JOB_MEMORY
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )

        proc_mask = ctypes.c_size_t(0)
        sys_mask = ctypes.c_size_t(0)
        if kernel32.GetProcessAffinityMask(kernel32.GetCurrentProcess(), ctypes.byref(proc_mask), ctypes.byref(sys_mask)):
            available = sys_mask.value
            targeted = STDIO_AFFINITY_MASK & available
            if targeted > 0:
                safe_affinity = targeted
            else:
                safe_affinity = proc_mask.value
                logger.warning(
                    "MCP stdio server requested affinity 0x%X has zero overlap with host system mask 0x%X; "
                    "degrading to process mask 0x%X",
                    STDIO_AFFINITY_MASK,
                    available,
                    proc_mask.value,
                )
            if safe_affinity:
                limit_flags |= JOB_OBJECT_LIMIT_AFFINITY
                ext_info.BasicLimitInformation.Affinity = safe_affinity

        ext_info.BasicLimitInformation.LimitFlags = limit_flags
        ext_info.BasicLimitInformation.ActiveProcessLimit = 8
        ext_info.JobMemoryLimit = max_memory_mb * 1024 * 1024

        set_ok = kernel32.SetInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(ext_info),
            ctypes.sizeof(ext_info),
        )
        if not set_ok:
            logger.warning("Failed to configure Job Object limits (error %s)", ctypes.get_last_error())
            kernel32.CloseHandle(h_job)
            return None

        assign_ok = kernel32.AssignProcessToJobObject(h_job, int(proc_handle))
        if not assign_ok:
            logger.warning("Failed to assign MCP process to Job Object (error %s)", ctypes.get_last_error())
            kernel32.CloseHandle(h_job)
            return None

        return int(h_job)
    except Exception as e:
        logger.debug("Win32 Job Object setup bypassed: %s", e)
        return None


def _close_win32_job_object(h_job: int | None) -> None:
    """Closes and terminates a Win32 Job Object handle."""
    if not h_job:
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        kernel32.TerminateJobObject(wintypes.HANDLE(h_job), 1)
        kernel32.CloseHandle(wintypes.HANDLE(h_job))
    except Exception as e:
        logger.debug("Error closing Job Object handle: %s", e)


class MCPClientSession:
    """Represents a connection session to an external Model Context Protocol server."""

    def __init__(
        self,
        name: str,
        transport: str = "http",
        endpoint_url: str | None = None,
        command: list[str] | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        headers: dict[str, str] | None = None,
        tool_cache_ttl_sec: float = DEFAULT_TOOL_CACHE_TTL_SEC,
        trusted_read_only_tools: Iterable[str] | None = None,
        consequential_tools: Iterable[str] | None = None,
    ):
        self.name = name
        self.transport = transport.lower()
        self.endpoint_url = endpoint_url
        self.command = command or []
        self.args = args or []
        self.env = env
        self.cwd = cwd
        self.headers = headers or {}
        self.tool_cache_ttl_sec = tool_cache_ttl_sec
        self.trusted_read_only_tools: set[str] = {
            t.lower().replace("-", "_") for t in (trusted_read_only_tools or [])
        }
        self.consequential_tools: set[str] = {
            t.lower().replace("-", "_") for t in (consequential_tools or [])
        }

        self._request_id: int = 0
        self._lock = threading.Lock()
        self._tool_cache: list[dict[str, Any]] | None = None
        self._cache_timestamp: float = 0.0
        self._is_closed: bool = False
        self._http_opener: urllib.request.OpenerDirector | None = None

        # Stdio specific state
        self._proc: subprocess.Popen | None = None
        self._job_handle: int | None = None
        self._stderr_buffer: collections.deque = collections.deque(maxlen=200)
        self._stdout_queue: queue.Queue = queue.Queue()
        self._stderr_thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None

    def _next_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def initialize(self, timeout: float = DEFAULT_TIMEOUT_SEC) -> dict[str, Any]:
        """Performs protocol handshake with MCP server."""
        proto_ver = self.headers.get("MCP-Protocol-Version", MCP_PROTOCOL_VERSION)
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": proto_ver,
                "capabilities": {},
                "clientInfo": {"name": "jarvis-mcp-client", "version": "2.0"},
            },
        }
        res = self._send_request(payload, timeout=timeout)
        logger.info("Initialized MCP session '%s' (transport: %s)", self.name, self.transport)
        return res

    def list_tools(self, force_refresh: bool = False, timeout: float = DEFAULT_TIMEOUT_SEC) -> list[dict[str, Any]]:
        """Queries exposed tools from the server, respecting ttl caching."""
        now = time.time()
        if (
            not force_refresh
            and self._tool_cache is not None
            and (now - self._cache_timestamp < self.tool_cache_ttl_sec)
        ):
            return list(self._tool_cache)

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
            "params": {},
        }
        resp = self._send_request(payload, timeout=timeout)
        tools = resp.get("result", {}).get("tools", [])
        if not isinstance(tools, list):
            tools = []

        with self._lock:
            self._tool_cache = tools
            self._cache_timestamp = now

        logger.info("Discovered %d MCP tools for session '%s'", len(tools), self.name)
        return list(tools)

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
        """Executes a tool on the external MCP server via JSON-RPC 2.0."""
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments or {},
            },
        }
        resp = self._send_request(payload, timeout=timeout)
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"MCP server '{self.name}' returned error on tool '{tool_name}': {err}")

        result = resp.get("result", {})
        return result

    def _send_request(self, payload: dict[str, Any], timeout: float = DEFAULT_TIMEOUT_SEC) -> dict[str, Any]:
        """Dispatches JSON-RPC request over active transport."""
        if self._is_closed:
            raise RuntimeError(f"MCPClientSession '{self.name}' is already closed.")

        if self.transport == "http":
            return self._send_http_request(payload, timeout=timeout)
        elif self.transport == "stdio":
            return self._send_stdio_request(payload, timeout=timeout)
        else:
            raise ValueError(f"Unsupported MCP transport: '{self.transport}'")

    def _send_http_request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Stateless HTTP POST transport conforming to 2026-07-28 specification."""
        if not self.endpoint_url:
            raise ValueError(f"HTTP transport requires endpoint_url for session '{self.name}'.")

        # Validate URL via SSRF boundary, resolving and pinning IP to prevent DNS rebinding
        clean_url, _, pinned_ip = resolve_and_validate_destination(self.endpoint_url, allow_local_mcp=True)

        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "User-Agent": "Jarvis-MCP-Client/2.0",
        }
        req_headers.update(self.headers)

        body_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(clean_url, data=body_bytes, headers=req_headers, method="POST")
        req._pinned_ip = pinned_ip

        try:
            opener = self._http_opener or build_safe_url_opener(allow_local_mcp=True)
            with opener.open(req, timeout=timeout) as resp:
                resp_bytes = resp.read()
                raw_text = resp_bytes.decode("utf-8").strip()
                if raw_text.startswith("{") or raw_text.startswith("["):
                    return json.loads(raw_text)
                for line in raw_text.splitlines():
                    line_str = line.strip()
                    if line_str.startswith("data:"):
                        return json.loads(line_str[5:].strip())
                return json.loads(raw_text)
        except Exception as err:
            logger.error("HTTP request error to MCP server '%s': %s", self.name, err)
            raise RuntimeError(f"MCP HTTP request to '{self.name}' failed: {err}") from err

    def _ensure_stdio_process(self) -> None:
        """Launches stdio subprocess pinned to E-Cores within a Windows Job Object."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return

            cmd_list = [self.command] if isinstance(self.command, str) else list(self.command)
            cmd_list.extend(self.args)

            if not cmd_list:
                raise ValueError(f"Stdio transport requires command list for session '{self.name}'.")

            # On Windows, resolve .cmd / .bat executables (e.g. npx -> npx.CMD)
            executable = shutil.which(cmd_list[0])
            if executable:
                cmd_list[0] = executable

            cwd_path = os.path.abspath(self.cwd) if self.cwd else os.getcwd()

            # Build scrubbed base environment (excluding host secrets, credentials, and API keys)
            proc_env: dict[str, str] = {}
            for k in MCP_BASE_ENV_ALLOWLIST:
                if k in os.environ:
                    proc_env[k] = os.environ[k]
                elif k.upper() in os.environ:
                    proc_env[k] = os.environ[k.upper()]

            proc_env.setdefault("SYSTEMROOT", r"C:\Windows")
            proc_env.setdefault("WINDIR", r"C:\Windows")
            proc_env.setdefault("SYSTEMDRIVE", "C:")

            # Explicitly overlay only declared server-specific environment variables
            if self.env and isinstance(self.env, dict):
                for k, v in self.env.items():
                    if isinstance(v, str):
                        proc_env[k] = os.path.expandvars(v)
                    else:
                        proc_env[k] = str(v)

            creation_flags = 0
            is_win = sys.platform == "win32"
            if is_win:
                from jarvis.skills.job_object_sandbox import CREATE_SUSPENDED
                creation_flags = subprocess.CREATE_NO_WINDOW | CREATE_SUSPENDED

            proc = subprocess.Popen(
                cmd_list,
                cwd=cwd_path,
                env=proc_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=creation_flags,
                close_fds=True,
            )
            self._proc = proc

            # Assign to Win32 Job Object before child process execution (E-Cores, 512MB RAM cap)
            if is_win:
                h_job = None
                try:
                    raw_handle = int(proc._handle)
                    h_job = _setup_win32_job_object(raw_handle, max_memory_mb=STDIO_MEMORY_LIMIT_MB)
                except Exception as setup_err:
                    logger.error("Job Object setup error for '%s': %s", self.name, setup_err)
                    h_job = None

                if not h_job:
                    # Fail-closed: kill unstarted child, release pipes, abort launch
                    try:
                        proc.kill()
                        proc.communicate()
                    except Exception:
                        pass
                    self._proc = None
                    raise RuntimeError(f"Failed to contain MCP stdio server '{self.name}' in Windows Job Object.")

                # Resume the contained suspended process
                try:
                    from jarvis.skills.job_object_sandbox import resume_process
                    resumed = resume_process(proc)
                except Exception as res_err:
                    logger.error("Failed to resume suspended MCP process '%s': %s", self.name, res_err)
                    resumed = False

                if not resumed:
                    try:
                        proc.kill()
                        proc.communicate()
                    except Exception:
                        pass
                    _close_win32_job_object(h_job)
                    self._proc = None
                    self._job_handle = None
                    raise RuntimeError(f"Failed to resume suspended MCP stdio server '{self.name}' after containment.")

                self._job_handle = h_job

            # Start asynchronous daemon stderr drain thread
            def _drain_stderr():
                while not self._is_closed and proc.poll() is None:
                    try:
                        line = proc.stderr.readline()
                        if not line or not isinstance(line, str):
                            break
                        self._stderr_buffer.append(line.rstrip())
                    except Exception:
                        break

            self._stderr_thread = threading.Thread(target=_drain_stderr, daemon=True, name=f"mcp-stderr-{self.name}")
            self._stderr_thread.start()

            # Start asynchronous daemon stdout reader thread
            def _drain_stdout():
                while not self._is_closed and proc.poll() is None:
                    try:
                        line = proc.stdout.readline()
                        if not line or not isinstance(line, str):
                            break
                        self._stdout_queue.put(line.rstrip())
                    except Exception:
                        break

            self._stdout_thread = threading.Thread(target=_drain_stdout, daemon=True, name=f"mcp-stdout-{self.name}")
            self._stdout_thread.start()

    def _send_stdio_request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Transmits JSON-RPC payload over stdio framing and waits for response line."""
        self._ensure_stdio_process()

        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                raise RuntimeError(f"MCP stdio subprocess for '{self.name}' is not running.")

            raw_req = json.dumps(payload) + "\n"
            try:
                self._proc.stdin.write(raw_req)
                self._proc.stdin.flush()
            except Exception as e:
                raise RuntimeError(f"Failed to write request to MCP stdin: {e}") from e

        # Wait for JSON-RPC response from stdout queue
        try:
            line = self._stdout_queue.get(timeout=timeout)
            return json.loads(line)
        except queue.Empty:
            recent_err = " | ".join(list(self._stderr_buffer)[-3:]) if self._stderr_buffer else "No stderr available."
            raise TimeoutError(
                f"MCP stdio server '{self.name}' timed out after {timeout}s waiting for response. "
                f"Stderr tail: {recent_err}"
            )

    def close(self) -> None:
        """Closes session, terminates stdio process and cleans up Job Object."""
        with self._lock:
            if self._is_closed:
                return
            self._is_closed = True
            self._tool_cache = None

            if self._proc:
                try:
                    if self._proc.stdin and not self._proc.stdin.closed:
                        self._proc.stdin.close()
                except Exception:
                    pass

                # Terminate Job Object to kill process tree cleanly
                if self._job_handle:
                    _close_win32_job_object(self._job_handle)
                    self._job_handle = None

                try:
                    if self._proc.poll() is None:
                        self._proc.kill()
                        self._proc.wait(timeout=1.0)
                except Exception:
                    pass

                try:
                    if self._proc.stdout and not self._proc.stdout.closed:
                        self._proc.stdout.close()
                    if self._proc.stderr and not self._proc.stderr.closed:
                        self._proc.stderr.close()
                except Exception:
                    pass

                self._proc = None

        logger.info("Closed MCP session '%s'", self.name)


def register_discovered_mcp_tools(
    session: MCPClientSession,
    trusted_read_only_tools: Iterable[str] | None = None,
    consequential_tools: Iterable[str] | None = None,
) -> list[str]:
    """Discovers tools from an MCPClientSession and registers them in SkillRegistry with taint tracking."""
    tools = session.list_tools()
    registered_names: list[str] = []

    trusted_ro: set[str] = set()
    if trusted_read_only_tools is not None:
        trusted_ro = {t.lower().replace("-", "_") for t in trusted_read_only_tools}
    elif getattr(session, "trusted_read_only_tools", None):
        trusted_ro = set(session.trusted_read_only_tools)

    consequential_set: set[str] = set()
    if consequential_tools is not None:
        consequential_set = {t.lower().replace("-", "_") for t in consequential_tools}
    elif getattr(session, "consequential_tools", None):
        consequential_set = set(session.consequential_tools)

    for tool_spec in tools:
        raw_name = tool_spec.get("name", "")
        if not raw_name:
            continue

        clean_tool_name = raw_name.replace("-", "_").lower()
        registered_name = f"mcp_{session.name}_{clean_tool_name}"
        desc = tool_spec.get("description", f"External MCP tool '{raw_name}' from server '{session.name}'.")

        # Consequential / destructive code or command execution tools fail closed to risk_level=2 (Critical)
        is_consequential = (
            (clean_tool_name in consequential_set or raw_name.lower() in consequential_set)
            or any(term in clean_tool_name for term in CONSEQUENTIAL_MCP_PATTERNS)
        )
        is_trusted_ro = (clean_tool_name in trusted_ro or raw_name.lower() in trusted_ro) and not is_consequential

        if is_trusted_ro:
            tool_risk = 0
            tool_ro = True
            SkillRegistry.certify_test_read_only_tool(registered_name)
        elif is_consequential:
            tool_risk = 2
            tool_ro = False
        else:
            tool_risk = 1
            tool_ro = False

        # Closure generator binding session and tool name
        def _build_handler(sess: MCPClientSession, t_name: str, reg_name: str, doc_desc: str):
            def _handler(**kwargs):
                # Mark current turn tainted upon invoking external MCP tool with provenance
                TurnTaintTracker.get_instance().record_ingestion(reg_name, kwargs)
                return sess.call_tool(t_name, kwargs)

            _handler.__name__ = reg_name
            _handler.__doc__ = doc_desc
            return _handler

        handler_fn = _build_handler(session, raw_name, registered_name, desc)
        # Register with risk_level=tool_risk (tainting & gated), origin="mcp", external_ingestion=True, and read_only=tool_ro
        SkillRegistry.register(
            name=registered_name,
            description=desc,
            risk_level=tool_risk,
            origin="mcp",
            external_ingestion=True,
            read_only=tool_ro,
        )(handler_fn)
        TurnTaintTracker.get_instance().register_ingestion_tool(registered_name)
        registered_names.append(registered_name)
        logger.info("Registered dynamic MCP skill: %s (risk_level=%d, read_only=%s)", registered_name, tool_risk, tool_ro)

    return registered_names


class MCPManager:
    """Manages active MCP client sessions and their exposed tool integrations."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        base_dir = Path(self.config.get("_base_dir", "."))
        cfg_rel_path = self.config.get("mcp", {}).get("config_path", "config/mcp_servers.json")
        self.config_path = base_dir / cfg_rel_path
        self.active_sessions: dict[str, MCPClientSession] = {}
        self._lock = threading.Lock()

    def connect_http(
        self,
        name: str,
        endpoint_url: str,
        headers: dict[str, str] | None = None,
        tool_cache_ttl_sec: float = DEFAULT_TOOL_CACHE_TTL_SEC,
        trusted_read_only_tools: Iterable[str] | None = None,
    ) -> MCPClientSession:
        """Establishes a stateless HTTP MCP session and auto-registers tools."""
        with self._lock:
            if name in self.active_sessions:
                self.active_sessions[name].close()

            session = MCPClientSession(
                name=name,
                transport="http",
                endpoint_url=endpoint_url,
                headers=headers,
                tool_cache_ttl_sec=tool_cache_ttl_sec,
                trusted_read_only_tools=trusted_read_only_tools,
            )
            self.active_sessions[name] = session
            return session

    def connect_stdio(
        self,
        name: str,
        command: list[str],
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        trusted_read_only_tools: Iterable[str] | None = None,
    ) -> MCPClientSession:
        """Establishes an E-core sandboxed stdio MCP session and auto-registers tools."""
        with self._lock:
            if name in self.active_sessions:
                self.active_sessions[name].close()

            session = MCPClientSession(
                name=name,
                transport="stdio",
                command=command,
                args=args,
                env=env,
                cwd=cwd,
                trusted_read_only_tools=trusted_read_only_tools,
            )
            self.active_sessions[name] = session
            return session

    def get_session(self, name: str) -> MCPClientSession | None:
        with self._lock:
            return self.active_sessions.get(name)

    def list_available_mcp_tools(self) -> list[dict[str, Any]]:
        """Aggregates all exposed tools across all enabled/connected sessions."""
        all_tools = []
        with self._lock:
            sessions = list(self.active_sessions.values())

        for sess in sessions:
            try:
                tools = sess.list_tools()
                for t in tools:
                    t_copy = dict(t)
                    t_copy["server"] = sess.name
                    all_tools.append(t_copy)
            except Exception as e:
                logger.warning("Could not list tools from MCP session '%s': %s", sess.name, e)

    def load_servers_config(self) -> dict[str, Any]:
        """Loads mcpServers definitions from config_path."""
        if not self.config_path.exists():
            logger.debug("MCP config file '%s' does not exist.", self.config_path)
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("mcpServers", {})
        except Exception as e:
            logger.error("Failed to parse MCP config '%s': %s", self.config_path, e)
            return {}

    def start_configured_servers(self, auto_register_skills: bool = True) -> dict[str, list[str]]:
        """Spawns enabled MCP servers from configuration and optionally registers tools."""
        servers = self.load_servers_config()
        registered_by_server: dict[str, list[str]] = {}

        for server_name, server_def in servers.items():
            if not isinstance(server_def, dict) or not server_def.get("enabled", True):
                logger.debug("Skipping disabled MCP server: %s", server_name)
                continue

            try:
                transport = server_def.get("transport")
                if not transport:
                    transport = "http" if "endpoint_url" in server_def else "stdio"

                trusted_ro = server_def.get("trusted_read_only_tools") or server_def.get("read_only_tools") or []
                session: MCPClientSession | None = None
                if transport == "http":
                    url = server_def.get("endpoint_url", "")
                    headers = server_def.get("headers")
                    session = self.connect_http(server_name, url, headers=headers, trusted_read_only_tools=trusted_ro)
                elif transport == "stdio":
                    cmd = server_def.get("command")
                    args = server_def.get("args", [])
                    raw_env = server_def.get("env")
                    cwd = server_def.get("cwd")

                    resolved_args = [os.path.expandvars(a) if isinstance(a, str) else str(a) for a in args]

                    resolved_env = {}
                    if raw_env and isinstance(raw_env, dict):
                        for k, v in raw_env.items():
                            if isinstance(v, str):
                                resolved_env[k] = os.path.expandvars(v)
                            else:
                                resolved_env[k] = str(v)

                    session = self.connect_stdio(
                        server_name,
                        command=[cmd] if isinstance(cmd, str) else cmd,
                        args=resolved_args,
                        env=resolved_env or None,
                        cwd=cwd,
                        trusted_read_only_tools=trusted_ro,
                    )

                if session and auto_register_skills:
                    registered_tools = register_discovered_mcp_tools(session, trusted_read_only_tools=trusted_ro)
                    registered_by_server[server_name] = registered_tools
                    logger.info("MCP server '%s' started with %d tools registered.", server_name, len(registered_tools))
                elif session:
                    registered_by_server[server_name] = []
            except Exception as e:
                logger.warning("Failed to start MCP server '%s': %s", server_name, e)

        return registered_by_server

    def shutdown_all(self) -> None:
        """Deterministically shuts down all active MCP client sessions."""
        with self._lock:
            sessions = list(self.active_sessions.values())
            self.active_sessions.clear()

        for sess in sessions:
            try:
                sess.close()
            except Exception as err:
                logger.debug("Error closing MCP session '%s': %s", sess.name, err)
        logger.info("All active MCP sessions have been terminated.")
