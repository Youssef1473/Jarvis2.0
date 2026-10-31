"""Self-Evolution Sandbox & Subprocess Validation Gate for Jarvis 2.0.

Provides out-of-process isolation, AST static inspection, canary verification,
and kernel-level Win32 Job Object resource containment for dynamically synthesized skills.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.skills.base import SkillRegistry
from jarvis.skills.job_object_sandbox import (
    BELOW_NORMAL_PRIORITY_CLASS,
    JOB_OBJECT_UILIMIT_ALL,
    SandboxedProcess,
)
from jarvis.skills.job_object_sandbox import (
    apply_job_ui_restrictions as _canonical_ui,
)

logger = logging.getLogger("Jarvis.Evolution.Sandbox")

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent.parent
CUSTOM_SKILLS_DIR = WORKSPACE_DIR / "jarvis" / "skills" / "custom"
SCRATCH_DIR = WORKSPACE_DIR / "data" / "evolution_scratch"
SNAPSHOTS_DIR = WORKSPACE_DIR / "data" / "evolution_snapshots"
ISOLATED_RUNNER_PATH = Path(__file__).resolve().parent / "isolated_runner.py"

CUSTOM_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

_SANDBOX_CONCURRENCY_SEMAPHORE = threading.Semaphore(4)


@dataclass
class SandboxResult:
    """Outcome of sandbox isolation and test verification."""
    success: bool
    skill_name: str
    message: str
    target_path: Path | None = None
    stdout: str = ""
    stderr: str = ""


def apply_job_ui_restrictions(job_handle: int) -> bool:
    """Applies Win32 UI Restrictions (JobObjectBasicUIRestrictions = 4) using JOB_OBJECT_UILIMIT_ALL (0xFF)."""
    return _canonical_ui(job_handle, ui_flags=JOB_OBJECT_UILIMIT_ALL)


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
    """Atomically restores target_path from snapshot_path via staging temp file and os.replace."""
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


def _get_scrubbed_env(scratch_dir: Path | None = None) -> dict[str, str]:
    """Returns a minimal scrubbed environment containing only required OS primitives.

    Explicitly excludes PYTHONPATH, PYTHONUTF8, and all user API keys/credentials.
    Points TEMP and TMP to the ephemeral scratch directory to prevent user temp access.
    """
    allowed_keys = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "COMSPEC",
        "PATHEXT",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "LOCALAPPDATA",
    }
    scrubbed = {}
    for k in allowed_keys:
        if k in os.environ:
            scrubbed[k] = os.environ[k]
    scrubbed.setdefault("SYSTEMROOT", r"C:\Windows")
    scrubbed.setdefault("WINDIR", r"C:\Windows")

    if scratch_dir is not None:
        scrubbed["TEMP"] = str(scratch_dir)
        scrubbed["TMP"] = str(scratch_dir)
    elif "TEMP" in os.environ:
        scrubbed["TEMP"] = os.environ["TEMP"]
        scrubbed["TMP"] = os.environ.get("TMP", os.environ["TEMP"])

    return scrubbed


def get_or_create_evolution_hmac_key() -> bytes:
    """Retrieves or generates a machine-local HMAC secret protected via Windows DPAPI."""
    key_dir = Path.home() / ".jarvis_sec"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_file = key_dir / "evolution_hmac.dat"

    if key_file.exists():
        try:
            raw_blob = key_file.read_bytes()
            if sys.platform == "win32":
                import win32crypt
                _, key = win32crypt.CryptUnprotectData(raw_blob, None, None, None, 0)
                return bytes(key)
            return raw_blob
        except Exception as e:
            logger.warning("Failed to decrypt evolution HMAC key: %s; regenerating", e)

    new_key = secrets.token_bytes(32)
    if sys.platform == "win32":
        try:
            import win32crypt
            blob = win32crypt.CryptProtectData(new_key, "Jarvis Evolution HMAC Key", None, None, None, 0)
            key_file.write_bytes(blob)
        except Exception as e:
            if os.getenv("JARVIS_ALLOW_INSECURE_KEY_STORAGE") == "1":
                logger.warning("DPAPI encryption failed; storing unencrypted evolution key (JARVIS_ALLOW_INSECURE_KEY_STORAGE=1): %s", e)
                key_file.write_bytes(new_key)
            else:
                raise RuntimeError(
                    f"[Security Failure] Windows DPAPI encryption failed: {e}. "
                    "Refusing to persist unencrypted evolution HMAC signing key to disk."
                ) from e
    else:
        key_file.write_bytes(new_key)
    return new_key


def compute_skill_signature_payload(
    skill_name: str,
    code_hash: str,
    tools_meta: dict[str, Any],
    risk_level: int = 2,
    external_ingestion: bool = True,
) -> str:
    """Formats a deterministic canonical JSON payload binding all security-critical metadata for HMAC signing."""
    canonical_payload = {
        "code_hash": code_hash.lower(),
        "external_ingestion": bool(external_ingestion),
        "risk_level": int(risk_level),
        "skill_name": skill_name,
        "tools": tools_meta,
    }
    return json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))


def sign_custom_skill(
    skill_name: str,
    code_hash: str,
    tools_meta: dict[str, Any],
    risk_level: int = 2,
    external_ingestion: bool = True,
) -> str:
    """Computes an HMAC-SHA256 signature binding the skill name, code hash, tool metadata,

    risk level, and external ingestion flag using the DPAPI-protected key.
    """
    key = get_or_create_evolution_hmac_key()
    payload = compute_skill_signature_payload(skill_name, code_hash, tools_meta, risk_level, external_ingestion)
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_custom_skill_signature(
    skill_name: str,
    code_hash: str,
    tools_meta: dict[str, Any],
    signature: str,
    risk_level: int = 2,
    external_ingestion: bool = True,
) -> bool:
    """Verifies HMAC-SHA256 signature against the canonical skill metadata."""
    if not signature:
        return False
    try:
        expected = sign_custom_skill(skill_name, code_hash, tools_meta, risk_level, external_ingestion)
        return hmac.compare_digest(expected.lower(), signature.lower())
    except Exception as e:
        logger.warning("Custom skill HMAC verification failed: %s", e)
        return False


def _create_sandboxed_tool_proxy(clean_name: str, tool_name: str, tool_meta: dict[str, Any], use_appcontainer: bool | None = None) -> Callable:
    """Creates a callable proxy function executing the tool out-of-process under Job Object sandboxing."""
    def sandboxed_tool_proxy(**kwargs):
        return SkillSandbox.execute_sandboxed_custom_skill(clean_name, tool_name, kwargs, use_appcontainer=use_appcontainer)

    sandboxed_tool_proxy.__name__ = tool_name
    description = tool_meta.get("description", "")
    sandboxed_tool_proxy.__doc__ = description

    type_map = {
        "int": int,
        "float": float,
        "str": str,
        "bool": bool,
        "list": list,
        "dict": dict,
    }
    parameters = []
    for p in tool_meta.get("parameters", []):
        p_name = p.get("name")
        if not p_name or not p_name.isidentifier():
            continue
        p_type = type_map.get(p.get("type", "str"), str)
        default = p.get("default", inspect.Parameter.empty) if p.get("has_default") else inspect.Parameter.empty
        parameters.append(
            inspect.Parameter(
                name=p_name,
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=p_type,
            )
        )
    sandboxed_tool_proxy.__signature__ = inspect.Signature(parameters)
    return sandboxed_tool_proxy


def _has_loop_scoped_break(loop_node: ast.While) -> bool:
    """Checks if the loop contains an ast.Break that directly breaks this loop,

    ignoring breaks nested inside inner loops (for, while, async for).
    """
    queue = list(loop_node.body) + list(loop_node.orelse)
    while queue:
        item = queue.pop()
        if isinstance(item, ast.Break):
            return True
        if isinstance(item, (ast.While, ast.For, ast.AsyncFor)):
            # Breaks inside nested inner loops target the inner loop, not this while loop
            continue
        for child in ast.iter_child_nodes(item):
            queue.append(child)
    return False


class SkillSandbox:
    """Isolates, compiles, and tests LLM-generated code in an ephemeral subprocess."""

    apply_job_ui_restrictions = staticmethod(apply_job_ui_restrictions)
    install_canary_c_audit_hook = staticmethod(install_canary_c_audit_hook)
    atomic_rollback_file = staticmethod(atomic_rollback_file)

    BLOCKED_CALLS: set[str] = {
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

    BLOCKED_DESTRUCTIVE_METHODS: set[str] = {
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

    BLOCKED_REFLECTION_CALLS: set[str] = {
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
    }

    BLOCKED_ATTRIBUTES: set[str] = {
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

    BLOCKED_MODULES: set[str] = {
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
    def check_ast_security(cls, code: str) -> tuple[bool, str]:
        """Performs static syntax and deep AST security checks on untrusted code."""
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"Python SyntaxError on line {e.lineno}: {e.msg}"

        for node in ast.walk(tree):
            # 1. Imports inspection: block dangerous modules & unapproved jarvis.* imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    if root_mod in cls.BLOCKED_MODULES:
                        return False, f"Security Violation: Importing module '{alias.name}' is blocked inside autonomous skills."
                    if root_mod == "jarvis":
                        return False, f"Security Violation: Importing '{alias.name}' is prohibited inside autonomous skills."
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_mod = node.module.split(".")[0]
                    if root_mod in cls.BLOCKED_MODULES:
                        return False, f"Security Violation: Importing from module '{node.module}' is blocked inside autonomous skills."
                    if root_mod == "jarvis":
                        # Allow exactly 'from jarvis.skills.base import SkillRegistry'
                        if node.module == "jarvis.skills.base" and all(a.name == "SkillRegistry" for a in node.names):
                            pass
                        else:
                            return False, f"Security Violation: Importing from '{node.module}' is prohibited inside autonomous skills."

            # 2. Attribute access inspection
            elif isinstance(node, ast.Attribute):
                if node.attr in cls.BLOCKED_ATTRIBUTES:
                    return False, f"Security Violation: Access to dunder attribute '{node.attr}' is prohibited (sandbox reflection protection)."
                val_id = node.value.id if isinstance(node.value, ast.Name) else ""
                full_attr = f"{val_id}.{node.attr}" if val_id else node.attr
                if full_attr in cls.BLOCKED_CALLS:
                    return False, f"Security Violation: '{full_attr}' is blocked inside autonomous skills."

            # 3. Call invocation inspection
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

            # 4. Operational Invariant: Infinite While Loop Detection
            elif isinstance(node, ast.While):
                is_const_true = (
                    (isinstance(node.test, ast.Constant) and bool(node.test.value) is True) or
                    (isinstance(node.test, ast.Name) and node.test.id == "True")
                )
                if is_const_true and not _has_loop_scoped_break(node):
                    return False, "Operational Guardrail Violation: Unbounded 'while True' loop detected without explicit break condition."

            # 5. Operational Invariant: Massive Literal Memory Allocation
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
                for operand in (node.left, node.right):
                    if isinstance(operand, ast.Constant) and isinstance(operand.value, (int, float)):
                        if operand.value > 50_000_000:
                            return False, f"Operational Guardrail Violation: Massive allocation multiplier ({int(operand.value):,}) detected."

            # 6. Reflection & Dangerous Function Aliasing Inspection
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                assigned_val = getattr(node, "value", None)
                if isinstance(assigned_val, ast.Name):
                    if (
                        assigned_val.id in cls.BLOCKED_REFLECTION_CALLS
                        or assigned_val.id in cls.BLOCKED_CALLS
                        or assigned_val.id in cls.BLOCKED_MODULES
                    ):
                        return False, f"Security Violation: Aliasing blocked function/module '{assigned_val.id}' is prohibited inside autonomous skills."
                elif isinstance(assigned_val, ast.Attribute):
                    val_id = assigned_val.value.id if isinstance(assigned_val.value, ast.Name) else ""
                    full_attr = f"{val_id}.{assigned_val.attr}" if val_id else assigned_val.attr
                    if full_attr in cls.BLOCKED_CALLS or assigned_val.attr in cls.BLOCKED_ATTRIBUTES:
                        return False, f"Security Violation: Aliasing blocked attribute/function '{full_attr}' is prohibited inside autonomous skills."

        return True, "AST security checks passed."

    @classmethod
    def create_snapshot(cls, clean_name: str) -> Path | None:
        """Creates a versioned backup snapshot of both .py and .meta.json before updating."""
        target_file = CUSTOM_SKILLS_DIR / f"{clean_name}.py"
        if not target_file.exists():
            return None
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        snapshot_file = SNAPSHOTS_DIR / f"{clean_name}_{timestamp}.py"
        try:
            shutil.copy2(target_file, snapshot_file)
            meta_file = CUSTOM_SKILLS_DIR / f"{clean_name}.meta.json"
            if meta_file.exists():
                shutil.copy2(meta_file, SNAPSHOTS_DIR / f"{clean_name}_{timestamp}.meta.json")
            logger.info("Created snapshot for '%s' at '%s'", clean_name, snapshot_file.name)
            return snapshot_file
        except Exception as e:
            logger.warning("Failed to create snapshot for '%s': %s", clean_name, e)
            return None

    @classmethod
    def rollback_snapshot(cls, clean_name: str, snapshot_path: Path | None) -> bool:
        """Restores a custom skill and sidecar from snapshot, re-registering proxies without host module import."""
        target_file = CUSTOM_SKILLS_DIR / f"{clean_name}.py"
        target_meta = CUSTOM_SKILLS_DIR / f"{clean_name}.meta.json"

        if snapshot_path and snapshot_path.exists():
            try:
                rolled_back = atomic_rollback_file(snapshot_path, target_file)
                if not rolled_back:
                    shutil.copy2(snapshot_path, target_file)

                snap_meta = snapshot_path.with_suffix(".meta.json")
                if snap_meta.exists():
                    shutil.copy2(snap_meta, target_meta)
                    try:
                        with open(target_meta, encoding="utf-8") as f:
                            meta_data = json.load(f)
                        for t_name, t_meta in meta_data.get("tools", {}).items():
                            proxy_fn = _create_sandboxed_tool_proxy(clean_name, t_name, t_meta)
                            SkillRegistry.register(
                                name=t_name,
                                description=t_meta.get("description", ""),
                                risk_level=2,
                                read_only=False,
                                external_ingestion=True,
                                default_timeout_ms=15000,
                                origin="custom",
                            )(proxy_fn)
                    except Exception as meta_err:
                        logger.warning("Failed to restore proxies on snapshot rollback: %s", meta_err)

                logger.info("Rolled back '%s' to snapshot '%s'", clean_name, snapshot_path.name)
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
            if target_meta.exists():
                try:
                    target_meta.unlink()
                except Exception:
                    pass
            return True

    @classmethod
    def list_snapshots(cls, clean_name: str | None = None) -> list[Path]:
        """Lists existing versioned snapshots, optionally filtered by skill name."""
        if not SNAPSHOTS_DIR.exists():
            return []
        pattern = f"{clean_name}_*.py" if clean_name else "*.py"
        return sorted(SNAPSHOTS_DIR.glob(pattern), reverse=True)

    @classmethod
    def generate_canary_script(cls, clean_name: str, scratch_path: Path) -> str:
        """Generates an automated semantic canary verification script for the subprocess."""
        escaped_path = str(scratch_path).replace("\\", "\\\\")
        escaped_scratch_dir = str(scratch_path.parent).replace("\\", "\\\\")
        escaped_ws = str(WORKSPACE_DIR).replace("\\", "\\\\")
        return f"""import builtins
