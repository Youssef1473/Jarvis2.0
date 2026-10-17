"""Developer, codebase stewardship, and shell execution tools for Windows."""
import os
import sys
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from jarvis.skills.base import SkillRegistry

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent


def _resolve_workspace_path(file_path: str) -> Path:
    p = Path(file_path)
    if not p.is_absolute():
        p = WORKSPACE_DIR / p
    resolved = p.resolve()
    try:
        resolved.relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        raise ValueError("Path must stay inside the Jarvis workspace.")
    return resolved


@SkillRegistry.register(
    name="execute_powershell",
    description="Executes a PowerShell terminal command safely inside the Jarvis workspace and returns stdout and stderr."
)
def execute_powershell(command: str, timeout_seconds: int = 25) -> str:
    """Executes a PowerShell command with timeout in the workspace directory."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        res = []
        if stdout:
            res.append(f"STDOUT:\n{stdout}")
        if stderr:
            res.append(f"STDERR:\n{stderr}")
            if "being used by another process" in stderr or "WinError 32" in stderr:
                try:
                    from jarvis.skills.rstrtmgr_lock_surgeon import inspect_file_locks
                    tokens = [t.strip("\"'") for t in command.split() if "." in t and not t.startswith("-")]
                    for tok in tokens:
                        if os.path.exists(tok):
                            locks = inspect_file_locks(tok)
                            if locks.get("status") == "locked":
                                holders = [f"PID {h['pid']} ({h['app_name']})" for h in locks.get("holders", [])]
                                res.append(f"LOCK DIAGNOSTIC (Rstrtmgr): '{tok}' locked by {', '.join(holders)}")
                except Exception:
                    pass
        return "\n\n".join(res) if res else "Command completed with no output."
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout_seconds} seconds."
    except Exception as e:
        return f"Failed to execute command: {e}"


@SkillRegistry.register(
    name="execute_sandboxed_powershell",
    description="Executes a PowerShell command inside a kernel Job Object pinned to E-Cores (0xFF00) with hard memory caps.",
    risk_level=1,
)
def execute_sandboxed_powershell(command: str, max_memory_mb: int = 1536, timeout_seconds: int = 25) -> str:
    """Runs PowerShell inside an isolated Win32 Job Object shielding P-cores."""
    from jarvis.skills.job_object_sandbox import run_job_sandboxed_process
    res = run_job_sandboxed_process(
        command=["powershell", "-NoProfile", "-Command", command],
        cwd=str(WORKSPACE_DIR),
        max_memory_mb=max_memory_mb,
        timeout_sec=float(timeout_seconds),
    )
    if res.get("status") == "ok":
        out = []
        if res.get("stdout_tail"):
            out.append(f"STDOUT:\n{res['stdout_tail']}")
        if res.get("stderr_tail"):
            out.append(f"STDERR:\n{res['stderr_tail']}")
        out.append(f"[Job Object: {res.get('peak_job_memory_mb', 0)}MB peak RAM | {res.get('duration_ms', 0)}ms | E-Core mask: {res.get('affinity_mask')}]")
        return "\n\n".join(out)
    return f"Sandboxed execution {res.get('status')}: {res.get('stderr_tail') or res.get('reason')}"


@SkillRegistry.register(
    name="list_directory",
    description="Lists files and subdirectories in a given folder path within the codebase."
)
def list_directory(folder_path: str = ".") -> str:
    """Lists files and folders with size in directory."""
    target = _resolve_workspace_path(folder_path)
    if not target.exists():
        return f"Directory does not exist: {target}"
    try:
        items = []
        for p in sorted(target.iterdir()):
            if p.name in (".git", "__pycache__", ".pytest_cache"):
                continue
            if p.is_dir():
                items.append(f"[DIR]  {p.name}/")
            else:
                size_kb = p.stat().st_size / 1024.0
                items.append(f"[FILE] {p.name:<28} ({size_kb:.1f} KB)")
        return "\n".join(items[:60]) or "Directory is empty."
    except Exception as e:
        return f"Error listing directory: {e}"


@SkillRegistry.register(
    name="read_file",
    description="Reads lines from a file with 1-indexed line numbers. By default reads the full file (up to 1000 lines). Specify start_line and line_count to inspect a specific code slice."
)
def read_file(file_path: str, start_line: int = 1, line_count: int = 1000) -> str:
    """Reads lines from a file with line numbers."""
    target = _resolve_workspace_path(file_path)
    if not target.exists() or not target.is_file():
        return f"File does not exist: {target}"
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        total = len(all_lines)
        start_idx = max(1, start_line) - 1
        end_idx = min(total, start_idx + max(1, line_count))

        slice_lines = all_lines[start_idx:end_idx]
        formatted = [f"{start_idx + i + 1:>4}: {line.rstrip()}" for i, line in enumerate(slice_lines)]
        header = f"[{target.name} - Lines {start_idx + 1} to {end_idx} of {total}]"
        return header + "\n" + "\n".join(formatted)
    except Exception as e:
        return f"Error reading file: {e}"


@SkillRegistry.register(
    name="read_multiple_files",
    description="Reads several workspace files in one call. Pass comma-separated paths; output is bounded to protect the model's context window."
)
def read_multiple_files(
    file_paths: str,
    max_lines_per_file: int = 220,
    max_total_chars: int = 24000,
) -> str:
    """Reads several files in batch to minimize latency and round-trips."""
    paths = [p.strip() for p in file_paths.split(",") if p.strip()]
    if not paths:
        return "No file paths provided."

    sections = []
    remaining_chars = max(1000, min(max_total_chars, 50000))
    line_limit = max(1, min(max_lines_per_file, 1000))
    for fp in paths[:12]:
        if remaining_chars <= 0:
            sections.append("[Output limit reached; read the remaining files in another call.]")
            break
        target = _resolve_workspace_path(fp)
        if not target.exists() or not target.is_file():
            section = f"=== {fp} ===\n[File does not exist: {target}]"
            sections.append(section[:remaining_chars])
            remaining_chars -= len(section)
            continue
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                lines = []
                truncated = False
                for line in f:
                    if len(lines) >= line_limit:
                        truncated = True
                        break
                    lines.append(line)
            shown = lines
            formatted = [f"{i + 1:>4}: {line.rstrip()}" for i, line in enumerate(shown)]
            suffix = (
                f"\n... [additional lines omitted after the first {line_limit}; use read_file with start_line to continue]"
                if truncated else ""
            )
            section = f"=== {target.relative_to(WORKSPACE_DIR)} ({len(shown)} lines shown) ===\n" + "\n".join(formatted) + suffix
            if len(section) > remaining_chars:
                section = section[:remaining_chars] + "\n[File text truncated by the batch output limit.]"
            sections.append(section)
            remaining_chars -= len(section)
        except Exception as e:
            section = f"=== {fp} ===\n[Error reading file: {e}]"
            sections.append(section[:remaining_chars])
            remaining_chars -= len(section)

    return "\n\n".join(sections)


@SkillRegistry.register(
    name="write_file",
    description="Creates a new file or completely overwrites an existing file in the workspace with new content."
)
def write_file(file_path: str, content: str, overwrite: bool = False) -> str:
    """Writes content to a file in the workspace."""
    target = _resolve_workspace_path(file_path)
    if target.exists() and not overwrite:
        return f"File already exists at '{target}'. Set overwrite=True if you intend to replace it."

    try:
        backup_created = False
        if target.exists() and overwrite:
            backup_path = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(target, backup_path)
            backup_created = True

        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        lines = content.splitlines()
        backup_msg = f" (backup saved to {target.name}.bak)" if backup_created else ""
        return f"Successfully wrote {len(content)} bytes ({len(lines)} lines) to {target.name}{backup_msg}."
    except Exception as e:
        return f"Error writing file: {e}"


@SkillRegistry.register(
    name="patch_file",
    description="Surgically replaces a unique code snippet with new code inside an existing file."
)
def patch_file(file_path: str, target_snippet: str, replacement_snippet: str) -> str:
    """Replaces a unique target_snippet in a file with replacement_snippet."""
    target = _resolve_workspace_path(file_path)
    if not target.exists() or not target.is_file():
        return f"File does not exist: {target}"

    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            full_content = f.read()

        count = full_content.count(target_snippet)
        if count == 0:
            return f"Error: Target snippet was not found in '{target.name}'. Please ensure the snippet matches the existing file content exactly."
        if count > 1:
            return f"Error: Target snippet matched {count} times in '{target.name}'. Please include more surrounding lines to make the snippet unique."

        new_content = full_content.replace(target_snippet, replacement_snippet, 1)
        backup_path = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, backup_path)
        with open(target, "w", encoding="utf-8") as f:
            f.write(new_content)

        return f"Successfully patched '{target.name}'. Replaced {len(target_snippet.splitlines())} lines with {len(replacement_snippet.splitlines())} lines (backup saved to {target.name}.bak)."
    except Exception as e:
        return f"Error patching file: {e}"


@SkillRegistry.register(
    name="search_codebase",
    description="Searches source and project files including Python, JavaScript, TypeScript, Markdown, YAML, JSON, TOML, and PowerShell. Skips generated folders and secret .env files."
)
def search_codebase(query: str, folder_path: str = ".", file_pattern: str = "*", max_results: int = 40) -> str:
    """Searches for query in files matching file_pattern."""
    target_dir = _resolve_workspace_path(folder_path)
    if not target_dir.exists() or not target_dir.is_dir():
        return f"Directory does not exist: {target_dir}"
    query = query.strip()
    if not query:
        return "Search query cannot be empty."

    results = []
    pattern_re = re.compile(re.escape(query), re.IGNORECASE)
    ignored_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", "data", "node_modules", "dist", "build"}
    text_suffixes = {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".json", ".jsonc", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".md", ".txt", ".html", ".css", ".scss", ".sql", ".ps1",
        ".psm1", ".bat", ".sh", ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".go", ".rs",
        ".xml", ".svg",
    }
    result_limit = max(1, min(max_results, 100))

    try:
        for p in target_dir.rglob(file_pattern):
            try:
                relative = p.relative_to(WORKSPACE_DIR)
            except ValueError:
                continue
            if any(part in ignored_dirs for part in relative.parts):
                continue
            if not p.is_file():
                continue
            if p.name == ".env" or (p.name.startswith(".env.") and p.name != ".env.example"):
                continue
            if p.suffix.lower() not in text_suffixes or p.stat().st_size > 2_000_000:
                continue
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    for line_num, line in enumerate(f, 1):
                        if pattern_re.search(line):
                            results.append(f"{relative}:{line_num}: {line.strip()[:180]}")
                            if len(results) >= result_limit:
                                break
            except Exception:
                continue
            if len(results) >= result_limit:
                break

        if not results:
            return f"No matches found for '{query}' in {file_pattern} files."
        return f"Found {len(results)} matches for '{query}':\n" + "\n".join(results)
    except Exception as e:
        return f"Error searching codebase: {e}"


@SkillRegistry.register(
    name="git_diff",
    description="Reviews tracked edits in the Jarvis workspace against HEAD, including staged and unstaged changes. Output is capped for context."
)
def git_diff(max_chars: int = 12000) -> str:
    """Returns a bounded tracked-file diff for reviewing recent edits."""
    try:
        proc = subprocess.run(
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
    except Exception as e:
        return f"Git diff error: {e}"


@SkillRegistry.register(
    name="run_project_tests",
    description="Runs Python test suites in the tests/ directory to verify code changes."
)
def run_project_tests(test_file: str = "") -> str:
    """Executes tests using the current Python virtual environment."""
    py_exec = sys.executable
    target = f"tests/{test_file}".strip("/") if test_file else "tests"

    try:
        cmd = [py_exec, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"] if not test_file else [py_exec, f"tests/{test_file}"]
        proc = subprocess.run(
            cmd,
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (proc.stdout + "\n" + proc.stderr).strip()
        status = "PASSED" if proc.returncode == 0 else f"FAILED (Exit Code {proc.returncode})"
        return f"Test Run [{status}]:\n{out}"
    except subprocess.TimeoutExpired:
        return "Error: Test run timed out after 30 seconds."
    except Exception as e:
        return f"Error executing tests: {e}"


@SkillRegistry.register(
    name="git_status",
    description="Checks git repository status and current branch in the Jarvis workspace."
)
def git_status() -> str:
    """Returns git status and branch."""
    try:
        proc = subprocess.run(
            ["git", "status", "-s"],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=10,
        )
        branch_proc = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = branch_proc.stdout.strip() or "unknown"
        changes = proc.stdout.strip() or "Working tree clean (no uncommitted changes)."
        return f"Branch: {branch}\n\nWorking Tree Status:\n{changes}"
    except Exception as e:
        return f"Git error: {e}"


@SkillRegistry.register(
    name="run_background_task",
    description="Executes a long-running terminal command asynchronously in the background (builds, tests, compiles, downloads) and proactively notifies Sir when it completes."
)
def run_background_task(command: str, task_name: str = "", notify_voice: bool = True) -> str:
    """Spawns an asynchronous background task with HUD and completion notifications."""
    from jarvis.core.task_manager import BackgroundTaskManager
    mgr = BackgroundTaskManager.get_instance()
    success, msg, task_id = mgr.run_task(command=command, task_name=task_name, notify_voice=notify_voice)
    return msg


@SkillRegistry.register(
    name="list_background_tasks",
    description="Lists all currently running and recently completed background terminal tasks."
)
def list_background_tasks() -> str:
    """Lists active and recent background tasks with status and duration."""
    from jarvis.core.task_manager import BackgroundTaskManager
    mgr = BackgroundTaskManager.get_instance()
    tasks = mgr.list_tasks()
    if not tasks:
        return "No background tasks currently active or recorded, Sir."

    lines = ["Background Terminal Tasks, Sir:"]
    for t in tasks[-8:]:
        st = t["status"].upper()
        dur = f"{t['duration_seconds']}s"
        name = t["name"]
        t_id = t["id"]
        code = f" (exit: {t['exit_code']})" if t['exit_code'] is not None else ""
        lines.append(f"• [{st}] {name} (ID: {t_id}, Elapsed: {dur}){code}")
    return "\n".join(lines)


@SkillRegistry.register(
    name="get_background_task_output",
    description="Inspects the live or recent stdout/stderr output lines from a background task by ID."
)
def get_background_task_output(task_id: str, max_lines: int = 40) -> str:
    """Retrieves stdout/stderr tail for a specific background task."""
    from jarvis.core.task_manager import BackgroundTaskManager
    mgr = BackgroundTaskManager.get_instance()
    out = mgr.get_task_output(task_id=task_id, max_lines=max_lines)
    if out is None:
        return f"No background task with ID '{task_id}' was found, Sir."
    return out


@SkillRegistry.register(
    name="cancel_background_task",
    description="Terminates an actively running background task by task ID."
)
def cancel_background_task(task_id: str) -> str:
    """Cancels a running background task."""
    from jarvis.core.task_manager import BackgroundTaskManager
    mgr = BackgroundTaskManager.get_instance()
    cancelled = mgr.cancel_task(task_id=task_id)
    if cancelled:
        return f"Background task '{task_id}' has been terminated, Sir."
    return f"Unable to cancel task '{task_id}'. It may not exist or has already completed, Sir."

