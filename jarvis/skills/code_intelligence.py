"""Static analysis, structural search, git inspections, and knowledge graph queries."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from jarvis.skills.base import SkillRegistry
from jarvis.skills.fs_confinement import WORKSPACE_DIR, _resolve_workspace_path
from jarvis.skills.job_object_sandbox import run_sandboxed_batch

logger = logging.getLogger("Jarvis.Skills.CodeIntelligence")

POWERSHELL_JOB_MAX_MEMORY_MB = 4096
POWERSHELL_JOB_MAX_PROCESSES = 64
POWERSHELL_OUTPUT_TAIL_CHARS = 16000


def _get_dev_shell_symbol(name: str, fallback: Any) -> Any:
    """Dynamically resolves a symbol from jarvis.skills.dev_shell to support mock patching."""
    mod = sys.modules.get("jarvis.skills.dev_shell")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return fallback


def _find_ast_grep() -> Path | None:
    """Locates ast-grep executable across python environment, PATH, and parent workspace."""
    ast_grep_bin = Path(sys.executable).parent / "ast-grep.exe"
    if ast_grep_bin.exists():
        return ast_grep_bin
    sg_bin = Path(sys.executable).parent / "sg.exe"
    if sg_bin.exists():
        return sg_bin
    candidate = shutil.which("ast-grep") or shutil.which("sg")
    if candidate:
        return Path(candidate)
    parent_venv = WORKSPACE_DIR.parent / ".venv" / "Scripts" / "ast-grep.exe"
    if parent_venv.exists():
        return parent_venv
    return None


def _find_ruff() -> Path | None:
    """Locates ruff executable across python environment, PATH, and parent workspace."""
    ruff_bin = Path(sys.executable).parent / "ruff.exe"
    if ruff_bin.exists():
        return ruff_bin
    candidate = shutil.which("ruff")
    if candidate:
        return Path(candidate)
    parent_venv = WORKSPACE_DIR.parent / ".venv" / "Scripts" / "ruff.exe"
    if parent_venv.exists():
        return parent_venv
    return None


@SkillRegistry.register(
    name="git_diff",
    description="Reviews tracked edits in the Jarvis workspace against HEAD, including staged and unstaged changes. Output is capped for context."
)
def git_diff(max_chars: int = 12000) -> str:
    """Returns a bounded tracked-file diff for reviewing recent edits."""
    batch_fn = _get_dev_shell_symbol("run_sandboxed_batch", run_sandboxed_batch)
    try:
        proc = batch_fn(
            ["git", "diff", "--no-ext-diff", "--unified=3", "HEAD", "--"],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = proc.stdout.strip()
        if proc.returncode != 0:
            return f"Git diff failed: {proc.stderr.strip()}"
        if not output:
            return "No tracked changes to review."
        limit = max(1000, min(max_chars, 30000))
        if len(output) > limit:
            return output[:limit] + "\n\n[Diff truncated; request a narrower review.]"
        return output
    except subprocess.TimeoutExpired:
        return "Git diff error: command timed out after 15 seconds."
    except (subprocess.SubprocessError, OSError) as e:
        return f"Git diff error: {e}"


@SkillRegistry.register(
    name="run_project_tests",
    description="Runs Python test suites using the high-performance parallel test runner (pytest-xdist) to verify code changes."
)
def run_project_tests(
    test_file: str = "",
    run_all: bool = False,
    timeout_seconds: int = 120,
    *,
    _allow_uncontained: bool = False,
) -> str:
    """Executes tests using the high-performance two-tier test runner.

    Process-tree containment under Windows is strictly enforced by default.
    Model-callable tool arguments cannot bypass containment. Trusted internal callers
    may explicitly override containment via the internal parameter `_allow_uncontained=True`,
    or trusted local configuration via `JARVIS_ALLOW_UNCONTAINED_TESTS=1`.
    """
    py_exec = sys.executable
    runner_script = WORKSPACE_DIR / "tools" / "run_fast_tests.py"

    if runner_script.exists():
        cmd = [py_exec, str(runner_script)]
        if test_file:
            cmd.append(test_file)
        elif run_all:
            cmd.append("--all")
        else:
            cmd.append("--fast")
    else:
        # Fallback to standard runner
        if test_file:
            target = f"tests/{test_file}".strip("/")
            cmd = [py_exec, "-m", "pytest", target]
        else:
            cmd = [py_exec, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]

    try:
        from jarvis.core.cancellation import get_current_tool_cancellation_token
        from jarvis.skills.job_object_sandbox import run_job_sandboxed_process

        token = get_current_tool_cancellation_token()
        if token is not None:
            if token.is_cancelled:
                return "Error: Test run cancelled before launch: execution deadline expired or interrupt signaled."
            if token.deadline is not None:
                remaining = token.remaining_time()
                if remaining <= 0.0:
                    return "Error: Test run cancelled before launch: execution deadline expired."
                effective_timeout = min(float(timeout_seconds), remaining)
            else:
                effective_timeout = float(timeout_seconds)
        else:
            effective_timeout = float(timeout_seconds)

        res = run_job_sandboxed_process(
            command=cmd,
            cwd=str(WORKSPACE_DIR),
            max_memory_mb=POWERSHELL_JOB_MAX_MEMORY_MB,
            max_processes=POWERSHELL_JOB_MAX_PROCESSES,
            timeout_sec=effective_timeout,
            requested_affinity=0,
            cancel_token=token,
            tail_chars=POWERSHELL_OUTPUT_TAIL_CHARS,
        )
        status = res.get("status")
        has_output = bool((res.get("stdout_tail") or "").strip() or (res.get("stderr_tail") or "").strip())
        if status in ("ok", "failed", "timeout", "cancelled") or has_output:
            # Subprocess output boundary: Taint active turn before returning test output to LLM
            from jarvis.core.security_boundaries import TurnTaintTracker
            TurnTaintTracker.get_instance().mark_tainted(
                source="subprocess_output:run_project_tests",
                metadata={"test_file": test_file[:64], "status": status}
            )
        if status == "timeout":
            return f"Error: Test run timed out after {effective_timeout:.1f} seconds, Sir."
        if status == "cancelled":
            return "Error: Test run cancelled by user."
        if status in ("ok", "failed"):
            out = (res.get("stdout_tail", "") + "\n" + res.get("stderr_tail", "")).strip()
            run_status = "PASSED" if res.get("exit_code") == 0 else f"FAILED (Exit Code {res.get('exit_code')})"
            return f"Test Run [{run_status}]:\n{out}"

        if status == "error":
            reason = res.get("reason", "Job Object containment failed")
            if sys.platform == "win32":
                uncontained_opt_in = (
                    _allow_uncontained
                    or os.environ.get("JARVIS_ALLOW_UNCONTAINED_TESTS") == "1"
                    or os.environ.get("JARVIS_ALLOW_UNCONTAINED_TEST_RUNNER") == "1"
                )
                if not uncontained_opt_in:
                    return (
                        f"Error: Job Object containment failed on Windows ({reason}). "
                        "Process-tree containment is required by default to prevent orphan processes. "
                        "Fallback to uncontained execution is disabled. "
                        "To override, set JARVIS_ALLOW_UNCONTAINED_TESTS=1 in trusted local environment "
                        "or invoke via trusted internal API (_allow_uncontained=True)."
                    )
                logger.warning(
                    "Executing test runner with uncontained fallback on Windows: %s", reason
                )
        else:
            return f"Error executing tests: unexpected sandbox status '{status}' ({res.get('reason', '')})"

        # Fallback if primary Job Object execution is unavailable (non-Windows hosts, or explicitly opted-in on Windows).
        # When authorized, we attempt process-tree containment via run_sandboxed_batch().
        # If run_sandboxed_batch() fails to establish containment, we fail closed and return an error;
        # under no circumstances do we fall back to bare subprocess.run().
        batch_fn = _get_dev_shell_symbol("run_sandboxed_batch", run_sandboxed_batch)
        try:
            proc = batch_fn(
                cmd,
                cwd=str(WORKSPACE_DIR),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (RuntimeError, OSError) as sand_err:
            platform_str = "Windows" if sys.platform == "win32" else "POSIX"
            return (
                f"Error: Contained fallback execution failed on {platform_str} ({sand_err}). "
                "Uncontained execution is strictly prohibited."
            )
        out = (proc.stdout + "\n" + proc.stderr).strip()
        run_status = "PASSED" if proc.returncode == 0 else f"FAILED (Exit Code {proc.returncode})"
        warning_banner = ""
        if sys.platform == "win32":
            warning_banner = (
                "[WARNING: UNCONTAINED EXECUTION — Windows Job Object process-tree containment is DISABLED. "
                "Descendant processes will not be tracked or automatically reaped on timeout/cancel.]\n"
            )
        from jarvis.core.security_boundaries import TurnTaintTracker
        TurnTaintTracker.get_instance().mark_tainted(
            source="subprocess_output:run_project_tests",
            metadata={"test_file": test_file[:64], "status": "fallback"}
        )
        return f"{warning_banner}Test Run [{run_status}]:\n{out}"
    except subprocess.TimeoutExpired:
        from jarvis.core.security_boundaries import TurnTaintTracker
        TurnTaintTracker.get_instance().mark_tainted(
            source="subprocess_output:run_project_tests",
            metadata={"test_file": test_file[:64], "status": "timeout"}
        )
        return f"Error: Test run timed out after {timeout_seconds} seconds, Sir."
    except Exception as e:
        from jarvis.core.security_boundaries import SecurityBoundaryViolation
        if isinstance(e, SecurityBoundaryViolation):
            raise
        return f"Error executing tests: {e}"


@SkillRegistry.register(
    name="git_status",
    description="Checks git repository status and current branch in the Jarvis workspace."
)
def git_status() -> str:
    """Returns git status and branch."""
    batch_fn = _get_dev_shell_symbol("run_sandboxed_batch", run_sandboxed_batch)
    try:
        proc = batch_fn(
            ["git", "status", "-s"],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=10,
        )
        branch_proc = batch_fn(
            ["git", "branch", "--show-current"],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = branch_proc.stdout.strip() or "unknown"
        changes = proc.stdout.strip() or "Working tree clean (no uncommitted changes)."
        return f"Branch: {branch}\n\nWorking Tree Status:\n{changes}"
    except subprocess.TimeoutExpired:
        return "Git error: git command timed out."
    except (subprocess.SubprocessError, OSError) as e:
        return f"Git error: {e}"


@SkillRegistry.register(
    name="query_codebase_graph",
    description="Queries the project's Tree-sitter knowledge graph (Graphify) to trace callers, dependencies, blast-radius, and architectural relationships across files in <1.5s."
)
def query_codebase_graph(query: str, mode: str = "query") -> str:
    """Queries the local Graphify knowledge graph (graphify query / path / explain)."""
    graph_path = WORKSPACE_DIR / "graphify-out" / "graph.json"
    if not graph_path.exists():
        return "No codebase knowledge graph found at graphify-out/graph.json. Run 'graphify extract . --code-only' first, Sir."

    graphify_bin = sys.executable.replace("python.exe", "graphify.exe")
    if not os.path.exists(graphify_bin):
        graphify_bin = "graphify"

    cmd = [graphify_bin]
    clean_mode = (mode or "query").strip().lower()
    if clean_mode == "path":
        parts = query.split()
        if len(parts) >= 2:
            cmd.extend(["path", parts[0], parts[1]])
        else:
            cmd.extend(["query", query])
    elif clean_mode == "explain":
        cmd.extend(["explain", query.strip()])
    else:
        cmd.extend(["query", query])

    batch_fn = _get_dev_shell_symbol("run_sandboxed_batch", run_sandboxed_batch)
    try:
        proc = batch_fn(
            cmd,
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 and not out:
            return f"Graph query failed (code {proc.returncode}): {err}"
        return out or err or "No relational nodes identified for that query, Sir."
    except subprocess.TimeoutExpired:
        return "Graph query timed out after 10 seconds, Sir."
    except (subprocess.SubprocessError, OSError) as e:
        return f"Graph query error: {e}"


@SkillRegistry.register(
    name="ast_search_codebase",
    description="Performs ultra-fast structural AST searches across the codebase using ast-grep. Matches syntax patterns (e.g. 'def $FUNC($$$ARGS): $$$BODY', '$OBJ.method($$$ARGS)') across Python, JavaScript, or JSON in <15ms."
)
def ast_search_codebase(pattern: str, path: str = ".", lang: str | None = None, max_lines: int = 60) -> str:
    """Performs structural AST pattern searching using ast-grep."""
    try:
        resolved_path = _resolve_workspace_path(path) if path and path != "." else WORKSPACE_DIR
    except ValueError as e:
        return f"Invalid path: {e}"

    find_ast_fn = _get_dev_shell_symbol("_find_ast_grep", _find_ast_grep)
    ast_grep_bin = find_ast_fn()
    if not ast_grep_bin:
        return "ast-grep binary is not installed in the environment. Run 'pip install ast-grep-cli', Sir."

    cmd = [str(ast_grep_bin), "run", "--color=never", "--heading=never", "-p", pattern]
    if lang:
        cmd.extend(["-l", lang])

    try:
        rel_target = str(resolved_path.relative_to(WORKSPACE_DIR))
        cmd.append("." if rel_target == "." else rel_target)
    except ValueError:
        cmd.append(str(resolved_path))

    batch_fn = _get_dev_shell_symbol("run_sandboxed_batch", run_sandboxed_batch)
    try:
        proc = batch_fn(
            cmd,
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=15,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        if proc.returncode != 0 and err:
            return f"ast-grep execution error: {err}"

        if not out:
            return f"No AST nodes matched pattern '{pattern}' in '{path}', Sir."

        lines = out.splitlines()
        total_lines = len(lines)
        if total_lines > max_lines:
            truncated = "\n".join(lines[:max_lines])
            return (
                f"{truncated}\n\n"
                f"[Output truncated: showing first {max_lines} of {total_lines} matched lines. "
                f"Narrow the target path or refine pattern, Sir.]"
            )
        return out
    except subprocess.TimeoutExpired:
        return "ast-grep search timed out after 15 seconds, Sir."
    except (subprocess.SubprocessError, OSError) as e:
        return f"ast-grep search error: {e}"


@SkillRegistry.register(
    name="check_code_diagnostics",
    description="Runs ultra-fast static analysis, compiler syntax checks, and linting across the codebase via Ruff in <20ms.",
    risk_level=0,
    read_only=True,
)
def check_code_diagnostics(target_path: str = ".", select: str | None = None, max_lines: int = 50, **kwargs: Any) -> str:
    """Runs Ruff static analysis and diagnostics on target files or directories."""
    # Reject model-supplied mutation flags outright
    if kwargs.get("fix") or kwargs.get("_fix"):
        return (
            "Execution rejected: Automated code mutation via 'fix=True' is prohibited in "
            "check_code_diagnostics. Use patch_file or write_file for authorized workspace modifications, Sir."
        )

    try:
        resolved_path = _resolve_workspace_path(target_path) if target_path and target_path != "." else WORKSPACE_DIR
    except ValueError as e:
        return f"Invalid path: {e}"

    find_ruff_fn = _get_dev_shell_symbol("_find_ruff", _find_ruff)
    ruff_bin = find_ruff_fn()
    if not ruff_bin:
        return "Ruff binary is not installed in the environment. Run 'pip install ruff', Sir."

    cmd = [str(ruff_bin), "check", "--color", "never", "--output-format", "concise"]
    if select:
        cmd.extend(["--select", select])

    try:
        rel_target = str(resolved_path.relative_to(WORKSPACE_DIR))
        cmd.append("." if rel_target == "." else rel_target)
    except ValueError:
        cmd.append(str(resolved_path))

    batch_fn = _get_dev_shell_symbol("run_sandboxed_batch", run_sandboxed_batch)
    try:
        proc = batch_fn(
            cmd,
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=15,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        if proc.returncode == 0:
            return f"All diagnostics clean: zero lint or syntax violations found across '{target_path}', Sir."

        display_text = out or err
        if not display_text:
            return f"Ruff reported non-zero exit code ({proc.returncode}) without output for '{target_path}', Sir."

        lines = display_text.splitlines()
        total_lines = len(lines)
        if total_lines > max_lines:
            truncated = "\n".join(lines[:max_lines])
            return (
                f"{truncated}\n\n"
                f"[Output truncated: showing first {max_lines} of {total_lines} diagnostic messages. "
                f"Target specific modules to inspect in detail, Sir.]"
            )
        return display_text
    except subprocess.TimeoutExpired:
        return "Ruff diagnostics check timed out after 15 seconds, Sir."
    except (subprocess.SubprocessError, OSError) as e:
        return f"Ruff diagnostics check error: {e}"
