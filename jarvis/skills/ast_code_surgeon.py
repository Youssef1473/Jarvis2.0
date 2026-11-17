"""Autonomous Diagnostic & Self-Healing AST Code Surgeon for Jarvis 2.0.

Parses traceback frames, localizes offending AST nodes in target source files,
applies surgical candidate patches, and verifies fixes inside an E-Core Win32
Job Object sandbox with automatic byte-for-byte rollback on regression.
"""
from __future__ import annotations

import ast
import contextlib
import difflib
import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
)
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
    except (OSError, UnicodeDecodeError) as e:
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


FORBIDDEN_PATCH_MODULES: frozenset[str] = frozenset({
    "jarvis/core/safety.py",
    "jarvis/core/security_boundaries.py",
    "jarvis/core/evolution/appcontainer.py",
    "jarvis/core/evolution/sandbox.py",
    "jarvis/skills/job_object_sandbox.py",
})

FORBIDDEN_PATCH_PREFIXES: tuple[str, ...] = (
    ".github/",
    "tools/",
    "tests/",
    "config/",
    ".venv/",
    ".git/",
)


def validate_target_patch_path(file_path: str | Path, workspace_root: str | Path = ".") -> Path:
    """Validates that candidate patch target path is safe and strictly bounded.

    Enforces:
    1. Must resolve inside workspace root.
    2. Must not contain traversal sequences ('..') or symlink escapes.
    3. Must not target forbidden security modules or governance/test/config directories.
    4. When in Jarvis workspace, must target files inside jarvis/.
    Raises SecurityBoundaryViolation on any violation.
    """
    ws = Path(workspace_root).resolve()
    target = Path(file_path)

    if not target.is_absolute():
        target = ws / target
    target = target.resolve()

    try:
        rel_path = target.relative_to(ws).as_posix()
    except ValueError as exc:
        raise SecurityBoundaryViolation(
            f"Path traversal detected: '{file_path}' resolves outside workspace '{ws}'."
        ) from exc

    if ".." in rel_path.split("/"):
        raise SecurityBoundaryViolation(
            f"Path traversal sequence detected in '{file_path}'."
        )

    # Check forbidden prefix directories
    if any(rel_path.startswith(prefix) for prefix in FORBIDDEN_PATCH_PREFIXES):
        raise SecurityBoundaryViolation(
            f"Target path '{rel_path}' is protected and cannot be modified by autonomous patching."
        )

    # Check forbidden security modules
    if rel_path in FORBIDDEN_PATCH_MODULES:
        raise SecurityBoundaryViolation(
            f"Security-critical module '{rel_path}' is strictly immutable and cannot be patched."
        )

    # In production Jarvis workspace (where jarvis/ exists), require target inside jarvis/
    if (ws / "jarvis").is_dir() and not rel_path.startswith("jarvis/"):
        raise SecurityBoundaryViolation(
            f"Target path '{rel_path}' must be inside 'jarvis/' subpackage."
        )

    return target


@dataclass
class EvolutionVerificationReport:
    """Structured report returned by sandboxed patch verification."""

    passed: bool
    file_path: str
    unified_diff: str
    lint_exit_code: int = 0
    diagnostics_exit_code: int = 0
    test_exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    elapsed_ms: float = 0.0
    error_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "file_path": self.file_path,
            "unified_diff": self.unified_diff,
            "lint_exit_code": self.lint_exit_code,
            "diagnostics_exit_code": self.diagnostics_exit_code,
            "test_exit_code": self.test_exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "elapsed_ms": self.elapsed_ms,
            "error_reason": self.error_reason,
        }


