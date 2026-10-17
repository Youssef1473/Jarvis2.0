"""Acoustic Interim Bridge for Full-Duplex Multi-Step Agency in Jarvis 2.0.

Provides speculative, non-blocking conversational status updates during extended
tool-use loops to eliminate awkward dead air while Kokoro-82M synthesizes speech.
"""
import logging
import threading
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger("Jarvis.Pipeline.InterimBridge")

# Bettany status phrases engineered for ear-level TTS delivery (Kokoro-82M)
_INTERIM_PHRASE_MAP: Dict[str, str] = {
    "web_search": "Consulting live network archives, Sir.",
    "deep_research": "Synthesizing multi-source technical intelligence, Sir.",
    "inspect_file_locks": "Interrogating Windows kernel handles for file locks, Sir.",
    "release_file_lock": "Surgically releasing non-critical file handles, Sir.",
    "run_job_sandboxed_process": "Executing within an isolated kernel job container, Sir.",
    "execute_powershell": "Running requested commands in workspace terminal, Sir.",
    "execute_sandboxed_powershell": "Executing in E-core sandboxed environment, Sir.",
    "browser_navigate": "Navigating via silent DevTools channel, Sir.",
    "read_webpage_content_cdp": "Extracting DOM text via Chrome DevTools Protocol, Sir.",
    "read_archived_document": "Querying local vector vault, Sir.",
    "document_ingest": "Distilling document parameters into memory, Sir.",
    "audit_vram_and_thermal_budget": "Inspecting GPU VRAM telemetry, Sir.",
    "get_recent_workspace_mutations": "Scanning recent workspace mutations, Sir.",
    "compile_workflow_reflex": "Compiling deterministic reflex macro, Sir.",
    "execute_workflow_reflex": "Replaying compiled workflow reflex, Sir.",
}

DEFAULT_INTERIM_PHRASE = "One moment, Sir, coordinating subsystems."


class InterimSpeechBridge:
    """Manages timed, non-blocking acoustic feedback during multi-step tool execution."""

    def __init__(
        self,
        speak_fn: Optional[Callable[[str], None]] = None,
        is_speaking_fn: Optional[Callable[[], bool]] = None,
        delay_step1_sec: float = 0.75,
        delay_step_n_sec: float = 0.20,
        cooldown_sec: float = 2.8,
    ):
        self.speak_fn = speak_fn
        self.is_speaking_fn = is_speaking_fn or (lambda: False)
        self.delay_step1_sec = delay_step1_sec
        self.delay_step_n_sec = delay_step_n_sec
        self.cooldown_sec = cooldown_sec

        self._lock = threading.Lock()
        self._pending_timer: Optional[threading.Timer] = None
        self._last_speech_time: float = 0.0
        self._spoken_count: int = 0

    def get_interim_phrase(self, tool_name: str) -> str:
        """Selects a context-appropriate, calm status phrase for the given tool."""
        return _INTERIM_PHRASE_MAP.get(tool_name, DEFAULT_INTERIM_PHRASE)

    def on_tool_start(self, tool_name: str, step_index: int = 1) -> None:
        """Arms a non-blocking timer to speak an interim status phrase if execution stalls."""
        with self._lock:
            self.cancel_pending()

            now = time.time()
            if now - self._last_speech_time < self.cooldown_sec:
                logger.debug("Interim speech suppressed: cooldown active (%.2fs)", now - self._last_speech_time)
                return

            delay = self.delay_step1_sec if step_index <= 1 else self.delay_step_n_sec
            phrase = self.get_interim_phrase(tool_name)

            def _fire():
                with self._lock:
                    if self.is_speaking_fn():
                        logger.debug("Interim speech aborted: system is already speaking.")
                        return

                    self._last_speech_time = time.time()
                    self._spoken_count += 1
                    logger.info("Acoustic interim phrase triggered for '%s': %s", tool_name, phrase)

                if self.speak_fn:
                    try:
                        self.speak_fn(phrase)
                    except Exception as err:
                        logger.warning("Error dispatching interim speech: %s", err)

            self._pending_timer = threading.Timer(delay, _fire)
            self._pending_timer.daemon = True
            self._pending_timer.start()

    def on_tool_end(self) -> None:
        """Disarms any pending interim speech timer when tool returns in time."""
        with self._lock:
            self.cancel_pending()

    def cancel_pending(self) -> None:
        """Cancels any armed timer immediately."""
        if self._pending_timer and self._pending_timer.is_alive():
            self._pending_timer.cancel()
            self._pending_timer = None

    def on_final_response_start(self) -> None:
        """Disarms interim bridge when the model starts producing its final turn response."""
        self.cancel_pending()

    @property
    def spoken_count(self) -> int:
        with self._lock:
            return self._spoken_count
