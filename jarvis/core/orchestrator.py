"""Central orchestrator coordinating sensory inputs, cognitive routing, speech, and holographic HUD."""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import threading
import time
from typing import Any

import numpy as np
import sounddevice as sd

from jarvis.audio.hotkey_listener import HotkeyListener
from jarvis.audio.sound_effects import SoundEffects
from jarvis.audio.speech_capture import AdaptiveSpeechCapture
from jarvis.audio.stt import SpeechToText
from jarvis.audio.tts import TextToSpeech
from jarvis.audio.wake_word import WakeWordDetector
from jarvis.core.brain import JarvisBrain
from jarvis.core.cognition.intent_acknowledgments import (
    extract_topic_label,
    get_immediate_intent_acknowledgment,
    is_shutdown_command,
)
from jarvis.core.heartbeat import JarvisHeartbeat
from jarvis.core.memory import MemoryManager
from jarvis.core.orchestrator_lifecycle import (
    OrchestratorShutdownCoordinator,
    trim_process_memory,
)
from jarvis.core.safety import SafetyGate
from jarvis.core.services import ManagedServiceManager
from jarvis.ui.hud import JarvisHUD

logger = logging.getLogger("Jarvis.Orchestrator")

__all__ = [
    "HotkeyListener",
    "JarvisBrain",
    "JarvisHUD",
    "JarvisHeartbeat",
    "JarvisOrchestrator",
    "MemoryManager",
    "SafetyGate",
    "SoundEffects",
    "SpeechToText",
    "TextToSpeech",
    "WakeWordDetector",
    "is_shutdown_command",
    "trim_process_memory",
]


