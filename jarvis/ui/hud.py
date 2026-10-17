"""Futuristic Arc-Reactor Holographic HUD Overlay for Jarvis 2.0."""
import math
import threading
import time
import tkinter as tk
from typing import Optional


class JarvisHUD:
    """A floating, transparent, draggable Arc-Reactor visualizer."""

    STATES = ["idle", "listening", "thinking", "speaking"]

    def __init__(self, size: int = 140):
        self.size = size
        self.center = size // 2
        self.state = "idle"
        self._target_state = "idle"
        self.angle = 0.0
        self.pulse = 0.0
        self.pulse_dir = 1
        self.running = False
        self.root: Optional[tk.Tk] = None
        self.canvas: Optional[tk.Canvas] = None
        self._thread: Optional[threading.Thread] = None

        # Dragging state
        self._drag_start_x = 0
        self._drag_start_y = 0

    def set_state(self, new_state: str):
        """Thread-safe state change (idle, listening, thinking, speaking)."""
        if new_state in self.STATES:
            self._target_state = new_state

    def _start_drag(self, event):
        self._drag_start_x = event.x
        self._drag_start_y = event.y

    def _on_drag(self, event):
        if self.root:
            deltax = event.x - self._drag_start_x
            deltay = event.y - self._drag_start_y
            new_x = self.root.winfo_x() + deltax
            new_y = self.root.winfo_y() + deltay
            self.root.geometry(f"+{new_x}+{new_y}")

    def _draw_hud(self):
        """Draws the animated Arc-Reactor elements onto the canvas."""
        if not self.canvas:
            return

        self.canvas.delete("all")
        self.state = self._target_state

        # Dynamics based on state
        if self.state == "listening":
            speed = 0.12
            base_color = "#00f0ff"
            glow_color = "#70f7ff"
            status_text = "LISTENING"
            ring_scale = 1.0 + 0.12 * math.sin(self.pulse)
        elif self.state == "thinking":
            speed = 0.22
            base_color = "#ffaa00"
            glow_color = "#ffd277"
            status_text = "PROCESSING"
            ring_scale = 1.0 + 0.06 * math.sin(self.pulse * 2)
        elif self.state == "speaking":
            speed = 0.14
            base_color = "#00e199"
            glow_color = "#8affd2"
            status_text = "SPEAKING"
            ring_scale = 1.0 + 0.18 * abs(math.sin(self.pulse * 3))
        else:  # idle
            speed = 0.04
            base_color = "#0088aa"
            glow_color = "#00b4d8"
            status_text = "STANDBY"
            ring_scale = 1.0 + 0.04 * math.sin(self.pulse)

        self.angle = (self.angle + speed) % (2 * math.pi)
        self.pulse = (self.pulse + 0.08) % (2 * math.pi)

        cx, cy = self.center, self.center

        # 1. Outer Ring
        r_outer = int(48 * ring_scale)
        self.canvas.create_oval(
            cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer,
            outline=base_color, width=2
        )

        # 2. Spinning Segment Arcs (Arc-Reactor turbine)
        num_arcs = 6
        for i in range(num_arcs):
            start_deg = math.degrees(self.angle + (i * 2 * math.pi / num_arcs))
            extent_deg = 30
            r_arc = int(38 * ring_scale)
            self.canvas.create_arc(
                cx - r_arc, cy - r_arc, cx + r_arc, cy + r_arc,
                start=start_deg, extent=extent_deg,
                outline=glow_color, width=3, style=tk.ARC
            )

        # 3. Inner Core Reactor
        r_inner = int(18 * ring_scale)
        self.canvas.create_oval(
            cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner,
            fill=base_color, outline=glow_color, width=2
        )

        # 4. Center High-Intensity Dot
        r_dot = 6
        self.canvas.create_oval(
            cx - r_dot, cy - r_dot, cx + r_dot, cy + r_dot,
            fill="#ffffff", outline=glow_color, width=1
        )

        # 5. Status Text Label
        self.canvas.create_text(
            cx, cy + 58,
            text=f"JARVIS // {status_text}",
            fill=glow_color,
            font=("Segoe UI", 8, "bold")
        )

        if self.running and self.root:
            self.root.after(30, self._draw_hud)

    def _run_tk(self):
        """Initializes transparent Tkinter window."""
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)

        # Transparent background setup for Windows
        trans_color = "#000001"
        self.root.wm_attributes("-transparentcolor", trans_color)
        self.root.config(bg=trans_color)

        # Position at bottom-right corner of screen
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        pos_x = screen_w - self.size - 40
        pos_y = screen_h - self.size - 90
        self.root.geometry(f"{self.size}x{self.size + 15}+{pos_x}+{pos_y}")

        # Canvas
        self.canvas = tk.Canvas(
            self.root, width=self.size, height=self.size + 15,
            bg=trans_color, highlightthickness=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Dragging events
        self.canvas.bind("<Button-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._on_drag)

        self.running = True
        self._draw_hud()
        self.root.mainloop()

    def start(self):
        """Starts the HUD in a dedicated background daemon thread."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run_tk, daemon=True)
            self._thread.start()

    def toggle_visibility(self) -> bool:
        """Toggles HUD overlay visibility. Returns new visibility state."""
        if not self.root:
            return False
        try:
            if self.root.winfo_viewable():
                self.root.withdraw()
                return False
            else:
                self.root.deiconify()
                return True
        except Exception:
            return False

    def is_visible(self) -> bool:
        """Returns True if the HUD overlay window is currently mapped on screen."""
        if not self.root:
            return False
        try:
            return bool(self.root.winfo_viewable())
        except Exception:
            return False

    def stop(self):
        """Destroys the HUD window."""
        self.running = False
        if self.root:
            try:
                self.root.quit()
            except Exception:
                pass
