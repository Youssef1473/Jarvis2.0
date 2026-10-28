"""Autonomous Diagnostic & Self-Healing AST Code Surgeon for Jarvis 2.0.

Parses traceback frames, localizes offending AST nodes in target source files,
applies surgical candidate patches, and verifies fixes inside an E-Core Win32
Job Object sandbox with automatic byte-for-byte rollback on regression.
"""
from __future__ import annotations

import ast
import logging
import os
import re
import sys
import tempfile
from typing import Any

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Skills.ASTSurgeon")

_FRAME_REGEX = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\w+))?')
_EXC_REGEX = re.compile(r'^([A-Z][a-zA-Z0-9_]*(?:Error|Exception|Warning|Interrupt|Fault|Exit)): (.*)$')


def parse_traceback_frames(traceback_text: str, workspace_root: str | None = None) -> dict[str, Any]:
    """Parses python traceback text and isolates workspace frames, filtering standard libraries."""
    if not traceback_text:
        return {"frames": [], "exception_type": "", "exception_message": "", "innermost_frame": None}

    raw_lines = [l.strip() for l in traceback_text.strip().splitlines() if l.strip()]

    # Extract exception type and message from trailing lines
    exc_type = ""
    exc_msg = ""
    for line in reversed(raw_lines):
        m = _EXC_REGEX.match(line)
        if m:
            exc_type = m.group(1)
            exc_msg = m.group(2)
            break
        elif ": " in line:
            parts = line.split(": ", 1)
            if parts[0].endswith(("Error", "Exception")):
                exc_type = parts[0]
                exc_msg = parts[1]
                break

    frames: list[dict[str, Any]] = []
    ws_real = os.path.realpath(os.path.abspath(workspace_root)) if workspace_root else None

    for match in _FRAME_REGEX.finditer(traceback_text):
        f_path = match.group(1).strip()
        line_num = int(match.group(2))
        func_name = match.group(3) or "<module>"

        # Filter out virtual environments and stdlib
        norm_path = f_path.replace("\\", "/").lower()
        if (
            "/.venv/" in norm_path
            or "/site-packages/" in norm_path
            or "/python3" in norm_path
            or "/lib/unittest/" in norm_path
        ):
            continue

        real_f = os.path.realpath(os.path.abspath(f_path)) if os.path.exists(f_path) else f_path
        is_ws = True
        if ws_real:
            is_ws = real_f.startswith(ws_real) or os.path.exists(f_path)

        frames.append({
            "file_path": f_path,
            "line_number": line_num,
            "func_name": func_name,
            "is_workspace": is_ws,
        })

    ws_frames = [f for f in frames if f.get("is_workspace")]
    innermost = ws_frames[-1] if ws_frames else (frames[-1] if frames else None)

    return {
        "frames": frames,
        "exception_type": exc_type,
        "exception_message": exc_msg,
        "innermost_frame": innermost,
    }


def localize_ast_fault_node(file_path: str, target_line: int) -> dict[str, Any]:
    """Isolates the innermost AST FunctionDef, AsyncFunctionDef, or ClassDef node spanning target_line."""
    if not os.path.isfile(file_path):
        return {"status": "error", "error": f"File '{file_path}' does not exist"}

    try:
        with open(file_path, encoding="utf-8") as f:
            source = f.read()
        lines = source.splitlines()
    except Exception as e:
        return {"status": "error", "error": f"Could not read '{file_path}': {e}"}

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as syn_err:
        return {"status": "error", "error": f"File '{file_path}' has invalid syntax: {syn_err}"}

    candidates = []

    for node in ast.walk(tree):
        if not hasattr(node, "lineno"):
            continue

        # Account for decorators in start_line
        start_line = node.lineno
        if hasattr(node, "decorator_list") and node.decorator_list:
            start_line = min(d.lineno for d in node.decorator_list)

        end_line = getattr(node, "end_lineno", node.lineno)

        if start_line <= target_line <= end_line:
            span = end_line - start_line
            # Prioritize functions, methods, and classes
            priority = 0
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                priority = 2
            elif isinstance(node, ast.ClassDef):
                priority = 1

            candidates.append((priority, span, start_line, end_line, node))

    if not candidates:
        # Fall back to the line itself
        start_line = max(1, target_line)
        end_line = min(len(lines), target_line)
        node_source = lines[target_line - 1] if 1 <= target_line <= len(lines) else ""
        return {
            "status": "ok",
            "file_path": file_path,
            "target_line": target_line,
            "node_type": "Line",
            "node_name": "<line>",
            "node_start_line": start_line,
            "node_end_line": end_line,
            "node_source": node_source,
            "local_symbols": [],
        }

    # Sort by priority desc, then span asc (innermost node)
    candidates.sort(key=lambda c: (-c[0], c[1]))
    _, _, best_start, best_end, best_node = candidates[0]

    node_source = "\n".join(lines[best_start - 1 : best_end])
    local_symbols = sorted({n.id for n in ast.walk(best_node) if isinstance(n, ast.Name)})

    return {
        "status": "ok",
        "file_path": file_path,
        "target_line": target_line,
        "node_type": type(best_node).__name__,
        "node_name": getattr(best_node, "name", "<anonymous>"),
        "node_start_line": best_start,
        "node_end_line": best_end,
        "node_source": node_source,
        "local_symbols": local_symbols,
    }