class JarvisOrchestrator:
    """Coordinates Jarvis's sensory inputs, cognitive loop, speech output, holographic HUD, and proactive heartbeat."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.sample_rate = config.get("audio", {}).get("sample_rate", 16000)

        # Resolve and validate active workspace (fails closed on empty or 'unscoped')
        raw_ws = config.get("workspace_id") or config.get("active_workspace_id") or "ws_default"
        clean_ws = str(raw_ws).strip()
        if not clean_ws or clean_ws.lower() == "unscoped":
            raise ValueError("Invalid active_workspace_id: cannot use empty or unscoped workspace.")
        self.active_workspace_id = clean_ws

        # Subsystems
        self.sfx = SoundEffects(config)
        self.memory = MemoryManager(config)
        if hasattr(self.memory, "set_active_context"):
            try:
                self.memory.set_active_context(self.active_workspace_id, None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Active context activation notice: %s", exc)

        try:
            import jarvis.skills.productivity as prod_skills

            prod_skills.set_memory_manager(self.memory)
        except (ImportError, AttributeError) as exc:
            logger.debug("Productivity skills memory binding notice: %s", exc)

        try:
            import jarvis.skills.self_evolution as evo_skills

            evo_skills.set_memory_manager(self.memory)
            evo_skills.init_custom_skills()
        except (ImportError, AttributeError, RuntimeError) as exc:
            logger.warning("Failed to initialize custom skills: %s", exc)

        self.safety = SafetyGate(config)
        self.stt = SpeechToText(config)
        self.tts = TextToSpeech(config)
        try:
            import jarvis.skills.voice_control as voice_skills

            voice_skills.set_tts_instance(self.tts)
        except (ImportError, AttributeError) as exc:
            logger.debug("Voice control skills TTS binding notice: %s", exc)

        self.service_manager = ManagedServiceManager()
        self.brain = JarvisBrain(
            config,
            self.memory,
            self.safety,
            service_manager=self.service_manager,
            workspace_id=self.active_workspace_id,
        )
        self.hud = JarvisHUD()
        self.heartbeat = JarvisHeartbeat(self, config)

        from jarvis.core.task_manager import BackgroundTaskManager

        self.task_manager = BackgroundTaskManager.get_instance(self)

        from jarvis.conversation.bookmarks import ReasoningBookmarkStack

        self.bookmarks = ReasoningBookmarkStack.get_instance()

        from jarvis.core.pipeline.latency_profiler import PipelineLatencyProfiler
        from jarvis.skills.diagnostics import get_global_latency_profiler

        self.latency_profiler: PipelineLatencyProfiler = (
            getattr(self.brain, "latency_profiler", None) or get_global_latency_profiler()
        )
        self.tool_orchestrator = getattr(self.brain, "tool_orchestrator", None)

        self._active_turn_id: str | None = None
        self._active_query: str = ""
        self._active_topic_label: str = "the architecture"
        self._active_tool_outputs: dict[str, Any] = {}
        self._active_partial_text: str | None = None

        # Web-Powered Holographic HUD Server
        ui_cfg = config.get("ui", {})
        if ui_cfg.get("enabled", True):
            from jarvis.ui.server import JarvisUIService

            self.ui_service: JarvisUIService | None = JarvisUIService(
                orchestrator=self,
                host=ui_cfg.get("host", "127.0.0.1"),
                port=int(ui_cfg.get("port", 20129)),
            )
        else:
            self.ui_service = None

            def _cli_approval_handler(details: dict[str, Any]) -> bool:
                if not sys.stdin or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
                    return False
                print("\n[JARVIS DUAL-KEY SECURITY GATE]")
                print(f" Tool: {details.get('tool_name')}")
                print(f" Action: {details.get('summary')}")
                print(f" Hash: {details.get('action_hash')}")
                try:
                    ans = input(" Authorize execution? [y/N]: ").strip().lower()
                    return ans in ("y", "yes")
                except (EOFError, KeyboardInterrupt):
                    return False

            from jarvis.core.security_boundaries import DualKeyApprovalManager

            DualKeyApprovalManager.get_instance().set_approval_handler(_cli_approval_handler)

        if hasattr(self.brain, "set_tts_instance"):
            self.brain.set_tts_instance(self.tts)
        if getattr(self, "ui_service", None) and hasattr(self.brain, "set_tool_status_callback"):
            self.brain.set_tool_status_callback(self._on_tool_status_event)

        # Mute and Push-to-Talk (PTT) states
        self._is_muted = False
        self._is_recording = False
        self._is_ptt_recording = False
        self.last_recording_had_overflow = False
        self._recorded_frames: list[np.ndarray] = []
        self._record_stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._ptt_press_time: float = 0.0

        # Background listeners
        self.hotkey_listener = HotkeyListener(
            config=config,
            on_press_callback=self._on_ptt_press,
            on_release_callback=self._on_ptt_release,
            on_any_key_callback=None,
        )
        self.wake_detector = WakeWordDetector(
            config,
            on_wake_callback=self._on_wake_word,
            is_speaking_callback=lambda: self.tts.is_speaking,
        )

        self._running = False
        self._stopped = False
        self._current_state = "idle"
        self._is_processing_command = False

    def set_state(self, new_state: str) -> None:
        """Unified state switcher notifying both desktop Tkinter HUD and Web Holographic HUD."""
        self._current_state = new_state
        self.hud.set_state(new_state)
        if getattr(self, "ui_service", None):
            self.ui_service.broadcast_state(new_state)

    def get_state(self) -> str:
        """Returns the current operational state of Jarvis."""
        return getattr(self, "_current_state", "idle")

    def abort_active_command(self) -> bool:
        """Execution kill-switch: immediately halts in-flight cognitive processing, tool execution, and speech."""
        from jarvis.core.cancellation import signal_global_barge_in

        epoch = signal_global_barge_in()
        was_busy = False
        spoken_clauses, unspoken_clauses = [], []
        if hasattr(self.tts, "abort_and_partition_clauses"):
            try:
                res = self.tts.abort_and_partition_clauses()
                if isinstance(res, (tuple, list)) and len(res) == 2:
                    spoken_clauses, unspoken_clauses = res
            except Exception as exc:  # noqa: BLE001
                logger.debug("TTS clause partitioning notice: %s", exc)

        if self.tts.is_speaking:
            self.tts.stop()
            was_busy = True

        if hasattr(self, "wake_detector"):
            try:
                self.wake_detector.stop_vad_session()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Wake detector VAD session stop notice: %s", exc)

        brain = getattr(self, "brain", None)
        if brain is not None and hasattr(brain, "cancel_active_turn"):
            try:
                brain.cancel_active_turn()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Brain turn cancellation notice: %s", exc)

        # Sync resolved tool outputs from brain if available
        active_tools = dict(getattr(self, "_active_tool_outputs", {}) or {})
        if brain is not None and hasattr(brain, "get_current_turn_tool_outputs"):
            try:
                brain_tools = brain.get_current_turn_tool_outputs()
                if brain_tools:
                    active_tools.update(brain_tools)
                    if hasattr(self, "_active_tool_outputs") and isinstance(self._active_tool_outputs, dict):
                        self._active_tool_outputs.update(brain_tools)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Tool outputs sync notice: %s", exc)

        remaining_clauses = list(unspoken_clauses or [])
        if not remaining_clauses:
            partial_text = getattr(self, "_active_partial_text", None)
            if partial_text:
                remaining_clauses = [partial_text]
            elif spoken_clauses:
                remaining_clauses = ["remaining explanation"]

        if getattr(self, "_active_turn_id", None) and hasattr(self, "bookmarks"):
            flow_xml = ""
            try:
                from jarvis.context.flow_state_mirror import FlowStateMirror

                flow_xml = FlowStateMirror.get_instance().format_quarantined_xml()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flow state XML formatting notice: %s", exc)

            if remaining_clauses:
                self.bookmarks.push_interrupted_turn(
                    turn_id=self._active_turn_id,
                    original_query=getattr(self, "_active_query", ""),
                    topic_label=getattr(self, "_active_topic_label", "the architecture"),
                    spoken_clauses=spoken_clauses,
                    unspoken_clauses=remaining_clauses,
                    resolved_tool_outputs=active_tools,
                    flow_state_xml=flow_xml,
                )

        if getattr(self, "_is_processing_command", False) or getattr(self, "_current_state", "idle") in (
            "thinking",
            "speaking",
        ):
            was_busy = True
            logger.info("Active command execution aborted via kill-switch (epoch -> %d).", epoch)
            self.set_state("idle")
            if getattr(self, "ui_service", None):
                try:
                    self.ui_service.broadcast_message("jarvis", "[Execution aborted by user interrupt]")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("UI broadcast message abort notice: %s", exc)
        return was_busy

    def on_atomic_barge_in(self) -> None:
        """Callback invoked immediately when the Win32 Named Event barge-in fires (<1us)."""
        self.abort_active_command()

    def broadcast_ambient_telemetry(self, data: dict[str, Any]) -> None:
        """Dispatches ambient desktop context and presence telemetry to the Holographic HUD."""
        if getattr(self, "ui_service", None):
            try:
                self.ui_service.broadcast({
                    "type": "ambient_telemetry",
                    "data": data,
                    "timestamp": time.strftime("%H:%M:%S"),
                })
            except Exception as exc:  # noqa: BLE001
                logger.debug("Ambient telemetry broadcast notice: %s", exc)

    def _on_tool_status_event(self, event: dict[str, Any]) -> None:
        """Dispatches live tool execution status events to the Holographic HUD."""
        if getattr(self, "ui_service", None):
            try:
                self.ui_service.broadcast({
                    "type": "tool_execution",
                    "data": event,
                    "timestamp": time.strftime("%H:%M:%S"),
                })
            except Exception as exc:  # noqa: BLE001
                logger.debug("Tool status broadcast notice: %s", exc)

    def is_muted(self) -> bool:
        """Returns True if voice listening is currently muted."""
        return self._is_muted

    def toggle_mute(self) -> bool:
        """Toggles audio sensing mute state. Returns new muted status."""
        self._is_muted = not self._is_muted
        if getattr(self, "ui_service", None):
            self.ui_service.broadcast_telemetry({"is_muted": self._is_muted})

        if self._is_muted:
            logger.info("Jarvis audio sensors MUTED.")
            if self.tts.is_speaking:
                self.tts.stop()
            self.wake_detector.stop()
            self.set_state("idle")
        else:
            logger.info("Jarvis audio sensors UNMUTED.")
            self.wake_detector.start()
            self.sfx.play_wake()
        return self._is_muted

    def _on_key_activity(self) -> None:
        """Barge-in hook: interrupts speech and active commands if user starts typing or presses a key."""
        if self.tts.is_speaking:
            self.abort_active_command()

    def _audio_record_callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        """Accumulates microphone audio during push-to-talk."""
        AdaptiveSpeechCapture.audio_record_callback(self, indata, frames, time_info, status)

    def _on_ptt_press(self) -> None:
        """Called immediately when user presses PTT trigger (MB2 or configured key)."""
        AdaptiveSpeechCapture.on_ptt_press(self, self._process_audio)

    def _on_ptt_release(self) -> None:
        """Called immediately when user releases PTT trigger."""
        AdaptiveSpeechCapture.on_ptt_release(self, self._process_audio)

    def _finalize_ptt_recording(self) -> None:
        """Concludes PTT recording session and initiates audio processing."""
        AdaptiveSpeechCapture.finalize_ptt_recording(self, self._process_audio)

    def _on_wake_word(self) -> None:
        """Called when openWakeWord detects 'Hey Jarvis'."""
        if self._is_muted or self._is_recording:
            return

        if self.tts.is_speaking:
            self.tts.stop()

        logger.info("Wake word detected! Listening with dynamic VAD auto-cutoff...")
        self.set_state("listening")
        self.sfx.play_wake()
        AdaptiveSpeechCapture.start_vad_capture(self, self._process_audio)

    def _process_audio(self, audio_array: np.ndarray) -> None:
        """Transcribes audio, sends to brain, and delivers spoken output."""
        try:
            from jarvis.audio.affinity import pin_and_elevate_current_thread

            pin_and_elevate_current_thread(tag="OrchestratorAudioWorker")
        except (ImportError, AttributeError, OSError) as exc:
            logger.debug("Audio worker affinity elevation notice: %s", exc)

        self.set_state("thinking")
        self.sfx.play_think()
        text = self.stt.transcribe(audio_array, sample_rate=self.sample_rate)
        if not text or len(text.strip()) < 2:
            logger.info("Audio was unintelligible or empty.")
            self.set_state("idle")
            return

        print(f"\n[Sir]: {text}")
        try:
            self.handle_command(text)
        except Exception:
            logger.exception("Error handling voice command '%s'", text)
            self.set_state("idle")
            self.sfx.play_done()

    @staticmethod
    def is_shutdown_command(query: str) -> bool:
        """Determines if query is a departure or shutdown request."""
        return is_shutdown_command(query)

    @staticmethod
    def _extract_topic_label(query: str) -> str:
        """Derives a concise 2-4 word topic phrase from the query for bookmark bridging."""
        return extract_topic_label(query)

    async def handle_user_utterance(self, turn_id: str, utterance: str) -> str:
        """Main cognitive execution entrypoint with automatic bookmark resumption."""
        self._active_turn_id = turn_id
        self._active_query = utterance
        self._active_tool_outputs.clear()

        if hasattr(self, "latency_profiler") and self.latency_profiler:
            self.latency_profiler.begin_turn(turn_id)
            self.latency_profiler.mark(turn_id, "stt_final_ns")

        bookmark_xml = (
            self.bookmarks.format_prompt_injection(utterance)
            if hasattr(self, "bookmarks") and self.bookmarks
            else ""
        )
        flow_xml = ""
        try:
            from jarvis.context.flow_state_mirror import FlowStateMirror

            flow_xml = FlowStateMirror.get_instance().format_quarantined_xml()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Flow state mirror XML format notice: %s", exc)

        if hasattr(self.brain, "generate_turn"):
            response_text, topic_label, tool_outputs = await self.brain.generate_turn(
                utterance=utterance,
                flow_state_xml=flow_xml,
                bookmark_context=bookmark_xml,
            )
            self._active_topic_label = topic_label
            self._active_tool_outputs.update(tool_outputs)
            if hasattr(self, "latency_profiler") and self.latency_profiler:
                self.latency_profiler.mark(turn_id, "tts_first_chunk_ns")
                self.latency_profiler.mark(turn_id, "audio_first_frame_ns")
                self.latency_profiler.complete_turn(turn_id)
            if hasattr(self.tts, "queue_response_clauses"):
                self.tts.queue_response_clauses(turn_id=turn_id, text=response_text)
            return response_text
        return self.handle_command(utterance)

    def handle_command(self, query: str) -> None:
        """Processes a text query, executes tools, and speaks the reply."""
        # Broadcast prompt to Holographic HUD
        if getattr(self, "ui_service", None):
            self.ui_service.broadcast_message("user", query)

        # Immediate direct shutdown handling
        if self.is_shutdown_command(query):
            farewell = "Powering down systems. Goodbye, Sir."
            print(f"[Jarvis]: {farewell}\n")
            if getattr(self, "ui_service", None):
                self.ui_service.broadcast_message("jarvis", farewell)
            self.sfx.play_done()
            self.set_state("speaking")
            # Speak synchronously so audio finishes completely without requiring an event loop
            self.tts.speak_sync(farewell)
            self.set_state("idle")
            self.stop()
            if threading.current_thread() is threading.main_thread():
                sys.exit(0)
            else:
                import _thread

                try:
                    _thread.interrupt_main()
                except Exception:  # noqa: BLE001
                    sys.exit(0)
            return

        from jarvis.core.cancellation import get_global_cancellation_token

        cancel_token = get_global_cancellation_token()
        self.brain.set_cancellation_token(cancel_token)
        self._is_processing_command = True
        self._active_turn_id = f"turn_{int(time.time() * 1000)}"
        self._active_query = query
        self._active_topic_label = self._extract_topic_label(query)
        self._active_tool_outputs = {}

        if hasattr(self, "latency_profiler") and self.latency_profiler:
            self.latency_profiler.begin_turn(self._active_turn_id)
            self.latency_profiler.mark(self._active_turn_id, "stt_final_ns")

        try:
            self.set_state("thinking")
            self.sfx.play_think()

            # Start streaming pipelined speech session
            tts_started = self.tts.start_streaming_session()
            if not tts_started:
                logger.warning("TTS streaming session failed to start; attempting stream lifecycle reconciliation.")
                if hasattr(self.tts, "reconcile_stream_lifecycle") and self.tts.reconcile_stream_lifecycle(force=True):
                    tts_started = self.tts.start_streaming_session()
                if not tts_started:
                    logger.error("TTS streaming session could not be established; audio output will fail closed or run synchronously.")

            # Format active resumption bookmark and flow state XML
            bookmark_xml = (
                self.bookmarks.format_prompt_injection(query)
                if hasattr(self, "bookmarks") and self.bookmarks
                else ""
            )
            flow_xml = ""
            try:
                from jarvis.context.flow_state_mirror import FlowStateMirror

                flow_xml = FlowStateMirror.get_instance().format_quarantined_xml()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flow state XML format notice: %s", exc)

            # Check if this query is an instant deterministic reflex (<10ms) or model switch
            norm_query = re.sub(r"^[\s,;:.]+|[\s,!?:.]+$", "", (query or "").strip()).strip()
            is_fast_reflex = self.brain.is_fast_reflex(norm_query)
            is_model_switch = bool(
                re.match(
                    r"^(?:switch to|use|activate|set model to|enable)\s+(pro|flash|claude|sonnet|opus|gemini|3\.8|3\.1)",
                    norm_query,
                    re.IGNORECASE,
                )
            )

            queued_ack = get_immediate_intent_acknowledgment(
                norm_query, is_fast_reflex=is_fast_reflex, is_model_switch=is_model_switch
            )
            if queued_ack:
                if self.get_state() != "speaking":
                    self.set_state("speaking")
                self.tts.queue_sentence(queued_ack)

            def queue_speech(sentence: str) -> None:
                nonlocal queued_ack
                if cancel_token.is_cancelled:
                    return
                s_clean = sentence.strip()
                # Strip standard prefix repetitions if immediate ack was already delivered
                if queued_ack:
                    s_clean = re.sub(
                        r"^(?:right away|on it|certainly|understood|very well)(?:,?\s+[a-z\s]+)?[.,]?\s*",
                        "",
                        s_clean,
                        flags=re.IGNORECASE,
                    ).strip()
                if s_clean:
                    if self.get_state() != "speaking":
                        self.set_state("speaking")
                    self.tts.queue_sentence(s_clean)

            reply = self.brain.process_query(
                query,
                sentence_callback=queue_speech,
                flow_state_xml=flow_xml,
                bookmark_context=bookmark_xml,
            )
            if cancel_token.is_cancelled:
                logger.info("Command '%s' aborted by cancellation token.", query[:40])
                self.set_state("idle")
                return

            # Sync tool outputs from brain
            if hasattr(self.brain, "get_current_turn_tool_outputs"):
                self._active_tool_outputs.update(self.brain.get_current_turn_tool_outputs())

            # Complete playback of remaining queued audio
            speech_completed = self.tts.finish_streaming_session()
            if hasattr(self, "latency_profiler") and self.latency_profiler:
                self.latency_profiler.mark(self._active_turn_id, "tts_first_chunk_ns")
                self.latency_profiler.mark(self._active_turn_id, "audio_first_frame_ns")
                self.latency_profiler.complete_turn(self._active_turn_id)
            if cancel_token.is_cancelled:
                self.set_state("idle")
                return

            if not speech_completed:
                reply += (
                    "\n\n[Speech playback timed out. The complete response is shown here, "
                    "but Jarvis could not finish speaking it.]"
                )

            print(f"[Jarvis]: {reply}\n")

            # Publish response to HUD
            if getattr(self, "ui_service", None):
                self.ui_service.broadcast_message("jarvis", reply)

            self.set_state("idle")
            self.sfx.play_done()
            trim_process_memory(self.config.get("performance", {}).get("aggressive_working_set_trim", False))

            # Check if shutdown tool was invoked during LLM reasoning
            from jarvis.skills.os_control import is_shutdown_requested

            if is_shutdown_requested():
                self.stop()
                import _thread

                try:
                    _thread.interrupt_main()
                except Exception:  # noqa: BLE001
                    sys.exit(0)
        finally:
            self._is_processing_command = False

    def start(self) -> None:
        """Starts all background listeners, Web HUD, and the desktop overlay."""
        self._running = True

        # Launch Web Holographic HUD Server
        if getattr(self, "ui_service", None):
            logger.info("Starting Jarvis Holographic Web HUD Server...")
            self.ui_service.start()
            if self.config.get("ui", {}).get("auto_open", False):
                try:
                    import webbrowser

                    from jarvis.ui.server import create_bootstrap_token

                    b_token = create_bootstrap_token(ttl_seconds=60.0)
                    webbrowser.open(f"http://{self.ui_service.host}:{self.ui_service.port}/#bootstrap={b_token}")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("HUD browser auto-open notice: %s", exc)

        if self.config.get("hud", {}).get("desktop_overlay", False):
            logger.info("Starting Jarvis HUD Desktop Overlay...")
            self.hud.start()

        logger.info("Starting Jarvis Hotkey Listener (Hold ALT to talk)...")
        self.hotkey_listener.start()

        logger.info("Starting Wake Word Detector ('Hey Jarvis')...")
        self.wake_detector.start()

        logger.info("Starting Jarvis Proactive Heartbeat Daemon...")
        self.heartbeat.start()

        self.sfx.play_done()
        hud_url = f"http://{self.ui_service.host}:{self.ui_service.port}" if self.ui_service else "Disabled"
        print("\n" + "=" * 60)
        print(" JARVIS 3.0 ONLINE & OPERATIONAL")
        print(f"  - Holographic Web HUD active at {hud_url}")
        print("  - Floating Arc-Reactor HUD active (Drag to move)")
        print("  - Hold Fin (End) key to talk (Push-to-Talk)")
        print("  - Say 'Hey Jarvis' to wake via voice")
        print("  - Ambient Proactive Heartbeat active (Briefings & Alerts)")
        print("  - Type directly below and press Enter to chat")
        print("  - Say 'Goodbye Jarvis' or type 'exit' to shut down")
        print("=" * 60 + "\n")

        # Spoken welcome greeting on launch
        self.set_state("speaking")
        asyncio.run(self.tts.speak("Jarvis systems operational. All sub-systems online, Sir."))
        self.set_state("idle")
        trim_process_memory(self.config.get("performance", {}).get("aggressive_working_set_trim", False))

    def run_interactive_loop(self) -> None:
        """Main execution loop for voice and console interaction."""
        self.start()
        try:
            is_interactive = sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
            if is_interactive:
                while self._running:
                    try:
                        user_input = input("[Sir]: ").strip()
                        if not user_input:
                            continue
                        self.handle_command(user_input)
                    except EOFError:
                        logger.info("Terminal stdin disconnected; continuing in ambient voice & HUD daemon mode.")
                        while self._running:
                            time.sleep(0.5)
                        break
                    except KeyboardInterrupt:
                        break
            else:
                while self._running:
                    time.sleep(0.5)
        except (KeyboardInterrupt, EOFError):
            print("\n[Jarvis]: Terminating session. Have a good day, Sir.")
            self.stop()
            sys.exit(0)
        finally:
            self.stop()

    def stop(self) -> None:
        """Stops Jarvis's internal workers, all tools, and every managed child service."""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self._running = False

        OrchestratorShutdownCoordinator.shutdown(self)
