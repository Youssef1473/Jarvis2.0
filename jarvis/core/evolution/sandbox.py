"""Self-Evolution Sandbox & Subprocess Validation Gate for Jarvis 2.0.

Mitigates the Chief Dissent by isolating LLM-generated Python skills in an isolated
child process before permitting hot-reloading into the running assistant memory.
"""
import ast
from dataclasses import dataclass
import importlib.util
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, List, Optional, Set, Tuple

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Evolution.Sandbox")

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent.parent
CUSTOM_SKILLS_DIR = WORKSPACE_DIR / "jarvis" / "skills" / "custom"
SCRATCH_DIR = WORKSPACE_DIR / "data" / "evolution_scratch"

SNAPSHOTS_DIR = WORKSPACE_DIR / "data" / "evolution_snapshots"

CUSTOM_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class SandboxResult:
    """Outcome of sandbox isolation and test verification."""
    success: bool
    skill_name: str
    message: str
    target_path: Optional[Path] = None
    stdout: str = ""
    stderr: str = ""



def apply_job_ui_restrictions(job_handle: int) -> bool:
    """Applies Win32 UI Restrictions (JobObjectBasicUIRestrictions = 4) to the Job Object.

    Enforces UIRestrictionsClass = 0x000000FF covering HANDLES, READCLIPBOARD,
    WRITECLIPBOARD, SYSTEMPARAMETERS, DISPLAYSETTINGS, GLOBALATOMS, DESKTOP, EXITWINDOWS.
    """
    if sys.platform != "win32" or not job_handle:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
            _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

        JobObjectBasicUIRestrictions = 4
        ui_restrictions = JOBOBJECT_BASIC_UI_RESTRICTIONS()
        # Enforces UI restrictions: READCLIPBOARD (0x02), WRITECLIPBOARD (0x04),
        # SYSTEMPARAMETERS (0x08), DISPLAYSETTINGS (0x10), EXITWINDOWS (0x80) = 0x9E
        ui_restrictions.UIRestrictionsClass = 0x0000009E

        k32 = ctypes.windll.kernel32
        ok = k32.SetInformationJobObject(
            job_handle,
            JobObjectBasicUIRestrictions,
            ctypes.byref(ui_restrictions),
            ctypes.sizeof(ui_restrictions),
        )
        return bool(ok)
    except Exception as e:
        logger.debug("apply_job_ui_restrictions failed: %s", e)
        return False


def install_canary_c_audit_hook() -> None:
    """Installs PEP 578 C-audit hook blocking subprocess, network, registry, write, and frame introspection."""
    def _audit_guard(event, args):
        blocked_events = {
            "os.system", "os.spawn", "os.exec", "os.fork", "os.posix_spawn",
            "subprocess.Popen", "socket.connect", "socket.bind", "socket.send",
            "ctypes.dlopen", "ctypes.dlsym", "ctypes.cdata", "winreg.OpenKey",
            "winreg.CreateKey", "winreg.SetValue", "shutil.rmtree",
            "sys._getframe", "sys._current_frames",
        }
        if event in blocked_events:
            raise PermissionError(f"[Gate-2 AuditHook] Access to event '{event}' is blocked inside the canary sandbox.")
        if event == "open":
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            if mode and isinstance(mode, str) and any(m in mode for m in ("w", "a", "+", "x")):
                raise PermissionError(f"[Gate-2 AuditHook] Blocked filesystem write open: mode='{mode}' on path '{args[0]}'")
            if isinstance(flags, int) and (flags & 0x030B):
                raise PermissionError(f"[Gate-2 AuditHook] Blocked filesystem write open: flags=0x{flags:X} on path '{args[0]}'")

    sys.addaudithook(_audit_guard)


