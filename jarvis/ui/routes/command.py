"""Command execution, OS action triggering, and mute toggle routes for Jarvis 2.0 HUD."""
from __future__ import annotations

import logging
import threading
from typing import Any

from starlette.responses import JSONResponse

logger = logging.getLogger("Jarvis.UI.Command")


class CommandRoutesMixin:
    """Mixin providing direct command execution, OS actuation, and mute control endpoints."""

    orchestrator: Any

    async def _handle_command(self, request: Any) -> JSONResponse:
        """Dispatches text commands to the active orchestrator session."""
        try:
            body = await request.json()
            cmd_text = body.get("command", "").strip()
        except (ValueError, TypeError):
            cmd_text = ""

        if not cmd_text:
            return JSONResponse({"error": "Empty command"}, status_code=400)

        if self.orchestrator:
            threading.Thread(target=self.orchestrator.handle_command, args=(cmd_text,), daemon=True).start()
            return JSONResponse({"status": "queued", "command": cmd_text})

        return JSONResponse({"error": "Orchestrator not connected"}, status_code=503)

    async def _handle_action(self, request: Any) -> JSONResponse:
        """Executes designated quick OS actuation commands from the HUD interface."""
        try:
            body = await request.json()
            action = body.get("action", "")
        except (ValueError, TypeError):
            action = ""

        if action == "set_volume":
            from jarvis.skills.os_control import adjust_volume
            try:
                percent = int(body.get("percent", 50))
            except (ValueError, TypeError):
                percent = 50
            adjust_volume(action="set", percent=percent)
            if hasattr(self, "broadcast_message"):
                self.broadcast_message("jarvis", f"Master volume set to {percent}%, Sir.")
            return JSONResponse({"status": "success", "percent": percent})

        elif action == "toggle_mute":
            if self.orchestrator:
                is_muted = self.orchestrator.toggle_mute()
                if hasattr(self, "broadcast_telemetry"):
                    self.broadcast_telemetry({"is_muted": is_muted})
                return JSONResponse({"is_muted": is_muted})

        elif action == "launch_private_browser":
            try:
                from jarvis.skills.os_control import open_browser_tab, press_hotkey
                press_hotkey("ctrl+shift+n")
            except (ImportError, AttributeError, OSError, RuntimeError):
                try:
                    open_browser_tab("about:blank", browser="brave")
                except (ImportError, AttributeError, OSError, RuntimeError):
                    pass
            if hasattr(self, "broadcast_message"):
                self.broadcast_message("jarvis", "Private browser window launched, Sir.")
            return JSONResponse({"status": "success", "action": "private_browser"})

        elif action == "focus_ide":
            try:
                from jarvis.skills.os_control import launch_or_focus_application
                res = launch_or_focus_application("Visual Studio Code")
                if hasattr(self, "broadcast_message"):
                    self.broadcast_message("jarvis", res)
            except Exception as e:  # noqa: BLE001
                res = f"Failed to focus IDE: {e}"
            return JSONResponse({"status": "success", "message": res})

        elif action == "lock_workstation":
            try:
                from jarvis.skills.os_control import lock_workstation
                res = lock_workstation()
                if hasattr(self, "broadcast_message"):
                    self.broadcast_message("jarvis", res)
            except Exception as e:  # noqa: BLE001
                res = f"Failed to lock workstation: {e}"
            return JSONResponse({"status": "success", "message": res})

        elif action == "optimize_pc":
            if self.orchestrator:
                threading.Thread(target=self.orchestrator.handle_command, args=("Jarvis, optimize my PC",), daemon=True).start()
                if hasattr(self, "broadcast_message"):
                    self.broadcast_message("jarvis", "Initiating workstation memory and cache optimization, Sir.")
            return JSONResponse({"status": "queued", "action": "optimize_pc"})

        return JSONResponse({"error": f"Unknown action '{action}'"}, status_code=400)

    async def _handle_mute(self, request: Any) -> JSONResponse:
        """Toggles audio speech synthesis mute state on the active orchestrator."""
        if self.orchestrator:
            is_muted = self.orchestrator.toggle_mute()
            if hasattr(self, "broadcast_telemetry"):
                self.broadcast_telemetry({"is_muted": is_muted})
            return JSONResponse({"is_muted": is_muted})
        return JSONResponse({"error": "Orchestrator not connected"}, status_code=503)