def create_shadow_patch(
    file_path: str | Path,
    node_start_line: int,
    node_end_line: int,
    replacement_code: str,
    shadow_root: str | Path,
    workspace_root: str | Path = ".",
) -> tuple[Path, str, str]:
    """Stages candidate AST patch in an isolated shadow directory and computes unified diff.

    Returns:
        (shadow_target_path, unified_diff, new_content)
    Raises:
        SyntaxError on candidate code syntax errors.
        SecurityBoundaryViolation on forbidden AST nodes or paths.
    """
    abs_target = validate_target_patch_path(file_path, workspace_root=workspace_root)
    ws = Path(workspace_root).resolve()
    rel_path = abs_target.relative_to(ws).as_posix()

    # Pre-validate syntax in memory (<0.1ms)
    ast.parse(replacement_code)

    # Deep AST security gate
    from jarvis.core.evolution.sandbox import SkillSandbox

    is_safe, sec_msg = SkillSandbox.check_ast_security(replacement_code)
    if not is_safe:
        raise SecurityBoundaryViolation(f"Candidate code failed AST security audit: {sec_msg}")

    with open(abs_target, encoding="utf-8") as f:
        orig_content = f.read()

    orig_lines = orig_content.splitlines(keepends=True)
    prefix = orig_lines[: max(0, node_start_line - 1)]
    suffix = orig_lines[node_end_line:]
    rep_formatted = replacement_code if replacement_code.endswith("\n") else replacement_code + "\n"
    new_content = "".join(prefix) + rep_formatted + "".join(suffix)

    # Compute deterministic unified diff
    new_lines = new_content.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            orig_lines,
            new_lines,
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="",
        )
    )
    unified_diff = "".join(diff_lines)

    # Stage in shadow directory
    shadow_target = Path(shadow_root) / rel_path
    shadow_target.parent.mkdir(parents=True, exist_ok=True)
    with open(shadow_target, "w", encoding="utf-8") as f:
        f.write(new_content)

    return shadow_target, unified_diff, new_content


def _run_sandboxed_command(
    command: list[str],
    cwd: str | Path,
    env: dict[str, str],
    timeout_sec: float = 15.0,
    grant_paths: list[str | Path] | None = None,
) -> tuple[int, str, str]:
    """Runs command inside SandboxedProcess with AppContainer if available, else Job Object / fallback."""
    from jarvis.skills.job_object_sandbox import SandboxedProcess

    use_appcontainer = False
    cfg = None
    if sys.platform == "win32":
        try:
            from jarvis.core.evolution.appcontainer import (
                AppContainerConfig,
                AppContainerProfileManager,
            )

            if AppContainerProfileManager.is_supported():
                use_appcontainer = True
                cfg = AppContainerConfig(
                    profile_name=f"J_Patch_{os.getpid()}_{time.time_ns()}",
                    auto_cleanup_profile=True,
                    grant_paths=list(grant_paths or []),
                )
        except (OSError, RuntimeError, ValueError, AttributeError) as e:
            logger.debug("AppContainer support check notice: %s", e)
            use_appcontainer = False

    try:
        proc_mgr = SandboxedProcess(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            use_appcontainer=use_appcontainer,
            appcontainer_config=cfg,
            max_memory_mb=1536,
            timeout=timeout_sec,
        )
        proc_mgr.start()
        stdout, stderr = proc_mgr.proc.communicate(timeout=timeout_sec)
        ret_code = proc_mgr.proc.returncode if proc_mgr.proc.returncode is not None else 0
        return ret_code, stdout or "", stderr or ""
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        logger.debug("SandboxedProcess execution exception: %s", exc)
        return 1, "", str(exc)


