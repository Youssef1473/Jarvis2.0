"""System tray icon and stealth background manager for Jarvis 2.0."""
import ctypes
import logging
import os
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pystray
from PIL import Image, ImageDraw

logger = logging.getLogger("Jarvis.UI.Tray")


def create_tray_icon_image(status: str = "online") -> Image.Image:
    """
    Procedurally renders a high-resolution Arc-Reactor icon for the Windows system tray.
    - 'online': Electric Cyan glowing reactor.
    - 'muted': Amber / Orange reactor indicating muted voice sensors.
    """
    width = 64
    height = 64
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if status == "muted":
        outer_color = (255, 140, 0, 255)
        core_color = (255, 175, 40, 255)
        center_color = (255, 230, 180, 255)
    else:  # online
        outer_color = (0, 210, 255, 255)
        core_color = (0, 240, 255, 255)
        center_color = (255, 255, 255, 255)

    # Outer glowing reactor chassis
    draw.ellipse((4, 4, 60, 60), outline=outer_color, width=4)
    # Reactor radial conduits
    draw.line((32, 6, 32, 18), fill=outer_color, width=3)
    draw.line((32, 46, 32, 58), fill=outer_color, width=3)
    draw.line((6, 32, 18, 32), fill=outer_color, width=3)
    draw.line((46, 32, 58, 32), fill=outer_color, width=3)
    # Inner energy cell
    draw.ellipse((18, 18, 46, 46), fill=core_color)
    # Center core highlight
    draw.ellipse((26, 26, 38, 38), fill=center_color)

    return image


def get_console_hwnd():
    """Returns the HWND of the console window hosting Python, if any."""
    try:
        return ctypes.windll.kernel32.GetConsoleWindow()
    except Exception:
        return 0


def is_console_visible() -> bool:
    """Returns True if the console terminal window is currently visible."""
    hwnd = get_console_hwnd()
    if hwnd == 0:
        return False
    try:
        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def set_console_visible(visible: bool):
    """Hides or reveals the console terminal window via Win32 ShowWindow."""
    hwnd = get_console_hwnd()
    if hwnd != 0:
        try:
            ctypes.windll.user32.ShowWindow(hwnd, 5 if visible else 0)
        except Exception:
            pass


def get_startup_vbs_path() -> Path:
    """Returns the path to the Jarvis auto-start script in the Windows Startup folder."""
    startup_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return startup_dir / "Jarvis2.0.vbs"


def is_startup_enabled() -> bool:
    """Checks whether the startup script is installed in the Windows Startup directory."""
    return get_startup_vbs_path().exists()


def set_startup_enabled(enabled: bool, project_dir: Path) -> bool:
    """Creates or removes the silent VBS launcher in the Windows Startup folder."""
    vbs_path = get_startup_vbs_path()
    if enabled:
        vbs_path.parent.mkdir(parents=True, exist_ok=True)
        pythonw_exe = project_dir / ".venv" / "Scripts" / "pythonw.exe"
        if not pythonw_exe.exists():
            pythonw_exe = Path(sys.executable).parent / "pythonw.exe"

        vbs_script = (
            f'Set WshShell = CreateObject("WScript.Shell")\n'
            f'WshShell.CurrentDirectory = "{project_dir}"\n'
            f'WshShell.Run """{pythonw_exe}"" run.py --stealth", 0, False\n'
        )
        vbs_path.write_text(vbs_script, encoding="utf-8")
        return True
    else:
        if vbs_path.exists():
            try:
                vbs_path.unlink()
            except Exception:
                pass
        return False


