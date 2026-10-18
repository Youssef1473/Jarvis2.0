"""Wake-word listener using openWakeWord ('Hey Jarvis')."""
from collections import deque
import logging
import threading
from typing import Dict, Any, Callable, Optional
import numpy as np
import sounddevice as sd

logger = logging.getLogger("Jarvis.WakeWord")


class WakeWordDetector:
    """Continuously monitors microphone stream for the wake word ('Hey Jarvis') with zero-idle CPU energy gating."""

    def __init__(
        self,
        config: Dict[str, Any],
        on_wake_callback: Optional[Callable[[], None]] = None,
        is_speaking_callback: Optional[Callable[[], bool]] = None
    ):
        audio_cfg = config.get("audio", {})
        self.wake_word = audio_cfg.get("wake_word", "hey jarvis")
        self.threshold = float(audio_cfg.get("wake_word_threshold", 0.55))
        self.energy_threshold = float(audio_cfg.get("wake_word_energy_threshold", 0.0035))
        self.energy_threshold_sq = self.energy_threshold * self.energy_threshold
        self.sample_rate = audio_cfg.get("sample_rate", 16000)
        self.on_wake_callback = on_wake_callback
        self.is_speaking_callback = is_speaking_callback

        self.model = None
        self._vad = None
        self._is_running = False
        self._stream = None
        self._thread: Optional[threading.Thread] = None

        # Energy gate state: rolling pre-roll buffer to retain lead-in speech frames while idling
        self._preroll_buffer: deque = deque(maxlen=4)
        self._active_chunks_remaining = 0
        self._affinity_set = False

    def _init_model(self):
        """Initializes openWakeWord model."""
        try:
            import openwakeword
            from openwakeword.model import Model

            # openWakeWord provides built-in models including "hey_jarvis"
            # Download models if not cached
            openwakeword.utils.download_models()
            self.model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
            logger.info("Loaded openWakeWord 'hey_jarvis' model successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize openWakeWord: {e}")
            self.model = None

    def get_vad(self):
        """Returns cached VAD instance to prevent reloading ONNX models on each wake event."""
        if self._vad is None:
            try:
                from openwakeword.vad import VAD
                self._vad = VAD()
            except Exception as e:
                logger.error(f"Failed to initialize openWakeWord VAD: {e}")
                return None
        return self._vad

    def _audio_callback(self, indata, frames, time_info, status):
        """Processes audio chunks from the sounddevice InputStream with energy-gated zero-idle CPU."""
        try:
            if not self._affinity_set:
                try:
                    from jarvis.audio.affinity import pin_and_elevate_current_thread
                    pin_and_elevate_current_thread(tag="WakeWordStream")
                except Exception:
                    pass
                self._affinity_set = True

            if not self._is_running or self.model is None:
                return

            # Acoustic Echo Suppression: Do not detect wake word while Jarvis is speaking
            if self.is_speaking_callback and self.is_speaking_callback():
                self._preroll_buffer.clear()
                self._active_chunks_remaining = 0
                return

            # indata is float32 [-1.0, 1.0], chunk has 1280 samples (80ms at 16kHz)
            raw_float = indata[:, 0]
            # Zero-allocation mean squared energy via BLAS dot product: eliminates np.sqrt and array allocation
            mean_square_energy = float(np.dot(raw_float, raw_float) / len(raw_float))

            # Sub-microsecond energy gate: if room is silent, skip heavy ONNX neural inferences
            if mean_square_energy >= self.energy_threshold_sq:
                # Voice / sound detected: activate predictor and sustain active processing for ~2.0s (25 chunks)
                self._active_chunks_remaining = 25
            elif self._active_chunks_remaining > 0:
                self._active_chunks_remaining -= 1

            # If completely idle (silent ambient room), store in pre-roll deque and return immediately (0.002ms)
            if self._active_chunks_remaining == 0:
                self._preroll_buffer.append(raw_float.copy())
                return

            # Active speech processing: replay pre-roll lead-in chunks if waking up from idle
            chunks_to_feed = []
            if self._preroll_buffer:
                while self._preroll_buffer:
                    chunks_to_feed.append((self._preroll_buffer.popleft() * 32767).astype(np.int16))

            chunks_to_feed.append((raw_float * 32767).astype(np.int16))

            try:
                for chunk_pcm in chunks_to_feed:
                    prediction = self.model.predict(chunk_pcm)
                    for model_name, score in prediction.items():
                        if score >= self.threshold:
                            logger.info(f"Wake word detected! Score: {score:.3f}")
                            try:
                                from jarvis.audio.cancellation import trigger_speech_barge_in
                                trigger_speech_barge_in()
                            except Exception:
                                pass
                            self.model.reset()
                            self._preroll_buffer.clear()
                            self._active_chunks_remaining = 0
                            if self.on_wake_callback:
                                self.on_wake_callback()
                            return
            except Exception as e:
                logger.error(f"Error predicting wake word: {e}")
        except Exception as e:
            logger.error(f"Error in wake word audio callback: {e}")

    def start(self):
        """Starts wake word listening in the background."""
        if self._is_running:
            return

        if self.model is None:
            self._init_model()

        if self.model is None:
            logger.warning("openWakeWord model unavailable. Wake word detection disabled.")
            return

        self._is_running = True
        try:
            # Block size 1280 samples = 80ms chunks at 16kHz
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=1280,
                callback=self._audio_callback,
            )
            self._stream.start()
            logger.info("Wake word detector started listening.")
        except Exception as e:
            logger.error(f"Failed to start audio stream for wake word: {e}")
            self._is_running = False

    def stop(self):
        """Stops wake word detection."""
        self._is_running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
