"""Central orchestrator coordinating audio, hotkeys, STT, LLM, tools, TTS, and HUD overlay."""
import asyncio
import logging
import os
import random
import re
import sys
import threading
import time
from typing import Dict, Any, List, Optional
import numpy as np
import sounddevice as sd

from jarvis.audio.sound_effects import SoundEffects
from jarvis.audio.hotkey_listener import HotkeyListener
from jarvis.audio.stt import SpeechToText
from jarvis.audio.tts import TextToSpeech
from jarvis.audio.wake_word import WakeWordDetector
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate
from jarvis.core.brain import JarvisBrain
from jarvis.core.heartbeat import JarvisHeartbeat
from jarvis.core.services import ManagedServiceManager
from jarvis.ui.hud import JarvisHUD

logger = logging.getLogger("Jarvis.Orchestrator")


def trim_process_memory(aggressive: bool = False):
    """Run low-cost collection; reserve working-set eviction for explicit opt-in."""
    import gc
    gc.collect(0)
    if not aggressive:
        return
    gc.collect()
    try:
        import ctypes
        ctypes.windll.psapi.EmptyWorkingSet(ctypes.windll.kernel32.GetCurrentProcess())
    except Exception:
        pass


class JarvisOrchestrator:
    """Coordinates Jarvis's sensory inputs, cognitive loop, speech output, holographic HUD, and proactive heartbeat."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.sample_rate = config.get("audio", {}).get("sample_rate", 16000)

        # Initialize sub-systems
        self.sfx = SoundEffects(config)
        self.memory = MemoryManager(config)
        try:
            import jarvis.skills.productivity as prod_skills
            prod_skills.set_memory_manager(self.memory)
        except Exception:
            pass
        try:
            import jarvis.skills.self_evolution as evo_skills
            evo_skills.set_memory_manager(self.memory)
        except Exception:
            pass
        self.safety = SafetyGate(config)
        self.stt = SpeechToText(config)
        self.tts = TextToSpeech(config)
        try:
            import jarvis.skills.voice_control as voice_skills
            voice_skills.set_tts_instance(self.tts)
        except Exception:
            pass
        self.service_manager = ManagedServiceManager()
        self.brain = JarvisBrain(config, self.memory, self.safety, service_manager=self.service_manager)
        self.hud = JarvisHUD()
        self.heartbeat = JarvisHeartbeat(self, config)
        from jarvis.core.task_manager import BackgroundTaskManager
        self.task_manager = BackgroundTaskManager.get_instance(self)

        from jarvis.conversation.bookmarks import ReasoningBookmarkStack
        self.bookmarks = ReasoningBookmarkStack.get_instance()
        from jarvis.core.pipeline.latency_profiler import PipelineLatencyProfiler
        from jarvis.skills.diagnostics import get_global_latency_profiler
        self.latency_profiler: PipelineLatencyProfiler = (
            getattr(self.brain, "latency_profiler", None)
            or get_global_latency_profiler()
        )
        self.tool_orchestrator = getattr(self.brain, "tool_orchestrator", None)
        self._active_turn_id: Optional[str] = None
        self._active_query: str = ""
        self._active_topic_label: str = "the architecture"
        self._active_tool_outputs: Dict[str, Any] = {}

        # Web-Powered Holographic HUD Server
        ui_cfg = config.get("ui", {})
        if ui_cfg.get("enabled", True):
            from jarvis.ui.server import JarvisUIService
            self.ui_service = JarvisUIService(
                orchestrator=self,
                host=ui_cfg.get("host", "127.0.0.1"),
                port=int(ui_cfg.get("port", 20129)),
            )
        else:
            self.ui_service = None
            def _cli_approval_handler(details: Dict[str, Any]) -> bool:
                if not sys.stdin or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
                    return False
                print(f"\n[JARVIS DUAL-KEY SECURITY GATE]")
                print(f" Tool: {details.get('tool_name')}")
                print(f" Action: {details.get('summary')}")
                print(f" Hash: {details.get('action_hash')}")
                try:
                    ans = input(" Authorize execution? [y/N]: ").strip().lower()
                    return ans in ("y", "yes")
                except Exception:
                    return False

            from jarvis.core.security_boundaries import DualKeyApprovalManager
            DualKeyApprovalManager.get_instance().set_approval_handler(_cli_approval_handler)

        if hasattr(self.brain, "set_tts_instance"):
            self.brain.set_tts_instance(self.tts)
        if getattr(self, "ui_service", None) and hasattr(self.brain, "set_tool_status_callback"):
            self.brain.set_tool_status_callback(self._on_tool_status_event)

        # Mute state
        self._is_muted = False

        # Recording state for Push-to-Talk (ALT)
        self._is_recording = False
        self._recorded_frames: List[np.ndarray] = []
        self._record_stream: sd.InputStream | None = None
        self._lock = threading.Lock()

        # Listeners
        self.hotkey_listener = HotkeyListener(
            config=config,
            on_press_callback=self._on_ptt_press,
            on_release_callback=self._on_ptt_release,
            on_any_key_callback=None,
        )
        self.wake_detector = WakeWordDetector(
            config,
            on_wake_callback=self._on_wake_word,
            is_speaking_callback=lambda: self.tts.is_speaking or self._is_recording,
        )

        self._running = False
        self._stopped = False
        self._stop_lock = threading.Lock()
        self._current_state = "idle"
        self._is_processing_command = False

    def set_state(self, new_state: str):
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
            except Exception:
                pass

        if self.tts.is_speaking:
            self.tts.stop()
            was_busy = True

        if hasattr(self, "brain") and hasattr(self.brain, "cancel_active_turn"):
            try:
                self.brain.cancel_active_turn()
            except Exception:
                pass

        # Sync resolved tool outputs from brain if available
        active_tools = dict(getattr(self, "_active_tool_outputs", {}) or {})
        if hasattr(self, "brain") and hasattr(self.brain, "get_current_turn_tool_outputs"):
            try:
                brain_tools = self.brain.get_current_turn_tool_outputs()
                if brain_tools:
                    active_tools.update(brain_tools)
                    self._active_tool_outputs.update(brain_tools)
            except Exception:
                pass

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
            except Exception:
                pass

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

        if getattr(self, "_is_processing_command", False) or getattr(self, "_current_state", "idle") in ("thinking", "speaking"):
            was_busy = True
            logger.info("Active command execution aborted via kill-switch (epoch -> %d).", epoch)
            self.set_state("idle")
            if getattr(self, "ui_service", None):
                try:
                    self.ui_service.broadcast_message("jarvis", "[Execution aborted by user interrupt]")
                except Exception:
                    pass
        return was_busy

    def on_atomic_barge_in(self) -> None:
        """
        Callback invoked immediately when the Win32 Named Event barge-in fires (<1us).
        Halts playback, cancels speculative futures, and snapshots the interrupted speech clause cursor.
        """
        self.abort_active_command()

    def broadcast_ambient_telemetry(self, data: Dict[str, Any]):
        """Dispatches ambient desktop context and presence telemetry to the Holographic HUD."""
        if getattr(self, "ui_service", None):
            try:
                self.ui_service.broadcast({
                    "type": "ambient_telemetry",
                    "data": data,
                    "timestamp": time.strftime("%H:%M:%S"),
                })
            except Exception:
                pass

    def _on_tool_status_event(self, event: Dict[str, Any]):
        """Dispatches live tool execution status events to the Holographic HUD."""
        if getattr(self, "ui_service", None):
            try:
                self.ui_service.broadcast({
                    "type": "tool_execution",
                    "data": event,
                    "timestamp": time.strftime("%H:%M:%S"),
                })
            except Exception:
                pass

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

    def _on_key_activity(self):
        """Barge-in hook: interrupts speech and active commands if user starts typing or presses a key."""
        if self.tts.is_speaking:
            self.abort_active_command()

    def _audio_record_callback(self, indata, frames, time_info, status):
        """Accumulates microphone audio during push-to-talk."""
        try:
            if self._is_recording:
                with self._lock:
                    self._recorded_frames.append(indata.copy())
        except Exception:
            pass

    def _on_ptt_press(self):
        """Called immediately when user presses PTT trigger (MB2 or configured key)."""
        if self._is_muted:
            return

        # Kill-switch: abort any in-flight command execution, tool calls, or speech
        was_busy = self.abort_active_command()
        if was_busy:
            # Settle window (25ms) allows WASAPI driver & worker threads to cleanly release audio endpoints
            time.sleep(0.025)

        ptt_mode = str(self.config.get("audio", {}).get("ptt_mode", "hold")).lower().strip()
        if ptt_mode == "toggle" and self._is_recording:
            # Second press in toggle mode concludes recording session
            self._finalize_ptt_recording()
            return

        with self._lock:
            if getattr(self, "_record_stream", None) is not None:
                try:
                    self._record_stream.stop()
                    self._record_stream.close()
                except Exception:
                    pass
                self._record_stream = None

            self._recorded_frames = []
            self._is_recording = True
            self._ptt_press_time = time.time()

        is_mouse = getattr(self.hotkey_listener, "mouse_target_btn", None) is not None
        if is_mouse:
            # Non-blocking delayed cue: chime and show HUD only if held >= 200ms
            def _delayed_cue():
                time.sleep(0.20)
                if self._is_recording:
                    logger.info("PTT held (mouse): Listening...")
                    self.set_state("listening")
                    self.sfx.play_wake()

            threading.Thread(target=_delayed_cue, daemon=True).start()
        else:
            # Dedicated keyboard key (e.g. Fin / End): immediate activation
            logger.info("PTT pressed (Fin / End): Listening...")
            self.set_state("listening")
            self.sfx.play_wake()

        try:
            with self._lock:
                self._record_stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    callback=self._audio_record_callback,
                )
                self._record_stream.start()
        except Exception as e:
            logger.error(f"Failed to start audio recording stream: {e}")
            with self._lock:
                self._is_recording = False
            self.set_state("idle")

    def _finalize_ptt_recording(self):
        """Concludes PTT recording session and initiates audio processing."""
        press_duration = time.time() - getattr(self, "_ptt_press_time", 0.0)
        self._is_recording = False

        if getattr(self, "_record_stream", None) is not None:
            try:
                self._record_stream.stop()
                self._record_stream.close()
            except Exception:
                pass
            self._record_stream = None

        # Filter out accidental mouse taps (< 220ms) if using mouse button in hold mode
        is_mouse = getattr(self.hotkey_listener, "mouse_target_btn", None) is not None
        ptt_mode = str(self.config.get("audio", {}).get("ptt_mode", "hold")).lower().strip()
        if is_mouse and ptt_mode != "toggle" and press_duration < 0.22:
            self.set_state("idle")
            return

        logger.info(f"PTT concluded (duration {press_duration:.2f}s): Processing speech...")
        self.set_state("thinking")

        with self._lock:
            if not self._recorded_frames:
                logger.info("No audio captured.")
                self.set_state("idle")
                return
            audio_array = np.concatenate(self._recorded_frames, axis=0).flatten()

        threading.Thread(target=self._process_audio, args=(audio_array,), daemon=True).start()

    def _on_ptt_release(self):
        """Called immediately when user releases PTT trigger."""
        if not self._is_recording:
            return

        ptt_mode = str(self.config.get("audio", {}).get("ptt_mode", "hold")).lower().strip()
        if ptt_mode == "toggle":
            # In toggle mode, releasing the key does NOT finish recording; user speaks until next press
            return

        self._finalize_ptt_recording()

    def _on_wake_word(self):
        """Called when openWakeWord detects 'Hey Jarvis'."""
        if self._is_muted or self._is_recording:
            return

        if self.tts.is_speaking:
            self.tts.stop()

        logger.info("Wake word detected! Listening with dynamic VAD auto-cutoff...")
        self.set_state("listening")
        self.sfx.play_wake()

        def record_and_process_vad():
            try:
                from jarvis.audio.affinity import pin_and_elevate_current_thread
                pin_and_elevate_current_thread(tag="OrchestratorVADCapture")
            except Exception:
                pass
            try:
                vad = self.wake_detector.get_vad()
                if vad is None:
                    from openwakeword.vad import VAD
                    vad = VAD()
                captured_chunks = []
                block_size = 1280
                sample_rate = self.sample_rate

                audio_cfg = self.config.get("audio", {})
                base_silence_s = float(audio_cfg.get("silence_threshold_seconds", 1.4))
                thinking_pause_s = float(audio_cfg.get("thinking_pause_seconds", 3.2))
                max_record_seconds = float(audio_cfg.get("max_record_seconds", 35.0))
                initial_timeout_seconds = float(audio_cfg.get("initial_timeout_seconds", 3.5))
                hesitation_damping = bool(audio_cfg.get("hesitation_damping", True))

                chunk_duration = block_size / sample_rate
                max_chunks = int(max_record_seconds / chunk_duration)
                initial_timeout_chunks = int(initial_timeout_seconds / chunk_duration)

                speech_started = False
                active_speech_chunks = 0
                silence_chunk_count = 0

                with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32", blocksize=block_size) as stream:
                    for chunk_idx in range(max_chunks):
                        if self._is_recording or getattr(self, "_stopped", False):
                            break

                        data, _ = stream.read(block_size)
                        captured_chunks.append(data.copy())

                        # Compute RMS energy for micro-hesitation and breathing detection
                        chunk_samples = data[:, 0]
                        rms_energy = float(np.sqrt(np.mean(chunk_samples ** 2)))

                        # Convert float32 to int16 PCM for VAD scoring
                        data_int16 = (chunk_samples * 32767).astype(np.int16)
                        vad_score = float(vad.predict(data_int16))

                        if vad_score >= 0.30:
                            speech_started = True
                            active_speech_chunks += 1
                            silence_chunk_count = 0
                        elif speech_started:
                            # Dynamic Adaptive Silence Scaling:
                            # Scales pause tolerance from base_silence_s up to thinking_pause_s (e.g. 3.2s)
                            # so Sir has ample breathing room to think during compound multi-clause sentences.
                            spoken_duration_s = active_speech_chunks * chunk_duration
                            dynamic_silence_s = min(thinking_pause_s, base_silence_s + (spoken_duration_s * 0.35))
                            silence_chunks_needed = max(14, int(dynamic_silence_s / chunk_duration))

                            # Hesitation & Murmur Damping:
                            # Intermediate vocal energy (vad 0.12-0.29 or breathing RMS >= 0.0038) indicates active thinking
                            if hesitation_damping and (0.12 <= vad_score < 0.30 or rms_energy >= 0.0038):
                                silence_chunk_count = max(0, silence_chunk_count - 2)
                            else:
                                silence_chunk_count += 1
                                if silence_chunk_count >= silence_chunks_needed:
                                    logger.info(
                                        f"VAD speech concluded at {chunk_idx * chunk_duration:.1f}s "
                                        f"({silence_chunk_count * chunk_duration:.2f}s pause, dynamic limit {dynamic_silence_s:.2f}s)."
                                    )
                                    break
                        else:
                            if chunk_idx >= initial_timeout_chunks:
                                logger.info("VAD: no speech detected after wake word.")
                                break

                if captured_chunks:
                    audio_array = np.concatenate(captured_chunks, axis=0).flatten()
                    self._process_audio(audio_array)
                else:
                    self.set_state("idle")

            except Exception as e:
                logger.error(f"VAD recording error: {e}")
                self.set_state("idle")

        threading.Thread(target=record_and_process_vad, daemon=True).start()

    def _process_audio(self, audio_array: np.ndarray):
        """Transcribes audio, sends to brain, and delivers spoken output."""
        try:
            from jarvis.audio.affinity import pin_and_elevate_current_thread
            pin_and_elevate_current_thread(tag="OrchestratorAudioWorker")
        except Exception:
            pass
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
        except Exception as e:
            logger.error(f"Error handling voice command '{text}': {e}", exc_info=True)
            self.set_state("idle")
            self.sfx.play_done()

    @staticmethod
    def is_shutdown_command(query: str) -> bool:
        """Determines if query is a departure or shutdown request."""
        clean = "".join(c.lower() for c in query if c.isalnum() or c.isspace())
        normalized = " ".join(clean.split())
        if normalized in {
            "stop", "stop please", "please stop", "can you stop", "could you stop",
            "exit", "quit", "goodbye", "good bye", "bye",
        }:
            return True

        # Questions or explanations asking 'how', 'why', 'what', 'who' must never trigger shutdown
        if any(normalized.startswith(q) for q in ("how ", "why ", "what ", "who ", "where ", "when ", "can you explain", "explain ")):
            return False

        # Programming instructions and technical directives must never trigger shutdown
        code_markers = {"def", "class", "import", "python", "code", "script", "step", "execute", "directive", "permission", "server", "vim"}
        query_words = set(normalized.split())
        if query_words & code_markers:
            return False

        # Guard against long technical sentences accidentally triggering departure
        if len(normalized.split()) > 7:
            return False

        import re
        triggers = [
            r"\bgoodbye\b", r"\bgood bye\b", r"\bbye\b", r"\bshutdown\b", r"\bshut down\b",
            r"\bexit\b", r"\bquit\b", r"\bthats it for now\b", r"\bthat is it for now\b",
            r"\bthats all for now\b", r"\bthat is all for now\b",
            r"\bpower down\b", r"\bdeactivate\b", r"\bgo to sleep\b", r"\bsleep now\b",
            r"\bpower off\b", r"\bgo offline\b", r"\bterminate\b", r"\bclose jarvis\b", r"\bstop jarvis\b",
            r"\bstop listening\b", r"\bstop working\b", r"\bstop all services\b", r"\bstop all the services\b",
            r"\bstop every service\b", r"\bshut yourself down\b",
            r"\bshut down every tool\b", r"\bshut down all tools\b", r"\bkill jarvis\b",
            r"\bkill every tool\b", r"\bkill all tools\b", r"\bkill everything\b",
            r"\bturn off\b", r"\bclose down\b", r"\bstop all tools\b"
        ]
        return any(re.search(t, normalized) for t in triggers)

    async def handle_user_utterance(self, turn_id: str, utterance: str) -> str:
        """Main cognitive execution entrypoint with automatic bookmark resumption."""
        self._active_turn_id = turn_id
        self._active_query = utterance
        self._active_tool_outputs.clear()

        if hasattr(self, "latency_profiler") and self.latency_profiler:
            self.latency_profiler.begin_turn(turn_id)
            self.latency_profiler.mark(turn_id, "stt_final_ns")

        bookmark_xml = self.bookmarks.format_prompt_injection(utterance) if hasattr(self, "bookmarks") and self.bookmarks else ""
        flow_xml = ""
        try:
            from jarvis.context.flow_state_mirror import FlowStateMirror
            flow_xml = FlowStateMirror.get_instance().format_quarantined_xml()
        except Exception:
            pass

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
        else:
            return self.handle_command(utterance)

    @staticmethod
    def _extract_topic_label(query: str) -> str:
        """Derives a concise 2-4 word topic phrase from the query for bookmark bridging."""
        q = (query or "").strip()
        pattern = re.compile(
            r"^(?:jarvis,?\s*|hey jarvis,?\s*|can you\s+|could you\s+|please\s+|tell me about\s+|explain\s+|what is\s+|how does\s+|why does\s+|what are\s+|how do\s+)",
            flags=re.IGNORECASE,
        )
        while True:
            new_q = pattern.sub("", q).strip()
            if new_q == q:
                break
            q = new_q
        q_clean = re.sub(r"[?!.,;:]+$", "", q).strip()
        words = q_clean.split()
        if not words:
            return "the prior topic"
        topic = " ".join(words[:4]).strip().lower()
        return topic or "the prior topic"

    def handle_command(self, query: str):
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
            asyncio.run(self.tts.speak(farewell))
            self.set_state("idle")
            self.stop()
            import os
            time.sleep(0.3)
            os._exit(0)
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
            self.tts.start_streaming_session()

            # Format active resumption bookmark and flow state XML
            bookmark_xml = self.bookmarks.format_prompt_injection(query) if hasattr(self, "bookmarks") and self.bookmarks else ""
            flow_xml = ""
            try:
                from jarvis.context.flow_state_mirror import FlowStateMirror
                flow_xml = FlowStateMirror.get_instance().format_quarantined_xml()
            except Exception:
                pass

            # Check if this query is an instant deterministic reflex (<10ms) or model switch
            norm_query = re.sub(r"^[\s,;:.]+|[\s,!?:.]+$", "", (query or "").strip()).strip()
            is_fast_reflex = self.brain.is_fast_reflex(norm_query)
            is_model_switch = bool(re.match(r"^(?:switch to|use|activate|set model to|enable)\s+(pro|flash|claude|sonnet|opus|gemini|3\.8|3\.1)", norm_query, re.I))

            queued_ack = None
            # For non-reflex cognitive tasks, speak an immediate contextual intent update (<150ms) so Sir is never left in silence
            if not is_fast_reflex and not is_model_switch:
                lower = norm_query.lower()
                is_conversational = any(
                    lower.startswith(k) or k in lower for k in [
                        "it's okay", "its okay", "forget about", "never mind", "hello", "hi", "hey",
                        "how are you", "who are you", "thank you", "thanks", "good morning", "good evening"
                    ]
                )
                if not is_conversational:
                    if any(k in lower for k in ["teach yourself", "add a feature", "create a feature", "create a skill", "build a skill", "learn how", "learn to"]):
                        queued_ack = "On it, Sir. Conceptualizing the architecture and writing the capability now."
                    elif any(lower.startswith(k) for k in ["search", "find", "locate", "where is", "where are", "lookup", "look up", "scan"]):
                        queued_ack = "Scanning through the codebase now, Sir."
                    elif any(k in lower for k in ["fix this", "patch this", "write code", "implement", "refactor", "run tests"]):
                        queued_ack = "Analyzing the implementation and preparing the changes, Sir."
                    elif any(k in lower for k in ["read that", "process that pdf", "ingest", "parse that"]):
                        queued_ack = "Parsing the document details now, Sir."
                    elif any(lower.startswith(k) for k in ["why", "explain", "analyze", "compare", "break down"]):
                        queued_ack = "Analyzing that now, Sir. Here is my assessment."

                if queued_ack:
                    if self.get_state() != "speaking":
                        self.set_state("speaking")
                    self.tts.queue_sentence(queued_ack)

            def queue_speech(sentence: str):
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
                        flags=re.I
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

            # Publish the response after playback drain so the HUD state and
            # transcript reflect the actual end of speech.
            if getattr(self, "ui_service", None):
                self.ui_service.broadcast_message("jarvis", reply)

            self.set_state("idle")
            self.sfx.play_done()
            trim_process_memory(self.config.get("performance", {}).get("aggressive_working_set_trim", False))

            # Check if shutdown tool was invoked during LLM reasoning
            from jarvis.skills.os_control import is_shutdown_requested
            if is_shutdown_requested():
                self.stop()
                import os
                time.sleep(0.3)
                os._exit(0)
        finally:
            self._is_processing_command = False

    def start(self):
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
                except Exception:
                    pass

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

    def run_interactive_loop(self):
        """Main execution loop for voice and console interaction."""
        self.start()
        try:
            import sys
            is_interactive = sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
            if is_interactive:
                while self._running:
                    try:
                        user_input = input("[Sir]: ").strip()
                        if not user_input:
                            continue
                        self.handle_command(user_input)
                    except EOFError:
                        # Terminal stdin disconnected, piped, or interrupted by child process teardown.
                        # Do NOT shut down! Jarvis is an ambient assistant with voice, PTT, HUD, and web endpoints.
                        logger.info("Terminal stdin disconnected; continuing in ambient voice & HUD daemon mode.")
                        while self._running:
                            time.sleep(0.5)
                        break
                    except KeyboardInterrupt:
                        # Deliberate user Ctrl+C termination
                        break
            else:
                # Running in stealth / background mode without terminal stdin
                while self._running:
                    time.sleep(0.5)
        except (KeyboardInterrupt, EOFError):
            print("\n[Jarvis]: Terminating session. Have a good day, Sir.")
            self.stop()
            import os
            os._exit(0)
        finally:
            self.stop()

    def stop(self):
        """Stops Jarvis's internal workers, all tools, and every managed child service."""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self._running = False

        # 1. Stop all global skill sentinels & background threads
        try:
            from jarvis.skills.system_watchdog import stop_hardware_watchdog
            stop_hardware_watchdog()
        except Exception:
            pass

        try:
            from jarvis.skills.timers import cancel_timer
            cancel_timer("all")
        except Exception:
            pass

        try:
            from jarvis.skills.photo_manager import shutdown_photo_manager
            shutdown_photo_manager()
        except Exception:
            pass

        # 2. Stop core subsystem components
        cleanup_steps = [
            ("Web HUD", getattr(self, "ui_service", None)),
            ("heartbeat", self.heartbeat),
            ("hotkey listener", self.hotkey_listener),
            ("wake detector", self.wake_detector),
            ("text-to-speech", self.tts),
            ("speech-to-text", self.stt),
            ("desktop HUD", self.hud),
            ("brain", getattr(self, "brain", None)),
            ("background tasks", getattr(self, "task_manager", None)),
        ]
        for name, component in cleanup_steps:
            if component is None:
                continue
            try:
                if hasattr(component, "stop"):
                    component.stop()
                elif hasattr(component, "stop_all"):
                    component.stop_all()
            except Exception:
                logger.exception("Could not stop Jarvis %s cleanly.", name)

        # 3. Stop managed services and sweep all child processes
        try:
            self.service_manager.stop_all()
        except Exception:
            logger.exception("Could not stop service manager cleanly.")

        # 4. Flush GPU VRAM and run garbage collection
        try:
            import gc
            gc.collect()
            if "torch" in sys.modules:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
        except Exception:
            pass

        # 5. Trim working set to prevent memory pressure
        trim_process_memory(aggressive=True)
        logger.info("Jarvis shutdown complete: All tools, child processes, and hardware threads terminated.")