class SystemTrayApp:
    """Manages the Windows taskbar notification tray icon and stealth mode controls."""

    def __init__(self, orchestrator: Any, on_quit_callback: Callable | None = None):
        self.orchestrator = orchestrator
        self.on_quit_callback = on_quit_callback
        self.icon: pystray.Icon | None = None
        self._project_dir = Path(__file__).resolve().parent.parent.parent

    def _toggle_mute(self, icon, item):
        """Toggles voice listening on or off."""
        muted = self.orchestrator.toggle_mute()
        new_status = "muted" if muted else "online"
        icon.icon = create_tray_icon_image(new_status)
        icon.title = f"JARVIS 3.0 ({'MUTED' if muted else 'ONLINE'})"
        icon.update_menu()

    def _toggle_hud(self, icon, item):
        """Toggles the floating Arc-Reactor holographic overlay."""
        if not getattr(self.orchestrator.hud, "running", False):
            self.orchestrator.hud.start()
        else:
            self.orchestrator.hud.toggle_visibility()
        icon.update_menu()

    def _toggle_console(self, icon, item):
        """Toggles the visibility of the black command prompt terminal window."""
        currently_visible = is_console_visible()
        set_console_visible(not currently_visible)
        icon.update_menu()

    def _trigger_morning_briefing(self, icon, item):
        """Manually requests an executive morning standup briefing."""
        threading.Thread(target=self.orchestrator.heartbeat.deliver_briefing, daemon=True).start()

    def _toggle_startup(self, icon, item):
        """Toggles Windows auto-start on boot."""
        current = is_startup_enabled()
        set_startup_enabled(not current, self._project_dir)
        icon.update_menu()

    def _open_project_folder(self, icon, item):
        """Opens the project folder in Windows Explorer."""
        try:
            os.startfile(str(self._project_dir))
        except Exception:
            pass

    def _open_web_hud(self, icon, item):
        """Opens the Holographic Web HUD in default browser."""
        try:
            import webbrowser
            from jarvis.ui.server import JarvisUIService
            srv = JarvisUIService.get_instance()
            url = srv.get_authenticated_url() if srv else "http://127.0.0.1:20129"
            webbrowser.open(url)
        except Exception:
            pass

    def _open_omniroute_dashboard(self, icon, item):
        """Opens the OmniRoute AI Gateway Web Dashboard."""
        try:
            import webbrowser
            webbrowser.open("http://localhost:20128/dashboard")
        except Exception:
            pass

    def _view_memory(self, icon, item):
        """Presents a sleek, dark-themed Tkinter viewer displaying stored facts and preferences."""
        def _show_gui():
            root = tk.Tk()
            root.title("JARVIS 3.0 — Long-Term Memory Profile")
            root.geometry("620x560")
            root.configure(bg="#0c1017")

            # Title
            header = tk.Label(
                root, text="✦ JARVIS 3.0 COGNITIVE MEMORY ✦",
                font=("Segoe UI", 12, "bold"), fg="#00f0ff", bg="#0c1017"
            )
            header.pack(pady=(15, 8))

            subtitle = tk.Label(
                root, text="Tier 1 Core Profile & Tier 2 Dynamic Knowledge Base",
                font=("Segoe UI", 9), fg="#6e7d91", bg="#0c1017"
            )
            subtitle.pack(pady=(0, 10))

            # Text frame with scrollbar
            frame = tk.Frame(root, bg="#131924")
            frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

            scrollbar = tk.Scrollbar(frame)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

            text_area = tk.Text(
                frame, wrap=tk.WORD, font=("Consolas", 10),
                bg="#131924", fg="#c9d1d9", insertbackground="#00f0ff",
                selectbackground="#005a70", relief=tk.FLAT, yscrollcommand=scrollbar.set
            )
            text_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
            scrollbar.config(command=text_area.yview)

            # Insert profile facts
            summary = self.orchestrator.memory.get_context_summary()
            text_area.insert(tk.END, summary)
            text_area.config(state=tk.DISABLED)

            # Close button
            btn = tk.Button(
                root, text="Close", font=("Segoe UI", 9, "bold"),
                bg="#00f0ff", fg="#000000", activebackground="#70f7ff",
                relief=tk.FLAT, padx=20, pady=5, command=root.destroy
            )
            btn.pack(pady=12)

            root.mainloop()

        threading.Thread(target=_show_gui, daemon=True).start()

    def _on_quit(self, icon, item):
        """Gracefully shuts down Jarvis."""
        set_console_visible(True)
        if self.on_quit_callback:
            self.on_quit_callback()
        icon.stop()
        time.sleep(0.3)
        os._exit(0)

    def _build_menu(self) -> pystray.Menu:
        """Assembles the interactive system tray context menu."""
        provider_name = getattr(self.orchestrator.brain, "provider", "ollama").upper()
        model_name = getattr(self.orchestrator.brain, "openai_model", "qwen3.5:2b-q4_K_M")

        menu_items = [
            pystray.MenuItem("JARVIS 3.0 (Online)", None, enabled=False),
            pystray.MenuItem(f"Brain: {provider_name} ({model_name})", None, enabled=False),
            pystray.MenuItem("Routing: local-first; cloud consultations by request", None, enabled=False),
            pystray.MenuItem("Voice: Kokoro-82M Neural (Local CUDA)", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Mute Voice Sensors",
                self._toggle_mute,
                checked=lambda item: self.orchestrator.is_muted()
            ),
            pystray.MenuItem(
                "Show Floating HUD Overlay",
                self._toggle_hud,
                checked=lambda item: self.orchestrator.hud.is_visible()
            ),
            pystray.MenuItem(
                "Hide Console Window",
                self._toggle_console,
                checked=lambda item: not is_console_visible()
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Holographic HUD Interface", self._open_web_hud),
            pystray.MenuItem("Open OmniRoute Gateway Dashboard", self._open_omniroute_dashboard),
            pystray.MenuItem("Deliver Morning Standup Briefing", self._trigger_morning_briefing),
            pystray.MenuItem("View Memory Knowledge Base", self._view_memory),
            pystray.MenuItem("Open Project Workspace", self._open_project_folder),
            pystray.MenuItem(
                "Start on Windows Boot",
                self._toggle_startup,
                checked=lambda item: is_startup_enabled()
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Power Down / Exit", self._on_quit),
        ]
        return pystray.Menu(*menu_items)

    def run(self):
        """Runs the tray icon loop."""
        try:
            image = create_tray_icon_image("online")
            menu = self._build_menu()
            self.icon = pystray.Icon("Jarvis3.0", image, "JARVIS 3.0 Assistant", menu)
            self.icon.run()
        except Exception as e:
            logger.warning(f"System tray icon initialization skipped: {e}")

    def run_in_background(self):
        """Starts the tray icon in a dedicated daemon thread."""
        thread = threading.Thread(target=self.run, name="JarvisTrayDaemon", daemon=True)
        thread.start()
        return thread
