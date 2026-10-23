"""Ruff Linter & Formatter Wrapper for Jarvis 2.0.

Provides reliable, cross-platform resolution and execution of Ruff without
requiring hardcoded developer paths or manual PATH configuration.
Supports Windows virtual environments, PATH discovery, and module fallbacks.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SUBPROCESS_TIMEOUT: float = 300.0


class RuffNotFoundError(FileNotFoundError):
    """Raised when Ruff cannot be located in any supported environment."""


def _candidate_binary_names() -> list[str]:
    """Returns candidate binary file names depending on OS."""
    if os.name == "nt":
        return ["ruff.exe", "ruff"]
    return ["ruff"]


def _search_env_dir(env_dir: Path) -> Path | None:
    """Searches a virtualenv root directory for the ruff executable."""
    subdirs = ["Scripts", "bin", ""]
    names = _candidate_binary_names()
    for subdir in subdirs:
        for name in names:
            target = (env_dir / subdir / name) if subdir else (env_dir / name)
            if target.is_file():
                return target
    return None


def _is_trusted_env_path(
    candidate: Path,
    workspace_root: Path,
    sys_exec: str | None = None,
) -> bool:
    """Validates that a binary candidate from an environment variable (VIRTUAL_ENV,
    CONDA_PREFIX) resides within an approved virtual environment location.

    Approved locations:
    1. Designated local workspace virtual environment: workspace_root / ".venv".
    2. Designated adjacent workspace virtual environment: workspace_root.parent / ".venv".
    3. Active Python interpreter environment: sys.prefix or active virtual environment root.

    Arbitrary in-repository subdirectories and unrelated sibling directories are rejected.

    Returns:
        True if candidate is confined within approved locations, False otherwise.
    """
    try:
        cand_resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return False

    trusted_roots: list[Path] = []

    # 1. Designated workspace virtual environments ONLY (not arbitrary in-repo subdirectories)
    try:
        ws_resolved = workspace_root.resolve()
        trusted_roots.append(ws_resolved / ".venv")
        trusted_roots.append(ws_resolved.parent / ".venv")
    except (OSError, RuntimeError):
        pass

    # 2. Active Python interpreter virtual environment prefix
    try:
        trusted_roots.append(Path(sys.prefix).resolve())
    except (OSError, RuntimeError):
        pass

    # 3. If sys_exec explicitly points inside a virtualenv (e.g. Scripts/python or bin/python)
    if sys_exec:
        try:
            exec_resolved = Path(sys_exec).resolve()
            if exec_resolved.parent.name.lower() in ("scripts", "bin"):
                trusted_roots.append(exec_resolved.parent.parent)
            else:
                trusted_roots.append(exec_resolved.parent)
        except (OSError, RuntimeError):
            pass

    for root in trusted_roots:
        try:
            if cand_resolved.is_relative_to(root):
                return True
        except (ValueError, AttributeError):
            try:
                cand_resolved.relative_to(root)
                return True
            except ValueError:
                pass

    return False


def resolve_ruff_command(
    workspace_root: Path | None = None,
    environ: dict[str, str] | None = None,
    sys_exec: str | None = None,
    which_func: Callable[[str], str | None] | None = None,
    module_checker: Callable[[], bool] | None = None,
) -> list[str]:
    """Resolves the command list required to invoke Ruff.

    Search precedence:
    1. Active virtual environment from environment variables (VIRTUAL_ENV, CONDA_PREFIX),
       validated against trusted root containment policies.
    2. Active Python interpreter environment (sys.executable directory / Scripts).
    3. Adjacent workspace virtual environments (WORKSPACE_ROOT.parent / .venv).
    4. Local repository virtual environment (WORKSPACE_ROOT / .venv).
    5. System PATH via shutil.which("ruff").
    6. Python module fallback (python -m ruff) if importable.

    Returns:
        A list of command tokens suitable for subprocess.run(cmd, shell=False).

    Raises:
        RuffNotFoundError: If Ruff cannot be resolved via any discovery tier.
    """
    root = workspace_root if workspace_root is not None else WORKSPACE_ROOT
    env = environ if environ is not None else os.environ
    active_python = sys_exec if sys_exec is not None else sys.executable
    which = which_func if which_func is not None else shutil.which

    def _default_module_check() -> bool:
        return importlib.util.find_spec("ruff") is not None

    has_ruff_module = module_checker if module_checker is not None else _default_module_check

    # 1. Environment variables (VIRTUAL_ENV / CONDA_PREFIX) - validated against trusted roots
    for env_var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        env_path_str = env.get(env_var)
        if env_path_str:
            candidate = _search_env_dir(Path(env_path_str))
            if candidate and _is_trusted_env_path(candidate, root, active_python):
                return [str(candidate)]

    # 2. Directory containing current Python interpreter
    python_parent = Path(active_python).parent
    for name in _candidate_binary_names():
        candidate = python_parent / name
        if candidate.is_file():
            return [str(candidate)]
    # Also check Scripts or bin if sys.executable was the venv root
    candidate = _search_env_dir(python_parent)
    if candidate:
        return [str(candidate)]

    # 3. Adjacent workspace virtualenv (e.g. parent / .venv)
    parent_venv = root.parent / ".venv"
    if parent_venv.is_dir():
        candidate = _search_env_dir(parent_venv)
        if candidate:
            return [str(candidate)]

    # 4. Local repository virtualenv (.venv in workspace root)
    local_venv = root / ".venv"
    if local_venv.is_dir():
        candidate = _search_env_dir(local_venv)
        if candidate:
            return [str(candidate)]

    # 5. System PATH
    path_ruff = which("ruff")
    if path_ruff:
        return [path_ruff]

    # 6. Python module fallback
    if has_ruff_module():
        return [active_python, "-m", "ruff"]

    raise RuffNotFoundError(
        "Could not locate 'ruff' executable or Python module in any expected location.\n"
        "Locations searched:\n"
        f"  - Active virtual environments ($VIRTUAL_ENV, $CONDA_PREFIX)\n"
        f"  - Current Python directory: {python_parent}\n"
        f"  - Adjacent workspace .venv: {parent_venv}\n"
        f"  - Local workspace .venv: {local_venv}\n"
        "  - System PATH\n"
        "  - Python import: 'python -m ruff'"
    )


def print_wrapper_help() -> None:
    """Prints wrapper documentation and usage examples."""
    print(
        "Jarvis 2.0 Ruff Linter & Formatter Wrapper (tools/lint.py)\n\n"
        "Usage:\n"
        "    python tools/lint.py [RUFF_COMMAND_OR_OPTIONS...]\n\n"
        "Common Invocations:\n"
        "    python tools/lint.py check <path>              # Lint target path\n"
        "    python tools/lint.py check --fix <path>        # Lint and apply safe fixes\n"
        "    python tools/lint.py format <path>             # Format target path\n"
        "    python tools/lint.py format --check <path>     # Check formatting\n\n"
        "Wrapper Invariants:\n"
        "    - Never uses shell=True; safe against paths containing spaces.\n"
        "    - Automatically resolves Ruff across Windows and POSIX virtualenvs.\n"
        "    - Strictly forwards exit codes to caller without modification.\n"
        "    - Does not auto-install or mutate system dependencies.\n\n"
        "Wrapper Flags:\n"
        "    --help, -h         Show this wrapper help and native Ruff help\n"
        "    --wrapper-info     Show resolved Ruff command tokens and exit"
    )
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for tools/lint.py."""
    if argv is None:
        argv = sys.argv[1:]

    # Handle wrapper-specific introspection
    if argv == ["--wrapper-info"]:
        try:
            cmd = resolve_ruff_command()
            print("Resolved Ruff command: " + " ".join(cmd))
            return 0
        except RuffNotFoundError as err:
            sys.stderr.write(f"Resolution failed:\n{err}\n")
            return 1

    # Handle wrapper help
    if argv == ["--help"] or argv == ["-h"]:
        print_wrapper_help()
        try:
            cmd = resolve_ruff_command()
            print("\n" + "=" * 30 + " Native Ruff Help " + "=" * 30 + "\n")
            return subprocess.run(
                cmd + ["--help"],
                check=False,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                timeout=15.0,
            ).returncode
        except (RuffNotFoundError, OSError, subprocess.TimeoutExpired):
            return 0

    try:
        ruff_cmd = resolve_ruff_command()
    except RuffNotFoundError as err:
        sys.stderr.write(f"\nError: {err}\n\n")
        sys.stderr.write(
            "Action required:\n"
            "    Install Ruff in your active Python environment:\n"
            "        pip install ruff\n"
            "    Or activate a virtual environment that contains Ruff.\n"
        )
        return 1

    full_cmd = ruff_cmd + argv
    try:
        # shell=False ensures argument list is passed directly without shell expansion
        result = subprocess.run(
            full_cmd,
            check=False,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            timeout=DEFAULT_SUBPROCESS_TIMEOUT,
        )
        return result.returncode
    except KeyboardInterrupt:
        return 130
    except subprocess.TimeoutExpired as err:
        bin_name = Path(full_cmd[0]).name if full_cmd else "Ruff"
        sys.stderr.write(f"Error: {bin_name} timed out after {err.timeout} seconds.\n")
        return 124
    except OSError as err:
        bin_name = Path(full_cmd[0]).name if full_cmd else "Ruff"
        sys.stderr.write(f"Error executing Ruff process ({bin_name}): {err}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
