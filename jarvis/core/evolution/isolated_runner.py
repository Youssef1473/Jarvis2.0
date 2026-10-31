"""Standalone Isolated Child Runner for Custom Dynamic Skills in Jarvis 2.0.

Executed out-of-process via `python -I` under Win32 Job Object containment.
Zero dependencies on external packages or jarvis internals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, ClassVar


# 1. Stub jarvis.skills.base injection
class StubSkillRegistry:
    """Mock registry capturing skill registrations in the isolated child."""
    _tools: ClassVar[dict[str, Any]] = {}
    _descriptions: ClassVar[dict[str, str]] = {}

    @classmethod
    def register(cls, name: Any = None, description: str | None = None, *args: Any, **kwargs: Any):
        def decorator(fn: Any):
            tool_name = name if isinstance(name, str) else fn.__name__
            doc = description or (fn.__doc__ or "").strip()
            cls._tools[tool_name] = fn
            cls._descriptions[tool_name] = doc
            return fn

        if callable(name):
            fn = name
            return decorator(fn)
        return decorator


def _inject_jarvis_stubs() -> None:
    """Injects synthetic stub modules for jarvis.skills.base so skills can import cleanly."""
    stub_base = types.ModuleType("jarvis.skills.base")
    stub_base.SkillRegistry = StubSkillRegistry

    stub_skills = types.ModuleType("jarvis.skills")
    stub_skills.base = stub_base

    stub_jarvis = types.ModuleType("jarvis")
    stub_jarvis.skills = stub_skills

    sys.modules["jarvis"] = stub_jarvis
    sys.modules["jarvis.skills"] = stub_skills
    sys.modules["jarvis.skills.base"] = stub_base


# 2. Default-Deny Import Allowlist
# Pure-compute standard library allowlist recommended for isolated custom skills.
# Everything else (network, process, OS, thread, registry, dynamic loaders) is blocked by default.
ALLOWED_STDLIB_ROOTS: frozenset[str] = frozenset({
    "math",
    "json",
    "re",
    "string",
    "textwrap",
    "datetime",
    "itertools",
    "functools",
    "operator",
    "collections",
    "heapq",
    "bisect",
    "statistics",
    "decimal",
    "fractions",
    "random",
    "hashlib",
    "base64",
    "struct",
    "copy",
    "enum",
    "dataclasses",
    "typing",
    "unicodedata",
    "difflib",
})

# Specific submodules permitted (e.g. urllib.parse is pure compute, but urllib.request is network I/O)
ALLOWED_EXPLICIT_MODULES: frozenset[str] = frozenset({
    "urllib",
    "urllib.parse",
})

BLOCKED_SUBMODULE_NAMES: frozenset[str] = frozenset({
    "urllib.request",
    "urllib.error",
    "urllib.robotparser",
})


class DefaultDenyModuleFinder:
    """Default-deny import finder for pure-compute custom skills.

    Security Architecture Invariant:
    DefaultDenyModuleFinder functions as an import hygiene filter against unintended or accidental
    module imports. The non-bypassable containment boundary is the PEP 578 C-level audit hook
    (_install_audit_hook) and Win32 Job Object limits. Even if user code attempts object traversal
    via pre-loaded interpreter modules (e.g., collections._sys.modules['os'].startfile), the audit
    hook intercepts and denies the operation before execution.
    """

    def __init__(self, allowed_skill_module: str):
        self.allowed_skill_module = allowed_skill_module

    def find_spec(self, fullname: str, path: Any = None, target: Any = None):
        # 1. Allow the skill module itself being executed
        if fullname == self.allowed_skill_module:
            return

        # 2. Allow pre-injected synthetic jarvis stubs (and deny any real jarvis internals)
        if fullname.startswith("jarvis."):
            if fullname in sys.modules:
                return
            raise ImportError(
                f"[Security Violation] Importing '{fullname}' is prohibited inside isolated custom skills. "
                "Custom skills are pure compute and cannot access host Jarvis internals."
            )

        # 3. Deny dangerous submodules of permitted packages (e.g. urllib.request)
        if fullname in BLOCKED_SUBMODULE_NAMES:
            raise ImportError(
                f"[Security Violation] Importing '{fullname}' is prohibited inside isolated custom skills. "
                "Network I/O modules are blocked by sandbox policy."
            )

        # 4. Check explicit submodule allowlist
        if fullname in ALLOWED_EXPLICIT_MODULES:
            return

        # 5. Check root module against pure-compute allowlist
        root = fullname.split(".")[0]
        if root not in ALLOWED_STDLIB_ROOTS:
            raise ImportError(
                f"[Security Violation] Importing '{fullname}' is prohibited inside isolated custom skills. "
                f"Module '{root}' is not on the pure-compute allowlist."
            )

        return


# 3. Normalized Read-Allowlist & Write-Deny Audit Hook
def _install_audit_hook(allowlist_roots: list[str], scratch_dir: str) -> None:
    """Installs a PEP 578 audit hook enforcing normalized read allowlisting and strict scratch-only mutations."""
    norm_allowlist = [os.path.realpath(os.path.normcase(r)) for r in allowlist_roots if r]
    norm_scratch = os.path.realpath(os.path.normcase(scratch_dir))

    def _normalize_path(path_candidate: Any) -> str | None:
        if isinstance(path_candidate, (int, float)):
            return None
        if isinstance(path_candidate, bytes):
            try:
                path_candidate = path_candidate.decode("utf-8", errors="replace")
            except (UnicodeDecodeError, AttributeError):
                return None
        if not isinstance(path_candidate, str) and not isinstance(path_candidate, Path):
            return None
        try:
            norm_target = os.path.realpath(os.path.normcase(os.path.abspath(str(path_candidate))))
            return norm_target.removeprefix("\\\\?\\")
        except (OSError, ValueError):
            return None

    def _is_scratch_path(path_candidate: Any) -> bool:
        norm = _normalize_path(path_candidate)
        if not norm:
            return False
        return norm == norm_scratch or norm.startswith(norm_scratch + os.sep)

    def _is_path_allowed(path_candidate: Any) -> bool:
        if isinstance(path_candidate, (int, float)):
            return False
        norm = _normalize_path(path_candidate)
        if not norm:
            return False
        # Explicitly deny site-packages and dist-packages under all circumstances
        if "site-packages" in norm or "dist-packages" in norm:
            return False
        for root in norm_allowlist:
            if norm == root or norm.startswith(root + os.sep):
                return True
        return False

    def _audit_guard(event: str, args: tuple):
        # 1. Deny network, DNS exfiltration, sockets
        if event.startswith("socket."):
            raise PermissionError(f"[AuditHook] Network/DNS operation '{event}' blocked in isolated sandbox.")

        # 2. Deny process creation & execution
        if event in (
            "os.startfile", "os.system", "os.spawn", "os.exec", "os.fork",
            "os.posix_spawn", "subprocess.Popen",
        ) or event.startswith("os.spawn"):
            raise PermissionError(f"[AuditHook] Process execution '{event}' blocked in isolated sandbox.")

        # 3. Deny native memory, ctypes, and registry access
        if event.startswith("ctypes.") or event in ("ctypes.dlopen", "ctypes.dlsym", "ctypes.cdata"):
            raise PermissionError(f"[AuditHook] Native memory access '{event}' blocked in isolated sandbox.")
        if event.startswith("winreg.") or event in ("winreg.OpenKey", "winreg.CreateKey", "winreg.SetValue"):
            raise PermissionError(f"[AuditHook] Registry access '{event}' blocked in isolated sandbox.")

        # 4. Deny frame / callstack inspection
        if event in ("sys._getframe", "sys._current_frames"):
            raise PermissionError(f"[AuditHook] Introspection operation '{event}' blocked in isolated sandbox.")

        # 5. Filesystem open inspection: write deny outside scratch + read allowlist
        if event == "open":
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            target_path = args[0] if len(args) > 0 else None

            # Disallow all direct integer file descriptor manipulation
            if isinstance(target_path, int):
                raise PermissionError(f"[AuditHook] Direct file descriptor manipulation is prohibited in isolated sandbox: {target_path}")

            is_write_mode = (mode and isinstance(mode, str) and any(m in mode for m in ("w", "a", "+", "x")))
            is_write_flags = (isinstance(flags, int) and bool(flags & 0x030B))

            if (is_write_mode or is_write_flags) and (target_path is None or not _is_scratch_path(target_path)):
                raise PermissionError(f"[AuditHook] Filesystem write prohibited outside scratch: '{target_path}'")

            if target_path is not None and not _is_path_allowed(target_path):
                raise PermissionError(f"[AuditHook] Read access denied: '{target_path}' is outside allowlist.")

        # 6. Directory listing enumeration inspection
        if event in ("os.listdir", "os.scandir"):
            target_path = args[0] if len(args) > 0 else "."
            if not _is_path_allowed(target_path):
                raise PermissionError(f"[AuditHook] Directory listing denied: '{target_path}' is outside allowlist.")

        # 7. Filesystem mutation events: deny dangerous operations outside scratch directory
        if event in ("os.remove", "os.unlink", "os.rmdir"):
            target_path = args[0] if len(args) > 0 else None
            if target_path is not None and not _is_scratch_path(target_path):
                raise PermissionError(f"[AuditHook] Filesystem deletion denied outside scratch: '{target_path}'")

        if event in ("os.rename", "os.replace"):
            src_path = args[0] if len(args) > 0 else None
            dst_path = args[1] if len(args) > 1 else None
            if src_path is not None and not _is_scratch_path(src_path):
                raise PermissionError(f"[AuditHook] Filesystem rename source denied outside scratch: '{src_path}'")
            if dst_path is not None and not _is_scratch_path(dst_path):
                raise PermissionError(f"[AuditHook] Filesystem rename destination denied outside scratch: '{dst_path}'")

        if event in ("os.link", "os.symlink"):
            raise PermissionError(f"[AuditHook] Link creation prohibited in isolated sandbox: '{event}'")

        if event == "os.mkdir":
            target_path = args[0] if len(args) > 0 else None
            if target_path is not None and not _is_scratch_path(target_path):
                raise PermissionError(f"[AuditHook] Directory creation denied outside scratch: '{target_path}'")

        if event in ("os.chmod", "os.truncate"):
            target_path = args[0] if len(args) > 0 else None
            if target_path is not None and not _is_scratch_path(target_path):
                raise PermissionError(f"[AuditHook] Filesystem attribute modification denied outside scratch: '{target_path}'")

    sys.addaudithook(_audit_guard)


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated custom skill runner")
    parser.add_argument("--module-path", required=True, help="Absolute path to custom skill .py file")
    parser.add_argument("--tool-name", required=True, help="Registered tool function to execute")
    parser.add_argument("--result-file", required=True, help="Pre-allocated result output file path")
    parser.add_argument("--expected-hash", required=True, help="Expected SHA-256 hex digest of the skill code")
    parser.add_argument("--scratch-dir", required=True, help="Ephemeral scratch working directory")

    args = parser.parse_args()

    module_path = Path(args.module_path).resolve()
    scratch_dir = Path(args.scratch_dir).resolve()
    result_path = Path(args.result_file).resolve()

    def _write_result(payload_dict: dict[str, Any]) -> None:
        try:
            json_str = json.dumps(payload_dict)
        except (TypeError, ValueError):
            payload_dict["result"] = str(payload_dict.get("result", ""))
            json_str = json.dumps(payload_dict)
        if len(json_str.encode("utf-8")) > 1048576:
            payload_dict = {
                "nonce": payload_dict.get("nonce", ""),
                "status": "error",
                "type": "SecurityViolation",
                "error": "Custom skill output exceeded 1MB ceiling; rejected.",
            }
            json_str = json.dumps(payload_dict)
        with open(result_path, "w", encoding="utf-8") as rf:
            rf.write(json_str)
            rf.flush()

    # Step 1: Inject jarvis stub modules
    _inject_jarvis_stubs()
    sys.meta_path.insert(0, DefaultDenyModuleFinder(allowed_skill_module=module_path.stem))

    # Strip dangerous modules imported during interpreter startup
    sys.modules.pop("winreg", None)
    sys.modules.pop("nt", None)

    # Step 2: Set up normalized read allowlist (Standard library, scratch, and skill only)
    stdlib_dir = os.path.dirname(os.__file__)
    base_prefix = getattr(sys, "base_prefix", sys.prefix)
    allowlist = [
        stdlib_dir,
        str(scratch_dir),
        str(module_path),
    ]
    dlls_dir = os.path.join(base_prefix, "DLLs")
    if os.path.isdir(dlls_dir):
        allowlist.append(dlls_dir)

    # Step 3: Install audit hook
    try:
        _install_audit_hook(allowlist, scratch_dir=str(scratch_dir))
    except (RuntimeError, ValueError, OSError) as e:
        _write_result({"status": "error", "error": f"Failed to install audit hook: {e}"})
        return 3

    # Step 4: Read nonce and kwargs from stdin
    try:
        raw_input = sys.stdin.read()
        sys.stdin.close()
        payload = json.loads(raw_input) if raw_input else {}
        if not isinstance(payload, dict):
            raise TypeError("Invalid stdin payload: expected a JSON dictionary object.")
        nonce = payload.get("nonce")
        kwargs = payload.get("kwargs")
        if not isinstance(nonce, str) or not nonce.strip():
            raise ValueError("Invalid stdin payload: 'nonce' must be a non-empty string.")
        if not isinstance(kwargs, dict):
            raise TypeError("Invalid stdin payload: 'kwargs' must be a JSON dictionary.")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError, OSError) as e:
        _write_result({"status": "error", "error": f"Failed to parse stdin payload: {e}"})
        return 4

    # Step 5: Single-read memory compilation and SHA-256 verification (no TOCTOU)
    try:
        if not module_path.exists():
            raise FileNotFoundError(f"Custom skill module not found: {module_path}")

        code_bytes = module_path.read_bytes()
        actual_hash = hashlib.sha256(code_bytes).hexdigest()

        if actual_hash.lower() != args.expected_hash.lower():
            raise ValueError(
                f"Integrity failure: SHA-256 mismatch for '{module_path.name}'. "
                f"Expected {args.expected_hash}, calculated {actual_hash}."
            )

        code_obj = compile(code_bytes, str(module_path), "exec")
        custom_mod = types.ModuleType(module_path.stem)
        custom_mod.__file__ = str(module_path)

        exec(code_obj, custom_mod.__dict__)  # noqa: S102
    except Exception as e:  # noqa: BLE001
        err_msg = str(e)
        if len(err_msg) > 1024:
            err_msg = err_msg[:1024] + "... [truncated]"
        resp = {
            "nonce": nonce,
            "status": "error",
            "type": type(e).__name__,
            "error": err_msg,
        }
        _write_result(resp)
        return 1

    # Step 6: Locate and invoke the target tool
    tool_fn = StubSkillRegistry._tools.get(args.tool_name)
    if not tool_fn:
        tool_fn = getattr(custom_mod, args.tool_name, None)

    if not tool_fn or not callable(tool_fn):
        resp = {
            "nonce": nonce,
            "status": "error",
            "type": "ToolNotFoundError",
            "error": f"Tool '{args.tool_name}' not registered or callable in module.",
        }
        _write_result(resp)
        return 1

    try:
        result = tool_fn(**kwargs)
        # Verify JSON serializability and 1MB size cap
        resp = {
            "nonce": nonce,
            "status": "success",
            "result": result,
        }
        _write_result(resp)
        return 0
    except Exception as e:  # noqa: BLE001
        err_msg = str(e)
        if len(err_msg) > 1024:
            err_msg = err_msg[:1024] + "... [truncated]"
        resp = {
            "nonce": nonce,
            "status": "error",
            "type": type(e).__name__,
            "error": err_msg,
        }
        _write_result(resp)
        return 1


if __name__ == "__main__":
    sys.exit(main())
