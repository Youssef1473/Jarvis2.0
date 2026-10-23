"""Unified Diagnostic & Structural Analysis Workflow for Jarvis 2.0.

Composes Ruff static lint diagnostics (reusing tools/lint.py) with AST-Grep
structural pattern queries and architectural invariant rule scans into a
single, portable engineering workflow.

Provides:
- Combined linting and structural rule verification (python tools/diagnostics.py check)
- On-demand AST pattern searching (python tools/diagnostics.py search)
- Reusable structural invariant scanning (python tools/diagnostics.py scan)
- Direct delegation to Ruff linter & formatter (tools/lint.py)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

# Direct reuse of canonical Ruff wrapper without duplicating discovery logic
from tools.lint import RuffNotFoundError, resolve_ruff_command

DEFAULT_RULES_DIR = WORKSPACE_ROOT / ".agents" / "rules" / "ast-grep"
DEFAULT_SGCONFIG = WORKSPACE_ROOT / "sgconfig.yml"


class AstGrepNotFoundError(FileNotFoundError):
    """Raised when ast-grep cannot be located in any supported environment."""


def _candidate_binary_names() -> list[str]:
    """Returns candidate binary file names depending on OS."""
    if os.name == "nt":
        return ["ast-grep.exe", "ast-grep", "sg.exe", "sg"]
    return ["ast-grep", "sg"]


def _search_env_dir(env_dir: Path) -> Path | None:
    """Searches a virtualenv root directory for the ast-grep executable."""
    subdirs = ["Scripts", "bin", ""]
    names = _candidate_binary_names()
    for subdir in subdirs:
        for name in names:
            target = (env_dir / subdir / name) if subdir else (env_dir / name)
            if target.is_file():
                return target
    return None


def resolve_ast_grep_command(
    workspace_root: Path | None = None,
    environ: dict[str, str] | None = None,
    sys_exec: str | None = None,
    which_func: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Resolves the command list required to invoke ast-grep.

    Search precedence:
    1. Active virtual environment from environment variables (VIRTUAL_ENV, CONDA_PREFIX).
    2. Active Python interpreter environment (sys.executable directory / Scripts).
    3. Adjacent workspace virtual environments (WORKSPACE_ROOT.parent / .venv).
    4. Local repository virtual environment (WORKSPACE_ROOT / .venv).
    5. System PATH via shutil.which("ast-grep") or shutil.which("sg").

    Returns:
        A list of command tokens suitable for subprocess.run(cmd, shell=False).

    Raises:
        AstGrepNotFoundError: If ast-grep cannot be resolved via any discovery tier.
    """
    root = workspace_root if workspace_root is not None else WORKSPACE_ROOT
    env = environ if environ is not None else os.environ
    active_python = sys_exec if sys_exec is not None else sys.executable
    which = which_func if which_func is not None else shutil.which

    # 1. Environment variables (VIRTUAL_ENV / CONDA_PREFIX)
    for env_var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        env_path_str = env.get(env_var)
        if env_path_str:
            candidate = _search_env_dir(Path(env_path_str))
            if candidate:
                return [str(candidate)]

    # 2. Directory containing current Python interpreter
    python_parent = Path(active_python).parent
    for name in _candidate_binary_names():
        candidate = python_parent / name
        if candidate.is_file():
            return [str(candidate)]
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
    for name in ("ast-grep", "sg"):
        path_ast_grep = which(name)
        if path_ast_grep:
            return [path_ast_grep]

    raise AstGrepNotFoundError(
        "Could not locate 'ast-grep' or 'sg' executable in any expected location.\n"
        "Locations searched:\n"
        f"  - Active virtual environments ($VIRTUAL_ENV, $CONDA_PREFIX)\n"
        f"  - Current Python directory: {python_parent}\n"
        f"  - Adjacent workspace .venv: {parent_venv}\n"
        f"  - Local workspace .venv: {local_venv}\n"
        "  - System PATH\n\n"
        "Action required:\n"
        "    Install ast-grep in your active Python environment:\n"
        "        pip install ast-grep-cli\n"
        "    Or activate a virtual environment containing ast-grep."
    )


