"""Text-to-Speech synthesis using Kokoro-82M neural TTS, Windows SAPI5, and Edge-TTS."""
import asyncio
import logging
import os
import queue
import re
import threading
import time
from collections import deque
from typing import Any

import numpy as np
import sounddevice as sd
import win32com.client

from jarvis.audio.affinity import pin_and_elevate_current_thread
from jarvis.audio.chunker import StreamingSentenceChunker
from jarvis.audio.ring_buffer import AudioRingBuffer, apply_cinematic_jarvis_dsp
from jarvis.core.win32_affinity import (
    MASK_PCORE_COMPUTE,
    THREAD_PRIORITY_ABOVE_NORMAL,
    elevate_audio_thread_mmcss,
    pin_current_thread_to_mask,
)

logger = logging.getLogger("Jarvis.TTS")

__all__ = [
    "AudioRingBuffer",
    "StatefulPolyphaseUpsampler",
    "StreamingSentenceChunker",
    "TextToSpeech",
    "apply_cinematic_jarvis_dsp",
    "create_bettanys_jarvis_voice_vector",
]


class StatefulPolyphaseUpsampler:
    """Eliminates the 0.96ms FIR group-delay boundary notch when upsampling
    streaming 24kHz prosodic chunks to 48kHz WASAPI DMA buffers.
    """

    __slots__ = ("_down", "_pad_samples", "_tail_24k", "_up")

    def __init__(self, up: int = 2, down: int = 1, pad_samples: int = 32) -> None:
        self._up = int(up)
        self._down = int(down)
        self._pad_samples = int(pad_samples)
        self._tail_24k: np.ndarray | None = None

    def reset(self) -> None:
        """Flushes lookbehind filter state on turn boundary or barge-in."""
        self._tail_24k = None

    def process(self, chunk_24k: np.ndarray) -> np.ndarray:
        """Upsamples a 1D float32 PCM chunk seamlessly across chunk boundaries."""
        pcm = np.asarray(chunk_24k, dtype=np.float32).ravel()
        if pcm.size == 0:
            return pcm
        if self._up == self._down:
            return pcm

        try:
            from scipy.signal import resample_poly
        except ImportError:
            return pcm

        p = self._pad_samples
        if self._tail_24k is not None and self._tail_24k.size == p:
            extended = np.concatenate((self._tail_24k, pcm))
            upsampled = resample_poly(extended, up=self._up, down=self._down).astype(
                np.float32, copy=False
            )
            trim = (p * self._up) // self._down
            out = upsampled[trim:]
        else:
            out = resample_poly(pcm, up=self._up, down=self._down).astype(
                np.float32, copy=False
            )

        if pcm.size >= p:
            self._tail_24k = pcm[-p:].copy()
        else:
            self._tail_24k = None
        return np.ascontiguousarray(out, dtype=np.float32)



def create_bettanys_jarvis_voice_vector(kokoro_instance) -> np.ndarray:
    """Blends Kokoro's British male latent vectors in R^256 to shift bm_george
    from 'warm documentary narrator' to 'crisp, dry Paul Bettany tenor'.
    """
    v_fable = kokoro_instance.get_voice_style("bm_fable")   # Dry, crisp ironic British cadence
    v_george = kokoro_instance.get_voice_style("bm_george")  # Composed resonant chest tone
    v_daniel = kokoro_instance.get_voice_style("bm_daniel")  # Clinical RP consonant precision

    # Weighted barycentric blend in StyleTTS-2 latent space
    jarvis_style = (0.50 * v_fable) + (0.30 * v_george) + (0.20 * v_daniel)
    return jarvis_style.astype(np.float32)


def _set_thread_realtime_priority(tag: str = "KokoroTTS"):
    """Pins current thread to P-Cores and sets priority to THREAD_PRIORITY_HIGHEST for zero audio jitter."""
    pin_and_elevate_current_thread(tag=tag)


_CACHED_KOKORO_SESSION: dict[tuple[str, str], Any] = {}
_CACHED_KOKORO_LOCK = threading.Lock()


