"""Adaptive audio capture, Voice Activity Detection (VAD) loop, and Push-to-Talk session manager."""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

logger = logging.getLogger("Jarvis.SpeechCapture")


class AdaptiveSpeechCapture:
    """Manages microphone stream buffering, VAD pause tolerance heuristics, and PTT state machines."""

    @staticmethod
    def audio_record_callback(orchestrator: Any, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        """Accumulates microphone audio during push-to-talk."""
        try:
            if getattr(orchestrator, "_is_recording", False):
                with orchestrator._lock:
                    orchestrator._recorded_frames.append(indata.copy())
        except Exception as exc:  # noqa: BLE001
            logger.debug("Audio recording buffer callback notice: %s", exc)

    @staticmethod
    def on_ptt_press(orchestrator: Any, on_speech_captured: Callable[[np.ndarray], None]) -> None:
        """Invoked immediately when user presses PTT trigger (MB2 or configured key)."""
        if getattr(orchestrator, "_is_muted", False):
            return

        # Kill-switch: abort in-flight cognitive processing, tool calls, or speech
        was_busy = orchestrator.abort_active_command()
        if was_busy:
            # Settle window (25ms) allows WASAPI driver & worker threads to cleanly release audio endpoints
            time.sleep(0.025)

        ptt_mode = str(orchestrator.config.get("audio", {}).get("ptt_mode", "hold")).lower().strip()
        if ptt_mode == "toggle" and getattr(orchestrator, "_is_ptt_recording", False):
            # Second press in toggle mode concludes recording session
            AdaptiveSpeechCapture.finalize_ptt_recording(orchestrator, on_speech_captured)
            return

        with orchestrator._lock:
            orchestrator._recorded_frames = []
            orchestrator._is_recording = True
            orchestrator._is_ptt_recording = True
            orchestrator._ptt_press_time = time.time()
            if hasattr(orchestrator, "wake_detector"):
                try:
                    orchestrator.wake_detector.start_ptt_session()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Wake detector PTT start notice: %s", exc)

        is_mouse = getattr(getattr(orchestrator, "hotkey_listener", None), "mouse_target_btn", None) is not None
        if is_mouse:
            # Non-blocking delayed cue: chime and show HUD only if held >= 200ms
            def _delayed_cue() -> None:
                time.sleep(0.20)
                if getattr(orchestrator, "_is_recording", False):
                    logger.info("PTT held (mouse): Listening...")
                    orchestrator.set_state("listening")
                    orchestrator.sfx.play_wake()

            threading.Thread(target=_delayed_cue, daemon=True).start()
        else:
            # Dedicated keyboard key (e.g. Fin / End): immediate activation
            logger.info("PTT pressed (Fin / End): Listening...")
            orchestrator.set_state("listening")
            orchestrator.sfx.play_wake()

    @staticmethod
    def on_ptt_release(orchestrator: Any, on_speech_captured: Callable[[np.ndarray], None]) -> None:
        """Invoked immediately when user releases PTT trigger."""
        if not getattr(orchestrator, "_is_ptt_recording", False):
            return

        ptt_mode = str(orchestrator.config.get("audio", {}).get("ptt_mode", "hold")).lower().strip()
        if ptt_mode == "toggle":
            # In toggle mode, releasing the key does NOT finish recording
            return

        AdaptiveSpeechCapture.finalize_ptt_recording(orchestrator, on_speech_captured)

    @staticmethod
    def finalize_ptt_recording(orchestrator: Any, on_speech_captured: Callable[[np.ndarray], None]) -> None:
        """Concludes PTT recording session and initiates audio transcription."""
        press_duration = time.time() - getattr(orchestrator, "_ptt_press_time", 0.0)
        orchestrator._is_recording = False
        orchestrator._is_ptt_recording = False

        chunks: list[np.ndarray] = []
        if hasattr(orchestrator, "wake_detector"):
            try:
                detector_chunks = orchestrator.wake_detector.stop_ptt_session()
                if isinstance(detector_chunks, list):
                    chunks.extend(detector_chunks)
                if getattr(orchestrator.wake_detector, "recording_overflow_detected", False) is True:
                    orchestrator.last_recording_had_overflow = True
                    logger.warning("PTT recording encountered queue overflow; some audio frames may have been dropped.")
            except Exception as exc:  # noqa: BLE001
                logger.debug("Wake detector PTT stop notice: %s", exc)

        # Filter out accidental mouse taps (< 220ms) if using mouse button in hold mode
        is_mouse = getattr(getattr(orchestrator, "hotkey_listener", None), "mouse_target_btn", None) is not None
        ptt_mode = str(orchestrator.config.get("audio", {}).get("ptt_mode", "hold")).lower().strip()
        if is_mouse and ptt_mode != "toggle" and press_duration < 0.22:
            orchestrator.set_state("idle")
            return

        logger.info("PTT concluded (duration %.2fs): Processing speech...", press_duration)
        orchestrator.set_state("thinking")

        with orchestrator._lock:
            if getattr(orchestrator, "_recorded_frames", None):
                chunks.extend(orchestrator._recorded_frames)
                orchestrator._recorded_frames = []

            if not chunks:
                logger.info("No audio captured.")
                orchestrator.set_state("idle")
                return
            audio_array = np.concatenate(chunks, axis=0).flatten()

        threading.Thread(target=on_speech_captured, args=(audio_array,), daemon=True).start()

    @staticmethod
    def start_vad_capture(orchestrator: Any, on_speech_captured: Callable[[np.ndarray], None]) -> None:
        """Spawns an elevated background worker to capture speech with adaptive silence scaling and hesitation damping."""
        def record_and_process_vad() -> None:
            try:
                from jarvis.audio.affinity import pin_and_elevate_current_thread
                pin_and_elevate_current_thread(tag="OrchestratorVADCapture")
            except (ImportError, AttributeError, OSError) as exc:
                logger.debug("VAD capture thread affinity elevation notice: %s", exc)

            try:
                vad = orchestrator.wake_detector.get_vad()
                if vad is None:
                    from openwakeword.vad import VAD
                    vad = VAD()

                captured_chunks: list[np.ndarray] = []
                block_size = 1280
                sample_rate = orchestrator.sample_rate

                audio_cfg = orchestrator.config.get("audio", {})
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

                orchestrator._is_recording = True
                vad_session = orchestrator.wake_detector.start_vad_session()
                try:
                    for chunk_idx in range(max_chunks):
                        if getattr(orchestrator, "_is_ptt_recording", False) or getattr(orchestrator, "_stopped", False):
                            break

                        try:
                            chunk_data = vad_session.get_chunk(timeout=0.35)
                        except queue.Empty:
                            if getattr(orchestrator, "_stopped", False) or getattr(orchestrator, "_is_ptt_recording", False):
                                break
                            continue

                        data = chunk_data.reshape(-1, 1)
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
                                        "VAD speech concluded at %.1fs (%.2fs pause, dynamic limit %.2fs).",
                                        chunk_idx * chunk_duration,
                                        silence_chunk_count * chunk_duration,
                                        dynamic_silence_s,
                                    )
                                    break
                        else:
                            if chunk_idx >= initial_timeout_chunks:
                                logger.info("VAD: no speech detected after wake word.")
                                break
                finally:
                    if getattr(vad_session, "overflow_detected", False):
                        orchestrator.last_recording_had_overflow = True
                        logger.warning("VAD recording encountered queue overflow; some audio frames may have been dropped.")
                    try:
                        orchestrator.wake_detector.stop_vad_session()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Wake detector VAD session stop notice: %s", exc)
                    orchestrator._is_recording = False

                if getattr(orchestrator, "_is_ptt_recording", False):
                    # PTT preempted VAD recording, do not process partial VAD audio
                    return

                if captured_chunks and speech_started:
                    audio_array = np.concatenate(captured_chunks, axis=0).flatten()
                    on_speech_captured(audio_array)
                else:
                    orchestrator.set_state("idle")

            except Exception as exc:  # noqa: BLE001
                logger.error("VAD recording error: %s", exc)
                orchestrator.set_state("idle")

        threading.Thread(target=record_and_process_vad, daemon=True).start()