def run_ruff(args: list[str]) -> int:
    """Executes Ruff via the canonical tools/lint.py wrapper."""
    from tools import lint
    return lint.main(args)


def run_ast_grep(args: list[str]) -> int:
    """Executes ast-grep with resolved binary and forwarded arguments."""
    try:
        ast_grep_cmd = resolve_ast_grep_command()
    except AstGrepNotFoundError as err:
        sys.stderr.write(f"\nError: {err}\n\n")
        return 1

    full_cmd = ast_grep_cmd + args
    try:
        result = subprocess.run(full_cmd, check=False)
        return result.returncode
    except KeyboardInterrupt:
        return 130
    except OSError as err:
        sys.stderr.write(f"Error executing ast-grep process ({full_cmd}): {err}\n")
        return 1


def run_combined_check(paths: list[str] | None = None) -> int:
    """Runs combined static analysis: Ruff linter + AST-Grep rule scan.

    1. Executes Ruff check on target paths.
    2. Executes AST-Grep scan for architectural invariants on target paths.
    3. Prints clear, visually separated sections and aggregated exit summary.

    Returns:
        0 if all checks pass; non-zero exit code if any check reports violations.
    """
    target_paths = paths if paths else ["."]

    print("=" * 64)
    print(" [1/2] Ruff Linter Diagnostics (tools/lint.py check)")
    print("=" * 64)
    sys.stdout.flush()

    ruff_args = ["check"] + target_paths
    ruff_code = run_ruff(ruff_args)

    print()
    print("=" * 64)
    print(" [2/2] AST-Grep Structural Invariants (.agents/rules/ast-grep)")
    print("=" * 64)
    sys.stdout.flush()

    scan_args = ["scan"]
    if DEFAULT_SGCONFIG.is_file():
        scan_args.extend(["-c", str(DEFAULT_SGCONFIG)])
    elif DEFAULT_RULES_DIR.is_dir():
        # Fall back to iterating rule files if no sgconfig.yml
        pass
    scan_args.extend(target_paths)

    ast_code = run_ast_grep(scan_args)

    print()
    print("=" * 64)
    print(" Diagnostic Workflow Summary:")
    print(f"   • Ruff Linter:     {'PASSED (exit 0)' if ruff_code == 0 else f'FAILED (exit {ruff_code})'}")
    print(f"   • AST-Grep Rules:  {'PASSED (exit 0)' if ast_code == 0 else f'FAILED (exit {ast_code})'}")

    if ruff_code == 0 and ast_code == 0:
        print("   Result: ALL DIAGNOSTICS PASSED")
        print("=" * 64)
        return 0
    else:
        print("   Result: DIAGNOSTIC VIOLATIONS DETECTED")
        print("=" * 64)
        return max(ruff_code, ast_code) if (ruff_code != 0 and ast_code != 0) else (ruff_code or ast_code)


def print_wrapper_help() -> None:
    """Prints wrapper documentation and usage examples."""
    print(
        "Jarvis 2.0 Unified Diagnostics & Structural Analysis (tools/diagnostics.py)\n\n"
        "Usage:\n"
        "    python tools/diagnostics.py <command> [OPTIONS...]\n\n"
        "Commands:\n"
        "    check [paths...]                Run combined Ruff lint + AST-Grep invariant scan\n"
        "    lint [options...] [paths...]    Run Ruff static linter (via tools/lint.py check)\n"
        "    format [options...] [paths...]  Run Ruff code formatter (via tools/lint.py format)\n"
        "    search <pattern> [paths...]     Run AST-Grep one-off structural search\n"
        "    scan [paths...]                 Run AST-Grep project rule scan (.agents/rules/ast-grep)\n"
        "    run [args...]                   Directly invoke ast-grep run with arbitrary arguments\n\n"
        "Common Invocations:\n"
        "    python tools/diagnostics.py check tools tests\n"
        "    python tools/diagnostics.py lint --select E9,F821 jarvis\n"
        "    python tools/diagnostics.py search 'subprocess.run($$$ARGS)' tools\n"
        "    python tools/diagnostics.py scan jarvis tools\n\n"
        "Introspection & Help:\n"
        "    --wrapper-info, -i              Inspect resolved binaries and active rules\n"
        "    --help, -h                      Show this wrapper help\n"
    )
    sys.stdout.flush()