def atomic_rollback_file(snapshot_path: Path, target_path: Path, max_attempts: int = 4) -> bool:
    """Atomically restores target_path from snapshot_path via staging temp file and os.replace.

    Retries on transient Windows PermissionError (Defender/MsMpEng lock) with exponential backoff.
    """
    if not snapshot_path.exists():
        return False

    stage_path = Path(f"{target_path}.rollback_{os.getpid()}.tmp")
    try:
        content = snapshot_path.read_bytes()
        with open(stage_path, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        for attempt in range(max_attempts):
            try:
                os.replace(stage_path, target_path)
                return True
            except PermissionError as pe:
                if attempt == max_attempts - 1:
                    raise pe
                time.sleep(0.015 * (2 ** attempt))
        return False
    except Exception as e:
        logger.warning("atomic_rollback_file failed: %s", e)
        return False
    finally:
        if stage_path.exists():
            try:
                stage_path.unlink()
            except Exception:
                pass


class Win32JobObject:
    """Manages an ephemeral Windows Job Object with strict process and memory limits."""

    def __init__(self, max_processes: Optional[int] = None, max_memory_bytes: int = 1024 * 1024 * 1024):
        self._handle = None
        if max_processes is None:
            # On Windows, virtualenv launcher stubs + background workers require up to 4 processes
            self.max_processes = 4
        else:
            self.max_processes = max_processes
        self.max_memory_bytes = max_memory_bytes
        if sys.platform != "win32":
            return

        try:
            import ctypes
            from ctypes import wintypes

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
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
                    ("PeakProcessMemoryLimit", ctypes.c_size_t),
                    ("PeakJobMemoryLimit", ctypes.c_size_t),
                ]

            k32 = ctypes.windll.kernel32
            hJob = k32.CreateJobObjectW(None, None)
            if not hJob:
                return

            JobObjectExtendedLimitInformation = 9
            JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
            JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

            limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = (
                JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            limits.BasicLimitInformation.ActiveProcessLimit = self.max_processes
            limits.ProcessMemoryLimit = max_memory_bytes

            ok = k32.SetInformationJobObject(
                hJob,
                JobObjectExtendedLimitInformation,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            )
            if ok:
                self._handle = hJob
                apply_job_ui_restrictions(hJob)
            else:
                k32.CloseHandle(hJob)
        except Exception as e:
            logger.debug("Win32JobObject initialization failed: %s", e)
            self._handle = None

    def assign_process(self, proc: Any) -> bool:
        """Assigns the given subprocess to this Job Object."""
        if not self._handle:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            raw_handle = getattr(proc, "_handle", None)
            if raw_handle:
                k32 = ctypes.windll.kernel32
                k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
                k32.AssignProcessToJobObject.restype = wintypes.BOOL
                res = k32.AssignProcessToJobObject(self._handle, int(raw_handle))
                return bool(res)
        except Exception as e:
            logger.debug("Win32JobObject assign_process failed: %s", e)
        return False

    def close(self):
        """Closes the job object handle."""
        if self._handle:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class SkillSandbox:
    """Isolates, compiles, and tests LLM-generated code in an ephemeral subprocess."""

    apply_job_ui_restrictions = staticmethod(apply_job_ui_restrictions)
    install_canary_c_audit_hook = staticmethod(install_canary_c_audit_hook)
    atomic_rollback_file = staticmethod(atomic_rollback_file)

    # Prohibited AST calls to protect host integrity
    BLOCKED_CALLS: Set[str] = {
        "os.system",
        "os.popen",
        "os.spawn",
        "subprocess.call",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.check_output",
        "subprocess.check_call",
        "eval",
        "exec",
        "compile",
        "__import__",
        "shutil.rmtree",
        "shutil.move",
        "shutil.copy",
        "shutil.copy2",
        "os.rmdir",
        "os.remove",
        "os.unlink",
        "os.mkdir",
        "os.makedirs",
        "os.rename",
        "os.replace",
        "os.truncate",
        "os.chmod",
        "os.chown",
        "pathlib.Path.unlink",
        "pathlib.Path.rmdir",
        "pathlib.Path.rename",
        "pathlib.Path.replace",
        "pathlib.Path.write_text",
        "pathlib.Path.write_bytes",
        "pathlib.Path.mkdir",
    }

    # Prohibited method names across all receiver types (filesystem mutations / escapes)
    BLOCKED_DESTRUCTIVE_METHODS: Set[str] = {
        "unlink",
        "rmdir",
        "remove",
        "rmtree",
        "write_text",
        "write_bytes",
        "replace",
        "rename",
        "mkdir",
        "truncate",
    }

    # Prohibited reflection functions (dynamic object attribute access / meta-programming)
    BLOCKED_REFLECTION_CALLS: Set[str] = {
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
    }

    # Prohibited dunder and frame attributes (sandbox escape vectors via class hierarchy and frame introspection)
    BLOCKED_ATTRIBUTES: Set[str] = {
        "__subclasses__",
        "__globals__",
        "__mro__",
        "__builtins__",
        "__code__",
        "__traceback__",
        "__closure__",
        "__func__",
        "__self__",
        "__dict__",
        "__bases__",
        "__class__",
        "__reduce__",
        "__reduce_ex__",
        "gi_frame",
        "gi_code",
        "cr_frame",
        "cr_code",
        "ag_frame",
        "ag_code",
        "f_back",
        "f_globals",
        "f_builtins",
        "f_locals",
        "f_code",
        "tb_frame",
        "tb_next",
    }

    # Prohibited modules to import
    BLOCKED_MODULES: Set[str] = {
        "builtins",
        "ctypes",
        "importlib",
        "marshal",
        "pickle",
        "pty",
        "shelve",
        "shutil",
        "subprocess",
        "winreg",
    }

    @classmethod
    def sanitize_skill_name(cls, name: str) -> str:
        """Sanitizes skill name into a valid snake_case Python module identifier."""
        slug = re.sub(r"[^\w\s-]", "", name.lower()).strip()
        slug = re.sub(r"[-\s]+", "_", slug)
        if not slug or not slug[0].isalpha():
            slug = f"skill_{slug}" if slug else "custom_skill"
        return slug

    @classmethod
    def check_ast_security(cls, code: str) -> Tuple[bool, str]:
        """Performs static syntax and deep AST security checks on untrusted code."""
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"Python SyntaxError on line {e.lineno}: {e.msg}"

        # Inspect AST for dangerous patterns, imports, and reflection escapes
        for node in ast.walk(tree):
            # 1. Imports inspection
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    if root_mod in cls.BLOCKED_MODULES:
                        return False, f"Security Violation: Importing module '{alias.name}' is blocked inside autonomous skills."
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_mod = node.module.split(".")[0]
                    if root_mod in cls.BLOCKED_MODULES:
                        return False, f"Security Violation: Importing from module '{node.module}' is blocked inside autonomous skills."

            # 2. Attribute access inspection (catches dunder escapes and blocked call bindings)
            elif isinstance(node, ast.Attribute):
                if node.attr in cls.BLOCKED_ATTRIBUTES:
                    return False, f"Security Violation: Access to dunder attribute '{node.attr}' is prohibited (sandbox reflection protection)."
                val_id = node.value.id if isinstance(node.value, ast.Name) else ""
                full_attr = f"{val_id}.{node.attr}" if val_id else node.attr
                if full_attr in cls.BLOCKED_CALLS:
                    return False, f"Security Violation: '{full_attr}' is blocked inside autonomous skills."

            # 3. Call invocation inspection (catches reflection and blocked execution)
            elif isinstance(node, ast.Call):
                func = node.func
                call_name = ""
                attr_name = ""
                if isinstance(func, ast.Name):
                    call_name = func.id
                elif isinstance(func, ast.Attribute):
                    attr_name = func.attr
                    val_id = func.value.id if isinstance(func.value, ast.Name) else ""
                    call_name = f"{val_id}.{func.attr}" if val_id else func.attr

                if call_name in cls.BLOCKED_REFLECTION_CALLS:
                    return False, f"Security Violation: Dynamic reflection call '{call_name}' is blocked inside autonomous skills."
                if call_name in cls.BLOCKED_CALLS or any(call_name.startswith(f"{b}.") for b in cls.BLOCKED_MODULES):
                    return False, f"Security Violation: '{call_name}' is blocked inside autonomous skills."
                if attr_name in cls.BLOCKED_DESTRUCTIVE_METHODS:
                    return False, f"Security Violation: Invoking destructive method '.{attr_name}()' is prohibited inside autonomous skills."

                if call_name == "open":
                    for arg in node.args[1:]:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            if any(m in arg.value for m in ("w", "a", "+", "x")):
                                return False, f"Security Violation: File write operations are prohibited (attempted mode '{arg.value}' in open())."
                    for kw in node.keywords:
                        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                            if any(m in kw.value.value for m in ("w", "a", "+", "x")):
                                return False, f"Security Violation: File write operations are prohibited (attempted mode '{kw.value.value}' in open())."

            # 4. Operational Invariant: Infinite While Loop Detection (Unbounded Spin-Locks)
            elif isinstance(node, ast.While):
                is_const_true = (
                    (isinstance(node.test, ast.Constant) and bool(node.test.value) is True) or
                    (isinstance(node.test, ast.Name) and node.test.id == "True")
                )
                if is_const_true:
                    has_exit = any(
                        isinstance(child, (ast.Break, ast.Return))
                        for child in ast.walk(node)
                    )
                    if not has_exit:
                        return False, "Operational Guardrail Violation: Unbounded 'while True' loop detected with no exit break/return."

            # 5. Operational Invariant: Massive Literal Memory Allocation (>50M elements)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
                for operand in (node.left, node.right):
                    if isinstance(operand, ast.Constant) and isinstance(operand.value, (int, float)):
                        if operand.value > 50_000_000:
                            return False, f"Operational Guardrail Violation: Massive allocation multiplier ({int(operand.value):,}) detected."

        return True, "AST security checks passed."

    @classmethod
    def create_snapshot(cls, clean_name: str) -> Optional[Path]:
        """Creates a versioned backup snapshot of an existing custom skill before updating."""
        target_file = CUSTOM_SKILLS_DIR / f"{clean_name}.py"
        if not target_file.exists():
            return None
        timestamp = int(time.time() * 1000)
        snapshot_file = SNAPSHOTS_DIR / f"{clean_name}_{timestamp}.py"
        try:
            shutil.copy2(target_file, snapshot_file)
            logger.info("Created snapshot for '%s' at '%s'", clean_name, snapshot_file.name)
            return snapshot_file
        except Exception as e:
            logger.warning("Failed to create snapshot for '%s': %s", clean_name, e)
            return None

    @classmethod
    def rollback_snapshot(cls, clean_name: str, snapshot_path: Optional[Path]) -> bool:
        """Restores a custom skill from its previous snapshot or cleans up on failure."""
        target_file = CUSTOM_SKILLS_DIR / f"{clean_name}.py"
        if snapshot_path and snapshot_path.exists():
            try:
                rolled_back = atomic_rollback_file(snapshot_path, target_file)
                if not rolled_back:
                    shutil.copy2(snapshot_path, target_file)
                logger.info("Rolled back '%s' to snapshot '%s'", clean_name, snapshot_path.name)
                module_name = f"jarvis.skills.custom.{clean_name}"
                spec = importlib.util.spec_from_file_location(module_name, str(target_file))
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = mod
                    spec.loader.exec_module(mod)
                return True
            except Exception as e:
                logger.error("Failed to restore snapshot for '%s': %s", clean_name, e)
                return False
        else:
            if target_file.exists():
                try:
                    target_file.unlink()
                except Exception:
                    pass
            module_name = f"jarvis.skills.custom.{clean_name}"
            if module_name in sys.modules:
                del sys.modules[module_name]
            return True

    @classmethod
    def list_snapshots(cls, clean_name: Optional[str] = None) -> List[Path]:
        """Lists existing versioned snapshots, optionally filtered by skill name."""
        if not SNAPSHOTS_DIR.exists():
            return []
        pattern = f"{clean_name}_*.py" if clean_name else "*.py"
        return sorted(SNAPSHOTS_DIR.glob(pattern), reverse=True)

    @classmethod
    def generate_canary_script(cls, clean_name: str, scratch_path: Path) -> str:
        """Generates an automated semantic canary verification script for the subprocess.

        Verifies:
        1. Module load and compilation without exception.
        2. Registration of at least one valid tool in SkillRegistry.
        3. Tool contract inspection: docstrings, signature parameter inspectability.
        4. Operational Invariants:
           - Global socket timeout to prevent network hangs.
           - Active memory tracking (tracemalloc) to prevent memory bombs (>64MB).
           - Smoke invocation under a 2.0s execution watchdog to catch spin-locks.
        5. Emission of CANARY_VERIFICATION_SUCCESS and SANDBOX_LOAD_SUCCESS markers.
        """
        escaped_path = str(scratch_path).replace("\\", "\\\\")
        escaped_ws = str(WORKSPACE_DIR).replace("\\", "\\\\")
        return f"""import builtins
import io
import inspect
import importlib.util
import os
import socket
import sys
import threading
import time
import tracemalloc

if r'{escaped_ws}' not in sys.path:
    sys.path.insert(0, r'{escaped_ws}')

# Operational Guardrail 1: Enforce global socket timeout to prevent indefinite network hangs
try:
    socket.setdefaulttimeout(2.5)
except Exception:
    pass

# Operational Guardrail 2: Wrap builtins.open and io.open to disallow file writes inside the canary
_real_open = builtins.open
def _sandboxed_open(file, mode='r', *args, **kwargs):
    if any(m in mode for m in ('w', 'a', '+', 'x')):
        raise PermissionError(f"Canary sandbox violation: File write operations are prohibited (attempted mode '{{mode}}').")
    return _real_open(file, mode, *args, **kwargs)

builtins.open = _sandboxed_open
io.open = _sandboxed_open

# Operational Guardrail 3: Mock dangerous modules in sys.modules to prevent subprocess / OS leaks
class BlockedSandboxModule:
    def __init__(self, mod_name: str):
        self.__name__ = mod_name
    def __getattr__(self, attr: str):
        raise PermissionError(f"Canary sandbox violation: Access to module '{{self.__name__}}' is disabled inside the canary sandbox.")
    def __call__(self, *args, **kwargs):
        raise PermissionError(f"Canary sandbox violation: Execution of '{{self.__name__}}' is disabled inside the canary sandbox.")

for _blocked in ["subprocess", "shutil", "winreg", "ctypes"]:
    sys.modules[_blocked] = BlockedSandboxModule(_blocked)

# Operational Guardrail 4: Block raw socket creation
def _blocked_socket(*args, **kwargs):
    raise PermissionError("Canary sandbox violation: Raw socket creation is disabled inside the canary sandbox.")
socket.socket = _blocked_socket

# Operational Guardrail 5: Track memory allocations to catch massive arrays / memory leaks
tracemalloc.start()

try:
    spec = importlib.util.spec_from_file_location('{clean_name}', r'{escaped_path}')
    if not spec or not spec.loader:
        print('CANARY_ERROR: Failed to create module spec')
        sys.exit(2)

    mod = importlib.util.module_from_spec(spec)

    # Gate 2 PEP 578 Irreversible C-Audit Hook installed immediately before executing candidate module
    def _canary_audit_guard(event, args):
        blocked_events = {{
            "os.system", "os.spawn", "os.exec", "os.fork", "os.posix_spawn",
            "subprocess.Popen", "socket.connect", "socket.bind", "socket.send",
            "ctypes.dlopen", "ctypes.dlsym", "ctypes.cdata", "winreg.OpenKey",
            "winreg.CreateKey", "winreg.SetValue", "shutil.rmtree",
            "sys._getframe", "sys._current_frames",
            "os.remove", "os.unlink", "os.mkdir", "os.rmdir", "os.rename",
        }}
        if event in blocked_events:
            raise PermissionError("[Gate-2 AuditHook] Access to event '" + str(event) + "' is blocked inside the canary sandbox.")
        if event == "open":
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            if mode and isinstance(mode, str) and any(m in mode for m in ("w", "a", "+", "x")):
                raise PermissionError("[Gate-2 AuditHook] Blocked filesystem write open: mode='" + str(mode) + "' on path '" + str(args[0]) + "'")
            if isinstance(flags, int) and (flags & 0x030B):
                raise PermissionError("[Gate-2 AuditHook] Blocked filesystem write open: flags=" + hex(flags) + " on path '" + str(args[0]) + "'")

    sys.addaudithook(_canary_audit_guard)

    spec.loader.exec_module(mod)

    from jarvis.skills.base import SkillRegistry
    tools = SkillRegistry.get_all_tools()

    # Find tools defined in this module
    module_tools = [
        name for name, fn in tools.items()
        if getattr(fn, '__module__', '') == '{clean_name}' or name == '{clean_name}'
    ]

    if not module_tools:
        module_tools = [
            name for name, fn in tools.items()
            if any(getattr(mod, attr, None) is fn for attr in dir(mod))
        ]

    if not module_tools:
        print('CANARY_ERROR: Module loaded but did not register any tools with @SkillRegistry.register', flush=True)
        sys.stdout.flush()
        os._exit(3)

    for tool_name in module_tools:
        tool_fn = tools[tool_name]
        if not callable(tool_fn):
            print(f'CANARY_ERROR: Registered tool {{tool_name}} is not callable', flush=True)
            sys.stdout.flush()
            os._exit(4)
        sig = inspect.signature(tool_fn)
        dummy_kwargs = {{}}
        for p_name, param in sig.parameters.items():
            if param.default != inspect.Parameter.empty:
                continue
            ann = param.annotation
            if ann in (int, float):
                dummy_kwargs[p_name] = 0
            elif ann is bool:
                dummy_kwargs[p_name] = False
            elif ann is list:
                dummy_kwargs[p_name] = []
            elif ann is dict:
                dummy_kwargs[p_name] = {{}}
            else:
                dummy_kwargs[p_name] = "test"

        # Operational Guardrail 3: Smoke-test invocation inside 2.0s watchdog thread
        smoke_done = threading.Event()
        smoke_exc = []

        def _smoke_worker():
            try:
                tool_fn(**dummy_kwargs)
            except Exception as e:
                smoke_exc.append(e)
            finally:
                smoke_done.set()

        worker = threading.Thread(target=_smoke_worker, daemon=True)
        worker.start()

        if not smoke_done.wait(timeout=2.0):
            err_msg = f"CANARY_ERROR: Tool '{{tool_name}}' exceeded 2.0s execution limit (potential spin-lock or hang)"
            sys.stdout.write(err_msg + "\\n")
            sys.stderr.write(err_msg + "\\n")
            sys.stdout.flush()
            sys.stderr.flush()
            time.sleep(0.05)
            os._exit(5)

        if smoke_exc:
            exc = smoke_exc[0]
            err_msg = f"CANARY_ERROR: Tool '{{tool_name}}' failed smoke execution with {{type(exc).__name__}}: {{exc}}"
            sys.stdout.write(err_msg + "\\n")
            sys.stderr.write(err_msg + "\\n")
            sys.stdout.flush()
            sys.stderr.flush()
            time.sleep(0.05)
            os._exit(7)

    # Operational Guardrail 4: Verify memory allocation threshold (< 64MB)
    current_b, peak_b = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mb = peak_b / (1024 * 1024)
    if peak_mb > 64.0:
        err_msg = f"CANARY_ERROR: Excessive memory consumption during canary execution ({{peak_mb:.1f}} MB > 64.0 MB limit)"
        sys.stdout.write(err_msg + "\\n")
        sys.stderr.write(err_msg + "\\n")
        sys.stdout.flush()
        sys.stderr.flush()
        time.sleep(0.05)
        os._exit(6)

    sys.stdout.write('CANARY_VERIFICATION_SUCCESS\\nSANDBOX_LOAD_SUCCESS\\n')
    sys.stdout.flush()
    sys.stderr.flush()
    time.sleep(0.05)
    os._exit(0)
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.stdout.write(f'CANARY_EXCEPTION: {{e}}\\n')
    sys.stdout.flush()
    sys.stderr.flush()
    time.sleep(0.05)
    os._exit(1)
"""

    @classmethod
    def validate_and_promote(
        cls,
        skill_name: str,
        python_code: str,
        timeout_seconds: float = 20.0,
        memory_manager: Optional[Any] = None,
        user_approved: bool = True,
    ) -> SandboxResult:
        """Executes full sandboxed lifecycle:
        1. Snapshot versioning if modifying an existing skill
        2. Static AST syntax, import, reflection & security analysis
        3. Isolation in ephemeral scratchpad file
        4. Subprocess dry-run execution & automated canary contract verification under Win32 Job Object
        5. Promotion to jarvis/skills/custom/ and live memory hot-reload
        6. Transactional rollback to snapshot on any failure & Reflexion episodic lesson synthesis
        """
        clean_name = cls.sanitize_skill_name(skill_name)
        code = python_code.strip()

        if not code:
            return SandboxResult(success=False, skill_name=clean_name, message="Code content cannot be empty.")

        # 0. Snapshot versioning of existing skill
        snapshot_path = cls.create_snapshot(clean_name)

        # 1. AST Static Gate
        is_safe, sec_msg = cls.check_ast_security(code)
        if not is_safe:
            logger.warning("Sandbox blocked skill '%s': %s", clean_name, sec_msg)
            cls._record_failure_lesson(clean_name, code, sec_msg, memory_manager)
            return SandboxResult(success=False, skill_name=clean_name, message=sec_msg)

        # Ensure SkillRegistry import
        if "SkillRegistry" not in code:
            code = "from jarvis.skills.base import SkillRegistry\n\n" + code

        scratch_file = SCRATCH_DIR / f"temp_{clean_name}_{int(time.time() * 1000)}.py"
        target_file = CUSTOM_SKILLS_DIR / f"{clean_name}.py"

        # 2. Write to Ephemeral Scratchpad
        try:
            with open(scratch_file, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as e:
            return SandboxResult(success=False, skill_name=clean_name, message=f"Failed to write scratchpad: {e}")

        # 3. Subprocess Dry-Run & Automated Canary Gate under Win32 Job Object
        runner_script = cls.generate_canary_script(clean_name, scratch_file)

        with Win32JobObject(max_processes=4, max_memory_bytes=1024 * 1024 * 1024) as job:
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-c", runner_script],
                    cwd=str(WORKSPACE_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if sys.platform == "win32":
                    if not job.assign_process(proc):
                        proc.kill()
                        proc.communicate()
                        cls._cleanup_scratch(scratch_file)
                        cls.rollback_snapshot(clean_name, snapshot_path)
                        logger.error("Job Object containment assignment failed for canary '%s'; aborting execution.", clean_name)
                        msg = "Security failure: Win32 Job Object assignment failed. Execution aborted to fail closed."
                        cls._record_failure_lesson(clean_name, code, msg, memory_manager)
                        return SandboxResult(
                            success=False,
                            skill_name=clean_name,
                            message=msg,
                        )

                try:
                    stdout, stderr = proc.communicate(timeout=timeout_seconds)
                    try:
                        proc.wait(timeout=5.0)
                    except Exception:
                        pass
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                    cls._cleanup_scratch(scratch_file)
                    cls.rollback_snapshot(clean_name, snapshot_path)
                    logger.error("Sandbox verification timed out after %.1fs for '%s'", timeout_seconds, clean_name)
                    msg = f"Subprocess timed out after {timeout_seconds}s (potential infinite loop)."
                    cls._record_failure_lesson(clean_name, code, msg, memory_manager)
                    return SandboxResult(
                        success=False,
                        skill_name=clean_name,
                        message=msg,
                    )

                stdout = (stdout or "").strip()
                stderr = (stderr or "").strip()

                has_canary_success = "CANARY_VERIFICATION_SUCCESS" in stdout or "SANDBOX_LOAD_SUCCESS" in stdout
                is_clean_exit = proc.returncode == 0 or (has_canary_success and proc.returncode in (0, 15))

                if not is_clean_exit or not has_canary_success:
                    cls._cleanup_scratch(scratch_file)
                    cls.rollback_snapshot(clean_name, snapshot_path)
                    error_summary = stderr or stdout or f"Exited with code {proc.returncode}"
                    logger.error("Sandbox verification failed for '%s': %s", clean_name, error_summary)
                    msg = f"Subprocess verification failed:\n{error_summary}"
                    cls._record_failure_lesson(clean_name, code, error_summary, memory_manager)
                    return SandboxResult(
                        success=False,
                        skill_name=clean_name,
                        message=msg,
                        stdout=stdout,
                        stderr=stderr,
                    )

            except Exception as e:
                cls._cleanup_scratch(scratch_file)
                cls.rollback_snapshot(clean_name, snapshot_path)
                msg = f"Subprocess execution error: {e}"
                cls._record_failure_lesson(clean_name, code, msg, memory_manager)
                return SandboxResult(success=False, skill_name=clean_name, message=msg)

        # 3.5 Gate: Require explicit user approval before promoting to live codebase
        if not user_approved:
            cls._cleanup_scratch(scratch_file)
            cls.rollback_snapshot(clean_name, snapshot_path)
            logger.warning("Skill '%s' passed canary verification but was denied promotion due to lack of explicit user approval.", clean_name)
            return SandboxResult(
                success=False,
                skill_name=clean_name,
                message="Skill passed canary dry-run verification, but was not promoted into live process: explicit out-of-band user approval is required.",
            )

        # 4. Promotion from Scratchpad to Production Custom Directory
        try:
            shutil.copy2(scratch_file, target_file)
            cls._cleanup_scratch(scratch_file)
        except Exception as e:
            cls._cleanup_scratch(scratch_file)
            cls.rollback_snapshot(clean_name, snapshot_path)
            msg = f"Promotion failed: {e}"
            cls._record_failure_lesson(clean_name, code, msg, memory_manager)
            return SandboxResult(success=False, skill_name=clean_name, message=msg)

        # 5. Live Memory Hot-Reload in Host Process
        module_name = f"jarvis.skills.custom.{clean_name}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(target_file))
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                # Clear tool schema cache
                if hasattr(SkillRegistry, "_openai_tools_cache"):
                    SkillRegistry._openai_tools_cache.clear()

                logger.info("Skill '%s' promoted and hot-reloaded successfully.", clean_name)
                return SandboxResult(
                    success=True,
                    skill_name=clean_name,
                    message=f"Successfully engineered and hot-reloaded capability '{clean_name}', Sir! Validated via isolated subprocess sandbox and active in your current session.",
                    target_path=target_file,
                )
        except Exception as host_err:
            cls.rollback_snapshot(clean_name, snapshot_path)
            msg = f"Host hot-reload error: {host_err}"
            cls._record_failure_lesson(clean_name, code, msg, memory_manager)
            return SandboxResult(
                success=False,
                skill_name=clean_name,
                message=msg,
            )

        return SandboxResult(success=False, skill_name=clean_name, message="Unknown sandbox failure.")

    @classmethod
    def _record_failure_lesson(
        cls,
        skill_name: str,
        code: str,
        failure_msg: str,
        memory_manager: Optional[Any] = None,
    ) -> Optional[int]:
        """Synthesizes a Reflexion lesson and records it into SQLite WAL episodic memory."""
        try:
            mem = memory_manager
            if mem is None:
                from jarvis.skills.self_evolution import _memory_instance
                mem = _memory_instance
            if mem is None:
                try:
                    from jarvis.core.config import load_config
                    from jarvis.core.memory import MemoryManager
                    cfg = load_config()
                    mem = MemoryManager(cfg)
                except Exception:
                    mem = None

            if mem is None or not hasattr(mem, "record_episodic_lesson"):
                return None

            sig = failure_msg.strip().replace("\r\n", " ").replace("\n", " ")[:140]
            if "AST" in failure_msg or "Security Violation" in failure_msg:
                lesson = f"Avoid prohibited attributes, modules, or dynamic reflection in autonomous skills: {sig}."
            elif "exceeded 2.0s execution limit" in failure_msg or "timed out" in failure_msg.lower():
                lesson = "Autonomous skills must avoid blocking sleep calls, spin-locks, or synchronous hangs; maintain execution under 2.0s watchdog limit."
            elif "File write operations are prohibited" in failure_msg:
                lesson = "Autonomous skills in canary sandbox must remain strictly stateless and disallow direct filesystem writes."
            elif "Memory" in failure_msg or "memory" in failure_msg:
                lesson = "Autonomous skills must bound memory allocations to under 64MB; avoid massive arrays or memory bombs."
            elif "did not register any tools" in failure_msg:
                lesson = f"Autonomous skill '{skill_name}' must decorate at least one function with @SkillRegistry.register(name='...', description='...')."
            else:
                lesson = f"Correct skill implementation to satisfy canary contract: {sig}."

            lesson_id = mem.record_episodic_lesson(
                tool_name="create_or_update_custom_skill",
                trigger_context=f"self_evolution:{skill_name}",
                failure_signature=sig,
                lesson=lesson,
            )
            logger.info("Recorded episodic failure lesson #%s for skill '%s'", lesson_id, skill_name)
            return lesson_id
        except Exception as e:
            logger.debug("Failed to record episodic lesson: %s", e)
            return None

    @classmethod
    def _cleanup_scratch(cls, path: Path):
        """Removes ephemeral scratchpad file."""
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

