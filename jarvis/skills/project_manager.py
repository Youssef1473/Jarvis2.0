"""Semantic project launcher and user folder navigator for Jarvis 2.0."""
import logging
import os
import re
import shutil
import subprocess

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.ProjectManager")


@SkillRegistry.register(
    name="open_project",
    description="Opens a coding project or repository in Visual Studio Code by name (e.g. 'Jarvis', 'Social Web', 'Qt-Project')."
)
def open_project(project_name: str) -> str:
    """Finds and opens a matching project folder in VS Code."""
    clean = re.sub(r"[;&|<>$`\"]", "", project_name).lower().strip()
    if not clean:
        return "Please specify a project name, Sir."

    user_home = os.environ.get("USERPROFILE", "")
    search_roots = [
        os.path.join(user_home, "Projects"),
        os.path.join(user_home, "Desktop"),
        os.path.join(user_home, "Documents"),
    ]

    matched_path = None
    matched_name = None

    for root in search_roots:
        if not os.path.exists(root):
            continue
        try:
            for item in os.listdir(root):
                full_path = os.path.join(root, item)
                if os.path.isdir(full_path):
                    item_clean = item.lower()
                    if clean == item_clean or clean in item_clean or item_clean in clean:
                        matched_path = full_path
                        matched_name = item
                        break
        except Exception:
            continue
        if matched_path:
            break

    if not matched_path:
        return f"Could not locate a project matching '{project_name}' in your Projects, Desktop, or Documents folders, Sir."

    # Launch VS Code with project directory
    code_bin = shutil.which("code.cmd") or shutil.which("code") or "code"
    try:
        subprocess.Popen(
            [code_bin, matched_path],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            close_fds=True,
            shell=False
        )
        return f"Project '{matched_name}' opened in Visual Studio Code, Sir."
    except Exception as e:
        logger.error(f"Failed to open project in VS Code: {e}")
        return f"Found project at '{matched_path}', but failed to launch VS Code: {e}"


@SkillRegistry.register(
    name="open_user_folder",
    description="Opens common user folders in Windows Explorer (e.g. 'downloads', 'screenshots', 'desktop', 'documents', 'projects')."
)
def open_user_folder(folder_name: str) -> str:
    """Opens a recognized system or user directory in Windows Explorer."""
    clean = folder_name.lower().strip()
    user_home = os.environ.get("USERPROFILE", "")

    folder_map = {
        "downloads": os.path.join(user_home, "Downloads"),
        "download": os.path.join(user_home, "Downloads"),
        "desktop": os.path.join(user_home, "Desktop"),
        "documents": os.path.join(user_home, "Documents"),
        "docs": os.path.join(user_home, "Documents"),
        "projects": os.path.join(user_home, "Projects"),
        "project": os.path.join(user_home, "Projects"),
        "pictures": os.path.join(user_home, "Pictures"),
        "screenshots": os.path.abspath("data/screenshots"),
        "music": os.path.join(user_home, "Music"),
        "videos": os.path.join(user_home, "Videos"),
    }

    target_dir = folder_map.get(clean)
    if not target_dir:
        # Fallback substring match
        for key, path in folder_map.items():
            if key in clean or clean in key:
                target_dir = path
                break

    if not target_dir or not os.path.exists(target_dir):
        return f"Folder '{folder_name}' could not be located, Sir."

    try:
        os.startfile(target_dir)
        return f"Opened your {folder_name} folder in Explorer, Sir."
    except Exception as e:
        return f"Failed to open folder '{folder_name}': {e}"