def print_wrapper_info() -> int:
    """Displays resolved Ruff and ast-grep environments and active rules."""
    print("Jarvis 2.0 Diagnostic Workflow Introspection:")
    print("-" * 50)

    # Ruff resolution
    try:
        ruff_cmd = resolve_ruff_command()
        print(f"Ruff binary:      {' '.join(ruff_cmd)}")
    except RuffNotFoundError as err:
        print(f"Ruff binary:      UNRESOLVED ({err})")

    # AST-Grep resolution
    try:
        ast_cmd = resolve_ast_grep_command()
        print(f"AST-Grep binary:  {' '.join(ast_cmd)}")
    except AstGrepNotFoundError as err:
        print(f"AST-Grep binary:  UNRESOLVED ({err})")

    # Rules configuration
    print(f"sgconfig.yml:     {DEFAULT_SGCONFIG} ({'exists' if DEFAULT_SGCONFIG.is_file() else 'missing'})")
    if DEFAULT_RULES_DIR.is_dir():
        rules = list(DEFAULT_RULES_DIR.glob("*.yml")) + list(DEFAULT_RULES_DIR.glob("*.yaml"))
        print(f"Active rules ({len(rules)}):")
        for r in rules:
            print(f"  - {r.name}")
    else:
        print(f"Rules directory:  {DEFAULT_RULES_DIR} (missing)")

    print("-" * 50)
    sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for tools/diagnostics.py."""
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv in (["--help"], ["-h"], ["help"]):
        print_wrapper_help()
        return 0

    if argv in (["--wrapper-info"], ["--info"], ["-i"]):
        return print_wrapper_info()

    subcommand = argv[0].lower()
    subargs = argv[1:]

    if subcommand == "check":
        return run_combined_check(subargs)

    if subcommand == "lint":
        # Forward to tools/lint.py check if first arg isn't already check or format
        if not subargs or not subargs[0].startswith(("-", "check", "format")):
            return run_ruff(["check"] + subargs)
        return run_ruff(subargs)

    if subcommand == "format":
        return run_ruff(["format"] + subargs)

    if subcommand == "search":
        if not subargs:
            sys.stderr.write("Error: 'search' requires an AST pattern argument.\n")
            sys.stderr.write("Usage: python tools/diagnostics.py search '<pattern>' [paths...]\n")
            return 1
        pattern = subargs[0]
        paths = subargs[1:]
        ast_args = ["run", "-p", pattern] + paths
        return run_ast_grep(ast_args)

    if subcommand == "run":
        return run_ast_grep(["run"] + subargs)

    if subcommand == "scan":
        scan_args = ["scan"]
        if DEFAULT_SGCONFIG.is_file() and "-c" not in subargs and "--config" not in subargs:
            scan_args.extend(["-c", str(DEFAULT_SGCONFIG)])
        scan_args.extend(subargs)
        return run_ast_grep(scan_args)

    # If first token looks like a path or flag, default to check
    if subcommand.startswith("-") or Path(subcommand).exists():
        return run_combined_check(argv)

    sys.stderr.write(f"Unknown diagnostic subcommand: '{subcommand}'\n")
    sys.stderr.write("Run 'python tools/diagnostics.py --help' for available commands.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
