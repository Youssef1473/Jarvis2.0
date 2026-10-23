"""PostToolUse Lifecycle Hook for Jarvis 2.0.

Lightweight, post-edit diagnostic hook that triggers only when Python files
within the repository are modified by Antigravity file-editing tools.
Runs fast static checks (E9 syntax errors, F821 undefined names) via tools/lint.py
and reports findings honestly without blocking subsequent operations or running
the full test suite.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LINT_PY = REPO_ROOT / "tools" / "lint.py"
DEFAULT_TIMEOUT_SECONDS = 5.0

SUPPORTED_TOOLS = {
    "edit_file",
    "write_file",
    "replace_file_content",
    "write_to_file",
    "multi_replace_file_content",
}

PYTHON_EXTENSIONS = {".py", ".pyi"}


PATH_KEYS_CANONICAL = {"targetfile", "path", "filepath"}
MULTI_PATH_KEYS_CANONICAL = {"files", "paths"}


def extract_target_paths(payload: dict) -> list[str]:
    """Extracts target file path(s) from tool call arguments in the hook payload.

    Handles documented Antigravity tool arguments (TargetFile) as well as
    common variants (Path, path, filePath, file_path) in a case- and delimiter-insensitive manner.
    """
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return []

    tool_name = tool_call.get("name")
    if not tool_name or tool_name not in SUPPORTED_TOOLS:
        return []

    args = tool_call.get("args")
    if not isinstance(args, dict):
        return []

    candidates: list[str] = []

    # Single path keys with normalization (e.g., TargetFile, target_file, Path, filePath)
    for raw_key, val in args.items():
        norm_key = str(raw_key).lower().replace("_", "")
        if norm_key in PATH_KEYS_CANONICAL and isinstance(val, str) and val.strip():
            candidates.append(val.strip())
        elif norm_key in MULTI_PATH_KEYS_CANONICAL and isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.strip():
                    candidates.append(item.strip())
                elif isinstance(item, dict):
                    for nested_key, nested_val in item.items():
                        norm_nested = str(nested_key).lower().replace("_", "")
                        if norm_nested in PATH_KEYS_CANONICAL and isinstance(nested_val, str) and nested_val.strip():
                            candidates.append(nested_val.strip())

    return list(dict.fromkeys(candidates))


def resolve_contained_python_file(path_str: str, repo_root: Path) -> Path | None:
    """Validates that a path is a Python file strictly contained within repo_root."""
    try:
        raw_path = Path(path_str)
        target = (repo_root / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()

        # Check repository boundary confinement
        target.relative_to(repo_root.resolve())

        # Check extension
        if target.suffix.lower() not in PYTHON_EXTENSIONS:
            return None

        # Check file existence on disk
        if not target.is_file():
            return None

        return target
    except (ValueError, OSError):
        return None


def run_fast_lint(
    target_path: Path,
    lint_py: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    """Executes fast static checks (E9, F821) via tools/lint.py."""
    if not lint_py.is_file():
        return 1, f"Linter wrapper not found at {lint_py}"

    cmd = [
        sys.executable,
        str(lint_py),
        "check",
        "--select",
        "E9,F821",
        str(target_path),
    ]

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = (res.stdout or "") + (res.stderr or "")
        return res.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return 1, f"Lint check timed out after {timeout:.1f}s"
    except OSError as err:
        return 1, f"Subprocess execution error: {err}"


def process_hook_payload(
    raw_json: str,
    repo_root: Path | None = None,
    lint_py: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """Parses hook stdin, validates target, runs lint diagnostics, and reports results."""
    root = repo_root if repo_root is not None else REPO_ROOT
    linter = lint_py if lint_py is not None else DEFAULT_LINT_PY

    # Always ensure valid PostToolUse stdout contract: empty JSON object
    output_contract = "{}\n"

    try:
        if not raw_json.strip():
            sys.stdout.write(output_contract)
            sys.stdout.flush()
            return 0

        payload = json.loads(raw_json)
        if not isinstance(payload, dict):
            sys.stdout.write(output_contract)
            sys.stdout.flush()
            return 0
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Gracefully handle malformed input without breaking the agent loop
        sys.stdout.write(output_contract)
        sys.stdout.flush()
        return 0

    path_strings = extract_target_paths(payload)
    if not path_strings:
        sys.stdout.write(output_contract)
        sys.stdout.flush()
        return 0

    for path_str in path_strings:
        target = resolve_contained_python_file(path_str, root)
        if target is None:
            continue

        rel_path = target.relative_to(root.resolve())
        code, diagnostics = run_fast_lint(target, linter, timeout=timeout)

        if code != 0 and diagnostics:
            # Report diagnostics to stderr so they surface in agent error telemetry
            sys.stderr.write(
                f"\n[PostToolUse Diagnostic] Fast lint check flagged issues in {rel_path}:\n"
                f"{diagnostics}\n"
            )
            sys.stderr.flush()

    sys.stdout.write(output_contract)
    sys.stdout.flush()
    return 0


def main() -> int:
    """CLI entry point reading from stdin."""
    try:
        raw_input = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        raw_input = ""
    return process_hook_payload(raw_input)


if __name__ == "__main__":
    sys.exit(main())