@SkillRegistry.register(
    name="diagnose_test_failure_ast",
    description="Parses traceback output, isolates the failing frame, and localizes the exact AST function or block bounds.",
    risk_level=0,
    read_only=True,
)
def diagnose_test_failure_ast(traceback_output: str, workspace_root: str = ".") -> dict[str, Any]:
    """Diagnoses a test failure traceback and pinpoints the AST code node to surgical precision."""
    parsed = parse_traceback_frames(traceback_output, workspace_root=workspace_root)
    innermost = parsed.get("innermost_frame")

    if not innermost:
        return {
            "status": "error",
            "error": "No relevant traceback frame could be isolated.",
            "exception_type": parsed.get("exception_type"),
            "exception_message": parsed.get("exception_message"),
        }

    fault = localize_ast_fault_node(innermost["file_path"], innermost["line_number"])
    if fault.get("status") != "ok":
        return fault

    return {
        "status": "ok",
        "file_path": innermost["file_path"],
        "line_number": innermost["line_number"],
        "func_name": innermost["func_name"],
        "exception_type": parsed.get("exception_type"),
        "exception_message": parsed.get("exception_message"),
        "node_type": fault.get("node_type"),
        "node_name": fault.get("node_name"),
        "node_start_line": fault.get("node_start_line"),
        "node_end_line": fault.get("node_end_line"),
        "node_source": fault.get("node_source"),
        "local_symbols": fault.get("local_symbols"),
    }


@SkillRegistry.register(
    name="apply_and_verify_surgical_patch",
    description="Applies a surgical AST node patch and verifies it inside an isolated sandbox, automatically rolling back on test failure.",
    risk_level=1,
)
def apply_and_verify_surgical_patch(
    file_path: str,
    node_start_line: int,
    node_end_line: int,
    replacement_code: str,
    target_test_module: str,
    workspace_root: str = ".",
    regression_test_modules: list[str] | None = None,
    timeout_sec: float = 5.0,
) -> dict[str, Any]:
    """Applies candidate replacement code and verifies tests in a Win32 Job Object sandbox with automatic rollback."""
    # 1. Pre-validate syntax in memory in <0.1ms
    try:
        ast.parse(replacement_code)
    except SyntaxError as syn_err:
        logger.warning("Surgical patch candidate rejected due to syntax error: %s", syn_err)
        return {
            "status": "rejected_syntax_error",
            "error": f"Candidate replacement code has invalid syntax: {syn_err}",
            "rolled_back": False,
        }

    abs_path = os.path.abspath(file_path)
    if not os.path.isfile(abs_path):
        return {"status": "error", "error": f"Target file '{file_path}' does not exist.", "rolled_back": False}

    # 2. Snapshot original file content in memory
    try:
        with open(abs_path, encoding="utf-8") as f:
            orig_content = f.read()
    except Exception as e:
        return {"status": "error", "error": f"Failed to read original file '{file_path}': {e}", "rolled_back": False}

    orig_lines = orig_content.splitlines(keepends=True)

    # 3. Splice replacement lines
    prefix = orig_lines[: max(0, node_start_line - 1)]
    suffix = orig_lines[node_end_line:]

    # Ensure replacement code has trailing newline matching source format
    rep_formatted = replacement_code if replacement_code.endswith("\n") else replacement_code + "\n"
    new_content = "".join(prefix) + rep_formatted + "".join(suffix)

    # Atomic write to file
    file_dir = os.path.dirname(abs_path)
    fd, tmp_path = tempfile.mkstemp(dir=file_dir, prefix="patch_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(new_content)
        os.replace(tmp_path, abs_path)
    except Exception as w_err:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return {"status": "error", "error": f"Failed to write candidate patch: {w_err}", "rolled_back": False}

    # Helper function to rollback byte-for-byte
    def _rollback() -> None:
        try:
            with open(abs_path, "w", encoding="utf-8") as rb_file:
                rb_file.write(orig_content)
            logger.info("Rolled back surgical patch on '%s' byte-for-byte.", file_path)
        except Exception as rb_err:
            logger.error("Critical failure rolling back patch on '%s': %s", file_path, rb_err)

    # 4. Execute target test in sandboxed Win32 Job Object
    try:
        from jarvis.skills.job_object_sandbox import run_job_sandboxed_process
        target_cmd = [sys.executable, "-m", "unittest", target_test_module]
        res = run_job_sandboxed_process(
            command=target_cmd,
            cwd=workspace_root,
            timeout_sec=timeout_sec,
            requested_affinity=0xFF00,
        )

        if res.get("status") != "ok" or res.get("exit_code") != 0:
            _rollback()
            return {
                "status": "rejected_target_test_failed",
                "rolled_back": True,
                "exit_code": res.get("exit_code"),
                "stdout_tail": res.get("stdout_tail"),
                "stderr_tail": res.get("stderr_tail"),
            }

        # 5. Execute regression test modules if specified
        if regression_test_modules:
            for reg_mod in regression_test_modules:
                reg_cmd = [sys.executable, "-m", "unittest", reg_mod]
                reg_res = run_job_sandboxed_process(
                    command=reg_cmd,
                    cwd=workspace_root,
                    timeout_sec=timeout_sec,
                    requested_affinity=0xFF00,
                )
                if reg_res.get("status") != "ok" or reg_res.get("exit_code") != 0:
                    _rollback()
                    return {
                        "status": "rejected_regression_failed",
                        "rolled_back": True,
                        "failed_module": reg_mod,
                        "exit_code": reg_res.get("exit_code"),
                        "stdout_tail": reg_res.get("stdout_tail"),
                        "stderr_tail": reg_res.get("stderr_tail"),
                    }

        # 6. All tests verified green! Commit cleanly
        logger.info("Surgical patch committed successfully on '%s' (verified via %s)", file_path, target_test_module)
        return {
            "status": "committed",
            "rolled_back": False,
            "file_path": file_path,
            "target_test": target_test_module,
        }

    except Exception as exec_err:
        _rollback()
        return {
            "status": "rejected_execution_error",
            "error": str(exec_err),
            "rolled_back": True,
        }
