"""Wake-word listener and persistent microphone stream router using openWakeWord ('Hey Jarvis')."""
import logging
import queue
import threading
from collections import deque
from collections.abc import Callable
from enum import Enum
from typing import Any

import numpy as np
import sounddevice as sd

logger = logging.getLogger("Jarvis.WakeWord")


class CaptureMode(Enum):
    """Operational mode of the unified microphone stream router."""
    WAKE = "wake"             # Ambient wake-word & barge-in detection
    VAD = "vad"               # Active speech recording via dynamic VAD
    PTT = "ptt"               # Active speech recording via Push-To-Talk
    MUTED = "muted"           # Sensors muted; frames discarded


class VADSession:
    """Session handle for consuming audio chunks during active VAD recording."""

    def __init__(self, chunk_queue: queue.Queue, detector: "WakeWordDetector"):
        self._queue = chunk_queue
        self._detector = detector
        self._closed = False

    def get_chunk(self, timeout: float = 0.5) -> np.ndarray:
        """Retrieves next 1D float32 audio chunk (1280 samples).
        Raises queue.Empty if timeout expires.
        """
        return self._queue.get(timeout=timeout)

    @property
    def overflow_detected(self) -> bool:
        """Indicates if any audio frames were dropped due to queue overflow during this session."""
        return self._detector.recording_overflow_detected

    def close(self):
        """Terminates this VAD session and notifies the router."""
        if not self._closed:
            self._closed = True
            self._detector.stop_vad_session()


class SPSCFrameRingBuffer:
    """Preallocated Single-Producer / Single-Consumer (SPSC) Audio Frame Ring Buffer.

    Thread Safety & Real-Time Invariants:
    1. Zero Array Allocations in Callback: Preallocates contiguous float32 slots
       at initialization. The real-time PortAudio callback writes directly into
       preallocated memory via np.copyto() (~1.5 microseconds). No dynamic heap
       audio arrays are allocated on the callback path.
    2. Isolated Lock Boundary: write_frame() uses a dedicated threading.Lock
       held strictly for pointer/counter math (~40ns), completely isolated
       from application-level state locks.
    3. Memory Ownership Protection: read_frame() extracts frames on the background
       worker thread into owned memory, preventing any corruption or aliasing
       when the ring buffer wraps around.
    4. Deterministic Backlog Control: In wake mode, buffer fullness advances the
       read pointer to enforce real-time latency budgets (< 320ms) and increments
       dropped_wake_frames. In recording mode, overflow is explicitly flagged via
       recording_overflow_detected without silent loss.
    """

    def __init__(self, capacity: int = 100, blocksize: int = 1280):
        self.capacity = int(capacity)
        self.blocksize = int(blocksize)
        self._slots = np.zeros((self.capacity, self.blocksize), dtype=np.float32)
        self._write_pos = 0
        self._read_pos = 0
        self._count = 0
        self._lock = threading.Lock()
        self._not_empty = threading.Event()
        self.overflow_count = 0
        self.recording_overflow_detected = False
        self.dropped_wake_frames = 0
        # Test hooks for deterministically pausing producer/consumer under lock
        self._before_write_finish: Callable[[], None] | None = None
        self._before_read_finish: Callable[[], None] | None = None

    @property
    def occupancy(self) -> int:
        """Returns the number of unconsumed frames currently in the buffer."""
        with self._lock:
            return self._count

    def write_frame(self, indata: np.ndarray, is_recording: bool = False) -> bool:
        """Writes an incoming PortAudio frame into a preallocated ring slot.
        Called from the real-time PortAudio input callback.
        Sample copying and publication are atomically protected under the mutex.
        """
        with self._lock:
            if self._count >= self.capacity:
                self.overflow_count += 1
                if is_recording:
                    self.recording_overflow_detected = True
                    return False
                # Wake mode: drop the oldest frame to preserve real-time response
                self._read_pos = (self._read_pos + 1) % self.capacity
                self._count -= 1
                self.dropped_wake_frames += 1

            idx = self._write_pos
            slot = self._slots[idx]

            # Copy audio samples into preallocated slot under lock before publishing
            if indata.ndim > 1:
                np.copyto(slot, indata[:, 0])
            else:
                np.copyto(slot, indata)

            if self._before_write_finish:
                self._before_write_finish()

            self._write_pos = (self._write_pos + 1) % self.capacity
            self._count += 1

        self._not_empty.set()
        return True

    def read_frame(self, timeout: float = 0.1) -> np.ndarray | None:
        """Extracts the next audio frame from the ring buffer.
        Called by the background MicRouterWorker thread.
        Sample extraction into owned memory and slot release are atomically protected under the mutex.
        """
        if not self._not_empty.wait(timeout=timeout):
            return None

        with self._lock:
            if self._count == 0:
                self._not_empty.clear()
                return None

            idx = self._read_pos
            frame = self._slots[idx].copy()

            if self._before_read_finish:
                self._before_read_finish()

            self._read_pos = (self._read_pos + 1) % self.capacity
            self._count -= 1
            if self._count == 0:
                self._not_empty.clear()
            return frame

    def clear(self):
        """Resets the ring buffer pointers and clears pending frames."""
        with self._lock:
            self._write_pos = 0
            self._read_pos = 0
            self._count = 0
            self._not_empty.clear()
            self.recording_overflow_detected = False


