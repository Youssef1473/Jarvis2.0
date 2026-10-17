"""Stark-style Multi-Action Protocols for Jarvis 2.0."""
import os
import sys
import ctypes
import logging
import pyautogui
import pyperclip
from jarvis.skills.base import SkillRegistry
from jarvis.skills.os_control import launch_application, adjust_volume, minimize_all_windows, lock_workstation

logger = logging.getLogger("Jarvis.Protocols")


@SkillRegistry.register(
    name="execute_protocol",
    description="Executes a coordinated multi-action protocol sequence (e.g. 'work', 'lockdown', 'relax', 'night', 'focus')."
)
def execute_protocol(protocol_name: str) -> str:
    """Executes a coordinated multi-action workflow sequence."""
    p = protocol_name.lower().strip()

    if any(k in p for k in ("work", "coding", "focus", "developer")):
        # 1. Work Protocol: stage IDE, browser, volume 35%
        launch_application("code")
        launch_application("brave")
        adjust_volume(action="set", percent=35)
        return "Work protocol initiated, Sir. VS Code and Brave are staged, audio set to 35%, and workstation systems are ready."

    elif any(k in p for k in ("lockdown", "secure", "security")):
        # 2. Lockdown Protocol: wipe clipboard, minimize all, lock workstation
        try:
            pyperclip.copy("")
        except Exception:
            pass
        minimize_all_windows()
        lock_workstation()
        return "Lockdown protocol engaged, Sir. Clipboard sanitized, desktop minimized, workstation locked."

    elif any(k in p for k in ("relax", "entertainment", "gaming", "chill")):
        # 3. Relax / Entertainment Protocol: stage media and set volume 50%
        launch_application("brave")
        adjust_volume(action="set", percent=50)
        return "Relax protocol engaged, Sir. Media browser staged, volume set to 50%."

    elif any(k in p for k in ("night", "sleep", "goodnight", "end of day")):
        # 4. Night Protocol: minimize windows, reduce volume to 15%
        minimize_all_windows()
        adjust_volume(action="set", percent=15)
        return "Night protocol engaged, Sir. Windows minimized, audio volume lowered to 15%. Systems ready for rest."

    return f"Protocol '{protocol_name}' is not recognized, Sir. Available protocols: Work, Lockdown, Relax, Night."
