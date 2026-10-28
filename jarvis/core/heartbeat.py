"""Proactive ambient heartbeat daemon for Jarvis 2.0.
Monitors calendar appointments, task deadlines, battery status, unread emails,
and delivers autonomous morning standup briefings without requiring wake-words.
"""
import asyncio
import ctypes
import datetime
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

from jarvis.context.flow_state_mirror import FlowStateMirror, FlowStateSnapshot
from jarvis.core.cognition.cadence_tracker import MotorCadenceTracker
from jarvis.core.cognition.habit_graph import TemporalHabitGraph
from jarvis.core.cognition.proactive_evi import (
    CognitiveState,
    CognitiveTwinEngine,
    DeliveryModality,
    TelemetrySlice,
)

logger = logging.getLogger("Jarvis.Heartbeat")



class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def _get_idle_duration_sec() -> float:
    """Queries Win32 GetLastInputInfo in <5us to measure user input idle time."""
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
            return max(0.0, float(millis) / 1000.0)
    except Exception:
        pass
    return 0.0


class JarvisHeartbeat:
    """Ambient background monitor coordinating proactive spoken alerts, briefings, and desktop presence."""

    def __init__(
        self,
        orchestrator: Any = None,
        config: dict[str, Any] | None = None,
        flow_mirror: Any | None = None,
        tts_engine: Any | None = None,
        ast_diagnoser: Callable[[FlowStateSnapshot], dict[str, Any]] | None = None,
        poll_interval_sec: float = 1.0,
    ):
        # Support alternative signature JarvisHeartbeat(flow_mirror, tts_engine, ast_diagnoser, poll_interval_sec)
        if orchestrator is not None and not hasattr(orchestrator, "brain") and not hasattr(orchestrator, "memory") and (flow_mirror is None and tts_engine is None):
            self._flow_mirror = orchestrator
            self._tts = config
            self._ast_diagnoser = ast_diagnoser
            self._poll_interval = poll_interval_sec
            self.orchestrator = None
            self.config = {}
            self.cognitive_twin = CognitiveTwinEngine()
            hb_cfg = {}
        else:
            self.orchestrator = orchestrator
            self.config = config or {}
            self._flow_mirror = flow_mirror
            self._tts = tts_engine or (getattr(self.orchestrator, "tts", None) if self.orchestrator else None)
            self._ast_diagnoser = ast_diagnoser
            self._poll_interval = poll_interval_sec
            self.cognitive_twin = CognitiveTwinEngine()
            if self.orchestrator and hasattr(self.orchestrator, "brain") and self.orchestrator.brain:
                self.orchestrator.brain.cognitive_twin = self.cognitive_twin
            hb_cfg = self.config.get("heartbeat", {}) if self.config else {}

        if self._flow_mirror is None:
            try:
                self._flow_mirror = FlowStateMirror.get_instance()
            except Exception:
                self._flow_mirror = None
        if self._tts is None and self.orchestrator is not None:
            self._tts = getattr(self.orchestrator, "tts", None)

        self.enabled = hb_cfg.get("enabled", True)
        self.tick_interval = hb_cfg.get("tick_interval_seconds", 30)
        self.auto_briefing = hb_cfg.get("auto_morning_briefing", True)
        self.calendar_alerts = hb_cfg.get("calendar_alerts", True)
        self.battery_alerts = hb_cfg.get("battery_alerts", True)
        self.ergonomic_alerts = hb_cfg.get("ergonomic_reminders", True)
        self.email_alerts = hb_cfg.get("email_notifications", True)
        self.desktop_presence = hb_cfg.get("desktop_presence", True)
        self.idle_threshold_seconds = hb_cfg.get("idle_threshold_seconds", 300)

        # Presence & active window state tracking (Wake-Decide-Sleep)
        self._is_user_idle = False
        self._last_active_window = ""
        self._last_return_alert = 0.0
        self._sleep_consolidation_done = False

        self._running = False
        self._stop_event = getattr(self, '_stop_event', threading.Event())
        self._stop_event.set()
        self._thread: threading.Thread | None = None

        # Deduplication & Circadian Tracking State
        self._last_alerted_cmd_ts: int = 0
        self._consecutive_fail_count: int = 0
        self._continuous_flow_start_mono: float = time.monotonic()
        self._last_circadian_quip_mono: float = float("-inf")
        self._idle_reset_threshold_sec: float = 300.0  # 5 min break resets 2-hr continuous timer
        self._circadian_cooldown_sec: float = 10800.0  # Max once every 3 hours

        # Alert deduplication sets (persisted in memory to survive restarts)
        today_str = datetime.date.today().isoformat()
        try:
            stored_alerted = ""
            if self.orchestrator and hasattr(self.orchestrator, "memory") and self.orchestrator.memory:
                stored_alerted = self.orchestrator.memory.get_fact(f"alerted_events_{today_str}") or ""
            self._alerted_event_ids: set[int] = {int(x) for x in stored_alerted.split(",") if x.strip().isdigit()}
        except Exception:
            self._alerted_event_ids: set[int] = set()
        self._alerted_task_ids: set[int] = set()

        # Cooldown timestamps
        self._last_battery_alert = 0.0
        self._last_break_reminder = time.time()
        self._last_email_check = 0.0
        self._last_unread_email_count: int | None = None

        # Session metrics
        self._session_start_time = time.time()
        self._continuous_active_seconds = 0.0
        # Startup grace period (wait 15s after startup before proactive alerts)
        self._startup_grace_period = time.time() + 15.0

        # Memory key for daily briefing tracking
        today_str = datetime.date.today().isoformat()
        last_briefing_date = None
        if self.orchestrator and hasattr(self.orchestrator, "memory") and self.orchestrator.memory:
            try:
                last_briefing_date = self.orchestrator.memory.get_fact("last_briefing_date")
            except Exception:
                pass
        self._briefing_delivered_today = (last_briefing_date == today_str)

        # Ambient WinEventHook Daemon & Background dHash Screen Monitor (Phase P2)
        self.window_hook_daemon: Any | None = None
        self.dhash_monitor: Any | None = None

        # Motor Cadence Tracker (Argon Dimension 1)
        self.cadence_tracker: MotorCadenceTracker | None = None
        if sys.platform == "win32":
            try:
                self.cadence_tracker = MotorCadenceTracker()
            except Exception as e:
                logger.debug("MotorCadenceTracker initialization notice: %s", e)

        # Window Switch vs Title Change Tracking (Requirement 2)
        self._window_switches: deque[float] = deque(maxlen=300)
        self._last_hwnd: int | None = None

        # Dwell-time IDE Focus Tracking (Requirement 2)
        self._focus_intervals: deque[tuple[float, float, bool]] = deque(maxlen=300)
        self._current_window_start: float = time.time()
        self._current_window_is_ide: bool = False

        # Decaying Active Debug Signals (120s TTL - Requirement 3)
        self._debug_signals: deque[float] = deque(maxlen=200)

        # Cognitive State Tracking
        self._last_cognitive_state: CognitiveState | None = None

        # Temporal Habit Graph (Argon Dimension 1.3)
        db_path = "data/jarvis_memory.db"
        if hasattr(self.orchestrator, "memory") and hasattr(self.orchestrator.memory, "db_path"):
            db_path = str(self.orchestrator.memory.db_path)
        self.habit_graph = TemporalHabitGraph(db_path=db_path)
        if hasattr(self.orchestrator, "brain") and self.orchestrator.brain:
            self.orchestrator.brain.habit_graph = self.habit_graph


        if sys.platform == "win32":
            try:
                from jarvis.vision.window_hook_daemon import WinEventHookDaemon
                self.window_hook_daemon = WinEventHookDaemon()
                self.window_hook_daemon.add_listener(self._on_window_event)
            except Exception as e:
                logger.debug("WinEventHookDaemon initialization notice: %s", e)

            try:
                from jarvis.vision.dhash_monitor import BackgroundDHashMonitor
                self.dhash_monitor = BackgroundDHashMonitor(
                    orchestrator=self.orchestrator,
                    get_idle_seconds_fn=self.get_idle_seconds,
                    check_interval=2.0,
                    hamming_threshold=140,
                    idle_threshold=3.0,
                )
            except Exception as e:
                logger.debug("BackgroundDHashMonitor initialization notice: %s", e)

    IDE_DETECTION_PATTERNS = (
        "code",
        "visual studio code",
        "cursor",
        "pycharm",
        "intellij",
        "idea",
        "devenv",
        "visual studio",
        "sublime_text",
        "sublime text",
        "qtcreator",
        "antigravity",
        "terminal",
        "powershell",
        "pwsh",
        "cmd",
        "nvim",
        "neovim",
        "alacritty",
        "wezterm",
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".rs",
        ".go",
        ".html",
        ".css",
        ".json",
        ".yaml",
        ".md",
    )

    DEBUG_PATTERNS = (
        "debug",
        "debugger",
        "call stack",
        "stack trace",
        "pytest",
        "failed",
        "unhandled exception",
        "traceback",
        "assertionerror",
    )

    @classmethod
    def is_ide_window(cls, title: str) -> bool:
        """Returns True if the window title indicates an active developer IDE/editor/terminal."""
        if not title:
            return False
        t_lower = title.lower()
        return any(p in t_lower for p in cls.IDE_DETECTION_PATTERNS)

    def record_debug_signal(self, timestamp: float | None = None) -> None:
        """Records an observed active debugging signal (test failure, debugger window, stack trace)."""
        ts = timestamp if timestamp is not None else time.time()
        self._debug_signals.append(ts)

    def get_active_debug_signals(self, window_seconds: float = 120.0) -> int:
        """Counts active debug signals recorded within the decaying 120-second window."""
        now = time.time()
        cutoff = now - window_seconds
        while self._debug_signals and self._debug_signals[0] < cutoff:
            self._debug_signals.popleft()
        return len(self._debug_signals)

    def compute_ide_focus_ratio(self, window_seconds: float = 60.0) -> float:
        """Computes the fraction of elapsed dwell time spent in IDE windows over the last 60s."""
        now = time.time()
        cutoff = now - window_seconds
        total_ide_dwell = 0.0
        total_dwell = 0.0

        intervals = list(self._focus_intervals)
        for start_t, end_t, is_ide in intervals:
            if end_t <= cutoff:
                continue
            effective_start = max(start_t, cutoff)
            duration = max(0.0, end_t - effective_start)
            total_dwell += duration
            if is_ide:
                total_ide_dwell += duration

        if self._current_window_start > 0:
            effective_start = max(self._current_window_start, cutoff)
            current_duration = max(0.0, now - effective_start)
            total_dwell += current_duration
            if self._current_window_is_ide:
                total_ide_dwell += current_duration

        if total_dwell <= 0.0:
            return 1.0 if self._current_window_is_ide else 0.0
        return max(0.0, min(1.0, total_ide_dwell / total_dwell))

    def get_window_switch_frequency(self, window_seconds: float = 60.0) -> float:
        """Returns the rate of foreground window switches per minute in the rolling window."""
        now = time.time()
        cutoff = now - window_seconds
        while self._window_switches and self._window_switches[0] < cutoff:
            self._window_switches.popleft()
        return float(len(self._window_switches))

    def sample_telemetry_slice(self) -> TelemetrySlice:
        """Gathers real-time motor cadence, focus dwell, and acoustic prosody into TelemetrySlice."""
        now = time.time()

        # 1. Motor Cadence (KPM & Backspace Ratio)
        kpm, backspace_ratio = (0.0, 0.0)
        if hasattr(self, "cadence_tracker") and self.cadence_tracker:
            try:
                kpm, backspace_ratio = self.cadence_tracker.get_cadence(window_seconds=60.0)
            except Exception as e:
                logger.debug("Cadence tracker read notice: %s", e)

        # 2. Window Switches Per Min
        window_switches = self.get_window_switch_frequency(window_seconds=60.0)

        # 3. Dwell-time IDE Focus Ratio
        ide_ratio = self.compute_ide_focus_ratio(window_seconds=60.0)

        # 4. Decaying Active Debug Signals (120s TTL - Requirement 3)
        debug_signals = self.get_active_debug_signals(window_seconds=120.0)

        # 5. Continuous Active Session Duration (Minutes)
        session_mins = max(0.0, self._continuous_active_seconds / 60.0)

        # 6. Whisper Acoustic Prosody with 180s TTL Expiry (Requirement 4)
        speech_rate = 4.2  # Neutral baseline
        if hasattr(self.orchestrator, "stt") and hasattr(self.orchestrator.stt, "last_prosody"):
            last_p = getattr(self.orchestrator.stt, "last_prosody", None)
            if last_p and (now - getattr(last_p, "timestamp", 0.0)) <= 180.0:
                speech_rate = getattr(last_p, "speech_rate_syl_sec", 4.2)

        slice_obj = TelemetrySlice(
            kpm=kpm,
            backspace_ratio=backspace_ratio,
            window_switches_per_min=window_switches,
            ide_focus_ratio=ide_ratio,
            active_debug_signals=debug_signals,
            continuous_session_mins=session_mins,
            speech_rate_syl_sec=speech_rate,
        )

        new_state = self.cognitive_twin.infer_state(slice_obj)

        if new_state != self._last_cognitive_state:
            self._last_cognitive_state = new_state
            if hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
                self.orchestrator.broadcast_ambient_telemetry({
                    "event": "cognitive_state_update",
                    "state": new_state.value,
                    "kpm": int(kpm),
                    "backspace_ratio": round(backspace_ratio, 3),
                    "ide_focus_ratio": round(ide_ratio, 2),
                    "session_mins": int(session_mins),
                    "speech_rate": round(speech_rate, 1),
                })

        return slice_obj

    @classmethod
    def classify_window_action(cls, title: str) -> str:
        """Extracts fine-grained behavioral action tokens from window title (Argon Dimension 1.3)."""
        if not title:
            return "focus:general"
        t = title.lower()

        # Terminal inspection
        if any(term in t for term in ("powershell", "pwsh", "terminal", "command prompt", "cmd.exe", "alacritty", "wezterm")):
            return "focus:terminal"

        # Browser inspection with domain classification (checked before IDE if browser window title)
        if any(b in t for b in ("brave", "chrome", "edge", "firefox", "browser")):
            if "github" in t or "gitlab" in t:
                return "focus:browser:github"
            elif any(doc in t for doc in ("doc", "docs", "api", "stackoverflow", "developer.mozilla", "spec", "manual")):
                return "focus:browser:docs"
            elif any(ai in t for ai in ("chatgpt", "claude", "gemini", "anthropic", "openai", "perplexity")):
                return "focus:browser:ai"
            return "focus:browser:general"

        # IDE inspection with language breakdown
        if cls.is_ide_window(title):
            if any(ext in t for ext in (".py", "python")):
                return "focus:ide:py"
            elif any(ext in t for ext in (".cpp", ".c", ".h", ".hpp", "c++")):
                return "focus:ide:cpp"
            elif any(ext in t for ext in (".tsx", ".ts", "typescript")):
                return "focus:ide:ts"
            elif any(ext in t for ext in (".jsx", ".js", "javascript")):
                return "focus:ide:js"
            elif any(ext in t for ext in (".rs", "rust")):
                return "focus:ide:rust"
            elif any(ext in t for ext in (".go", "golang")):
                return "focus:ide:go"
            elif any(k in t for k in ("test", "spec", "pytest")):
                return "focus:ide:test"
            return "focus:ide:general"

        return "focus:general"


    @classmethod
    def get_action_domain(cls, action: str) -> str:
        """Returns the high-level application domain for a classified action."""
        if action.startswith("focus:ide"):
            return "ide"
        elif action.startswith("focus:browser"):
            return "browser"
        elif action.startswith("focus:terminal"):
            return "terminal"
        return "general"

    def _on_window_event(self, hwnd: int, event_id: int, title: str) -> None:
        """Instant callback triggered by Win32 SetWinEventHook on foreground/title change (<0.02ms)."""
        now = time.time()

        # Requirement 2: Only record a switch when event_id == 0x0003 (EVENT_SYSTEM_FOREGROUND) and hwnd != self._last_hwnd
        if event_id == 0x0003 and hwnd != self._last_hwnd:
            self._window_switches.append(now)
            self._last_hwnd = hwnd

            # Finer Action Granularity (Adjustment 4) & Non-Blocking Hook Dispatch (Adjustment 5)
            if hasattr(self, "habit_graph") and self.habit_graph and title:
                try:
                    action = self.classify_window_action(title)
                    domain = self.get_action_domain(action)
                    cog_state = self.cognitive_twin.state.value if hasattr(self, "cognitive_twin") and self.cognitive_twin else "ROUTINE_ADMIN"
                    pred = self.habit_graph.observe_action(action, cognitive_state=cog_state, domain=domain, timestamp=now)
                    if pred and pred.is_proactive_candidate:
                        if hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
                            self.orchestrator.broadcast_ambient_telemetry({
                                "event": "proactive_habit_prediction",
                                "target_action": pred.target_action,
                                "probability": pred.probability,
                                "confidence": pred.confidence,
                                "transition_count": pred.transition_count,
                            })
                except Exception as h_err:
                    logger.debug("Habit observation notice: %s", h_err)

        if not title:
            return

        # Track dwell time intervals in IDE windows vs non-IDE windows
        is_ide = self.is_ide_window(title)
        if self._current_window_start > 0:
            self._focus_intervals.append((self._current_window_start, now, self._current_window_is_ide))
        self._current_window_start = now
        self._current_window_is_ide = is_ide

        # Check for debug signals in window title
        t_lower = title.lower()
        if any(p in t_lower for p in self.DEBUG_PATTERNS):
            self.record_debug_signal(now)

        if title == self._last_active_window:
            return

        self._last_active_window = title
        try:
            self.orchestrator.memory.remember_fact(
                "active_desktop_context",
                title,
                category="workstation"
            )
        except Exception as e:
            logger.debug("Could not stage active desktop context from hook: %s", e)

        idle_sec = self.get_idle_seconds()
        if self._is_user_idle and idle_sec < 15.0:
            self._is_user_idle = False
            self._sleep_consolidation_done = False
            self._handle_user_return(title)

        if hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
            self.orchestrator.broadcast_ambient_telemetry({
                "event": "window_focus",
                "title": title,
                "hwnd": hwnd,
                "instant_hook": True,
            })

    def start(self):
        """Starts the ambient heartbeat thread and background perception monitors."""
        if not self.enabled:
            logger.info("Heartbeat daemon disabled in configuration.")
            return

        self._running = True
        self._stop_event = getattr(self, '_stop_event', threading.Event())
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._heartbeat_loop, name="JarvisHeartbeatDaemon", daemon=True)
        self._thread.start()

        if self.window_hook_daemon:
            self.window_hook_daemon.start()
        if self.dhash_monitor:
            self.dhash_monitor.start()
        if hasattr(self, "cadence_tracker") and self.cadence_tracker:
            self.cadence_tracker.start()

        logger.info("Jarvis Proactive Heartbeat daemon started.")

    def stop(self):
        """Stops the ambient heartbeat loop and background perception monitors."""
        self._running = False
        self._stop_event = getattr(self, '_stop_event', threading.Event())
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

        if self.window_hook_daemon:
            self.window_hook_daemon.stop()
        if self.dhash_monitor:
            self.dhash_monitor.stop()
        if hasattr(self, "cadence_tracker") and self.cadence_tracker:
            self.cadence_tracker.stop()
        if hasattr(self, "habit_graph") and self.habit_graph:
            self.habit_graph.stop()

        logger.info("Jarvis Proactive Heartbeat daemon stopped.")



    def speak_proactive(self, text: str) -> bool:
        """
        Politely delivers a proactive spoken announcement.
        Bypasses or postpones if user is currently speaking or in push-to-talk.
        """
        if not text or not text.strip():
            return False

        # Guard: Never talk over the user
        if getattr(self.orchestrator, "_is_recording", False) or self.orchestrator.tts.is_speaking:
            logger.info("Heartbeat alert postponed: audio channel is busy.")
            return False

        logger.info(f"Proactive Announcement: {text}")
        print(f"\n[Jarvis Proactive]: {text}\n")

        try:
            self.orchestrator.hud.set_state("speaking")
            self.orchestrator.sfx.play_alert()
            time.sleep(0.35)

            # Start streaming speech
            self.orchestrator.tts.start_streaming_session()
            import re
            sentences = [s.strip() for s in re.split(r"(?<!\d)(?<=[.!?])(?!\d)\s+", text) if s.strip()]
            for s in sentences:
                self.orchestrator.tts.queue_sentence(s)
            self.orchestrator.tts.finish_streaming_session()

            self.orchestrator.sfx.play_done()
            self.orchestrator.hud.set_state("idle")
            return True
        except Exception as e:
            logger.error(f"Error in proactive speech delivery: {e}")
            self.orchestrator.hud.set_state("idle")
            return False

    def deliver_proactive(
        self,
        text: str,
        intrinsic_utility: float = 0.5,
        context_relevance: float = 0.8,
        urgency: float = 0.5,
        can_act_silently: bool = False,
    ) -> bool:
        """Gates proactive announcements through CognitiveTwinEngine EVI evaluation."""
        if not text or not text.strip():
            return False

        modality, score = self.cognitive_twin.evaluate_proactive_action(
            intrinsic_utility=intrinsic_utility,
            context_relevance=context_relevance,
            urgency=urgency,
            can_act_silently=can_act_silently,
        )

        logger.info(
            "Proactive EVI evaluated: text='%s' state=%s modality=%s score=%.2f",
            text[:40], self.cognitive_twin.state.value, modality.value, score
        )

        if modality == DeliveryModality.VOICE:
            return self.speak_proactive(text)
        elif modality == DeliveryModality.HUD_PULSE:
            if hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
                self.orchestrator.broadcast_ambient_telemetry({
                    "event": "proactive_notice",
                    "message": text,
                    "evi_score": round(score, 2),
                    "cognitive_state": self.cognitive_twin.state.value,
                })
            return True
        elif modality == DeliveryModality.SILENT_ACT:
            return True
        else:
            logger.debug("Proactive message suppressed by EVI (score=%.2f): %s", score, text)
            return False

    async def _tick(self) -> None:
        if self._flow_mirror is not None:
            snapshot = self._flow_mirror.get_latest_snapshot()
            if snapshot is not None:
                await self._evaluate_terminal_anomaly(snapshot)

        await self._evaluate_circadian_cadence()

    async def _evaluate_terminal_anomaly(self, snap: FlowStateSnapshot) -> None:
        """
        Dimension 2.1 (Hybrid Dispatch):
        On non-zero terminal exit, immediately push full AST diagnosis & fix diff to the
        Holographic HUD WebSocket and deliver a crisp 5-word spoken chime.
        """
        if snap.last_exit_code is None or snap.last_exit_code == 0:
            self._consecutive_fail_count = 0
            return

        captured_ns = getattr(snap, "captured_monotonic_ns", 0)
        if captured_ns <= self._last_alerted_cmd_ts:
            return

        self._last_alerted_cmd_ts = captured_ns
        self._consecutive_fail_count += 1

        # 1. Generate fast AST diff diagnosis off the main loop if available
        diagnosis_payload: dict[str, Any] = {
            "cmd": snap.last_terminal_cmd or "unknown",
            "exit_code": snap.last_exit_code,
            "active_file": snap.active_file_path,
            "cursor_line": snap.cursor_line,
            "enclosing_symbol": snap.enclosing_ast_symbol,
            "stderr": snap.stderr_tail or "",
            "fix": None,
        }
        if self._ast_diagnoser is not None:
            try:
                diagnosis_payload["fix"] = await asyncio.to_thread(self._ast_diagnoser, snap)
            except Exception as exc:
                logger.debug("Silent AST diagnosis skipped: %s", exc)

        # 2. Dispatch full diagnostic telemetry to the Holographic HUD WebSocket
        try:
            from jarvis.ui.server import broadcast_ambient_telemetry
            await broadcast_ambient_telemetry(
                type="terminal_failure",
                data=diagnosis_payload,
            )
        except Exception:
            if self.orchestrator and hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
                self.orchestrator.broadcast_ambient_telemetry({
                    "event": "terminal_failure",
                    "data": diagnosis_payload,
                })

        # 3. Synthesize crisp 5-word ear-normalized spoken chime
        chime_text = self._format_five_word_chime(snap)
        if self._tts and hasattr(self._tts, "queue_sentence"):
            self._tts.queue_sentence(chime_text)

    @staticmethod
    def _format_five_word_chime(snap: FlowStateSnapshot) -> str:
        """
        Produces a strict 5-word ear-normalized alert (e.g., 'Assertion failed in router test, Sir.').
        """
        stderr = (snap.stderr_tail or "").lower()
        if "assertionerror" in stderr:
            err_noun = "Assertion"
        elif "syntaxerror" in stderr:
            err_noun = "Syntax"
        elif "typeerror" in stderr:
            err_noun = "Type"
        elif "importerror" in stderr or "modulenotfounderror" in stderr:
            err_noun = "Import"
        else:
            err_noun = "Execution"

        target_stem = "workspace"
        if snap.active_file_path:
            stem = Path(snap.active_file_path).stem.replace("test_", "").replace("_test", "")
            target_stem = stem.replace("_", " ").split()[0] if stem else "module"

        # Exact 5-word cadence + vocative pivot: "[Err] failed in [target] test, Sir."
        return f"{err_noun} failed in {target_stem} test, Sir."

    async def _evaluate_circadian_cadence(self) -> None:
        """
        Dimension 2.2 (Subtle British Irony):
        When local time >= 00:00 (00:00 to 05:00) and continuous input >= 120 minutes,
        deliver a rare, understated Paul Bettany quip.
        """
        idle_sec = _get_idle_duration_sec()
        now_mono = time.monotonic()

        if idle_sec >= self._idle_reset_threshold_sec:
            self._continuous_flow_start_mono = now_mono
            return

        continuous_minutes = (now_mono - self._continuous_flow_start_mono) / 60.0
        local_hour = datetime.datetime.now().hour

        is_late_night = 0 <= local_hour < 5
        cooldown_elapsed = (now_mono - self._last_circadian_quip_mono) >= self._circadian_cooldown_sec

        if is_late_night and continuous_minutes >= 120.0 and cooldown_elapsed:
            self._last_circadian_quip_mono = now_mono
            quip = (
                "Operating well past optimal circadian parameters, Sir. "
                "I have prepared coffee diagnostics, though physical delivery "
                "remains outside my present capabilities."
            )
            try:
                from jarvis.ui.server import broadcast_ambient_telemetry
                await broadcast_ambient_telemetry(
                    type="circadian_advisory",
                    data={"continuous_minutes": round(continuous_minutes, 1), "message": quip},
                )
            except Exception:
                if self.orchestrator and hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
                    self.orchestrator.broadcast_ambient_telemetry({
                        "event": "circadian_advisory",
                        "data": {"continuous_minutes": round(continuous_minutes, 1), "message": quip},
                    })
            if self._tts and hasattr(self._tts, "queue_sentence"):
                self._tts.queue_sentence(quip)

    def _heartbeat_loop(self):
        """Main periodic evaluation loop."""
        # Initial boot delay
        time.sleep(5.0)
        last_tick = time.time()

        while self._running:
            try:
                now = time.time()
                elapsed = max(0.0, now - last_tick)
                last_tick = now
                if now >= self._startup_grace_period:
                    try:
                        asyncio.run(self._tick())
                    except Exception as tick_err:
                        logger.debug("Heartbeat async tick notice: %s", tick_err)
                    if self.desktop_presence:
                        self._check_desktop_presence()
                    try:
                        self.sample_telemetry_slice()
                    except Exception as t_err:
                        logger.debug("Heartbeat telemetry sampling notice: %s", t_err)
                    if self.auto_briefing:

                        self._check_morning_briefing()
                    if self.calendar_alerts:
                        self._check_calendar_events()
                    if self.battery_alerts:
                        self._check_battery_status()
                    if self.ergonomic_alerts:
                        self._check_ergonomics(elapsed)
                    if self.email_alerts:
                        self._check_new_emails()
            except Exception as e:
                logger.error(f"Heartbeat tick exception: {e}")

            # Laptop Thermal Profile: relax timer frequency on battery power to prevent waking CPU cores
            sleep_duration = self.tick_interval * 2 if self._is_on_battery() else self.tick_interval
            getattr(self, '_stop_event', threading.Event()).wait(timeout=sleep_duration)

    def get_idle_seconds(self) -> float:
        """Returns the number of seconds since the last physical keyboard or mouse input (<0.01ms)."""
        try:
            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                millis = (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
                return max(0.0, float(millis) / 1000.0)
        except Exception as e:
            logger.debug(f"Win32 GetLastInputInfo notice: {e}")
        return 0.0

    def get_active_window_title(self) -> str:
        """Returns the window title of the current foreground window in <0.05ms."""
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if hwnd:
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                    return buf.value.strip()
        except Exception as e:
            logger.debug(f"Win32 GetForegroundWindow notice: {e}")
        return ""

    def _check_desktop_presence(self):
        """Monitors user presence and active workstation context (Wake-Decide-Sleep architecture)."""
        idle_sec = self.get_idle_seconds()
        current_title = self.get_active_window_title()

        # 1. User Idle / Absence State Transition
        if idle_sec >= self.idle_threshold_seconds:
            if not self._is_user_idle:
                self._is_user_idle = True
                logger.info(f"Workstation user presence transitioned to IDLE ({idle_sec:.0f}s elapsed).")
                if hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
                    self.orchestrator.broadcast_ambient_telemetry({
                        "event": "user_idle",
                        "idle_duration_s": round(idle_sec, 1),
                    })
                # Trigger zero-contention sleep-phase memory consolidation on WAL snapshot
                if not self._sleep_consolidation_done:
                    self._sleep_consolidation_done = True
                    if hasattr(self.orchestrator, "memory") and hasattr(self.orchestrator.memory, "consolidate_sleep_phase"):
                        try:
                            res = self.orchestrator.memory.consolidate_sleep_phase(
                                is_user_active_fn=lambda: self.get_idle_seconds() < self.idle_threshold_seconds
                            )
                            if hasattr(self.orchestrator, "broadcast_ambient_telemetry") and res:
                                dist = res.get("distillation", {})
                                dupes = res.get("duplicates_merged", 0)
                                edges = res.get("edges_promoted", 0)
                                macros = res.get("reflex_macros_compiled", 0)
                                if dupes > 0 or edges > 0 or macros > 0:
                                    self.orchestrator.broadcast_ambient_telemetry({
                                        "event": "sleep_consolidation_complete",
                                        "duplicates_merged": dupes,
                                        "edges_promoted": edges,
                                        "reflex_macros_compiled": macros,
                                        "wal_pages_checkpointed": res.get("wal_pages_checkpointed", 0),
                                    })
                                elif dist.get("traits_extracted", 0) > 0 or res.get("facts_pruned", 0) > 0:
                                    self.orchestrator.broadcast_ambient_telemetry({
                                        "event": "memory_consolidated",
                                        "traits_extracted": dist.get("traits_extracted", 0),
                                        "facts_pruned": res.get("facts_pruned", 0),
                                        "decayed_habits": res.get("decayed_habits", 0),
                                    })
                        except Exception as c_err:
                            logger.debug(f"Sleep-phase consolidation notice: {c_err}")
        elif self._is_user_idle and idle_sec < 15.0:
            # 2. User Return Detection (User resumed activity after >idle_threshold_seconds)
            self._is_user_idle = False
            self._sleep_consolidation_done = False
            logger.info("Workstation user presence transitioned to ACTIVE (Sir has returned).")
            self._handle_user_return(current_title)

        # 3. Active Window Focus Transition Tracking
        if current_title and current_title != self._last_active_window:
            self._last_active_window = current_title
            try:
                self.orchestrator.memory.remember_fact(
                    "active_desktop_context",
                    current_title,
                    category="workstation"
                )
            except Exception as e:
                logger.debug(f"Could not stage active desktop context: {e}")

            if hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
                self.orchestrator.broadcast_ambient_telemetry({
                    "event": "window_focus",
                    "title": current_title,
                })

    def _handle_user_return(self, current_title: str):
        """Processes user return: stages context, updates HUD, and checks for imminent meetings."""
        now = time.time()
        # Cooldown of at least 5 minutes between return alerts to prevent flapping
        if now - self._last_return_alert < 300:
            return

        self._last_return_alert = now

        if current_title:
            try:
                self.orchestrator.memory.remember_fact(
                    "active_desktop_context",
                    current_title,
                    category="workstation"
                )
            except Exception:
                pass

        if hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
            self.orchestrator.broadcast_ambient_telemetry({
                "event": "user_returned",
                "active_context": current_title,
            })

        # Check for urgent upcoming calendar events starting in <= 10 minutes
        try:
            events = self.orchestrator.memory.get_calendar_events()
            dt_now = datetime.datetime.now()
            for event in events:
                eid = event["id"]
                if eid in self._alerted_event_ids:
                    continue
                start_dt = self._parse_time(event["start_time"], event.get("created_at"))
                if not start_dt:
                    continue
                delta_minutes = int((start_dt - dt_now).total_seconds() // 60)
                if 0 <= delta_minutes <= 10:
                    title = event["title"]
                    loc = f" at {event['location']}" if event.get("location") else ""
                    time_desc = "shortly" if delta_minutes <= 1 else f"in {delta_minutes} minutes"
                    msg = f"Welcome back, Sir. Quick reminder: '{title}' commences {time_desc}{loc}."
                    if self.speak_proactive(msg):
                        self._mark_event_alerted(eid)
                        break
        except Exception as e:
            logger.debug(f"Return calendar check notice: {e}")

    def _is_on_battery(self) -> bool:
        """Returns True if workstation is running on battery power (unplugged)."""
        try:
            status = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return status.ACLineStatus == 0
        except Exception:
            pass
        return False

    def _check_morning_briefing(self):
        """Delivers autonomous standup briefing if morning and not yet delivered today."""
        today_str = datetime.date.today().isoformat()
        if self._briefing_delivered_today:
            return

        now = datetime.datetime.now()
        # Eligible morning hours: 06:00 to 12:30 PM
        if 6 <= now.hour < 13:
            logger.info("Triggering autonomous morning briefing...")
            delivered = self.deliver_briefing()
            if delivered:
                self._briefing_delivered_today = True
                self.orchestrator.memory.remember_fact("last_briefing_date", today_str, category="system")

    def deliver_briefing(self) -> bool:
        """Compiles and speaks the executive standup briefing."""
        try:
            from jarvis.skills.calendar_manager import morning_protocol
            raw_telemetry = morning_protocol()

            prompt = (
                f"You are JARVIS delivering your executive spoken standup briefing to Sir. "
                f"Synthesize the following telemetry into a concise, 3-to-4 sentence natural spoken briefing. "
                f"Keep it elegant, composed, and direct for audio text-to-speech. "
                f"Do not read raw email IDs, bullet lists, or URLs.\n\n"
                f"Telemetry:\n{raw_telemetry}"
            )

            # Generate natural spoken summary through Brain
            spoken_summary = self.orchestrator.brain.process_query(prompt)
            if spoken_summary:
                return self.speak_proactive(spoken_summary)
            return False
        except Exception as e:
            logger.error(f"Failed to generate morning briefing: {e}")
            return False

    def _mark_event_alerted(self, eid: int):
        """Marks an event as alerted and persists to memory so restarts do not repeat it."""
        self._alerted_event_ids.add(eid)
        try:
            today_str = datetime.date.today().isoformat()
            self.orchestrator.memory.remember_fact(
                f"alerted_events_{today_str}",
                ",".join(str(i) for i in self._alerted_event_ids),
                category="system"
            )
        except Exception as e:
            logger.debug(f"Could not persist alerted event id {eid}: {e}")

    def _parse_time(self, timestr: str, created_at: str | None = None) -> datetime.datetime | None:
        """Parses various date/time formats into a datetime object."""
        timestr = timestr.strip()
        now = datetime.datetime.now()
        formats = [
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %I:%M %p",
            "%H:%M",
            "%I:%M %p",
            "%I %p",
        ]
        for fmt in formats:
            try:
                dt = datetime.datetime.strptime(timestr, fmt)
                if fmt in ("%H:%M", "%I:%M %p", "%I %p"):
                    if created_at:
                        try:
                            # created_at is typically "YYYY-MM-DD HH:MM:SS"
                            c_dt = datetime.datetime.fromisoformat(created_at.strip())
                            # If created on an earlier day without explicit date, it was for that creation day
                            dt = dt.replace(year=c_dt.year, month=c_dt.month, day=c_dt.day)
                        except Exception:
                            dt = dt.replace(year=now.year, month=now.month, day=now.day)
                    else:
                        dt = dt.replace(year=now.year, month=now.month, day=now.day)
                return dt
            except ValueError:
                pass
        return None

    def _check_calendar_events(self):
        """Checks for events starting within the next 15 minutes."""
        try:
            events = self.orchestrator.memory.get_calendar_events()
            now = datetime.datetime.now()

            for event in events:
                eid = event["id"]
                if eid in self._alerted_event_ids:
                    continue

                start_dt = self._parse_time(event["start_time"], event.get("created_at"))
                if not start_dt:
                    continue

                # Calculate delta in seconds and minutes
                delta_seconds = (start_dt - now).total_seconds()
                delta_minutes = int(delta_seconds // 60)

                # If event is already in the past (more than 10 minutes ago), mark alerted to prevent lingering
                if delta_seconds < -600:
                    self._mark_event_alerted(eid)
                    continue

                # Broadcast countdown to HUD if within 30 minutes
                if 0 <= delta_minutes <= 30:
                    if hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
                        self.orchestrator.broadcast_ambient_telemetry({
                            "event": "calendar_countdown",
                            "event_id": eid,
                            "title": event["title"],
                            "start_time": event["start_time"],
                            "minutes_remaining": delta_minutes,
                            "location": event.get("location", ""),
                        })

                # Alert if event is 0 to 15 minutes away
                if 0 <= delta_minutes <= 15:
                    title = event["title"]
                    loc = f" at {event['location']}" if event.get("location") else ""
                    time_desc = "shortly" if delta_minutes <= 1 else f"in {delta_minutes} minutes"
                    msg = f"Pardon the interruption, Sir. You have '{title}' scheduled {time_desc}{loc}."
                    
                    if self.deliver_proactive(msg, intrinsic_utility=0.85, context_relevance=0.95, urgency=0.9):
                        self._mark_event_alerted(eid)
                        break  # Announce one at a time to prevent stacking
        except Exception as e:
            logger.debug(f"Calendar check notice: {e}")

    def _check_battery_status(self):
        """Alerts if battery drops below 20% while running on battery power."""
        now = time.time()
        if now - self._last_battery_alert < 1800:  # 30-minute cooldown
            return

        try:
            status = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                # ACLineStatus == 0 means running on battery (unplugged)
                if status.ACLineStatus == 0 and status.BatteryLifePercent <= 20:
                    pct = status.BatteryLifePercent
                    msg = f"Sir, workstation battery reserves have fallen to {pct}%. I recommend connecting to AC power."
                    if self.deliver_proactive(msg, intrinsic_utility=0.9, context_relevance=1.0, urgency=0.95):
                        self._last_battery_alert = now
        except Exception as e:
            logger.debug(f"Power check notice: {e}")

    def _check_ergonomics(self, elapsed_seconds: float = 30.0):
        """Monitors uninterrupted physical keyboard/mouse activity and provides ergonomic pacing."""
        idle_sec = self.get_idle_seconds()
        if idle_sec < 15.0:
            self._continuous_active_seconds += elapsed_seconds
        elif idle_sec >= 300.0:
            # 5-minute break resets the continuous work clock
            self._continuous_active_seconds = 0.0

        now = time.time()
        is_extended_work = self._continuous_active_seconds >= 5400 or (now - self._session_start_time >= 7200)
        if is_extended_work and (now - self._last_break_reminder >= 3600):
            drink = self.orchestrator.memory.get_fact("late_night_drink") or "espresso"
            drink_clean = "an espresso" if "espresso" in drink.lower() else "a refreshment"
            active_mins = max(60, int(self._continuous_active_seconds // 60)) if self._continuous_active_seconds else 120
            msg = f"Sir, you have been actively engaged at the workstation for over {active_mins} minutes. May I suggest a brief stretch or taking a moment to enjoy {drink_clean}?"
            
            # Broadcast ambient HUD notification card
            if hasattr(self.orchestrator, "broadcast_ambient_telemetry"):
                self.orchestrator.broadcast_ambient_telemetry({
                    "event": "ergonomic_break",
                    "continuous_active_minutes": active_mins,
                    "message": msg,
                })

            if self.deliver_proactive(msg, intrinsic_utility=0.35, context_relevance=0.5, urgency=0.25):
                self._last_break_reminder = now

    def _check_new_emails(self):
        """Checks for new unread incoming emails every 15 minutes."""
        now = time.time()
        if now - self._last_email_check < 900:  # 15 minutes
            return

        self._last_email_check = now
        try:
            from jarvis.skills.email_manager import get_unread_email_count
            current_count = get_unread_email_count()

            if self._last_unread_email_count is None:
                # First run initializes baseline count
                self._last_unread_email_count = current_count
                return

            if current_count > self._last_unread_email_count:
                delta = current_count - self._last_unread_email_count
                self._last_unread_email_count = current_count
                msg = f"Pardon me, Sir. You have received {delta} new unread email{'s' if delta > 1 else ''} in your inbox."
                self.speak_proactive(msg)
            else:
                self._last_unread_email_count = current_count
        except Exception as e:
            logger.debug(f"Email heartbeat check notice: {e}")