class TextToSpeech:
    """Streams and plays synthesized neural audio with instant interrupt capability and local fallback."""

    VOICE_PROFILES: dict[str, Any] = {
        "jarvis_bettany": [("bm_fable", 0.50), ("bm_george", 0.30), ("bm_daniel", 0.20)],
        "bettany": [("bm_fable", 0.50), ("bm_george", 0.30), ("bm_daniel", 0.20)],
        "jarvis_classic": [("bm_george", 0.70), ("bm_daniel", 0.30)],
        "jarvis_authoritative": [("bm_george", 0.50), ("bm_lewis", 0.50)],
        "jarvis_crisp": [("bm_daniel", 0.80), ("bm_fable", 0.20)],
        "friday": [("bf_emma", 0.70), ("bf_isabella", 0.30)],
        "george": "bm_george",
        "lewis": "bm_lewis",
        "daniel": "bm_daniel",
        "fable": "bm_fable",
        "emma": "bf_emma",
        "alice": "bf_alice",
        "isabella": "bf_isabella",
        "lily": "bf_lily",
    }

    SHUTDOWN_CLEAN_STOP = "CLEAN_STOP"
    SHUTDOWN_RECOVERED_ABORT = "RECOVERED_ABORT"
    SHUTDOWN_UNCLEAN_FAILED = "UNCLEAN_FAILED"
    SHUTDOWN_TIMEOUT_BLOCKED = "TIMEOUT_BLOCKED"

    def __init__(self, config: dict[str, Any]):
        tts_cfg = config.get("tts", {})
        self.engine = tts_cfg.get("engine", "kokoro").lower()
        self._voice_cache: dict[str, Any] = {}
        
        # Kokoro Neural Voice Settings
        self.kokoro_voice = tts_cfg.get("kokoro_voice", "jarvis_bettany")
        self.kokoro_speed = float(tts_cfg.get("kokoro_speed", 1.03))
        self.kokoro_lang = tts_cfg.get("kokoro_lang", "en-gb")
        self.device = tts_cfg.get("device", "cpu").lower()
        self.dsp_filter = bool(tts_cfg.get("dsp_filter", True))
        self.cpu_threads = int(tts_cfg.get("cpu_threads", 4))
        base_dir = config.get("_base_dir", os.getcwd())
        self.kokoro_model_path = os.path.join(base_dir, tts_cfg.get("kokoro_model", "data/models/kokoro/kokoro-v1.0.onnx"))
        self.kokoro_voices_path = os.path.join(base_dir, tts_cfg.get("kokoro_voices", "data/models/kokoro/voices-v1.0.bin"))
        
        # SAPI5 Settings
        self.sapi_voice = tts_cfg.get("sapi_voice", "David")
        self.sapi_rate = tts_cfg.get("sapi_rate", 1)
        
        # Edge-TTS Settings
        self.voice = tts_cfg.get("voice", "en-GB-RyanNeural")
        self.rate = tts_cfg.get("rate", "+10%")
        self.volume = tts_cfg.get("volume", "+0%")
        self.pitch = tts_cfg.get("pitch", "+0Hz")
        self.stream_timeout_seconds = max(5.0, float(tts_cfg.get("stream_timeout_seconds", 30.0)))
        
        self._is_speaking = False
        self._stop_event = threading.Event()
        
        # Streaming worker state & Speculative Voice Reflex pipeline
        self._sentence_queue: queue.Queue = queue.Queue()
        self._audio_queue: queue.Queue = queue.Queue()
        self._synth_thread: threading.Thread | None = None
        self._playback_thread: threading.Thread | None = None
        self._output_stream: sd.OutputStream | None = None
        self._stream_active = False
        self._session_word_count = 0
        self._underrun_count: int = 0
        self._retained_streams: deque = deque(maxlen=8)
        self._stream_shutdown_timed_out: bool = False
        self._stream_cleanup_uncertain: bool = False
        self._last_shutdown_outcome: str = "NONE"
        self._shutdown_worker = None
        self._shutdown_stream = None
        self._in_stop: bool = False

        # Speculative token chunker & high-throughput lock-free audio ring buffer (120s capacity at up to 48kHz)
        self._chunker = StreamingSentenceChunker()
        self._ring_buffer = AudioRingBuffer(capacity_samples=48000 * 120, ramp_samples=120)
        self._stream_device: int | None = None
        self._stream_sample_rate: int = 24000
        self._synth_has_started: bool = False
        self._resampler = StatefulPolyphaseUpsampler(up=2, down=1, pad_samples=32)

        # Kokoro engine instance
        self._kokoro = None
        if self.engine == "kokoro":
            self._init_kokoro()

        # Initialize local SAPI5 instance as fallback
        self._sapi = None
        self._init_sapi()

        # Register audio flush callback for immediate barge-in interruption
        try:
            from jarvis.audio.cancellation import register_audio_flush_callback
            register_audio_flush_callback(self.stop)
        except Exception:
            pass

        # Zero-copy shared memory transport (enabled via config or runtime fallback)
        self._shm_transport: Any | None = None
        if tts_cfg.get("use_shm_transport", False):
            try:
                from jarvis.audio.shm_transport import (
                    DEFAULT_RING_NAME,
                    ShmAudioRingTransport,
                )
                # Attach if the DSP client already created the ring, create it otherwise.
                self._shm_transport = ShmAudioRingTransport.open_or_create(
                    name=tts_cfg.get("shm_name", DEFAULT_RING_NAME),
                    capacity_samples=24000 * 10,
                    sample_rate=24000,
                )
            except Exception as e:
                logger.warning("ShmAudioRingTransport unavailable, continuing without it: %s", e)

    def _init_kokoro(self):
        """Initializes Kokoro neural engine with constrained CPU threading to prevent CPU spikes."""
        try:
            # Silence phonemizer stderr mismatch warnings and protect Windows atexit teardown
            try:
                import phonemizer.logger
                phonemizer.logger.get_logger("quiet")
                logging.getLogger("phonemizer").setLevel(logging.ERROR)
                from phonemizer.backend.espeak.api import EspeakAPI
                _orig_del = getattr(EspeakAPI, "_delete_win32", None)
                if _orig_del:
                    def _safe_del_win32(self):
                        try:
                            if getattr(self, "_library", None) and getattr(self._library, "_handle", None):
                                _orig_del(self)
                        except Exception:
                            pass
                    EspeakAPI._delete_win32 = _safe_del_win32
            except Exception:
                pass

            if os.path.exists(self.kokoro_model_path) and os.path.exists(self.kokoro_voices_path):
                cache_key = (os.path.abspath(self.kokoro_model_path), os.path.abspath(self.kokoro_voices_path))
                with _CACHED_KOKORO_LOCK:
                    if cache_key in _CACHED_KOKORO_SESSION:
                        self._kokoro = _CACHED_KOKORO_SESSION[cache_key]
                        logger.debug("Reused cached Kokoro neural engine session.")
                        return

                from jarvis.audio.stt import _setup_nvidia_dll_paths
                _setup_nvidia_dll_paths()
                import onnxruntime as rt
                rt.set_default_logger_severity(3)
                from kokoro_onnx import Kokoro
                opts = rt.SessionOptions()
                opts.intra_op_num_threads = self.cpu_threads
                opts.inter_op_num_threads = 1
                opts.enable_cpu_mem_arena = False
                opts.log_severity_level = 3
                opts.graph_optimization_level = rt.GraphOptimizationLevel.ORT_ENABLE_ALL
                # Eliminate thread-pool busy-spinning to protect Python event loops and audio callbacks
                opts.add_session_config_entry("session.intra_op.allow_spinning", "0")

                available = rt.get_available_providers()
                providers = []
                # Kokoro-82M runs with sub-50ms latency on CPU with 4 P-core threads,
                # avoiding CUDA context collisions and 0xC0000005 access violations with faster-whisper.
                preferred_device = getattr(self, "device", "cpu")
                use_cuda = (preferred_device == "cuda") and ("CUDAExecutionProvider" in available)

                # Dynamic VRAM arbiter check for RTX 3050 4GB ceiling if CUDA requested
                if use_cuda:
                    try:
                        from jarvis.skills.nvml_dxgi_vram_arbiter import (
                            audit_vram_and_thermal_budget,
                        )
                        vram_info = audit_vram_and_thermal_budget(reclaim_if_pressured=True)
                        if vram_info.get("status") == "ok":
                            free_mb = vram_info.get("vram_free_mb", 4000.0)
                            if free_mb < 600.0:
                                logger.warning(
                                    "RTX 3050 VRAM heavily pressured (%.1f MB free < 600MB). Falling back Kokoro TTS to CPUExecutionProvider.",
                                    free_mb
                                )
                                use_cuda = False
                    except Exception as arb_err:
                        logger.debug("VRAM arbiter check note in Kokoro init: %s", arb_err)

                if use_cuda:
                    cuda_opts = {
                        "device_id": 0,
                        "arena_extend_strategy": "kNextPowerOfTwo",
                        "gpu_mem_limit": 2 * 1024 * 1024 * 1024,  # 2GB VRAM allocation ceiling
                        "cudnn_conv_algo_search": "HEURISTIC",     # Fast heuristic conv search, eliminates fallback warnings
                        "do_copy_in_default_stream": "1",
                    }
                    providers.append(("CUDAExecutionProvider", cuda_opts))
                elif "DmlExecutionProvider" in available:
                    providers.append(("DmlExecutionProvider", {"device_id": 0}))
                providers.append("CPUExecutionProvider")

                try:
                    sess = rt.InferenceSession(self.kokoro_model_path, sess_options=opts, providers=providers)
                except Exception as prov_err:
                    logger.warning("GPU provider initialization failed (%s); falling back to CPUExecutionProvider.", prov_err)
                    sess = rt.InferenceSession(self.kokoro_model_path, sess_options=opts, providers=["CPUExecutionProvider"])

                self._kokoro = Kokoro.from_session(sess, self.kokoro_voices_path)

                # Wrap Kokoro voice lookup methods to seamlessly handle custom blended profiles
                orig_create = self._kokoro.create
                orig_get_style = self._kokoro.get_voice_style
                orig_get_voices = self._kokoro.get_voices

                def _wrapped_get_voice_style(v_name: str):
                    clean = v_name.lower().strip()
                    if clean in self.VOICE_PROFILES and clean not in self._kokoro.voices:
                        return self._resolve_voice(clean)
                    return orig_get_style(v_name)

                def _wrapped_get_voices():
                    voices = list(orig_get_voices())
                    for prof in self.VOICE_PROFILES:
                        if prof not in voices:
                            voices.append(prof)
                    return sorted(voices)

                def _wrapped_create(text: str, voice: Any = None, *args, **kwargs):
                    if voice is None:
                        voice = self._resolve_voice()
                    elif isinstance(voice, str) and (voice.lower().strip() in self.VOICE_PROFILES or voice.lower().strip() in self._voice_cache):
                        voice = self._resolve_voice(voice)
                    return orig_create(text, voice=voice, *args, **kwargs)

                self._kokoro.get_voice_style = _wrapped_get_voice_style
                self._kokoro.get_voices = _wrapped_get_voices
                self._kokoro.create = _wrapped_create

                # Pre-warm GPU execution graphs and voice style tensors to eliminate 700ms first-turn latency
                try:
                    warmup_voice = self._resolve_voice()
                    self._kokoro.create(".", voice=warmup_voice, speed=1.0, lang=self.kokoro_lang)
                except Exception as w_err:
                    logger.debug("Kokoro startup warmup note: %s", w_err)

                with _CACHED_KOKORO_LOCK:
                    _CACHED_KOKORO_SESSION[cache_key] = self._kokoro
            else:
                logger.warning("Kokoro model files not found. Defaulting to SAPI5.")
                self.engine = "sapi5"
        except Exception as e:
            logger.warning(f"Kokoro initialization failed: {e}. Defaulting to SAPI5.")
            self.engine = "sapi5"

    def _init_sapi(self):
        try:
            self._sapi = win32com.client.Dispatch("SAPI.SpVoice")
            self._sapi.Rate = self.sapi_rate
        except Exception as e:
            logger.warning(f"Local SAPI5 voice initialization failed: {e}")

    def _resolve_voice(self, voice_name: str | None = None) -> Any:
        """Resolves active voice into a string identifier or normalized blended latent style array."""
        if not self._kokoro:
            return voice_name or self.kokoro_voice
        target = voice_name or self.kokoro_voice
        if isinstance(target, str):
            clean = target.lower().strip()
            if clean in self._voice_cache:
                return self._voice_cache[clean]

            profile = self.VOICE_PROFILES.get(clean)
            if profile:
                if isinstance(profile, str):
                    self._voice_cache[clean] = profile
                    return profile
                try:
                    import numpy as np
                    total_weight = sum(w for _, w in profile)
                    blended = None
                    for v_name, weight in profile:
                        v_style = self._kokoro.get_voice_style(v_name)
                        norm_w = weight / total_weight
                        if blended is None:
                            blended = (norm_w * v_style).astype(np.float32)
                        else:
                            blended += (norm_w * v_style).astype(np.float32)
                    self._voice_cache[clean] = blended
                    return blended
                except Exception as e:
                    logger.warning(f"Voice blend calculation error for '{clean}': {e}")
                    return "bm_george"
        return target

    def set_voice_profile(self, profile_name: str) -> str:
        """Dynamically switches active Kokoro voice to a named profile or voice ID."""
        clean = (profile_name or "").lower().strip()
        available_kokoro = self._kokoro.get_voices() if self._kokoro else []
        if clean in self.VOICE_PROFILES or clean in available_kokoro:
            self.kokoro_voice = clean
            return f"Voice profile successfully set to '{clean}', Sir."
        profiles_list = ", ".join(list(self.VOICE_PROFILES.keys())[:6])
        return f"Unknown voice profile '{profile_name}'. Available profiles include: {profiles_list}..."

    def set_speed(self, speed: float) -> str:
        """Adjusts speech tempo multiplier (0.8x to 1.5x)."""
        try:
            val = float(speed)
            val = max(0.75, min(1.50, val))
            self.kokoro_speed = val
            return f"Speech rate set to {val:.2f}x, Sir."
        except Exception as e:
            return f"Failed to set speech rate: {e}"

    def get_voice_settings(self) -> dict[str, Any]:
        """Returns current TTS engine parameters and active voice profile."""
        return {
            "engine": self.engine,
            "voice": self.kokoro_voice,
            "speed": self.kokoro_speed,
            "lang": self.kokoro_lang,
            "profiles": list(self.VOICE_PROFILES.keys()),
        }

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    def abort_and_partition_clauses(self) -> tuple[list[str], list[str]]:
        """Partitions spoken completed clauses vs unspoken remaining clauses upon barge-in."""
        spoken = list(getattr(self, "_spoken_clauses", []))
        enqueued = list(getattr(self, "_enqueued_clauses", []))
        unspoken = enqueued[len(spoken):] if len(enqueued) > len(spoken) else []
        self.stop()
        return spoken, unspoken

    def queue_response_clauses(self, turn_id: str, text: str):
        """Splits response text into clauses and enqueues them for streaming playback."""
        import re
        clauses = [c.strip() for c in re.split(r"(?<!\d)(?<=[.!?])(?!\d)\s+", text) if c.strip()]
        for c in clauses:
            self.queue_sentence(c)

    def _close_stream_safely(self, stream: Any, stop_timeout: float = 0.100) -> bool:
        """Safely stops and closes a PortAudio hardware stream on a dedicated control thread.

        PortAudio stream operations (stop, abort, close) are NOT thread-safe on the
        same stream pointer. If Pa_StopStream hangs inside a kernel driver deadlock,
        concurrently invoking abort() or close() on that same stream handle will cause
        memory corruption or process termination.

        This method bounds the caller's wait time to `stop_timeout`. If the native
        operation does not complete within `stop_timeout`, the caller abandons the wait,
        retains ownership of the stream handle in `_retained_streams` without calling
        abort or close, and logs an explicit warning. Outstanding shutdown workers are
        strictly bounded: at most one worker thread may be active, and subsequent stream
        creation fails closed until the active worker finishes.

        Returns:
            bool: True if the stream stopped and closed cleanly within `stop_timeout`,
                  False if the native call stalled or failed and was safely retained.
        """
        if stream is None:
            return True

        if hasattr(self, "_retained_streams"):
            self._retained_streams.append(stream)

        # Enforce single outstanding native shutdown operation
        if getattr(self, "_shutdown_worker", None) and self._shutdown_worker.is_alive():
            if self._shutdown_stream is stream:
                self._shutdown_worker.join(timeout=stop_timeout)
                if self._shutdown_worker.is_alive():
                    self._stream_shutdown_timed_out = True
                    self._stream_cleanup_uncertain = True
                    self._last_shutdown_outcome = self.SHUTDOWN_TIMEOUT_BLOCKED
                    return False
                prior_status = getattr(self._shutdown_worker, "outcome", {}).get("status", self.SHUTDOWN_UNCLEAN_FAILED)
                self._last_shutdown_outcome = prior_status
                if prior_status in (self.SHUTDOWN_CLEAN_STOP, self.SHUTDOWN_RECOVERED_ABORT):
                    self._shutdown_worker = None
                    self._shutdown_stream = None
                    self._stream_shutdown_timed_out = False
                    self._stream_cleanup_uncertain = False
                    return True
                self._shutdown_worker = None
                self._stream_shutdown_timed_out = False
                self._stream_cleanup_uncertain = True
                logger.error("Prior native stream shutdown completed with unclean outcome: %s", prior_status)
                return False

            logger.error(
                "Cannot initiate stream shutdown: prior shutdown worker is still active. "
                "Refusing to spawn additional shutdown workers to prevent thread and handle accumulation."
            )
            self._stream_shutdown_timed_out = True
            self._stream_cleanup_uncertain = True
            self._last_shutdown_outcome = self.SHUTDOWN_TIMEOUT_BLOCKED
            return False

        self._shutdown_stream = stream
        worker_outcome: dict[str, Any] = {
            "status": "UNKNOWN",
            "stop_error": None,
            "abort_error": None,
            "close_error": None,
        }

        def _native_shutdown_worker():
            stop_ok = False
            abort_ok = False
            close_ok = False
            try:
                stream.stop(ignore_errors=False)
                stop_ok = True
            except (sd.PortAudioError, OSError, RuntimeError) as exc:
                worker_outcome["stop_error"] = exc
                logger.debug("stream.stop(ignore_errors=False) raised (%s); attempting sequential stream.abort()", exc)
                try:
                    stream.abort(ignore_errors=False)
                    abort_ok = True
                except (sd.PortAudioError, OSError, RuntimeError) as abort_exc:
                    worker_outcome["abort_error"] = abort_exc
                    logger.warning("Sequential stream.abort(ignore_errors=False) failed: %s", abort_exc)
                except Exception as abort_exc:  # noqa: BLE001
                    worker_outcome["abort_error"] = abort_exc
                    logger.warning("Unexpected exception during stream.abort(): %s", abort_exc)
            except Exception as exc:  # noqa: BLE001
                worker_outcome["stop_error"] = exc
                logger.debug("Unexpected exception during stream.stop(): %s", exc)
                try:
                    stream.abort(ignore_errors=False)
                    abort_ok = True
                except Exception as abort_exc:  # noqa: BLE001
                    worker_outcome["abort_error"] = abort_exc
                    logger.warning("Unexpected exception during stream.abort(): %s", abort_exc)
            finally:
                try:
                    stream.close(ignore_errors=False)
                    close_ok = True
                except Exception as close_exc:  # noqa: BLE001
                    worker_outcome["close_error"] = close_exc
                    logger.error("stream.close(ignore_errors=False) failed: %s", close_exc)

            if close_ok and stop_ok:
                worker_outcome["status"] = self.SHUTDOWN_CLEAN_STOP
            elif close_ok and abort_ok:
                worker_outcome["status"] = self.SHUTDOWN_RECOVERED_ABORT
            else:
                worker_outcome["status"] = self.SHUTDOWN_UNCLEAN_FAILED

        shutdown_thread = threading.Thread(
            target=_native_shutdown_worker,
            name="Jarvis-TTS-StreamShutdown",
            daemon=True,
        )
        shutdown_thread.outcome = worker_outcome
        self._shutdown_worker = shutdown_thread
        shutdown_thread.start()
        shutdown_thread.join(timeout=stop_timeout)

        if shutdown_thread.is_alive():
            logger.warning(
                "Native audio stream.stop() did not complete within %.3fs. "
                "Retaining handle without concurrent abort/close to prevent PortAudio memory corruption.",
                stop_timeout,
            )
            self._stream_shutdown_timed_out = True
            self._stream_cleanup_uncertain = True
            self._last_shutdown_outcome = self.SHUTDOWN_TIMEOUT_BLOCKED
            return False

        status = worker_outcome.get("status", self.SHUTDOWN_UNCLEAN_FAILED)
        self._last_shutdown_outcome = status

        if status in (self.SHUTDOWN_CLEAN_STOP, self.SHUTDOWN_RECOVERED_ABORT):
            self._shutdown_worker = None
            self._shutdown_stream = None
            self._stream_shutdown_timed_out = False
            self._stream_cleanup_uncertain = False
            return True

        self._shutdown_worker = None
        self._stream_shutdown_timed_out = False
        self._stream_cleanup_uncertain = True
        logger.error(
            "Native audio stream cleanup failed (stop_err=%s, abort_err=%s, close_err=%s). "
            "Stream state is uncertain.",
            worker_outcome["stop_error"],
            worker_outcome["abort_error"],
            worker_outcome["close_error"],
        )
        return False

    def reconcile_stream_lifecycle(self, force: bool = False) -> bool:
        """Reconciles uncertain or stalled stream lifecycle states.

        If a prior shutdown worker was running and has since terminated cleanly,
        this reconciles the state and returns True.
        If `force=True` is provided, forces clearing of `_stream_cleanup_uncertain`
        and `_shutdown_stream` (e.g. after explicit audio device re-enumeration or
        subsystem reset by the host).

        Returns:
            bool: True if stream lifecycle is clear/reconciled, False if still stalled or unclean.
        """
        if getattr(self, "_shutdown_worker", None):
            if self._shutdown_worker.is_alive():
                if not force:
                    return False
                logger.warning("Force-reconciling stream lifecycle while shutdown worker is still alive.")
            else:
                prior_outcome = getattr(self._shutdown_worker, "outcome", {}).get("status", self.SHUTDOWN_UNCLEAN_FAILED)
                self._last_shutdown_outcome = prior_outcome
                if prior_outcome in (self.SHUTDOWN_CLEAN_STOP, self.SHUTDOWN_RECOVERED_ABORT):
                    self._shutdown_worker = None
                    self._shutdown_stream = None
                    self._stream_shutdown_timed_out = False
                    self._stream_cleanup_uncertain = False
                    return True

        if getattr(self, "_stream_cleanup_uncertain", False):
            if not force:
                return False
            logger.warning("Force-clearing uncertain stream cleanup state.")

        self._shutdown_worker = None
        self._shutdown_stream = None
        self._stream_shutdown_timed_out = False
        self._stream_cleanup_uncertain = False
        return True

    def stop(self, stop_timeout: float = 0.100, ramp_timeout: float = 0.030):
        """Immediately interrupts and halts speech playback across all engines."""
        if getattr(self, "_in_stop", False):
            return
        self._in_stop = True
        try:
            self._do_stop(stop_timeout=stop_timeout, ramp_timeout=ramp_timeout)
        finally:
            self._in_stop = False

    def _do_stop(self, stop_timeout: float = 0.100, ramp_timeout: float = 0.030):
        self._stop_event.set()
        self._stream_active = False

        # Reset speculative token chunker
        if hasattr(self, "_chunker"):
            self._chunker.reset()

        # Reset stateful polyphase resampler lookbehind tail
        if hasattr(self, "_resampler"):
            self._resampler.reset()

        # Trigger clickless barge-in (< 1 microsecond atomic epoch reset + 2.5ms cos^2 ramp)
        if hasattr(self, "_ring_buffer"):
            self._ring_buffer.barge_in()

        if hasattr(self, "_shm_transport") and self._shm_transport:
            try:
                self._shm_transport.trigger_barge_in_clear()
            except Exception:
                pass

        try:
            from jarvis.core.cancellation import (
                is_barge_in_active,
                signal_global_barge_in,
            )
            if not is_barge_in_active():
                signal_global_barge_in()
        except Exception:
            pass

        # Empty queues
        while not self._sentence_queue.empty():
            try:
                self._sentence_queue.get_nowait()
            except Exception:
                break

        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except Exception:
                break

        # Coordinated fade-and-stop: allow bounded time (~25-30 ms) for the callback to consume the anti-click ramp
        stream = getattr(self, "_output_stream", None)
        if stream:
            self._output_stream = None
            if getattr(stream, "active", False):
                ramp_deadline = time.monotonic() + ramp_timeout
                while (
                    hasattr(self, "_ring_buffer")
                    and self._ring_buffer._consumer_epoch != self._ring_buffer._barge_in_epoch
                    and time.monotonic() < ramp_deadline
                ):
                    time.sleep(0.002)
            self._close_stream_safely(stream, stop_timeout=stop_timeout)
        elif getattr(self, "_shutdown_worker", None) and self._shutdown_worker.is_alive():
            # A previous native stream shutdown is still pending; wait bounded time on existing worker
            self._close_stream_safely(self._shutdown_stream, stop_timeout=stop_timeout)
        else:
            # Fallback halt for default sounddevice stream
            try:
                sd.stop()
            except Exception:
                pass

        # Halt SAPI5 speech only if sapi engine was active
        if self.engine == "sapi5" and self._sapi:
            try:
                self._sapi.Speak("", 2)
            except Exception:
                pass

        # Join any running synthesis and playback worker threads cleanly with microsecond budget
        synth = getattr(self, "_synth_thread", None)
        if synth and synth.is_alive():
            try:
                synth.join(timeout=0.01)
            except Exception:
                pass

        play = getattr(self, "_playback_thread", None)
        if play and play.is_alive():
            try:
                play.join(timeout=0.01)
            except Exception:
                pass

        self._is_speaking = False

    def _clean_text_for_speech(self, text: str) -> str:
        """Normalizes times, numbers, dates, units, and formatting for natural spoken delivery."""
        if not text:
            return ""
        from jarvis.audio.chunker import sanitize_clause_for_ear
        from jarvis.audio.normalizer import SpeechNormalizer
        text_ear = sanitize_clause_for_ear(text)
        cleaned = SpeechNormalizer.normalize(text_ear)
        if not re.search(r"[a-zA-Z0-9]", cleaned):
            return ""
        return cleaned

    @staticmethod
    def _smooth_and_pad_audio(samples: np.ndarray, sr: int, chunk_text: str, apply_dsp: bool = True) -> np.ndarray:
        """Applies signature MCU J.A.R.V.I.S. DSP filter (110Hz HPF + 7.5ms holographic sheen),
        5ms Hann window smoothing to eliminate boundary transients/clicks, peak limiting to prevent
        clipping, and appends natural acoustic silence pauses."""
        if samples is None or len(samples) == 0:
            return samples

        # 0. Apply signature MCU J.A.R.V.I.S. DSP (110Hz HPF + 7.5ms holographic sheen)
        if apply_dsp:
            samples = apply_cinematic_jarvis_dsp(samples, sr)

        # 1. 5ms Hann window smoothing on head and tail (anti-click / anti-baggy cross-fade)
        if len(samples) > 240:
            fade_len = min(120, len(samples) // 8)  # 5ms at 24kHz = 120 samples
            fade_in = (0.5 * (1.0 - np.cos(np.pi * np.arange(fade_len) / fade_len))).astype(np.float32)
            samples[:fade_len] *= fade_in
            samples[-fade_len:] *= fade_in[::-1]

        # 2. Peak limiting to prevent digital speaker clipping distortion
        max_val = float(np.max(np.abs(samples)))
        if max_val > 0.95:
            samples = samples * (0.95 / max_val)

        # 3. Append natural acoustic breathing silence based on punctuation
        stripped = (chunk_text or "").strip()
        if re.search(r'[.!?\n]$', stripped):
            pause_duration = 0.24  # 240ms inter-sentence breath pause
        elif re.search(r'[,;:—–]$', stripped):
            pause_duration = 0.13  # 130ms inter-clause comma pause
        else:
            pause_duration = 0.04  # 40ms micro-pause for seamless syllable transition

        pause_samples = int(sr * pause_duration)
        if pause_samples > 0:
            silence = np.zeros(pause_samples, dtype=np.float32)
            return np.concatenate([samples, silence])
        return samples

    def _split_into_audio_chunks(self, text: str) -> list[str]:
        """Splits text into natural clause phrases and sentences
        to enable ultra-low Time-to-First-Audio (<50ms) via progressive chunked synthesis.
        Guards colons inside timestamps (e.g. 12:15), decimals (3.14), and titles (Dr., Mr.)."""
        if not text:
            return []

        raw_parts = re.split(r'((?<!\d)[.!?]+(?!\d)|[,;\n]+|(?<!\d):|:(?!\d)|[—–]+)', text)
        abbr_set = {"dr.", "mr.", "mrs.", "ms.", "prof.", "sr.", "jr.", "vs.", "etc.", "e.g.", "i.e."}
        chunks = []
        current = ""

        for part in raw_parts:
            if not part:
                continue
            current += part
            words = current.strip().split()
            last_word = words[-1].lower() if words else ""
            if last_word in abbr_set:
                continue

            # Terminal punctuation (. ! ?) emits complete sentences at >= 3 words, or merges short tails
            if re.search(r'[.!?]$', current.strip()):
                if len(words) >= 3 or not chunks:
                    chunks.append(current.strip())
                    current = ""
                else:
                    chunks[-1] = chunks[-1] + " " + current.strip()
                    current = ""
            # Clause punctuation (, ; : —) emits only when clause has substantial breath weight (>= 5 words)
            elif re.search(r'[,;:—–\n]$', current.strip()) and len(words) >= 5 or len(words) >= 12:
                chunks.append(current.strip())
                current = ""

        if current.strip():
            if chunks and len(current.strip().split()) < 3:
                chunks[-1] = chunks[-1] + " " + current.strip()
            else:
                chunks.append(current.strip())

        return [c for c in chunks if c]

    def _resolve_output_stream_params(self) -> tuple[int | None, int]:
        """Resolves target audio output device index and native hardware sample rate.
        Prefers Windows WASAPI endpoints at native sample rate (e.g. 48000 Hz) to eliminate
        legacy MME software buffer underruns, aliasing, and PaErrorCode -9997 invalid sample rate crashes.
        """
        try:
            hostapis = sd.query_hostapis()
            wasapi_api_idx = None
            for i, h in enumerate(hostapis):
                if "WASAPI" in h.get("name", ""):
                    wasapi_api_idx = i
                    break

            if wasapi_api_idx is not None:
                devices = sd.query_devices()
                _, def_out_idx = sd.default.device
                def_name = ""
                if def_out_idx is not None and 0 <= def_out_idx < len(devices):
                    def_name = devices[def_out_idx].get("name", "").lower()

                wasapi_devs = [
                    d for d in devices
                    if d.get("hostapi") == wasapi_api_idx and d.get("max_output_channels", 0) > 0
                ]

                # Match by default device name
                if def_name:
                    for d in wasapi_devs:
                        d_name = d.get("name", "").lower()
                        if d_name in def_name or def_name in d_name:
                            target_sr = int(d.get("default_samplerate", 48000))
                            return d["index"], target_sr

                # Prioritize headphone / speaker
                for d in wasapi_devs:
                    d_name = d.get("name", "").lower()
                    if "headphone" in d_name or "speaker" in d_name:
                        target_sr = int(d.get("default_samplerate", 48000))
                        return d["index"], target_sr

                if wasapi_devs:
                    return wasapi_devs[0]["index"], int(wasapi_devs[0].get("default_samplerate", 48000))
        except Exception as e:
            logger.warning("Could not auto-resolve WASAPI output endpoint: %s", e)

        return None, 24000

    def _stream_audio_callback(self, outdata, frames, time_info, status):
        """Hardware WASAPI / PortAudio streaming callback reading from the lock-free audio ring buffer.

        Real-Time Invariant: Zero logging, zero CRT file I/O locks, and zero unhandled exceptions
        inside the native PortAudio WASAPI callback thread. Must remain a stable instance method
        to prevent ephemeral closure deallocation during CFFI lifecycle teardown.
        """
        try:
            if status and getattr(status, "output_underflow", False):
                self._underrun_count += 1

            has_pending_ramp = bool(
                hasattr(self, "_ring_buffer")
                and self._ring_buffer._consumer_epoch != self._ring_buffer._barge_in_epoch
            )

            if (self._stop_event.is_set() or not self._stream_active) and not has_pending_ramp:
                outdata.fill(0.0)
                return

            synth_active = bool(getattr(self, "_synth_thread", None) and self._synth_thread.is_alive())
            q_has_work = bool(getattr(self, "_sentence_queue", None) and not self._sentence_queue.empty())
            synth_done = getattr(self, "_synth_has_started", False) and (not synth_active) and (not q_has_work)

            # Pre-buffer cushion (~2 PortAudio blocks: 2048 @ 48kHz / 1024 @ 24kHz = ~42.7ms)
            # Prevents rapid buffer-starvation underruns without adding artificial 100ms latency.
            if not getattr(self, "_prebuffer_filled", True) and not has_pending_ramp:
                cushion = max(1024, frames * 2)
                if self._ring_buffer.available_samples >= cushion or (synth_done and self._ring_buffer.available_samples > 0):
                    self._prebuffer_filled = True
                else:
                    outdata.fill(0.0)
                    return

            # Direct zero-allocation read into PortAudio outdata buffer
            n = self._ring_buffer.read_into(outdata, frames=frames)

            if n < frames and (synth_active or q_has_work):
                self._underrun_count += 1
                if n == 0:
                    self._prebuffer_filled = False
        except Exception:
            # Guarantee zero-fill in all failure paths; never let exceptions escape across PortAudio CFFI boundary
            outdata.fill(0.0)

    def start_streaming_session(self):
        """Initializes a double-buffered streaming session with concurrent pre-synthesis."""
        # Fail closed if a prior stream shutdown is still stalled in the native driver
        if getattr(self, "_shutdown_worker", None):
            if self._shutdown_worker.is_alive():
                logger.error(
                    "Cannot start streaming session: prior PortAudio stream shutdown is still stalled "
                    "in native driver. Refusing to create new stream until ownership is released."
                )
                self._stream_active = False
                self._is_speaking = False
                self._stream_shutdown_timed_out = True
                self._stream_cleanup_uncertain = True
                return False
            prior_outcome = getattr(self._shutdown_worker, "outcome", {}).get("status", self.SHUTDOWN_UNCLEAN_FAILED)
            self._last_shutdown_outcome = prior_outcome
            if prior_outcome in (self.SHUTDOWN_CLEAN_STOP, self.SHUTDOWN_RECOVERED_ABORT):
                self._shutdown_worker = None
                self._shutdown_stream = None
                self._stream_shutdown_timed_out = False
                self._stream_cleanup_uncertain = False
            else:
                self._shutdown_worker = None
                self._stream_shutdown_timed_out = False
                self._stream_cleanup_uncertain = True
                logger.error(
                    "Cannot start streaming session: prior stream shutdown completed with unclean outcome (%s).",
                    prior_outcome,
                )
                self._stream_active = False
                self._is_speaking = False
                return False

        if getattr(self, "_stream_cleanup_uncertain", False):
            logger.error(
                "Cannot start streaming session: prior stream cleanup failed or remains uncertain. "
                "Refusing to create new hardware stream to prevent concurrent driver corruption."
            )
            self._stream_active = False
            self._is_speaking = False
            return False

        self._stop_event.clear()
        self._is_speaking = True
        self._stream_active = True
        self._prebuffer_filled = False
        self._synth_has_started = False
        self._session_word_count = 0
        self._spoken_clauses: list[str] = []
        self._enqueued_clauses: list[str] = []

        if hasattr(self, "_chunker"):
            self._chunker.reset()
        if hasattr(self, "_resampler"):
            self._resampler.reset()
        if hasattr(self, "_ring_buffer"):
            self._ring_buffer.clear()

        # Resolve output device and sample rate (native WASAPI 48kHz preferred)
        self._stream_device, self._stream_sample_rate = self._resolve_output_stream_params()

        if self.engine == "kokoro" and self._kokoro:
            while not self._sentence_queue.empty():
                try:
                    self._sentence_queue.get_nowait()
                except Exception:
                    break

            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except Exception:
                    break

            # Stage 1: Dedicated Synthesis Producer Worker with Real-Time Priority
            def _synth_worker():
                if not pin_current_thread_to_mask(MASK_PCORE_COMPUTE, THREAD_PRIORITY_ABOVE_NORMAL):
                    _set_thread_realtime_priority("KokoroSynthWorker")
                while self._stream_active and not self._stop_event.is_set():
                    try:
                        sentence = self._sentence_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue

                    if sentence is None or self._stop_event.is_set():
                        self._audio_queue.put(None)
                        break

                    clean_text = self._clean_text_for_speech(sentence)
                    if not clean_text:
                        continue

                    sub_chunks = self._split_into_audio_chunks(clean_text)
                    for chunk in sub_chunks:
                        if self._stop_event.is_set() or not self._stream_active:
                            break
                        try:
                            producer_epoch = self._ring_buffer.barge_in_epoch
                            samples, sr = self._kokoro.create(
                                chunk,
                                voice=self._resolve_voice(),
                                speed=self.kokoro_speed,
                                lang=self.kokoro_lang
                            )
                            if self._ring_buffer.barge_in_epoch != producer_epoch or self._stop_event.is_set():
                                if hasattr(self, "_resampler"):
                                    self._resampler.reset()
                                break

                            padded_samples = self._smooth_and_pad_audio(
                                samples, sr, chunk, apply_dsp=self.dsp_filter
                            )

                            # Seamless polyphase resample to 48kHz for native WASAPI hardware stream
                            if getattr(self, "_stream_sample_rate", 24000) == 48000:
                                try:
                                    padded_samples = self._resampler.process(padded_samples)
                                except Exception:
                                    pass

                            if self._ring_buffer.barge_in_epoch != producer_epoch or self._stop_event.is_set():
                                if hasattr(self, "_resampler"):
                                    self._resampler.reset()
                                break

                            if not self._stop_event.is_set():
                                # Write directly to the audio ring buffer for gapless WASAPI playback
                                written = self._ring_buffer.write(padded_samples, producer_epoch=producer_epoch)
                                if written == 0 and self._ring_buffer.barge_in_epoch != producer_epoch:
                                    if hasattr(self, "_resampler"):
                                        self._resampler.reset()
                                    break
                                if getattr(self, "_shm_transport", None):
                                    try:
                                        self._shm_transport.write_pcm(padded_samples)
                                    except Exception:
                                        pass
                                # Keep legacy audio queue populated for backwards compatibility
                                self._audio_queue.put((padded_samples, sr))
                                try:
                                    from jarvis.audio.dsp_tap import AudioDspTap
                                    AudioDspTap.get_instance().process_and_broadcast(padded_samples[:512])
                                except Exception:
                                    pass
                        except Exception as e:
                            logger.error(f"Kokoro synth error on '{chunk}': {e}")
                    if not self._stop_event.is_set() and self._stream_active:
                        self._spoken_clauses.append(sentence)

            def _playback_worker():
                if not elevate_audio_thread_mmcss("Pro Audio"):
                    _set_thread_realtime_priority("WASAPIPlaybackWorker")
                while self._stream_active and not self._stop_event.is_set():
                    try:
                        item = self._audio_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue

                    if item is None or self._stop_event.is_set():
                        break

                    if not use_stream_callback:
                        samples, sr = item
                        try:
                            sd.play(samples, sr)
                            sd.wait()
                        except Exception as e:
                            logger.error(f"Playback error: {e}")

                if not use_stream_callback:
                    self._is_speaking = False

            # Launch synthesis producer thread first
            self._synth_thread = threading.Thread(target=_synth_worker, daemon=True, name="Jarvis-TTS-Synth")
            self._playback_thread = threading.Thread(target=_playback_worker, daemon=True, name="Jarvis-TTS-Play")
            self._synth_has_started = True
            self._synth_thread.start()
            self._playback_thread.start()

            # Stage 2: Hardware WASAPI Output Stream
            use_stream_callback = True
            try:
                self._output_stream = sd.OutputStream(
                    device=self._stream_device,
                    samplerate=self._stream_sample_rate,
                    channels=2,
                    dtype='float32',
                    blocksize=1024,
                    latency='low',
                    callback=self._stream_audio_callback
                )
                self._output_stream.start()
            except Exception as e:
                logger.warning(f"Could not open hardware OutputStream callback on device {self._stream_device} at {self._stream_sample_rate}Hz: {e}. Falling back to standard playback.")
                use_stream_callback = False
                self._output_stream = None

        else:
            # SAPI5 streaming path
            import pythoncom
            try:
                pythoncom.CoInitialize()
                self._sapi = win32com.client.Dispatch("SAPI.SpVoice")
                self._sapi.Rate = self.sapi_rate
                if self.sapi_voice:
                    for i in range(self._sapi.GetVoices().Count):
                        v = self._sapi.GetVoices().Item(i)
                        if self.sapi_voice.lower() in v.GetDescription().lower():
                            self._sapi.Voice = v
                            break
            except Exception as e:
                logger.error(f"Failed to start streaming SAPI session: {e}")

    def feed_token(self, token: str) -> list[str]:
        """Streams an incremental LLM token into the speculative chunker.
        Immediately dispatches newly-delimited phoneme clauses to the real-time synthesis queue."""
        if not self._stream_active or self._stop_event.is_set():
            return []

        emitted_chunks = self._chunker.feed(token)
        for chunk in emitted_chunks:
            self.queue_sentence(chunk)
        return emitted_chunks

    def flush_tokens(self) -> list[str]:
        """Flushes any remaining text in the speculative chunker into the synthesis queue."""
        if not self._stream_active or self._stop_event.is_set():
            return []

        remaining_chunks = self._chunker.flush()
        for chunk in remaining_chunks:
            self.queue_sentence(chunk)
        return remaining_chunks

    def queue_sentence(self, sentence: str):
        """Enqueues and immediately begins pre-synthesizing a sentence in real time."""
        text = sentence.strip()
        if not text or self._stop_event.is_set():
            return

        self._session_word_count = getattr(self, "_session_word_count", 0) + len(text.split())
        if not hasattr(self, "_enqueued_clauses"):
            self._enqueued_clauses = []
        self._enqueued_clauses.append(text)

        if self.engine == "kokoro" and self._kokoro:
            self._sentence_queue.put(text)
        elif self._sapi:
            clean_text = self._clean_text_for_speech(text)
            if clean_text:
                try:
                    self._sapi.Speak(clean_text, 1)  # 1 = Async
                except Exception as e:
                    logger.error(f"SAPI queue error: {e}")

    def finish_streaming_session(self) -> bool:
        """Waits for all queued speech to finish; returns False only on a true hardware stall timeout."""
        if self.engine == "kokoro" and self._kokoro:
            # Flush any un-emitted text in the speculative chunker
            self.flush_tokens()

            self._sentence_queue.put(None)  # Signal synth worker

            # Dynamic timeout scaling based on total queued word count:
            # Bettany cadence (~130 wpm / 2.2 words/sec) + breath pauses.
            # Scale deadline dynamically to prevent premature truncation of longer answers.
            word_count = getattr(self, "_session_word_count", 0)
            est_speech_seconds = (word_count / 1.5) + 20.0
            max_session_ceiling = max(self.stream_timeout_seconds, est_speech_seconds, 60.0)
            deadline = time.monotonic() + max_session_ceiling
            workers = [self._synth_thread, self._playback_thread]
            timed_out = False
            stall_timeout = 15.0

            # Wait for synthesis thread to finish with forward progress tracking
            last_synth_progress = time.monotonic()
            if self._synth_thread:
                last_q_size = self._sentence_queue.qsize()
                while self._synth_thread.is_alive():
                    now = time.monotonic()
                    if now >= deadline:
                        timed_out = True
                        break

                    current_q_size = self._sentence_queue.qsize()
                    if current_q_size != last_q_size or not self._ring_buffer.is_empty:
                        last_synth_progress = now
                        last_q_size = current_q_size
                    elif now - last_synth_progress > stall_timeout:
                        logger.warning("TTS synthesis stalled for %.1fs without forward progress.", stall_timeout)
                        timed_out = True
                        break

                    self._synth_thread.join(timeout=0.05)

            # Wait for audio ring buffer / hardware output stream to drain with forward progress tracking
            last_playback_progress = time.monotonic()
            last_available_samples = self._ring_buffer.available_samples

            while (not self._ring_buffer.is_empty) and not self._stop_event.is_set() and not timed_out:
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    break

                current_samples = self._ring_buffer.available_samples
                if current_samples != last_available_samples:
                    # Forward progress: samples are actively being read and played by WASAPI
                    last_playback_progress = now
                    last_available_samples = current_samples
                elif now - last_playback_progress > stall_timeout:
                    # Stalled: Ring buffer hasn't moved for 15 seconds
                    logger.warning("WASAPI audio output stalled for %.1fs without consuming samples.", stall_timeout)
                    timed_out = True
                    break

                time.sleep(0.01)

            # Brief cushion (~50ms empirical estimate of driver DMA buffer latency;
            # accounts for queued hardware samples after the ring buffer drains)
            if not self._stop_event.is_set() and not timed_out:
                time.sleep(0.05)

            # Stop and close the hardware output stream
            if self._output_stream:
                stream = self._output_stream
                self._output_stream = None
                self._close_stream_safely(stream, stop_timeout=0.100)

            if self._playback_thread and self._playback_thread.is_alive():
                self._playback_thread.join(timeout=0.2)

            if timed_out:
                logger.error(
                    "Speech stream did not drain (ceiling: %.1fs, words: %d); stopping playback.",
                    max_session_ceiling,
                    word_count,
                )
                self.stop()
                for worker in workers:
                    if worker and worker.is_alive():
                        worker.join(timeout=2.0)

            self._is_speaking = False
            self._stream_active = False
            return not timed_out

        else:
            import pythoncom
            if self._sapi:
                try:
                    while not self._stop_event.is_set():
                        if self._sapi.WaitUntilDone(100):
                            break
                    if self._stop_event.is_set():
                        self._sapi.Speak("", 2)
                except Exception:
                    pass
                finally:
                    self._is_speaking = False
                    self._sapi = None
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass
            return True

    def speak_sync(self, text: str):
        """Synchronously speaks text with Kokoro or SAPI."""
        clean_text = self._clean_text_for_speech(text)
        if not clean_text:
            return

        self._stop_event.clear()
        self._is_speaking = True

        if self.engine == "kokoro" and self._kokoro:
            try:
                sub_chunks = self._split_into_audio_chunks(clean_text)
                for chunk in sub_chunks:
                    if self._stop_event.is_set():
                        break
                    samples, sr = self._kokoro.create(
                        chunk,
                        voice=self._resolve_voice(),
                        speed=self.kokoro_speed,
                        lang=self.kokoro_lang
                    )
                    padded_samples = self._smooth_and_pad_audio(
                        samples, sr, chunk, apply_dsp=self.dsp_filter
                    )
                    if not self._stop_event.is_set():
                        try:
                            from jarvis.audio.dsp_tap import AudioDspTap
                            AudioDspTap.get_instance().process_and_broadcast(padded_samples[:512])
                        except Exception:
                            pass
                        sd.play(padded_samples, sr)
                        sd.wait()
            except Exception as e:
                logger.error(f"Kokoro speak_sync error: {e}. Falling back to SAPI5.")
                self._speak_local_sapi(clean_text)
            finally:
                self._is_speaking = False
        else:
            self._speak_local_sapi(clean_text)

    def _speak_local_sapi(self, text: str):
        """Speaks via local Windows SAPI5 engine."""
        import pythoncom
        pythoncom.CoInitialize()
        self._is_speaking = True
        try:
            sapi = win32com.client.Dispatch("SAPI.SpVoice")
            sapi.Rate = self.sapi_rate
            if self.sapi_voice:
                for i in range(sapi.GetVoices().Count):
                    v = sapi.GetVoices().Item(i)
                    if self.sapi_voice.lower() in v.GetDescription().lower():
                        sapi.Voice = v
                        break
            sapi.Speak(text, 1)
            while not self._stop_event.is_set():
                if sapi.WaitUntilDone(100):
                    break
            if self._stop_event.is_set():
                try:
                    sapi.Speak("", 2)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"SAPI5 speech error: {e}")
        finally:
            self._is_speaking = False
            pythoncom.CoUninitialize()

    async def speak(self, text: str):
        """Asynchronous speak helper."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.speak_sync, text)

    def get_underrun_count(self) -> int:
        """Returns the cumulative count of PortAudio / WASAPI buffer underruns detected."""
        return getattr(self, "_underrun_count", 0)

    def reset_underrun_count(self) -> None:
        """Resets the PortAudio buffer underrun counter to zero."""
        self._underrun_count = 0

