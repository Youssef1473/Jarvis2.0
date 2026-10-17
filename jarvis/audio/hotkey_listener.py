"""Global input trigger listener for Jarvis 2.0 (Push-to-Talk via Mouse MB2 or Keyboard)."""
import logging
import threading
import time
from typing import Callable, Optional, Dict, Any
from pynput import keyboard, mouse

logger = logging.getLogger("Jarvis.InputListener")


class HotkeyListener:
    """Listens globally for Mouse Button (MB2, MB3, MB4, MB5) or Keyboard (ALT) for Push-to-Talk."""

    MOUSE_BUTTON_MAP = {
        "mb1": mouse.Button.left,
        "mouse1": mouse.Button.left,
        "left": mouse.Button.left,
        "mb2": mouse.Button.right,
        "mouse2": mouse.Button.right,
        "right": mouse.Button.right,
        "mouse_right": mouse.Button.right,
        "mb3": mouse.Button.middle,
        "mouse3": mouse.Button.middle,
        "middle": mouse.Button.middle,
        "mb4": mouse.Button.x1,
        "mouse4": mouse.Button.x1,
        "x1": mouse.Button.x1,
        "side1": mouse.Button.x1,
        "back": mouse.Button.x1,
        "mb5": mouse.Button.x2,
        "mouse5": mouse.Button.x2,
        "x2": mouse.Button.x2,
        "side2": mouse.Button.x2,
        "forward": mouse.Button.x2,
    }

    KEYBOARD_KEY_MAP = {
        "alt": (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r),
        "alt_r": (keyboard.Key.alt_r, keyboard.Key.alt_gr),
        "alt_l": (keyboard.Key.alt_l,),
        "ctrl": (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r),
        "ctrl_r": (keyboard.Key.ctrl_r,),
        "ctrl_l": (keyboard.Key.ctrl_l,),
        "shift": (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r),
        "space": (keyboard.Key.space,),
        "caps_lock": (keyboard.Key.caps_lock,),
        "caps": (keyboard.Key.caps_lock,),
        "tilde": (keyboard.KeyCode.from_char("`"),),
        "`": (keyboard.KeyCode.from_char("`"),),
        "f1": (keyboard.Key.f1,),
        "f2": (keyboard.Key.f2,),
        "f3": (keyboard.Key.f3,),
        "f4": (keyboard.Key.f4,),
        "f5": (keyboard.Key.f5,),
        "f6": (keyboard.Key.f6,),
        "f7": (keyboard.Key.f7,),
        "f8": (keyboard.Key.f8,),
        "f9": (keyboard.Key.f9,),
        "f10": (keyboard.Key.f10,),
        "f11": (keyboard.Key.f11,),
        "f12": (keyboard.Key.f12,),
        "fin": (keyboard.Key.end,),
        "end": (keyboard.Key.end,),
    }

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        on_press_callback: Optional[Callable[[], None]] = None,
        on_release_callback: Optional[Callable[[], None]] = None,
        on_any_key_callback: Optional[Callable[[], None]] = None,
    ):
        self.config = config or {}
        self.on_press_callback = on_press_callback
        self.on_release_callback = on_release_callback
        self.on_any_key_callback = on_any_key_callback

        # Determine trigger from config (defaults to MB2 / mouse right click)
        ptt_setting = str(
            self.config.get("audio", {}).get("ptt_trigger", "") or 
            self.config.get("audio", {}).get("ptt_key", "mb2")
        ).lower().strip()

        self.mouse_target_btn = self.MOUSE_BUTTON_MAP.get(ptt_setting)
        self.keyboard_target_keys = self.KEYBOARD_KEY_MAP.get(ptt_setting)

        # Default fallback to MB2 if not matching a keyboard key
        if not self.mouse_target_btn and not self.keyboard_target_keys:
            self.mouse_target_btn = mouse.Button.right

        trigger_desc = f"Mouse {self.mouse_target_btn.name.upper()}" if self.mouse_target_btn else ptt_setting.upper()
        logger.info(f"Push-to-Talk configured on trigger: {trigger_desc}")

        self._is_pressed = False
        self._keyboard_listener: Optional[keyboard.Listener] = None
        self._mouse_listener: Optional[mouse.Listener] = None
        self._lock = threading.Lock()

    def _on_mouse_click(self, x, y, button, pressed):
        """Monitors global mouse clicks."""
        if self.on_any_key_callback and pressed:
            try:
                self.on_any_key_callback()
            except Exception:
                pass

        if self.mouse_target_btn and button == self.mouse_target_btn:
            with self._lock:
                if pressed:
                    if not self._is_pressed:
                        self._is_pressed = True
                        if self.on_press_callback:
                            try:
                                self.on_press_callback()
                            except Exception as e:
                                logger.error(f"Error in on_press_callback: {e}")
                else:
                    if self._is_pressed:
                        self._is_pressed = False
                        if self.on_release_callback:
                            try:
                                self.on_release_callback()
                            except Exception as e:
                                logger.error(f"Error in on_release_callback: {e}")

    def _on_key_down(self, key):
        """Monitors global keyboard key down for barge-in and keyboard triggers."""
        if self.on_any_key_callback:
            try:
                self.on_any_key_callback()
            except Exception:
                pass

        if self.keyboard_target_keys and key in self.keyboard_target_keys:
            with self._lock:
                if not self._is_pressed:
                    self._is_pressed = True
                    if self.on_press_callback:
                        try:
                            self.on_press_callback()
                        except Exception as e:
                            logger.error(f"Error in on_press_callback: {e}")

    def _on_key_up(self, key):
        """Monitors global keyboard key up."""
        if self.keyboard_target_keys and key in self.keyboard_target_keys:
            with self._lock:
                if self._is_pressed:
                    self._is_pressed = False
                    if self.on_release_callback:
                        try:
                            self.on_release_callback()
                        except Exception as e:
                            logger.error(f"Error in on_release_callback: {e}")

    def start(self):
        """Starts both keyboard and mouse global listeners in dedicated background threads."""
        if self._keyboard_listener is None:
            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_key_down,
                on_release=self._on_key_up,
            )
            self._keyboard_listener.daemon = True
            self._keyboard_listener.start()

        if self._mouse_listener is None and self.mouse_target_btn is not None:
            self._mouse_listener = mouse.Listener(
                on_click=self._on_mouse_click,
            )
            self._mouse_listener.daemon = True
            self._mouse_listener.start()

    def stop(self):
        """Stops global listeners."""
        if self._keyboard_listener is not None:
            try:
                self._keyboard_listener.stop()
            except Exception:
                pass
            self._keyboard_listener = None

        if self._mouse_listener is not None:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
            self._mouse_listener = None