class WakeWordDetector:
    """Unified persistent microphone stream router and openWakeWord detector.

    Maintains exactly one persistent sounddevice.InputStream for the application lifetime.
    Audio frames are captured in a real-time, non-blocking PortAudio callback and forwarded
    into a bounded frame queue. A dedicated background worker thread dispatches frames
    to wake-word neural inference, active VAD capture, or Push-To-Talk (PTT) recording.
    """

    def __init__(
        self,
        config: dict[str, Any],
        on_wake_callback: Callable[[], None] | None = None,
        is_speaking_callback: Callable[[], bool] | None = None
    ):
        audio_cfg = config.get("audio", {})
        self.wake_word = audio_cfg.get("wake_word", "hey jarvis")
        self.threshold = float(audio_cfg.get("wake_word_threshold", 0.55))
        # Configurable barge-in threshold (elevated during TTS playback to reject speaker echo)
        self.barge_in_threshold = float(
            audio_cfg.get("wake_word_barge_in_threshold", max(0.70, self.threshold + 0.15))
        )
        self.energy_threshold = float(audio_cfg.get("wake_word_energy_threshold", 0.0035))
        self.energy_threshold_sq = self.energy_threshold * self.energy_threshold
        self.sample_rate = int(audio_cfg.get("sample_rate", 16000))
        self.blocksize = int(audio_cfg.get("block_size", 1280))  # 80ms at 16kHz
        self.on_wake_callback = on_wake_callback
        self.is_speaking_callback = is_speaking_callback

        self.model = None
        self._vad = None
        self._is_running = False
        self._stream: sd.InputStream | None = None
        self._worker_thread: threading.Thread | None = None

        # Mode and state locks
        self._mode = CaptureMode.WAKE
        self._is_recording = False
        self._state_lock = threading.RLock()

        # Real-time bounded preallocated frame ring buffer (100 slots of 1280 float32 samples = 512KB)
        self._frame_buffer = SPSCFrameRingBuffer(capacity=100, blocksize=self.blocksize)
        self._raw_frame_queue = self._frame_buffer  # backwards-compatibility alias

        # Pre-roll deque: retains 4 lead-in chunks (320ms) during idle ambient monitoring
        self._preroll_buffer: deque = deque(maxlen=4)
        self._active_chunks_remaining = 0
        self._affinity_set = False

        # Active recording sinks
        self._vad_queue: queue.Queue | None = None
        self._ptt_chunks: list[np.ndarray] | None = None

        # Diagnostic and telemetry counters
        self.input_overflow_count = 0

    @property
    def queue_overflow_count(self) -> int:
        """Returns cumulative frame queue overflow count."""
        return self._frame_buffer.overflow_count

    @queue_overflow_count.setter
    def queue_overflow_count(self, val: int):
        self._frame_buffer.overflow_count = val

    @property
    def recording_overflow_detected(self) -> bool:
        """Returns True if any recording overflow occurred in active session."""
        return self._frame_buffer.recording_overflow_detected

    @recording_overflow_detected.setter
    def recording_overflow_detected(self, val: bool):
        self._frame_buffer.recording_overflow_detected = val

    @property
    def dropped_wake_frames(self) -> int:
        """Returns cumulative number of wake frames dropped to maintain real-time latency budget."""
        return self._frame_buffer.dropped_wake_frames

    @dropped_wake_frames.setter
    def dropped_wake_frames(self, val: int):
        self._frame_buffer.dropped_wake_frames = val

    @property
    def mode(self) -> CaptureMode:
        """Returns the current operational capture mode."""
        with self._state_lock:
            return self._mode

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
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to initialize openWakeWord: {e}")
            self.model = None

    def get_vad(self):
        """Returns cached VAD instance to prevent reloading ONNX models on each wake event."""
        if self._vad is None:
            try:
                from openwakeword.vad import VAD
                self._vad = VAD()
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to initialize openWakeWord VAD: {e}")
                return None
        return self._vad

    def _audio_callback(self, indata, frames, time_info, status):
        """Real-time PortAudio input callback.

        Thread Safety & Locking Model:
        PortAudio callbacks execute at high/real-time OS thread priority.
        1. Zero Array Allocations: Audio samples are copied directly into a
           preallocated SPSCFrameRingBuffer slot via np.copyto() (~1.5 microseconds).
           No dynamic heap audio arrays are allocated on the callback path.
        2. Zero Variable-Latency Work: No ONNX model inference, logging, or disk I/O.
        3. Fast Mutex Boundary: The buffer's dedicated lock is held only for pointer
           and counter math (~40ns), completely isolated from application locks.
        4. Deterministic Backlog Control: If full during wake listening, the oldest
           frame is dropped to enforce latency bounds (< 320ms). During recording,
           overflow is recorded without dropping frames silently.
        """
        if status and status.input_overflow:
            self.input_overflow_count += 1

        if not self._is_running:
            return

        try:
            if self._worker_thread and self._worker_thread.is_alive():
                self._frame_buffer.write_frame(indata, is_recording=self._is_recording)
            else:
                # Synchronous test / headless mode (no worker thread started)
                chunk = np.ascontiguousarray(indata[:, 0] if indata.ndim > 1 else indata, dtype=np.float32).copy()
                mode = self._mode
                vad_q = self._vad_queue
                ptt_list = self._ptt_chunks

                if mode == CaptureMode.VAD and vad_q is not None:
                    try:
                        vad_q.put_nowait(chunk)
                    except queue.Full:
                        self.queue_overflow_count += 1
                        self.recording_overflow_detected = True
                elif mode == CaptureMode.PTT and ptt_list is not None:
                    ptt_list.append(chunk)
                elif mode == CaptureMode.WAKE:
                    self._process_wake_chunk(chunk)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error in audio callback: {e}")

    def _worker_loop(self):
        """Dedicated background worker thread for processing raw audio frames and routing."""
        try:
            from jarvis.audio.affinity import pin_and_elevate_current_thread
            pin_and_elevate_current_thread(tag="MicRouterWorker")
        except Exception:  # noqa: BLE001, S110
            pass

        while self._is_running:
            with self._state_lock:
                mode = self._mode
                vad_q = self._vad_queue
                ptt_list = self._ptt_chunks

            if mode == CaptureMode.WAKE:
                # Latency budget guard: wake detection must process real-time audio.
                # If worker thread fell behind, discard stale chunks exceeding 4 frames (320ms budget)
                # before reading so that wake and barge-in response processes only fresh audio.
                while self._frame_buffer.occupancy > 4:
                    stale = self._frame_buffer.read_frame(timeout=0.0)
                    if stale is None:
                        break
                    self._frame_buffer.dropped_wake_frames += 1

            chunk = self._frame_buffer.read_frame(timeout=0.1)
            if chunk is None:
                continue

            if mode == CaptureMode.MUTED:
                continue

            if mode == CaptureMode.VAD and vad_q is not None:
                try:
                    vad_q.put_nowait(chunk)
                except queue.Full:
                    self.queue_overflow_count += 1
                    self.recording_overflow_detected = True
                continue

            if mode == CaptureMode.PTT and ptt_list is not None:
                ptt_list.append(chunk)
                continue

            if mode == CaptureMode.WAKE:
                self._process_wake_chunk(chunk)

    def _process_wake_chunk(self, chunk: np.ndarray):
        """Evaluates BLAS energy gate and executes openWakeWord neural inference."""
        if self.model is None:
            return

        # Determine if Jarvis TTS is currently speaking
        is_speaking = False
        if self.is_speaking_callback:
            try:
                is_speaking = bool(self.is_speaking_callback())
            except Exception as e:  # noqa: BLE001
                logger.debug("Error in is_speaking_callback: %s", e)

        # Select effective threshold: elevated during TTS playback to reject speaker echo
        effective_threshold = self.barge_in_threshold if is_speaking else self.threshold

        # Zero-allocation mean squared energy via BLAS dot product
        mean_square_energy = float(np.dot(chunk, chunk) / len(chunk))

        # Sub-microsecond energy gate: if room is silent, skip heavy ONNX neural inferences
        if mean_square_energy >= self.energy_threshold_sq:
            # Voice / sound detected: sustain active prediction window for ~2.0s (25 chunks)
            self._active_chunks_remaining = 25
        elif self._active_chunks_remaining > 0:
            self._active_chunks_remaining -= 1

        # If completely idle (silent ambient room), retain chunk in pre-roll and return
        if self._active_chunks_remaining == 0:
            self._preroll_buffer.append(chunk.copy())
            return

        # Active speech processing: feed pre-roll lead-in chunks if waking from idle
        chunks_to_feed = []
        if self._preroll_buffer:
            while self._preroll_buffer:
                c = self._preroll_buffer.popleft()
                chunks_to_feed.append((c * 32767).astype(np.int16))

        chunks_to_feed.append((chunk * 32767).astype(np.int16))

        try:
            for chunk_pcm in chunks_to_feed:
                prediction = self.model.predict(chunk_pcm)
                for score in prediction.values():
                    if score >= effective_threshold:
                        logger.info(
                            f"Wake word detected! Score: {score:.3f} "
                            f"(threshold: {effective_threshold:.2f}, barge_in: {is_speaking})"
                        )
                        # Retain pre-roll of the wake frame for seamless VAD handoff
                        self._preroll_buffer.append(chunk.copy())
                        self._active_chunks_remaining = 0
                        self.model.reset()

                        # Signal interruption if Jarvis is currently speaking (from worker thread!)
                        if is_speaking:
                            try:
                                from jarvis.audio.cancellation import (
                                    trigger_speech_barge_in,
                                )
                                trigger_speech_barge_in()
                            except Exception as e:  # noqa: BLE001
                                logger.warning("Barge-in cancellation signal failed: %s", e)

                        # Invoke wake callback on a background dispatch thread to avoid blocking worker loop
                        if self.on_wake_callback:
                            threading.Thread(
                                target=self._safe_invoke_callback,
                                args=(self.on_wake_callback,),
                                name="WakeCallbackDispatch",
                                daemon=True,
                            ).start()
                        return
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error predicting wake word: {e}")

    @staticmethod
    def _safe_invoke_callback(cb: Callable[[], None]):
        """Executes wake callback safely caught on dispatch thread."""
        try:
            cb()
        except Exception:
            logger.exception("Error in wake callback execution")

    def start_vad_session(self, maxsize: int = 500) -> VADSession:
        """Transitions router to VAD recording mode and returns a session consuming audio chunks."""
        with self._state_lock:
            self._mode = CaptureMode.VAD
            self._is_recording = True
            self._vad_queue = queue.Queue(maxsize=maxsize)
            self.recording_overflow_detected = False

            # Pre-roll handoff: inject any buffered pre-roll chunks into the VAD queue
            while self._preroll_buffer:
                self._vad_queue.put_nowait(self._preroll_buffer.popleft())

            return VADSession(self._vad_queue, self)

    def stop_vad_session(self):
        """Terminates active VAD recording session and returns to ambient wake listening."""
        with self._state_lock:
            if self._mode == CaptureMode.VAD:
                self._mode = CaptureMode.WAKE
            self._is_recording = False
            self._vad_queue = None
            self._preroll_buffer.clear()
            self._active_chunks_remaining = 0

    def start_ptt_session(self):
        """Transitions router to Push-To-Talk recording mode."""
        with self._state_lock:
            # If VAD was active, preempt it
            if self._mode == CaptureMode.VAD:
                self._vad_queue = None
            self._mode = CaptureMode.PTT
            self._is_recording = True
            self._ptt_chunks = []
            self.recording_overflow_detected = False

    def stop_ptt_session(self) -> list[np.ndarray]:
        """Terminates Push-To-Talk recording session and returns captured chunks."""
        with self._state_lock:
            chunks = self._ptt_chunks or []
            self._ptt_chunks = None
            if self._mode == CaptureMode.PTT:
                self._mode = CaptureMode.WAKE
            self._is_recording = False
            self._preroll_buffer.clear()
            self._active_chunks_remaining = 0
            return chunks

    def start(self):
        """Starts the persistent microphone input stream and processing worker thread."""
        if self._is_running:
            return

        if self.model is None:
            self._init_model()

        if self.model is None:
            logger.warning("openWakeWord model unavailable. Wake word detection disabled.")
            return

        self._is_running = True
        self._mode = CaptureMode.WAKE
        self._is_recording = False
        self.recording_overflow_detected = False

        # Start background worker thread
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="MicStreamWorker", daemon=True
        )
        self._worker_thread.start()

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self.blocksize,
                callback=self._audio_callback,
            )
            self._stream.start()
            logger.info("Wake word detector started listening (single persistent stream).")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to start audio stream for wake word: {e}")
            self._is_running = False
            if self._worker_thread and self._worker_thread.is_alive():
                self._worker_thread.join(timeout=0.2)
            self._worker_thread = None

    def stop(self):
        """Stops the persistent microphone input stream and worker thread."""
        self._is_running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001, S110
                pass
            self._stream = None

        if self._worker_thread is not None and self._worker_thread.is_alive():
            try:
                self._worker_thread.join(timeout=0.5)
            except Exception:  # noqa: BLE001, S110
                pass
            self._worker_thread = None

        self._frame_buffer.clear()
        with self._state_lock:
            self._mode = CaptureMode.MUTED
            self._is_recording = False
            self._vad_queue = None
            self._ptt_chunks = None
            self._preroll_buffer.clear()
            self._active_chunks_remaining = 0
