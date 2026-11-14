"""File viewing, editing, patching, and codebase search tools registered in SkillRegistry."""
from __future__ import annotations

import hmac
import logging
import os
import re
import shutil

from jarvis.skills.base import SkillRegistry
from jarvis.skills.fs_confinement import (
    WORKSPACE_DIR,
    _compute_fd_sha256,
    _compute_file_sha256,
    _is_reparse_point_or_symlink,
    _resolve_workspace_path,
    open_handle_bound_workspace_file,
)

logger = logging.getLogger("Jarvis.Skills.FsOperations")


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
    except (OSError, ValueError) as e:
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
        with open(target, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        total = len(all_lines)
        start_idx = max(1, start_line) - 1
        end_idx = min(total, start_idx + max(1, line_count))

        slice_lines = all_lines[start_idx:end_idx]
        formatted = [f"{start_idx + i + 1:>4}: {line.rstrip()}" for i, line in enumerate(slice_lines)]
        header = f"[{target.name} - Lines {start_idx + 1} to {end_idx} of {total}]"
        return header + "\n" + "\n".join(formatted)
    except (OSError, UnicodeDecodeError, ValueError) as e:
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
        try:
            target = _resolve_workspace_path(fp)
        except ValueError as err:
            section = f"=== {fp} ===\n[Security Violation: {err}]"
            sections.append(section[:remaining_chars])
            remaining_chars -= len(section)
            continue
        if not target.exists() or not target.is_file():
            section = f"=== {fp} ===\n[File does not exist: {target}]"
            sections.append(section[:remaining_chars])
            remaining_chars -= len(section)
            continue
        try:
            with open(target, encoding="utf-8", errors="replace") as f:
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
        except (OSError, UnicodeDecodeError, ValueError) as e:
            section = f"=== {fp} ===\n[Error reading file: {e}]"
            sections.append(section[:remaining_chars])
            remaining_chars -= len(section)

    return "\n\n".join(sections)


@SkillRegistry.register(
    name="write_file",
    description="Creates a new file or completely overwrites an existing file in the workspace with new content.",
    risk_level=2,
)
def write_file(
    file_path: str,
    content: str,
    overwrite: bool = False,
    preimage_digest: str | None = None,
) -> str:
    """Writes content to a file in the workspace."""
    try:
        target = _resolve_workspace_path(file_path, for_write=True)
    except ValueError as val_err:
        return f"Error writing file: {val_err}"

    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        _active_auth_context,
        compute_action_hash,
    )

    active_token = _active_auth_context.get()
    if active_token is None or not active_token.is_valid:
        raise SecurityBoundaryViolation(
            "Authorization Required: write_file requires explicit human approval."
        )

    if active_token.tool_name != "write_file":
        raise SecurityBoundaryViolation(
            f"Authorization Mismatch: Active authorization is for '{active_token.tool_name}', not 'write_file'."
        )

    norm_path = str(file_path).replace("\\", "/")
    candidate_paths = [file_path]
    if norm_path != file_path:
        candidate_paths.append(norm_path)

    candidate_args = []
    for p in candidate_paths:
        if preimage_digest is not None:
            candidate_args.append({
                "file_path": p,
                "content": content,
                "overwrite": overwrite,
                "preimage_digest": preimage_digest,
            })
        candidate_args.append({"file_path": p, "content": content, "overwrite": overwrite})
        if overwrite is False:
            if preimage_digest is not None:
                candidate_args.append({"file_path": p, "content": content, "preimage_digest": preimage_digest})
            candidate_args.append({"file_path": p, "content": content})

    matched_hash = None
    for cand in candidate_args:
        cand_hash = compute_action_hash("write_file", cand)
        if hmac.compare_digest(active_token.action_hash, cand_hash):
            matched_hash = cand_hash
            break

    if matched_hash is None:
        raise SecurityBoundaryViolation(
            "Action Hash Mismatch: File path, content, or overwrite arguments do not match approved action hash."
        )

    if not active_token.consume("write_file", matched_hash):
        raise SecurityBoundaryViolation(
            "Single-Use Authorization Violation: write_file authorization grant was already consumed or invalidated."
        )

    if _is_reparse_point_or_symlink(target):
        raise SecurityBoundaryViolation(
            f"Security Boundary Violation: Destination path '{target.name}' is a symbolic link or reparse point; file mutation rejected."
        )

    if not overwrite:
        if target.exists():
            raise SecurityBoundaryViolation(
                f"Preimage Integrity Violation: Destination file '{target.name}' already exists when creating new file. Unexpected file creation rejected."
            )
    else:
        if preimage_digest is not None:
            if not target.exists():
                raise SecurityBoundaryViolation(
                    f"Preimage Integrity Violation: Target file '{target.name}' was unexpectedly deleted before overwrite."
                )
            live_digest = _compute_file_sha256(target)
            if live_digest != preimage_digest:
                raise SecurityBoundaryViolation(
                    f"Preimage Integrity Violation: Destination file '{target.name}' content was modified after approval (expected {preimage_digest}, got {live_digest}). Overwrite rejected."
                )

    try:
        backup_created = False
        if target.exists() and overwrite:
            backup_path = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(target, backup_path)
            backup_created = True

        with open_handle_bound_workspace_file(target, overwrite=overwrite, must_exist=False) as (fd, was_created):
            if overwrite and not was_created:
                if preimage_digest is not None:
                    live_digest = _compute_fd_sha256(fd)
                    if live_digest != preimage_digest:
                        raise SecurityBoundaryViolation(
                            f"Preimage Integrity Violation: Destination file '{target.name}' content was modified after approval (expected {preimage_digest}, got {live_digest}). Overwrite rejected."
                        )
            elif overwrite and was_created and preimage_digest is not None:
                raise SecurityBoundaryViolation(
                    f"Preimage Integrity Violation: Target file '{target.name}' was unexpectedly deleted before overwrite."
                )

            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.truncate(0)
                f.write(content)

        lines = content.splitlines()
        backup_msg = f" (backup saved to {target.name}.bak)" if backup_created else ""
        return f"Successfully wrote {len(content)} bytes ({len(lines)} lines) to {target.name}{backup_msg}."
    except SecurityBoundaryViolation:
        raise
    except (OSError, UnicodeEncodeError, ValueError) as e:
        return f"Error writing file: {e}"