def verify_patch_in_appcontainer(
    shadow_target: Path,
    target_test_module: str,
    shadow_root: Path,
    workspace_root: Path | str = ".",
    timeout_sec: float = 15.0,
    unified_diff: str = "",
    file_path: str = "",
) -> EvolutionVerificationReport:
    """Executes 3-stage verification (linter, diagnostics, targeted test) in AppContainer sandbox."""
    start_t = time.perf_counter()
    ws = Path(workspace_root).resolve()

    # Build clean environment with stripped secrets
    env = os.environ.copy()
    for s_key in (
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "SLACK_BOT_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
    ):
        env.pop(s_key, None)

    # Prepend shadow root to PYTHONPATH so modified files load first
    orig_py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{shadow_root}{os.pathsep}{ws}{os.pathsep}{orig_py_path}".strip(os.pathsep)

    py_bin = sys.executable
    grant_paths = [shadow_root, ws]

    # Stage 1: Ruff Linter
    lint_cmd = [py_bin, "tools/lint.py", "check", str(shadow_target)]
    rc_lint, out_lint, err_lint = _run_sandboxed_command(
        lint_cmd, cwd=ws, env=env, timeout_sec=timeout_sec, grant_paths=grant_paths
    )
    if rc_lint != 0:
        elapsed = (time.perf_counter() - start_t) * 1000.0
        return EvolutionVerificationReport(
            passed=False,
            file_path=file_path or str(shadow_target),
            unified_diff=unified_diff,
            lint_exit_code=rc_lint,
            stdout=out_lint,
            stderr=err_lint,
            elapsed_ms=elapsed,
            error_reason=f"Stage 1 Ruff linter violation: {out_lint.strip() or err_lint.strip()}",
        )

    # Stage 2: Structural Diagnostics (AST-Grep)
    diag_cmd = [py_bin, "tools/diagnostics.py", "check", str(shadow_target)]
    rc_diag, out_diag, err_diag = _run_sandboxed_command(
        diag_cmd, cwd=ws, env=env, timeout_sec=timeout_sec, grant_paths=grant_paths
    )
    if rc_diag != 0:
        elapsed = (time.perf_counter() - start_t) * 1000.0
        return EvolutionVerificationReport(
            passed=False,
            file_path=file_path or str(shadow_target),
            unified_diff=unified_diff,
            lint_exit_code=0,
            diagnostics_exit_code=rc_diag,
            stdout=out_diag,
            stderr=err_diag,
            elapsed_ms=elapsed,
            error_reason=f"Stage 2 Structural diagnostics violation: {out_diag.strip() or err_diag.strip()}",
        )

    # Stage 3: Targeted Test Execution
    if (
        target_test_module.endswith(".py")
        or Path(target_test_module).exists()
        or (ws / target_test_module).exists()
    ):
        test_cmd = [py_bin, "-m", "pytest", target_test_module, "-q"]
    else:
        test_cmd = [py_bin, "-m", "unittest", target_test_module]

    rc_test, out_test, err_test = _run_sandboxed_command(
        test_cmd, cwd=ws, env=env, timeout_sec=timeout_sec, grant_paths=grant_paths
    )
    elapsed = (time.perf_counter() - start_t) * 1000.0
    if rc_test != 0:
        return EvolutionVerificationReport(
            passed=False,
            file_path=file_path or str(shadow_target),
            unified_diff=unified_diff,
            lint_exit_code=0,
            diagnostics_exit_code=0,
            test_exit_code=rc_test,
            stdout=out_test,
            stderr=err_test,
            elapsed_ms=elapsed,
            error_reason=f"Stage 3 Targeted test failure: {out_test.strip() or err_test.strip()}",
        )

    return EvolutionVerificationReport(
        passed=True,
        file_path=file_path or str(shadow_target),
        unified_diff=unified_diff,
        lint_exit_code=0,
        diagnostics_exit_code=0,
        test_exit_code=0,
        stdout=out_test,
        stderr=err_test,
        elapsed_ms=elapsed,
        error_reason=None,
    )


