"""Voidtools Everything instant multi-drive file search integration for Jarvis 2.0.
Provides sub-15ms system-wide file and folder search across all NTFS drives.
"""
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional
import psutil

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.EverythingSearch")

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent
LOCAL_ES_PATH = WORKSPACE_DIR / "tools" / "es.exe"
WINGET_ES_GLOB = (
    Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
)


def find_es_executable() -> Optional[str]:
    """Locates the voidtools es.exe command-line utility across standard paths."""
    # 1. Local tools/ directory
    if LOCAL_ES_PATH.exists():
        return str(LOCAL_ES_PATH)

    # 2. In PATH
    which_es = shutil.which("es.exe") or shutil.which("es")
    if which_es:
        return which_es

    # 3. Standard Program Files
    prog_files = Path(r"C:\Program Files\Everything\es.exe")
    if prog_files.exists():
        return str(prog_files)

    prog_files_x86 = Path(r"C:\Program Files (x86)\Everything\es.exe")
    if prog_files_x86.exists():
        return str(prog_files_x86)

    # 4. WinGet package directory
    if WINGET_ES_GLOB.exists():
        try:
            matches = list(WINGET_ES_GLOB.glob("voidtools.Everything.Cli*/es.exe"))
            if matches:
                return str(matches[0])
        except Exception:
            pass

    return None


def ensure_everything_service() -> bool:
    """Ensures Everything.exe background service is running for IPC queries."""
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name")
            if name and name.lower().startswith("everything"):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    exe_candidates = [
        r"C:\Program Files\Everything\Everything.exe",
        r"C:\Program Files (x86)\Everything\Everything.exe",
    ]
    for candidate in exe_candidates:
        if os.path.exists(candidate):
            try:
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                    creation_flags |= subprocess.CREATE_NEW_PROCESS_GROUP
                subprocess.Popen(
                    [candidate, "-startup"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creation_flags,
                )
                time.sleep(0.35)
                return True
            except Exception as e:
                logger.debug("Failed to spawn Everything service: %s", e)
    return False


def _fallback_local_search(query: str, max_results: int = 20) -> str:
    """Independent fallback search traversing user project and document trees."""
    user_home = Path.home()
    roots = [
        user_home / "Projects",
        user_home / "Desktop",
        user_home / "Documents",
        user_home / "Downloads",
    ]
    found = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for p in root.rglob(f"*{query}*"):
                if p.is_file():
                    parts = p.parts
                    if any(part.startswith(".") or part == "__pycache__" for part in parts):
                        continue
                    found.append(str(p))
                    if len(found) >= max_results:
                        break
        except Exception:
            continue
        if len(found) >= max_results:
            break

    if not found:
        return f"No files matching '{query}' were found across your primary directories, Sir."
    return f"Found {len(found)} file(s), Sir:\n" + "\n".join(f"- {p}" for p in found)


@SkillRegistry.register(
    name="search_everything",
    description=(
        "Instant sub-15ms system-wide file and folder search across all local drives using Voidtools Everything. "
        "Locates files, folders, codebases, or documents by name, pattern, or extension with zero disk thrashing."
    ),
)
def search_everything(
    query: str,
    max_results: int = 20,
    extension: str = "",
    folders_only: bool = False,
    files_only: bool = False,
) -> str:
    """Searches files instantly across all NTFS drives via Voidtools Everything IPC."""
    q_clean = (query or "").strip()
    if not q_clean and not extension:
        return "Please specify a search term or file extension, Sir."

    es_path = find_es_executable()
    if not es_path:
        return _fallback_local_search(query=q_clean, max_results=max_results)

    # Ensure background service is running
    ensure_everything_service()

    cmd = [es_path, "-n", str(max(1, min(max_results, 50)))]
    if folders_only:
        cmd.append("/ad")
    elif files_only:
        cmd.append("/a-d")

    search_terms = []
    if extension:
        ext_clean = extension.lstrip(".")
        search_terms.append(f"ext:{ext_clean}")
    if q_clean:
        search_terms.append(q_clean)

    cmd.extend(search_terms)

    try:
        from jarvis.skills.job_object_sandbox import run_sandboxed_batch
        proc = run_sandboxed_batch(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
        )

        output = proc.stdout.strip()
        if proc.returncode != 0:
            err = proc.stderr.strip()
            # If IPC error, attempt one service relaunch
            if "Error 8" in err or "IPC not found" in err:
                ensure_everything_service()
                time.sleep(0.4)
                retry_proc = run_sandboxed_batch(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5.0,
                )
                if retry_proc.returncode == 0 and retry_proc.stdout.strip():
                    output = retry_proc.stdout.strip()
                else:
                    return _fallback_local_search(query=q_clean, max_results=max_results)
            else:
                return f"Everything search notice: {err}"

        if not output:
            return f"No files matching '{q_clean}' were found across your drives, Sir."

        lines = [line for line in output.splitlines() if line.strip()]
        clean_lines = [
            l for l in lines if not any(bad in l for bad in ("$Recycle.Bin", "System Volume Information"))
        ]
        results_to_show = clean_lines[:max_results] or lines[:max_results]

        header = f"Found {len(results_to_show)} instant result(s) via Voidtools Everything, Sir:"
        items = "\n".join(f"- {p}" for p in results_to_show)
        return f"{header}\n{items}"

    except subprocess.TimeoutExpired:
        return _fallback_local_search(query=q_clean, max_results=max_results)
    except Exception as e:
        logger.warning("Everything search error: %s. Falling back to local directory search.", e)
        return _fallback_local_search(query=q_clean, max_results=max_results)