import io
import inspect
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import tracemalloc

if r'{escaped_ws}' not in sys.path:
    sys.path.insert(0, r'{escaped_ws}')

try:
    socket.setdefaulttimeout(2.5)
except Exception:
    pass

_real_open = builtins.open
def _sandboxed_open(file, mode='r', *args, **kwargs):
    if any(m in mode for m in ('w', 'a', '+', 'x')):
        raise PermissionError(f"Canary sandbox violation: File write operations are prohibited (attempted mode '{{mode}}').")
    return _real_open(file, mode, *args, **kwargs)

builtins.open = _sandboxed_open
io.open = _sandboxed_open

class BlockedSandboxModule:
    def __init__(self, mod_name: str):
        self.__name__ = mod_name
    def __getattr__(self, attr: str):
        raise PermissionError(f"Canary sandbox violation: Access to module '{{self.__name__}}' is disabled inside the canary sandbox.")
    def __call__(self, *args, **kwargs):
        raise PermissionError(f"Canary sandbox violation: Execution of '{{self.__name__}}' is disabled inside the canary sandbox.")

for _blocked in ["subprocess", "shutil", "winreg", "ctypes"]:
    sys.modules[_blocked] = BlockedSandboxModule(_blocked)

def _blocked_socket(*args, **kwargs):
    raise PermissionError("Canary sandbox violation: Raw socket creation is disabled inside the canary sandbox.")