def promote_verified_patch(
    file_path: str | Path,
    replacement_code: str,
    node_start_line: int,
    node_end_line: int,
    user_approved: bool = False,
    workspace_root: str | Path = ".",
) -> dict[str, Any]:
    """Promotes a verified patch to the live repository guarded by DualKeyApprovalManager."""
    abs_target = validate_target_patch_path(file_path, workspace_root=workspace_root)

    approval_mgr = DualKeyApprovalManager.get_instance()
    is_human_authorized = approval_mgr.verify_and_consume_active_context(
        "promote_verified_patch",
        {
            "file_path": str(file_path),
            "node_start_line": node_start_line,
            "node_end_line": node_end_line,
        },
    ) or user_approved

    if not is_human_authorized:
        return {
            "status": "rejected_unauthorized",
            "error": "Security Boundary Violation: Live code promotion requires explicit human authorization.",
            "promoted": False,
        }

    with open(abs_target, encoding="utf-8") as f:
        orig_content = f.read()

    orig_lines = orig_content.splitlines(keepends=True)
    prefix = orig_lines[: max(0, node_start_line - 1)]
    suffix = orig_lines[node_end_line:]
    rep_formatted = replacement_code if replacement_code.endswith("\n") else replacement_code + "\n"
    new_content = "".join(prefix) + rep_formatted + "".join(suffix)

    file_dir = os.path.dirname(abs_target)
    fd, tmp_path = tempfile.mkstemp(dir=file_dir, prefix="patch_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(new_content)
        os.replace(tmp_path, abs_target)
        logger.info("Promoted verified patch to live file '%s'", file_path)
        return {
            "status": "promoted",
            "file_path": str(file_path),
            "promoted": True,
        }
    except (OSError, UnicodeEncodeError) as exc:
        if os.path.exists(tmp_path):
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
        return {
            "status": "error",
            "error": f"Failed to promote verified patch: {exc}",
            "promoted": False,
        }


@dataclass
class PatchProposal:
    """Represents a verified AST patch proposal awaiting human operator promotion."""

    proposal_id: str
    file_path: str
    node_start_line: int
    node_end_line: int
    replacement_code: str
    target_test_module: str
    sha256_before: str
    sha256_shadow: str
    unified_diff: str
    report: EvolutionVerificationReport
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 900.0
    status: str = "pending"  # "pending" | "approved" | "rejected" | "expired" | "conflict"
    rejection_reason: str | None = None
    workspace_root: str = "."

    @property
    def is_expired(self) -> bool:
        return time.time() > (self.created_at + self.ttl_seconds)

    @property
    def expires_in(self) -> float:
        return max(0.0, (self.created_at + self.ttl_seconds) - time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "file_path": self.file_path,
            "node_start_line": self.node_start_line,
            "node_end_line": self.node_end_line,
            "sha256_before": self.sha256_before,
            "sha256_shadow": self.sha256_shadow,
            "unified_diff": self.unified_diff,
            "report": self.report.to_dict() if hasattr(self.report, "to_dict") else getattr(self.report, "__dict__", {}),
            "created_at": self.created_at,
            "expires_in": self.expires_in,
            "status": "expired" if self.status == "pending" and self.is_expired else self.status,
            "rejection_reason": self.rejection_reason,
            "workspace_root": self.workspace_root,
        }


class PatchProposalRegistry:
    """Thread-safe bounded in-memory registry for AppContainer verified code patch proposals."""

    def __init__(self, max_proposals: int = 50, ttl_seconds: float = 900.0) -> None:
        self.max_proposals = max_proposals
        self.ttl_seconds = ttl_seconds
        self._proposals: dict[str, PatchProposal] = {}
        self._lock = threading.Lock()

    def register_proposal(
        self,
        file_path: str | Path,
        node_start_line: int,
        node_end_line: int,
        replacement_code: str,
        target_test_module: str,
        sha256_before: str,
        sha256_shadow: str,
        unified_diff: str,
        report: EvolutionVerificationReport,
        proposal_id: str | None = None,
        ttl_seconds: float | None = None,
        workspace_root: str | Path = ".",
    ) -> PatchProposal:
        """Validates path safety and stores a verified candidate patch proposal."""
        validate_target_patch_path(file_path, workspace_root=workspace_root)

        pid = proposal_id or f"prop_{uuid.uuid4().hex[:12]}"
        ttl = float(ttl_seconds if ttl_seconds is not None else self.ttl_seconds)

        proposal = PatchProposal(
            proposal_id=pid,
            file_path=str(file_path),
            node_start_line=node_start_line,
            node_end_line=node_end_line,
            replacement_code=replacement_code,
            target_test_module=target_test_module,
            sha256_before=sha256_before,
            sha256_shadow=sha256_shadow,
            unified_diff=unified_diff,
            report=report,
            ttl_seconds=ttl,
            workspace_root=str(workspace_root),
        )

        with self._lock:
            # Prune expired proposals if approaching capacity
            if len(self._proposals) >= self.max_proposals:
                expired_keys = [
                    k for k, p in self._proposals.items()
                    if p.is_expired or p.status in ("approved", "rejected", "conflict")
                ]
                for k in expired_keys:
                    self._proposals.pop(k, None)

            # If still at max capacity, evict oldest by created_at
            if len(self._proposals) >= self.max_proposals:
                oldest_key = min(self._proposals.keys(), key=lambda k: self._proposals[k].created_at)
                self._proposals.pop(oldest_key, None)

            self._proposals[pid] = proposal

        logger.info("Registered patch proposal '%s' for '%s' (TTL: %.0fs)", pid, file_path, ttl)
        return proposal

    def get_proposal(self, proposal_id: str) -> PatchProposal | None:
        """Retrieves proposal by id, updating expired state lazily."""
        with self._lock:
            p = self._proposals.get(proposal_id)
            if p is not None and p.status == "pending" and p.is_expired:
                p.status = "expired"
            return p

    def list_proposals(self, include_resolved: bool = False, limit: int = 50) -> list[PatchProposal]:
        """Lists proposals filtered by active unexpired status (or all if include_resolved=True)."""
        with self._lock:
            res: list[PatchProposal] = []
            for p in self._proposals.values():
                if p.status == "pending" and p.is_expired:
                    p.status = "expired"
                if include_resolved or p.status == "pending":
                    res.append(p)
            res.sort(key=lambda x: x.created_at, reverse=True)
            return res[:limit]

    def get_pending_count(self) -> int:
        """Returns the number of currently pending unexpired proposals."""
        with self._lock:
            count = 0
            for p in self._proposals.values():
                if p.status == "pending" and p.is_expired:
                    p.status = "expired"
                if p.status == "pending":
                    count += 1
            return count

    def approve_proposal(
        self,
        proposal_id: str,
        user_approved: bool = True,
        workspace_root: str | Path | None = None,
    ) -> dict[str, Any]:
        """Approves and promotes verified proposal guarded by optimistic concurrency (sha256_before check)."""
        with self._lock:
            p = self._proposals.get(proposal_id)
            if not p:
                return {"status": "not_found", "error": f"Proposal '{proposal_id}' not found."}
            if p.is_expired:
                p.status = "expired"
                return {"status": "expired", "error": f"Proposal '{proposal_id}' has expired."}
            if p.status != "pending":
                return {
                    "status": "invalid_status",
                    "error": f"Proposal status is '{p.status}', expected 'pending'.",
                }

        ws_root = workspace_root if workspace_root is not None else getattr(p, "workspace_root", ".")
        abs_target = validate_target_patch_path(p.file_path, workspace_root=ws_root)
        if not os.path.isfile(abs_target):
            return {"status": "error", "error": f"Target file '{p.file_path}' does not exist on disk."}

        # Compute live disk SHA-256 for optimistic concurrency guard
        try:
            with open(abs_target, "rb") as f:
                current_sha256 = hashlib.sha256(f.read()).hexdigest()
        except OSError as e:
            return {"status": "error", "error": f"Failed reading live file '{p.file_path}': {e}"}

        if current_sha256 != p.sha256_before:
            with self._lock:
                p.status = "conflict"
            logger.warning(
                "Optimistic concurrency conflict on '%s': expected sha256=%s, actual=%s",
                p.file_path,
                p.sha256_before,
                current_sha256,
            )
            return {
                "status": "conflict",
                "error": f"Optimistic concurrency conflict: live file '{p.file_path}' has drifted on disk.",
                "sha256_expected": p.sha256_before,
                "sha256_actual": current_sha256,
            }

        # Record human authorization in DualKeyApprovalManager
        approval_mgr = DualKeyApprovalManager.get_instance()
        with contextlib.suppress(Exception):
            approval_mgr.grant_approval(
                "promote_verified_patch",
                {
                    "file_path": str(p.file_path),
                    "node_start_line": p.node_start_line,
                    "node_end_line": p.node_end_line,
                },
                summary=f"HUD Operator approved evolution patch {proposal_id} for {p.file_path}",
                ttl_seconds=60.0,
            )

        # Atomic promotion
        promote_res = promote_verified_patch(
            file_path=p.file_path,
            replacement_code=p.replacement_code,
            node_start_line=p.node_start_line,
            node_end_line=p.node_end_line,
            user_approved=user_approved,
            workspace_root=ws_root,
        )

        if promote_res.get("status") == "promoted":
            with self._lock:
                p.status = "approved"
            with open(abs_target, "rb") as f_new:
                new_sha256 = hashlib.sha256(f_new.read()).hexdigest()
            return {
                "status": "approved",
                "proposal_id": proposal_id,
                "file_path": p.file_path,
                "promoted": True,
                "sha256_before": p.sha256_before,
                "sha256_after": new_sha256,
            }

        return promote_res

    def reject_proposal(self, proposal_id: str, reason: str = "") -> dict[str, Any]:
        """Rejects a pending proposal with an optional operator rationale."""
        with self._lock:
            p = self._proposals.get(proposal_id)
            if not p:
                return {"status": "not_found", "error": f"Proposal '{proposal_id}' not found."}
            if p.is_expired:
                p.status = "expired"
                return {"status": "expired", "error": f"Proposal '{proposal_id}' has expired."}
            if p.status != "pending":
                return {
                    "status": "invalid_status",
                    "error": f"Proposal status is '{p.status}', expected 'pending'.",
                }

            p.status = "rejected"
            p.rejection_reason = reason or "Rejected by operator"
            return {
                "status": "rejected",
                "proposal_id": proposal_id,
                "reason": p.rejection_reason,
            }

    def reset(self) -> None:
        """Clears all stored proposals (used for hermetic test isolation)."""
        with self._lock:
            self._proposals.clear()


_GLOBAL_PROPOSAL_REGISTRY: PatchProposalRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_proposal_registry() -> PatchProposalRegistry:
    """Returns the singleton PatchProposalRegistry instance."""
    global _GLOBAL_PROPOSAL_REGISTRY
    if _GLOBAL_PROPOSAL_REGISTRY is None:
        with _REGISTRY_LOCK:
            if _GLOBAL_PROPOSAL_REGISTRY is None:
                _GLOBAL_PROPOSAL_REGISTRY = PatchProposalRegistry()
    return _GLOBAL_PROPOSAL_REGISTRY


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

    try:
        validate_target_patch_path(file_path, workspace_root=workspace_root)
    except SecurityBoundaryViolation as sec_err:
        return {
            "status": "rejected_security_violation",
            "error": str(sec_err),
            "rolled_back": False,
        }

    # 2. Snapshot original file content in memory
    try:
        with open(abs_path, encoding="utf-8") as f:
            orig_content = f.read()
    except (OSError, UnicodeDecodeError) as e:
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
    except (OSError, UnicodeEncodeError) as w_err:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as rm_err:
                logger.debug("Failed removing candidate patch tempfile: %s", rm_err)
        return {"status": "error", "error": f"Failed to write candidate patch: {w_err}", "rolled_back": False}

    # Helper function to rollback byte-for-byte
    def _rollback() -> None:
        try:
            with open(abs_path, "w", encoding="utf-8") as rb_file:
                rb_file.write(orig_content)
            logger.info("Rolled back surgical patch on '%s' byte-for-byte.", file_path)
        except OSError as rb_err:
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

    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exec_err:
        _rollback()
        return {
            "status": "rejected_execution_error",
            "error": str(exec_err),
            "rolled_back": True,
        }
