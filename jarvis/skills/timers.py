"""Spoken timers, countdowns, and alert skills for Jarvis 2.0."""
import logging
import os
import re
import threading
import time
from typing import Any

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Timers")

_active_timers: dict[str, dict[str, Any]] = {}
_timer_lock = threading.Lock()
_timer_counter = 0


def _parse_duration(text: str) -> int:
    """Parses natural duration strings (e.g., '5 minutes', '10 mins', '45 seconds', '1h') into seconds."""
    clean = str(text).lower().strip()
    if clean.isdigit():
        return int(clean)

    total_seconds = 0
    h = re.search(r'(\d+)\s*(?:hours?|hrs?|h)\b', clean)
    if h:
        total_seconds += int(h.group(1)) * 3600
    m = re.search(r'(\d+)\s*(?:minutes?|mins?|m)\b', clean)
    if m:
        total_seconds += int(m.group(1)) * 60
    s = re.search(r'(\d+)\s*(?:seconds?|secs?|s)\b', clean)
    if s:
        total_seconds += int(s.group(1))

    if total_seconds == 0:
        num = re.search(r'\b(\d+)\b', clean)
        if num:
            val = int(num.group(1))
            total_seconds = val * 60 if val <= 60 else val

    return max(1, total_seconds)


def _format_seconds(seconds: int) -> str:
    """Formats seconds into human-readable duration."""
    if seconds >= 3600:
        hrs = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hrs} hour{'s' if hrs != 1 else ''} {mins} minute{'s' if mins != 1 else ''}".strip()
    elif seconds >= 60:
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins} minute{'s' if mins != 1 else ''}" + (f" {secs} second{'s' if secs != 1 else ''}" if secs else "")
    else:
        return f"{seconds} second{'s' if seconds != 1 else ''}"


def _announce_timer_completion(label: str, duration_str: str):
    """Plays completion chime and speaks announcement via Windows SAPI5."""
    try:
        import winsound
        sound_path = os.path.abspath("data/sounds/done.wav")
        if not os.path.exists(sound_path):
            sound_path = os.path.abspath("data/sounds/alert.wav")
        if os.path.exists(sound_path):
            winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        logger.debug(f"Chime playback notice: {e}")

    co_inited = False
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        co_inited = True
        sapi = win32com.client.Dispatch("SAPI.SpVoice")
        sapi.Rate = 1
        spoken_msg = f"Sir, your timer for {label} ({duration_str}) has finished."
        sapi.Speak(spoken_msg, 1)  # 1 = Async speak
    except Exception as e:
        logger.warning(f"Timer speech announcement notice: {e}")
    finally:
        if co_inited:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def _timer_worker(timer_id: str, label: str, duration_sec: int, cancel_event: threading.Event):
    """Background worker that waits and announces timer completion."""
    finished = not cancel_event.wait(timeout=float(duration_sec))
    with _timer_lock:
        _active_timers.pop(timer_id, None)

    if finished and not cancel_event.is_set():
        dur_str = _format_seconds(duration_sec)
        logger.info(f"Timer '{label}' ({dur_str}) completed! Alerting Sir...")
        _announce_timer_completion(label, dur_str)


@SkillRegistry.register(
    name="start_timer",
    description="Starts a countdown timer (e.g. '5 minutes', '10 mins', '45 seconds', '1 hour') with an optional label."
)
def start_timer(duration: str, label: str = "Timer") -> str:
    """Starts a non-blocking countdown timer with audio chime and spoken completion alert."""
    global _timer_counter
    dur_sec = _parse_duration(duration)
    formatted = _format_seconds(dur_sec)
    clean_label = label.strip() or "Timer"

    cancel_event = threading.Event()
    with _timer_lock:
        _timer_counter += 1
        timer_id = f"timer_{_timer_counter}"
        _active_timers[timer_id] = {
            "id": timer_id,
            "label": clean_label,
            "duration": dur_sec,
            "start_time": time.time(),
            "end_time": time.time() + dur_sec,
            "event": cancel_event,
        }

    t = threading.Thread(
        target=_timer_worker,
        args=(timer_id, clean_label, dur_sec, cancel_event),
        daemon=True,
        name=f"JarvisTimer-{timer_id}"
    )
    t.start()
    return f"Timer set for {formatted} ({clean_label}), Sir."


@SkillRegistry.register(
    name="cancel_timer",
    description="Cancels an active timer by label or cancels all active timers."
)
def cancel_timer(label: str = "") -> str:
    """Cancels running timers cleanly."""
    target = label.lower().strip()
    with _timer_lock:
        if not _active_timers:
            return "No timers are currently active, Sir."

        if target in ("all", "every timer", "all timers"):
            count = len(_active_timers)
            for t_info in _active_timers.values():
                t_info["event"].set()
            _active_timers.clear()
            return f"Cancelled all {count} active timer(s), Sir."

        # Match specific label or pop the most recent timer
        matched_id = None
        for t_id, t_info in _active_timers.items():
            if target and target in t_info["label"].lower():
                matched_id = t_id
                break

        if not matched_id and not target:
            # Cancel most recent
            matched_id = list(_active_timers.keys())[-1]

        if matched_id:
            info = _active_timers.pop(matched_id)
            info["event"].set()
            return f"Timer '{info['label']}' cancelled, Sir."

        return f"Could not find an active timer matching '{label}', Sir."


@SkillRegistry.register(
    name="list_active_timers",
    description="Lists all currently running countdown timers and their remaining time."
)
def list_active_timers() -> str:
    """Returns active countdown timers with live remaining duration."""
    now = time.time()
    with _timer_lock:
        if not _active_timers:
            return "There are no active countdown timers at the moment, Sir."

        lines = []
        for t_info in _active_timers.values():
            rem = max(0, int(t_info["end_time"] - now))
            lines.append(f"• {t_info['label']}: {_format_seconds(rem)} remaining (originally {_format_seconds(t_info['duration'])})")

        return f"Active Timers, Sir ({len(_active_timers)}):\n" + "\n".join(lines)