socket.socket = _blocked_socket

tracemalloc.start()

try:
    spec = importlib.util.spec_from_file_location('{clean_name}', r'{escaped_path}')
    if not spec or not spec.loader:
        print('CANARY_ERROR: Failed to create module spec')
        sys.exit(2)

    mod = importlib.util.module_from_spec(spec)

    from jarvis.skills.base import SkillRegistry

    stdlib_dir = os.path.realpath(os.path.normcase(os.path.dirname(os.__file__)))
    base_prefix = getattr(sys, "base_prefix", sys.prefix)
    dlls_dir = os.path.realpath(os.path.normcase(os.path.join(base_prefix, "DLLs")))
    scratch_dir = os.path.realpath(os.path.normcase(r'{escaped_scratch_dir}'))
    skill_file = os.path.realpath(os.path.normcase(r'{escaped_path}'))
    canary_allowlist = [stdlib_dir, dlls_dir, scratch_dir, skill_file]

    def _normalize_path(path_candidate):
        if isinstance(path_candidate, (int, float)):
            return None
        if isinstance(path_candidate, bytes):
            try:
                path_candidate = path_candidate.decode("utf-8", errors="replace")
            except Exception:
                return None
        if not isinstance(path_candidate, (str, getattr(os, "PathLike", str))):
            return None
        try:
            norm_target = os.path.realpath(os.path.normcase(os.path.abspath(str(path_candidate))))
            return norm_target.removeprefix(r"\\?\" if os.name == "nt" else "")
        except Exception:
            return None

    def _is_path_allowed(path_candidate):
        if isinstance(path_candidate, int):
            return False
        norm = _normalize_path(path_candidate)
        if not norm:
            return False
        if "site-packages" in norm or "dist-packages" in norm:
            return False
        for root in canary_allowlist:
            if norm == root or norm.startswith(root + os.sep):
                return True
        return False

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
            target_path = args[0] if len(args) > 0 else None
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0

            if isinstance(target_path, int):
                raise PermissionError("[Gate-2 AuditHook] Direct integer file descriptor access is prohibited inside the canary sandbox.")
            if mode and isinstance(mode, str) and any(m in mode for m in ("w", "a", "+", "x")):
                raise PermissionError("[Gate-2 AuditHook] Blocked filesystem write open: mode='" + str(mode) + "' on path '" + str(target_path) + "'")
            if isinstance(flags, int) and (flags & 0x030B):
                raise PermissionError("[Gate-2 AuditHook] Blocked filesystem write open: flags=" + hex(flags) + " on path '" + str(target_path) + "'")
            if target_path is not None and not _is_path_allowed(target_path):
                raise PermissionError("[Gate-2 AuditHook] Read access denied: '" + str(target_path) + "' is outside allowlist.")
        if event in ("os.listdir", "os.scandir"):
            target_path = args[0] if len(args) > 0 else "."
            if not _is_path_allowed(target_path):
                raise PermissionError("[Gate-2 AuditHook] Directory listing denied: '" + str(target_path) + "' is outside allowlist.")

    sys.addaudithook(_canary_audit_guard)

    spec.loader.exec_module(mod)

    tools = SkillRegistry.get_all_tools()

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

    type_str_map = {{
        int: "int",
        float: "float",
        str: "str",
        bool: "bool",
        list: "list",
        dict: "dict",
    }}
    canary_meta = {{}}

    for tool_name in module_tools:
        tool_fn = tools[tool_name]
        if not callable(tool_fn):
            print(f'CANARY_ERROR: Registered tool {{tool_name}} is not callable', flush=True)
            sys.stdout.flush()
            os._exit(4)
        sig = inspect.signature(tool_fn)
        raw_doc = (getattr(tool_fn, "__doc__", "") or "").strip()
        clean_doc = "".join(c for c in raw_doc if c.isprintable())[:500]

        param_list = []
        dummy_kwargs = {{}}
        for p_name, param in sig.parameters.items():
            if not p_name.isidentifier():
                continue
            ann = param.annotation
            ann_str = type_str_map.get(ann, "str")
            has_default = param.default != inspect.Parameter.empty
            def_val = param.default if has_default else None
            if has_default:
                try:
                    json.dumps(def_val)
                except Exception:
                    def_val = str(def_val)
            param_list.append({{
                "name": p_name,
                "type": ann_str,
                "has_default": has_default,
                "default": def_val,
            }})

            if not has_default:
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

        canary_meta[tool_name] = {{
            "description": clean_doc,
            "parameters": param_list,
        }}

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
            exc_str = str(exc)
            if len(exc_str) > 120:
                exc_str = exc_str[:120] + "... [truncated]"
            err_msg = f"CANARY_ERROR: Tool '{{tool_name}}' failed smoke execution with {{type(exc).__name__}}: {{exc_str}}"
            sys.stdout.write(err_msg + "\\n")
            sys.stderr.write(err_msg + "\\n")
            sys.stdout.flush()
            sys.stderr.flush()
            time.sleep(0.05)
            os._exit(7)

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

    sys.stdout.write('CANARY_METADATA_START\\n' + json.dumps(canary_meta) + '\\nCANARY_METADATA_END\\n')
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
        memory_manager: Any | None = None,
        user_approved: bool = False,
    ) -> SandboxResult:
        """Executes full sandboxed lifecycle: AST gate, canary execution, metadata extraction,
        HMAC-signed sidecar creation, and proxy registration without host module import.
        """
        clean_name = cls.sanitize_skill_name(skill_name)
        code = python_code.strip()

        if not code:
            return SandboxResult(success=False, skill_name=clean_name, message="Code content cannot be empty.")

        snapshot_path = cls.create_snapshot(clean_name)

        is_safe, sec_msg = cls.check_ast_security(code)
        if not is_safe:
            logger.warning("Sandbox blocked skill '%s': %s", clean_name, sec_msg)
            cls._record_failure_lesson(clean_name, code, sec_msg, memory_manager)
            return SandboxResult(success=False, skill_name=clean_name, message=sec_msg)

        # Gate 1.5: Explicit out-of-band user approval is mandatory BEFORE spawning canary execution
        if not user_approved:
            cls.rollback_snapshot(clean_name, snapshot_path)
            logger.warning("Skill '%s' denied canary execution: explicit out-of-band user approval is required.", clean_name)
            return SandboxResult(
                success=False,
                skill_name=clean_name,
                message="Skill execution and promotion denied: explicit out-of-band user approval is required.",
            )

        if "SkillRegistry" not in code:
            code = "from jarvis.skills.base import SkillRegistry\n\n" + code

        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        scratch_file = SCRATCH_DIR / f"temp_{clean_name}_{int(time.time() * 1000)}.py"
        target_file = CUSTOM_SKILLS_DIR / f"{clean_name}.py"

        try:
            with open(scratch_file, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as e:
            return SandboxResult(success=False, skill_name=clean_name, message=f"Failed to write scratchpad: {e}")

        runner_script = cls.generate_canary_script(clean_name, scratch_file)

        is_win = sys.platform == "win32"
        proc_mgr = SandboxedProcess(
            [sys.executable, "-I", "-S", "-c", runner_script],
            cwd=str(WORKSPACE_DIR),
            env=_get_scrubbed_env(scratch_dir=scratch_file.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            max_memory_mb=256,
            max_processes=4,
            requested_affinity=0xFF00,
            priority_class=BELOW_NORMAL_PRIORITY_CLASS if is_win else None,
            apply_ui_restrictions=is_win,
        )

        try:
            with proc_mgr as proc:
                try:
                    stdout, stderr = proc.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    proc_mgr.terminate_tree()
                    cls._cleanup_scratch(scratch_file)
                    cls.rollback_snapshot(clean_name, snapshot_path)
                    msg = f"Subprocess timed out after {timeout_seconds}s (potential infinite loop)."
                    cls._record_failure_lesson(clean_name, code, msg, memory_manager)
                    return SandboxResult(success=False, skill_name=clean_name, message=msg)

                stdout = (stdout or "").strip()
                stderr = (stderr or "").strip()

                has_canary_success = "CANARY_VERIFICATION_SUCCESS" in stdout or "SANDBOX_LOAD_SUCCESS" in stdout
                is_clean_exit = proc.returncode == 0 or (has_canary_success and proc.returncode in (0, 15))

                if not is_clean_exit or not has_canary_success:
                    cls._cleanup_scratch(scratch_file)
                    cls.rollback_snapshot(clean_name, snapshot_path)
                    error_summary = stderr or stdout or f"Exited with code {proc.returncode}"
                    msg = f"Subprocess verification failed:\n{error_summary}"
                    cls._record_failure_lesson(clean_name, code, error_summary, memory_manager)
                    return SandboxResult(
                        success=False,
                        skill_name=clean_name,
                        message=msg,
                        stdout=stdout,
                        stderr=stderr,
                    )

                # Parse tool metadata emitted by canary
                meta_dict = {}
                if "CANARY_METADATA_START" in stdout and "CANARY_METADATA_END" in stdout:
                    try:
                        raw_meta = stdout.split("CANARY_METADATA_START")[1].split("CANARY_METADATA_END")[0].strip()
                        meta_dict = json.loads(raw_meta)
                    except Exception as e:
                        logger.warning("Failed to parse canary metadata: %s", e)

                # Enforce symmetrical name collision guard on every tool in meta_dict
                for t_name in meta_dict.keys():
                    existing_meta = SkillRegistry.get_metadata(t_name)
                    if existing_meta and getattr(existing_meta, "origin", "builtin") == "builtin":
                        cls._cleanup_scratch(scratch_file)
                        cls.rollback_snapshot(clean_name, snapshot_path)
                        msg = f"Security Violation: Custom tool '{t_name}' collides with built-in tool."
                        cls._record_failure_lesson(clean_name, code, msg, memory_manager)
                        return SandboxResult(success=False, skill_name=clean_name, message=msg)

        except Exception as e:
            cls._cleanup_scratch(scratch_file)
            cls.rollback_snapshot(clean_name, snapshot_path)
            err_msg = str(e)
            if "Job Object" in err_msg or "assignment failed" in err_msg.lower():
                msg = "Security failure: Win32 Job Object assignment failed. Execution aborted to fail closed."
            elif "Failed to resume suspended process" in err_msg:
                msg = "Security failure: Failed to resume suspended canary process. Execution aborted to fail closed."
            else:
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

        # 4. Promotion: Write custom skill file and DPAPI HMAC signed sidecar
        try:
            shutil.copy2(scratch_file, target_file)
            cls._cleanup_scratch(scratch_file)

            code_bytes = target_file.read_bytes()
            code_hash = hashlib.sha256(code_bytes).hexdigest()
            code_hmac = sign_custom_skill(
                clean_name,
                code_hash,
                meta_dict,
                risk_level=2,
                external_ingestion=True,
            )

            sidecar_path = CUSTOM_SKILLS_DIR / f"{clean_name}.meta.json"
            sidecar_payload = {
                "skill_name": clean_name,
                "sha256": code_hash,
                "hmac": code_hmac,
                "risk_level": 2,
                "external_ingestion": True,
                "tools": meta_dict,
            }
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump(sidecar_payload, f, indent=2)

        except Exception as e:
            cls._cleanup_scratch(scratch_file)
            cls.rollback_snapshot(clean_name, snapshot_path)
            msg = f"Promotion failed: {e}"
            cls._record_failure_lesson(clean_name, code, msg, memory_manager)
            return SandboxResult(success=False, skill_name=clean_name, message=msg)

        # 5. Out-of-process proxy registration (NO in-process host module import)
        try:
            for t_name, t_meta in meta_dict.items():
                proxy_fn = _create_sandboxed_tool_proxy(clean_name, t_name, t_meta)
                SkillRegistry.register(
                    name=t_name,
                    description=t_meta.get("description", ""),
                    risk_level=2,
                    read_only=False,
                    external_ingestion=True,
                    default_timeout_ms=15000,
                    origin="custom",
                )(proxy_fn)

            if hasattr(SkillRegistry, "_openai_tools_cache"):
                SkillRegistry._openai_tools_cache.clear()

            logger.info("Skill '%s' promoted and registered via sandboxed proxy.", clean_name)
            return SandboxResult(
                success=True,
                skill_name=clean_name,
                message=f"Successfully engineered and hot-reloaded capability '{clean_name}', Sir! Validated via isolated subprocess sandbox and active in your current session.",
                target_path=target_file,
            )
        except Exception as host_err:
            cls.rollback_snapshot(clean_name, snapshot_path)
            msg = f"Host proxy registration error: {host_err}"
            cls._record_failure_lesson(clean_name, code, msg, memory_manager)
            return SandboxResult(success=False, skill_name=clean_name, message=msg)

    @classmethod
    def execute_sandboxed_custom_skill(
        cls,
        clean_name: str,
        tool_name: str,
        kwargs: dict[str, Any],
        timeout_seconds: float = 10.0,
        use_appcontainer: bool | None = None,
    ) -> Any:
        """Executes a custom skill out-of-process inside an E-core-pinned Win32 Job Object and optional AppContainer."""
        if use_appcontainer is None:
            use_appcontainer = (sys.platform == "win32" and os.environ.get("CI", "").lower() != "true")
        acquired = _SANDBOX_CONCURRENCY_SEMAPHORE.acquire(timeout=2.0)
        if not acquired:
            return "Error: Sandbox is busy executing concurrent tasks; try again shortly."

        try:
            target_file = CUSTOM_SKILLS_DIR / f"{clean_name}.py"
            meta_file = CUSTOM_SKILLS_DIR / f"{clean_name}.meta.json"

            if not target_file.exists() or not meta_file.exists():
                return f"Error: Custom skill '{clean_name}' does not exist on disk."

            try:
                with open(meta_file, encoding="utf-8") as f:
                    meta_data = json.load(f)
            except Exception as e:
                return f"Error reading skill sidecar: {e}"

            code_bytes = target_file.read_bytes()
            actual_hash = hashlib.sha256(code_bytes).hexdigest()
            expected_hash = meta_data.get("sha256", "")

            if actual_hash.lower() != expected_hash.lower():
                return f"Security Violation: Integrity mismatch for skill '{clean_name}'. File on disk was modified."

            signature = meta_data.get("hmac", "")
            tools_meta = meta_data.get("tools", {})
            risk_level = meta_data.get("risk_level", 2)
            external_ingestion = meta_data.get("external_ingestion", True)
            if not verify_custom_skill_signature(
                clean_name,
                actual_hash,
                tools_meta,
                signature,
                risk_level=risk_level,
                external_ingestion=external_ingestion,
            ):
                return f"Security Violation: DPAPI HMAC signature verification failed for skill '{clean_name}'."

            nonce = secrets.token_hex(32)
            dir_id = secrets.token_hex(16)
            run_scratch = SCRATCH_DIR / f"run_{dir_id}"
            run_scratch.mkdir(parents=True, exist_ok=True)
            result_file = run_scratch / "result.json"

            runner_exec_path = ISOLATED_RUNNER_PATH
            target_exec_path = target_file
            if use_appcontainer:
                runner_exec_path = run_scratch / "isolated_runner.py"
                shutil.copy2(ISOLATED_RUNNER_PATH, runner_exec_path)
                target_exec_path = run_scratch / f"{clean_name}.py"
                shutil.copy2(target_file, target_exec_path)

            try:
                cmd = [
                    sys.executable,
                    "-I",
                    "-S",
                    str(runner_exec_path),
                    "--module-path", str(target_exec_path),
                    "--tool-name", tool_name,
                    "--result-file", str(result_file),
                    "--expected-hash", actual_hash,
                    "--scratch-dir", str(run_scratch),
                ]

                payload = json.dumps({"nonce": nonce, "kwargs": kwargs})
                is_win = sys.platform == "win32"

                appcontainer_config = None
                if use_appcontainer:
                    from jarvis.core.evolution.appcontainer import AppContainerConfig
                    appcontainer_config = AppContainerConfig(
                        profile_name=f"J_Skill_{clean_name[:12]}_{dir_id[:8]}",
                        grant_paths=[run_scratch],
                        auto_cleanup_profile=True,
                    )

                proc_mgr = SandboxedProcess(
                    cmd,
                    cwd=str(run_scratch),
                    env=_get_scrubbed_env(scratch_dir=run_scratch),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    max_memory_mb=256,
                    max_processes=4,
                    requested_affinity=0xFF00,
                    priority_class=BELOW_NORMAL_PRIORITY_CLASS if is_win else None,
                    apply_ui_restrictions=is_win,
                    use_appcontainer=use_appcontainer,
                    appcontainer_config=appcontainer_config,
                )

                try:
                    with proc_mgr as proc:
                        try:
                            proc.communicate(input=payload, timeout=timeout_seconds)
                        except subprocess.TimeoutExpired:
                            proc_mgr.terminate_tree()
                            return f"Error: Tool '{tool_name}' timed out after {timeout_seconds}s in isolated sandbox."

                        if not result_file.exists():
                            return f"Error: Tool '{tool_name}' terminated unexpectedly (code {proc.returncode}) without result."

                    # Symlink / reparse point defense: reject aliased or escaped result paths
                    if result_file.is_symlink():
                        return "Security Violation: Result file is a symbolic link or reparse point; rejected."
                    try:
                        resolved_result = result_file.resolve()
                        if resolved_result.parent != run_scratch.resolve():
                            return "Security Violation: Result file resolved outside run scratch directory; rejected."
                    except (OSError, RuntimeError) as res_err:
                        return f"Security Violation: Failed to verify result file containment: {res_err}"

                    if result_file.stat().st_size > 1048576:
                        return f"Security Violation: Output from tool '{tool_name}' exceeded 1MB ceiling; rejected."

                    try:
                        with open(result_file, encoding="utf-8") as f:
                            res_data = json.load(f)
                    except (json.JSONDecodeError, UnicodeDecodeError) as decode_err:
                        return f"Security Violation: Child response payload is not valid JSON: {decode_err}"

                    # Strict schema validation
                    if not isinstance(res_data, dict):
                        return "Security Violation: Child response payload is not a valid JSON dictionary."

                    child_nonce = res_data.get("nonce")
                    if not isinstance(child_nonce, str) or not isinstance(nonce, str) or not hmac.compare_digest(child_nonce, nonce):
                        return "Security Violation: Nonce verification failed for child response."

                    if "status" not in res_data:
                        return "Security Violation: Child response missing required 'status' field."

                    if res_data.get("status") == "success":
                        return res_data.get("result")
                    else:
                        return f"Error executing tool '{tool_name}': {res_data.get('error', 'Unknown error')}"

                except Exception as run_exc:
                    return f"Error: Sandboxed execution failed: {run_exc}"

            finally:
                shutil.rmtree(run_scratch, ignore_errors=True)

        finally:
            _SANDBOX_CONCURRENCY_SEMAPHORE.release()

    @classmethod
    def _record_failure_lesson(
        cls,
        skill_name: str,
        code: str,
        failure_msg: str,
        memory_manager: Any | None = None,
    ) -> int | None:
        """Synthesizes a Reflexion lesson and records it into SQLite WAL episodic memory."""
        try:
            mem = memory_manager
            if mem is None:
                from jarvis.skills.self_evolution import _memory_instance
                mem = _memory_instance
            if mem is None:
                try:
                    from jarvis.core.config_loader import load_config
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