@SkillRegistry.register(
    name="patch_file",
    description="Surgically replaces a unique code snippet with new code inside an existing file.",
    risk_level=2,
)
def patch_file(
    file_path: str,
    target_snippet: str,
    replacement_snippet: str,
    preimage_digest: str | None = None,
) -> str:
    """Replaces a unique target_snippet in a file with replacement_snippet."""
    try:
        target = _resolve_workspace_path(file_path, for_write=True)
    except ValueError as val_err:
        return f"Error patching file: {val_err}"

    from jarvis.core.security_boundaries import (
        SecurityBoundaryViolation,
        _active_auth_context,
        compute_action_hash,
    )

    active_token = _active_auth_context.get()
    if active_token is None or not active_token.is_valid:
        raise SecurityBoundaryViolation(
            "Authorization Required: patch_file requires explicit human approval."
        )

    if active_token.tool_name != "patch_file":
        raise SecurityBoundaryViolation(
            f"Authorization Mismatch: Active authorization is for '{active_token.tool_name}', not 'patch_file'."
        )

    norm_path = str(file_path).replace("\\", "/")
    candidate_paths = [file_path]
    if norm_path != file_path:
        candidate_paths.append(norm_path)

    candidate_args = []
    for p in candidate_paths:
        if preimage_digest is not None:
            candidate_args.append({
                "file_path": p,
                "target_snippet": target_snippet,
                "replacement_snippet": replacement_snippet,
                "preimage_digest": preimage_digest,
            })
        candidate_args.append({
            "file_path": p,
            "target_snippet": target_snippet,
            "replacement_snippet": replacement_snippet,
        })

    matched_hash = None
    for cand in candidate_args:
        cand_hash = compute_action_hash("patch_file", cand)
        if hmac.compare_digest(active_token.action_hash, cand_hash):
            matched_hash = cand_hash
            break

    if matched_hash is None:
        raise SecurityBoundaryViolation(
            "Action Hash Mismatch: File path or snippet arguments do not match approved action hash."
        )

    if not active_token.consume("patch_file", matched_hash):
        raise SecurityBoundaryViolation(
            "Single-Use Authorization Violation: patch_file authorization grant was already consumed or invalidated."
        )

    if _is_reparse_point_or_symlink(target):
        raise SecurityBoundaryViolation(
            f"Security Boundary Violation: Destination path '{target.name}' is a symbolic link or reparse point; file mutation rejected."
        )

    if not target.exists() or not target.is_file():
        return f"File does not exist: {target}"

    if preimage_digest is not None:
        live_digest = _compute_file_sha256(target)
        if live_digest != preimage_digest:
            raise SecurityBoundaryViolation(
                f"Preimage Integrity Violation: File '{target.name}' content was modified after approval (expected {preimage_digest}, got {live_digest}). Patch rejected."
            )

    try:
        backup_created = False
        if target.exists():
            backup_path = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(target, backup_path)
            backup_created = True

        with open_handle_bound_workspace_file(target, overwrite=True, must_exist=True) as (fd, _was_created):
            if preimage_digest is not None:
                live_digest = _compute_fd_sha256(fd)
                if live_digest != preimage_digest:
                    raise SecurityBoundaryViolation(
                        f"Preimage Integrity Violation: File '{target.name}' content was modified after approval (expected {preimage_digest}, got {live_digest}). Patch rejected."
                    )

            with os.fdopen(fd, "r+", encoding="utf-8", errors="replace") as f:
                full_content = f.read()
                count = full_content.count(target_snippet)
                if count == 0:
                    return f"Error: Target snippet was not found in '{target.name}'. Please ensure the snippet matches the existing file content exactly."
                if count > 1:
                    return f"Error: Target snippet matched {count} times in '{target.name}'. Please include more surrounding lines to make the snippet unique."

                new_content = full_content.replace(target_snippet, replacement_snippet, 1)
                f.seek(0)
                f.truncate(0)
                f.write(new_content)

        backup_msg = f" (backup saved to {target.name}.bak)" if backup_created else ""
        return f"Successfully patched '{target.name}'. Replaced {len(target_snippet.splitlines())} lines with {len(replacement_snippet.splitlines())} lines{backup_msg}."
    except FileNotFoundError:
        return f"File does not exist: {target}"
    except SecurityBoundaryViolation:
        raise
    except (OSError, ValueError) as e:
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
                with open(p, encoding="utf-8", errors="replace") as f:
                    for line_num, line in enumerate(f, 1):
                        if pattern_re.search(line):
                            results.append(f"{relative}:{line_num}: {line.strip()[:180]}")
                            if len(results) >= result_limit:
                                break
            except (OSError, UnicodeDecodeError) as exc:
                logger.debug("Skipping unreadable file during search %s: %s", p, exc)
                continue
            if len(results) >= result_limit:
                break

        if not results:
            return f"No matches found for '{query}' in {file_pattern} files."
        return f"Found {len(results)} matches for '{query}':\n" + "\n".join(results)
    except (OSError, ValueError) as e:
        return f"Error searching codebase: {e}"
