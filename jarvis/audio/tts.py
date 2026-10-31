"""Text-to-Speech synthesis using Kokoro-82M neural TTS, Windows SAPI5, and Edge-TTS."""
import asyncio
import itertools
import logging
import os
import queue
import re
import threading
import time
import weakref
from collections import deque
from typing import Any, ClassVar

import numpy as np
import sounddevice as sd

from jarvis.audio.affinity import pin_and_elevate_current_thread
from jarvis.audio.chunker import StreamingSentenceChunker
from jarvis.audio.ring_buffer import AudioRingBuffer, apply_cinematic_jarvis_dsp
from jarvis.core.cancellation import GLOBAL_EPOCH_STATE, is_in_cancellation_flush
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
    "_safe_standalone_stream_close",
    "apply_cinematic_jarvis_dsp",
    "create_bettanys_jarvis_voice_vector",
]


def _safe_standalone_stream_close(stream: Any, timeout: float = 0.2) -> bool:
    """Safely closes a standalone PortAudio stream executing stop -> abort -> close bounded by timeout."""
    if stream is None:
        return True

    outcome = {"stop_ok": False, "abort_ok": False, "close_ok": False}

    def _worker():
        try:
            stream.stop()
            outcome["stop_ok"] = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("stream.stop() failed in standalone close: %s", exc)
            try:
                stream.abort()
                outcome["abort_ok"] = True
            except Exception as abort_exc:  # noqa: BLE001
                logger.debug("stream.abort() failed in standalone close: %s", abort_exc)
        finally:
            try:
                stream.close()
                outcome["close_ok"] = True
            except Exception as close_exc:  # noqa: BLE001
                logger.debug("stream.close() failed in standalone close: %s", close_exc)

    t = threading.Thread(target=_worker, daemon=True, name="Jarvis-StandaloneStreamClose")
    t.start()
    t.join(timeout=max(0.0, float(timeout)))
    if t.is_alive():
        logger.warning("Standalone stream close timed out after %.3fs", timeout)
        return False
    return (outcome["stop_ok"] or outcome["abort_ok"]) and outcome["close_ok"]


class _ClosedStreamsTracker:
    """Tracks closed audio streams using weak references while supporting ID membership tests."""

    def __init__(self) -> None:
        self._set: weakref.WeakSet[Any] = weakref.WeakSet()

    def add(self, item: Any) -> None:
        if item is not None:
            try:
                self._set.add(item)
            except TypeError:
                pass

    def discard(self, item: Any) -> None:
        try:
            self._set.discard(item)
        except TypeError:
            pass

    def __contains__(self, item: Any) -> bool:
        if isinstance(item, int):
            return any(id(x) == item for x in self._set)
        return item in self._set

    def __len__(self) -> int:
        return len(self._set)


def _async_close_worker_loop(tts_ref: weakref.ReferenceType[Any], q: queue.Queue) -> None:
    """Background worker draining and closing native PortAudio hardware streams asynchronously."""
    while True:
        try:
            item = q.get(timeout=1.0)
        except queue.Empty:
            if tts_ref() is None:
                break
            continue
        except Exception:  # noqa: BLE001
            break

        if item is None:
            q.task_done()
            break

        tts = tts_ref()
        try:
            if item == "STOP_DEFAULT" or (isinstance(item, tuple) and item and item[0] == "STOP_DEFAULT"):
                ack_event = item[1] if isinstance(item, tuple) and len(item) > 1 else None
                outcome = item[2] if isinstance(item, tuple) and len(item) > 2 else None
                success = False
                stream_err = None
                try:
                    if tts is None:
                        sd.stop()
                        success = True
                    else:
                        acquired = tts._sd_playback_lock.acquire(timeout=0.040)
                        if not acquired:
                            tts._stream_cleanup_uncertain = True
                            if outcome is not None:
                                outcome["error"] = TimeoutError("Could not acquire _sd_playback_lock for default stop")
                        else:
                            try:
                                stream = None
                                acq_claim = tts._stream_claim_lock.acquire(timeout=0.020)
                                try:
                                    stream = getattr(tts, "_default_stream", None)
                                    tts._default_stream = None
                                finally:
                                    if acq_claim:
                                        tts._stream_claim_lock.release()

                                if stream is not None:
                                    try:
                                        stream.abort(ignore_errors=False)
                                    except Exception as ab_ex:  # noqa: BLE001
                                        stream_err = ab_ex
                                    try:
                                        stream.close(ignore_errors=False)
                                    except Exception as cl_ex:  # noqa: BLE001
                                        stream_err = stream_err or cl_ex
                                sd.stop()
                                if stream_err is not None:
                                    tts._stream_cleanup_uncertain = True
                                    if outcome is not None:
                                        outcome["error"] = stream_err
                                else:
                                    success = True
                            finally:
                                tts._sd_playback_lock.release()
                except Exception as e:  # noqa: BLE001
                    logger.warning("Default sounddevice stop failed: %s", e)
                    if outcome is not None:
                        outcome["error"] = e
                    if tts is not None:
                        tts._stream_cleanup_uncertain = True
                finally:
                    if outcome is not None:
                        outcome["ok"] = success
                    if tts is not None:
                        acq_c = tts._stream_claim_lock.acquire(timeout=0.050)
                        if acq_c:
                            try:
                                tts._default_stop_pending = False
                                if hasattr(tts, "_default_stop_cond"):
                                    tts._default_stop_cond.notify_all()
                            finally:
                                tts._stream_claim_lock.release()
                        else:
                            tts._stream_cleanup_uncertain = True

                            def _deferred_clear_site1(tts_obj=tts):
                                with tts_obj._stream_claim_lock:
                                    tts_obj._default_stop_pending = False
                                    if hasattr(tts_obj, "_default_stop_cond"):
                                        tts_obj._default_stop_cond.notify_all()

                            threading.Thread(
                                target=_deferred_clear_site1,
                                daemon=True,
                                name="Jarvis-TTS-StopCondClear1",
                            ).start()
                    if ack_event is not None:
                        ack_event.set()
                    q.task_done()
                continue

            stream = item[0] if isinstance(item, tuple) else item
            ack_event = item[1] if isinstance(item, tuple) and len(item) > 1 else None
            outcome = item[2] if isinstance(item, tuple) and len(item) > 2 else None
            ok = False
            try:
                if tts is not None:
                    ok = tts._close_stream_safely(stream, stop_timeout=0.100)
                else:
                    ok = _safe_standalone_stream_close(stream, timeout=0.100)
                if outcome is not None:
                    outcome["ok"] = ok
            except Exception as e:  # noqa: BLE001
                if outcome is not None:
                    outcome["error"] = e
            finally:
                if tts is not None:
                    with tts._stream_claim_lock:
                        tts._pending_close_streams.discard(id(stream))
                        tts._pending_close_streams.discard(stream)
                        if not ok and stream is not None:
                            tts._unresolved_streams.add(stream)
                            tts._unresolved_streams.add(id(stream))
                            tts._stream_cleanup_uncertain = True
                if ack_event is not None:
                    ack_event.set()
                q.task_done()
        finally:
            tts = None
            item = None


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
        if pcm.size == 0 or self._up == self._down:
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

    VOICE_PROFILES: ClassVar[dict[str, Any]] = {
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
        self.kokoro_model_path = os.path.join(
            base_dir, tts_cfg.get("kokoro_model", "data/models/kokoro/kokoro-v1.0.onnx")
        )
        self.kokoro_voices_path = os.path.join(
            base_dir, tts_cfg.get("kokoro_voices", "data/models/kokoro/voices-v1.0.bin")
        )

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

        # Concurrency & lifecycle locks
        self._lifecycle_lock = threading.RLock()
        self._stop_lock = threading.RLock()
        self._stream_claim_lock = threading.RLock()
        self._close_lock = threading.RLock()
        self._sd_playback_lock = threading.Lock()
        self._chunker_lock = threading.Lock()
        self._startup_cond = threading.Condition(self._lifecycle_lock)

        self._closed_streams = _ClosedStreamsTracker()
        self._pending_close_streams: set[Any] = set()
        self._unresolved_streams: set[Any] = set()
        self._default_stream: Any | None = None

        self._fallback_lock = threading.Lock()
        self._fallback_streams: deque[Any] = deque()
        self._fallback_default_stop: bool = False
        self._fallback_thread: threading.Thread | None = None

        self._deferred_thread_lock = threading.Lock()
        self._deferred_thread: threading.Thread | None = None

        self._fallback_sd_stop_lock = threading.Lock()
        self._fallback_sd_stop_thread: threading.Thread | None = None

        self._session_counter = itertools.count(1)
        self._session_generation: int = 0
        self._last_handled_barge_in_epoch: int = 0
        self._session_activation_epoch: int = 0

        self._sync_speech_active: int = 0
        self._stream_starting: bool = False
        self._startup_in_progress: bool = False
        self._startup_active: int = 0
        self._stop_in_progress: bool = False
        self._closing: bool = False
        self._closed: bool = False

        self._sapi_purge_event = threading.Event()
        self._sapi_purge_failed: bool = False
        self._sapi_worker_error: Exception | None = None
        self._sapi_thread: threading.Thread | None = None
        self._shm_flush_uncertain: bool = False
        self._barge_in_during_startup: bool = False
        self._default_stop_pending: bool = False
        self._default_stop_cond = threading.Condition(self._stream_claim_lock)

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
        self._shutdown_worker: threading.Thread | None = None
        self._startup_worker: threading.Thread | None = None
        self._shutdown_stream: Any = None
        self._in_stop: bool = False

        # Speculative token chunker & high-throughput lock-free audio ring buffer
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

        # Initialize local SAPI5 stub (streaming sessions run worker-local COM STA thread)
        self._sapi = None
        self._init_sapi()

        # Dedicated background async close worker
        self._async_close_queue: queue.Queue = queue.Queue(maxsize=32)
        self._async_close_worker = threading.Thread(
            target=_async_close_worker_loop,
            args=(weakref.ref(self), self._async_close_queue),
            daemon=True,
            name="Jarvis-TTS-AsyncCloseWorker",
        )
        self._async_close_worker.start()

        # Register audio flush callback for immediate barge-in interruption
        try:
            from jarvis.audio.cancellation import register_audio_flush_callback
            register_audio_flush_callback(self.on_barge_in_flush)
            self._session_activation_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)
        except Exception as e:
            try:
                self._async_close_queue.put_nowait(None)
                self._async_close_worker.join(timeout=0.1)
            except Exception:  # noqa: BLE001, S110
                pass
            raise RuntimeError(f"Barge-in callback registration failed: {e}") from e

        # Zero-copy shared memory transport
        self._shm_transport: Any | None = None
        if tts_cfg.get("use_shm_transport", False):
            try:
                from jarvis.audio.shm_transport import (
                    DEFAULT_RING_NAME,
                    ShmAudioRingTransport,
                )
                self._shm_transport = ShmAudioRingTransport.open_or_create(
                    name=tts_cfg.get("shm_name", DEFAULT_RING_NAME),
                    capacity_samples=24000 * 10,
                    sample_rate=24000,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("ShmAudioRingTransport unavailable, continuing without it: %s", e)

    def _init_kokoro(self):
        """Initializes Kokoro neural engine with constrained CPU threading to prevent CPU spikes."""
        try:
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
                        except Exception:  # noqa: BLE001, S110
                            pass
                    EspeakAPI._delete_win32 = _safe_del_win32
            except Exception:  # noqa: BLE001, S110
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
                opts.add_session_config_entry("session.intra_op.allow_spinning", "0")

                available = rt.get_available_providers()
                providers = []
                preferred_device = getattr(self, "device", "cpu")
                use_cuda = (preferred_device == "cuda") and ("CUDAExecutionProvider" in available)

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
                                    "RTX 3050 VRAM heavily pressured (%.1f MB free < 600MB). Falling back to CPU.",
                                    free_mb,
                                )
                                use_cuda = False
                    except Exception as arb_err:  # noqa: BLE001
                        logger.debug("VRAM arbiter check note in Kokoro init: %s", arb_err)

                if use_cuda:
                    cuda_opts = {
                        "device_id": 0,
                        "arena_extend_strategy": "kNextPowerOfTwo",
                        "gpu_mem_limit": 2 * 1024 * 1024 * 1024,
                        "cudnn_conv_algo_search": "HEURISTIC",
                        "do_copy_in_default_stream": "1",
                    }
                    providers.append(("CUDAExecutionProvider", cuda_opts))
                elif "DmlExecutionProvider" in available:
                    providers.append(("DmlExecutionProvider", {"device_id": 0}))
                providers.append("CPUExecutionProvider")

                try:
                    sess = rt.InferenceSession(self.kokoro_model_path, sess_options=opts, providers=providers)
                except Exception as prov_err:  # noqa: BLE001
                    logger.warning("GPU provider init failed (%s); falling back to CPUExecutionProvider.", prov_err)
                    sess = rt.InferenceSession(
                        self.kokoro_model_path, sess_options=opts, providers=["CPUExecutionProvider"]
                    )

                self._kokoro = Kokoro.from_session(sess, self.kokoro_voices_path)

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
                    elif isinstance(voice, str) and (
                        voice.lower().strip() in self.VOICE_PROFILES
                        or voice.lower().strip() in self._voice_cache
                    ):
                        voice = self._resolve_voice(voice)
                    return orig_create(text, *args, voice=voice, **kwargs)

                self._kokoro.get_voice_style = _wrapped_get_voice_style
                self._kokoro.get_voices = _wrapped_get_voices
                self._kokoro.create = _wrapped_create

                try:
                    warmup_voice = self._resolve_voice()
                    self._kokoro.create(".", voice=warmup_voice, speed=1.0, lang=self.kokoro_lang)
                except Exception as w_err:  # noqa: BLE001
                    logger.debug("Kokoro startup warmup note: %s", w_err)

                with _CACHED_KOKORO_LOCK:
                    _CACHED_KOKORO_SESSION[cache_key] = self._kokoro
            else:
                logger.warning("Kokoro model files not found. Defaulting to SAPI5.")
                self.engine = "sapi5"
        except Exception as e:  # noqa: BLE001
            logger.warning("Kokoro initialization failed: %s. Defaulting to SAPI5.", e)
            self.engine = "sapi5"

    def _init_sapi(self):
        """No-op stub for backward compatibility; SAPI streams initialize per COM STA worker."""

    def _safe_purge_sapi(self, sapi: Any = None) -> bool:
        """Purges queued SAPI speech safely in its calling apartment."""
        target_sapi = sapi or getattr(self, "_sapi", None)
        if target_sapi is None:
            return True
        try:
            target_sapi.Speak("", 2)
            self._sapi_purge_failed = False
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("SAPI purge error: %s", e)
            self._sapi_purge_failed = True
            return False

    def _safe_sd_stop(self, timeout: float = 0.05) -> bool:
        """Dispatches default sounddevice stop safely bounded by timeout."""
        ack_event = threading.Event()
        outcome: dict[str, Any] = {"ok": False, "error": None}
        item = ("STOP_DEFAULT", ack_event, outcome)

        acquired_claim = self._stream_claim_lock.acquire(timeout=min(0.015, max(0.0, float(timeout))))
        if acquired_claim:
            try:
                self._default_stop_pending = True
            finally:
                self._stream_claim_lock.release()
        else:
            self._default_stop_pending = True

        worker = getattr(self, "_async_close_worker", None)
        if worker and worker.is_alive():
            try:
                self._async_close_queue.put(item, timeout=0.01)
            except (queue.Full, Exception):  # noqa: BLE001, S110
                pass
            else:
                if timeout <= 0:
                    return outcome.get("ok", False) and ack_event.is_set()
                ack_event.wait(timeout=max(0.0, float(timeout)))
                return outcome.get("ok", False) and ack_event.is_set()

        def _do():
            success = False
            stream_err = None
            try:
                acquired = self._sd_playback_lock.acquire(timeout=0.040)
                if not acquired:
                    self._stream_cleanup_uncertain = True
                    outcome["error"] = TimeoutError("Could not acquire _sd_playback_lock for safe sd.stop")
                else:
                    try:
                        stream = None
                        acq_claim = self._stream_claim_lock.acquire(timeout=0.020)
                        try:
                            stream = getattr(self, "_default_stream", None)
                            self._default_stream = None
                        finally:
                            if acq_claim:
                                self._stream_claim_lock.release()

                        if stream is not None:
                            try:
                                stream.abort(ignore_errors=False)
                            except Exception as ab_ex:  # noqa: BLE001
                                stream_err = ab_ex
                            try:
                                stream.close(ignore_errors=False)
                            except Exception as cl_ex:  # noqa: BLE001
                                stream_err = stream_err or cl_ex
                        sd.stop()
                        if stream_err is not None:
                            self._stream_cleanup_uncertain = True
                            outcome["error"] = stream_err
                        else:
                            success = True
                    finally:
                        self._sd_playback_lock.release()
            except Exception as e:  # noqa: BLE001
                outcome["error"] = e
                self._stream_cleanup_uncertain = True
            finally:
                outcome["ok"] = success
                acq_fin = self._stream_claim_lock.acquire(timeout=0.050)
                if acq_fin:
                    try:
                        self._default_stop_pending = False
                        if hasattr(self, "_default_stop_cond"):
                            self._default_stop_cond.notify_all()
                    finally:
                        self._stream_claim_lock.release()
                else:
                    self._stream_cleanup_uncertain = True

                    def _deferred_clear_site2():
                        with self._stream_claim_lock:
                            self._default_stop_pending = False
                            if hasattr(self, "_default_stop_cond"):
                                self._default_stop_cond.notify_all()

                    threading.Thread(
                        target=_deferred_clear_site2,
                        daemon=True,
                        name="Jarvis-TTS-StopCondClear2",
                    ).start()
                ack_event.set()

        with getattr(self, "_fallback_sd_stop_lock", threading.Lock()):
            prior_t = getattr(self, "_fallback_sd_stop_thread", None)
            if prior_t is not None and prior_t.is_alive():
                if timeout <= 0:
                    return False
                prior_t.join(timeout=max(0.0, float(timeout)))
                if prior_t.is_alive():
                    self._stream_cleanup_uncertain = True
                    return False
                return True

            t = threading.Thread(target=_do, daemon=True, name="Jarvis-SafeSdStop")
            self._fallback_sd_stop_thread = t
            t.start()

        if timeout <= 0:
            return outcome.get("ok", False) and ack_event.is_set()
        t.join(timeout=max(0.0, float(timeout)))
        return outcome.get("ok", False) and ack_event.is_set()

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
                except Exception as e:  # noqa: BLE001
                    logger.warning("Voice blend calculation error for '%s': %s", clean, e)
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
        except Exception as e:  # noqa: BLE001
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
        clauses = [c.strip() for c in re.split(r"(?<!\d)(?<=[.!?])(?!\d)\s+", text) if c.strip()]
        for c in clauses:
            self.queue_sentence(c)

    def _close_stream_safely(self, stream: Any, stop_timeout: float = 0.100) -> bool:
        """Safely stops and closes a PortAudio hardware stream on a dedicated control thread."""
        if stream is None:
            return True

        if hasattr(self, "_retained_streams"):
            self._retained_streams.append(stream)

        worker_to_join = None
        prior_worker_to_join = None

        with self._stream_claim_lock:
            if stream in self._closed_streams or id(stream) in self._closed_streams:
                return True

            if getattr(self, "_shutdown_worker", None) and not self._shutdown_worker.is_alive():
                prior_status = getattr(self._shutdown_worker, "outcome", {}).get(
                    "status", self.SHUTDOWN_UNCLEAN_FAILED
                )
                self._last_shutdown_outcome = prior_status
                prior_stream = self._shutdown_stream
                if prior_status in (self.SHUTDOWN_CLEAN_STOP, self.SHUTDOWN_RECOVERED_ABORT):
                    if prior_stream is not None:
                        self._closed_streams.add(prior_stream)
                        self._closed_streams.add(id(prior_stream))
                        self._unresolved_streams.discard(prior_stream)
                        self._unresolved_streams.discard(id(prior_stream))
                    self._shutdown_worker = None
                    self._shutdown_stream = None
                    self._stream_shutdown_timed_out = False
                    if not self._unresolved_streams:
                        self._stream_cleanup_uncertain = False
                    if prior_stream is stream:
                        return True
                else:
                    if prior_stream is not None:
                        self._unresolved_streams.add(prior_stream)
                        self._unresolved_streams.add(id(prior_stream))
                    self._shutdown_worker = None
                    self._shutdown_stream = None
                    self._stream_shutdown_timed_out = False
                    self._stream_cleanup_uncertain = True
                    return False

            if getattr(self, "_shutdown_worker", None) and self._shutdown_worker.is_alive():
                if self._shutdown_stream is stream:
                    worker_to_join = self._shutdown_worker
                else:
                    prior_worker_to_join = self._shutdown_worker
            else:
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
                        try:
                            stream.abort(ignore_errors=False)
                            abort_ok = True
                        except Exception as abort_exc:  # noqa: BLE001
                            worker_outcome["abort_error"] = abort_exc
                    except Exception as exc:  # noqa: BLE001
                        worker_outcome["stop_error"] = exc
                        try:
                            stream.abort(ignore_errors=False)
                            abort_ok = True
                        except Exception as abort_exc:  # noqa: BLE001
                            worker_outcome["abort_error"] = abort_exc
                    finally:
                        try:
                            stream.close(ignore_errors=False)
                            close_ok = True
                        except Exception as close_exc:  # noqa: BLE001
                            worker_outcome["close_error"] = close_exc

                    was_active = getattr(stream, "active", False)
                    if close_ok and (stop_ok or not was_active):
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
                worker_to_join = shutdown_thread

        if prior_worker_to_join is not None:
            t0 = time.monotonic()
            prior_worker_to_join.join(timeout=stop_timeout)
            elapsed = time.monotonic() - t0
            rem_timeout = max(0.001, stop_timeout - elapsed)
            with self._stream_claim_lock:
                if prior_worker_to_join.is_alive():
                    self._stream_shutdown_timed_out = True
                    self._stream_cleanup_uncertain = True
                    self._last_shutdown_outcome = self.SHUTDOWN_TIMEOUT_BLOCKED
                    if stream is not None:
                        self._unresolved_streams.add(stream)
                        self._unresolved_streams.add(id(stream))
                    return False

                prior_status = getattr(prior_worker_to_join, "outcome", {}).get(
                    "status", self.SHUTDOWN_UNCLEAN_FAILED
                )
                self._last_shutdown_outcome = prior_status
                prior_stream = self._shutdown_stream
                if prior_status in (self.SHUTDOWN_CLEAN_STOP, self.SHUTDOWN_RECOVERED_ABORT):
                    if prior_stream is not None:
                        self._closed_streams.add(prior_stream)
                        self._closed_streams.add(id(prior_stream))
                        self._unresolved_streams.discard(prior_stream)
                        self._unresolved_streams.discard(id(prior_stream))
                    if not self._unresolved_streams:
                        self._stream_cleanup_uncertain = False
                else:
                    if prior_stream is not None:
                        self._unresolved_streams.add(prior_stream)
                        self._unresolved_streams.add(id(prior_stream))
                    self._stream_cleanup_uncertain = True
                self._shutdown_worker = None
                self._shutdown_stream = None
                self._stream_shutdown_timed_out = False

            return self._close_stream_safely(stream, stop_timeout=rem_timeout)

        if worker_to_join is not None:
            worker_to_join.join(timeout=stop_timeout)

        with self._stream_claim_lock:
            if worker_to_join is not None and worker_to_join.is_alive():
                self._stream_shutdown_timed_out = True
                self._stream_cleanup_uncertain = True
                self._last_shutdown_outcome = self.SHUTDOWN_TIMEOUT_BLOCKED
                if stream is not None:
                    self._unresolved_streams.add(stream)
                    self._unresolved_streams.add(id(stream))
                return False

            status = getattr(worker_to_join, "outcome", {}).get("status", self.SHUTDOWN_UNCLEAN_FAILED)
            self._last_shutdown_outcome = status
            close_succeeded = (
                getattr(worker_to_join, "outcome", {}).get("close_error") is None
                and status != self.SHUTDOWN_TIMEOUT_BLOCKED
            )
            if close_succeeded and stream is not None:
                self._closed_streams.add(stream)
                self._closed_streams.add(id(stream))

            if status in (self.SHUTDOWN_CLEAN_STOP, self.SHUTDOWN_RECOVERED_ABORT):
                self._unresolved_streams.discard(stream)
                self._unresolved_streams.discard(id(stream))
                if self._shutdown_stream is stream:
                    self._shutdown_worker = None
                    self._shutdown_stream = None
                self._stream_shutdown_timed_out = False
                if not self._unresolved_streams:
                    self._stream_cleanup_uncertain = False
                return True

            self._unresolved_streams.add(stream)
            self._unresolved_streams.add(id(stream))
            if self._shutdown_stream is stream:
                pass
            self._stream_shutdown_timed_out = False
            self._stream_cleanup_uncertain = True
            return False

    def reconcile_stream_lifecycle(self, force: bool = False, device_reset: bool = False) -> bool:
        """Reconciles uncertain or stalled stream lifecycle states."""
        if getattr(self, "_startup_worker", None):
            if self._startup_worker.is_alive():
                return False
            self._startup_worker = None

        with self._stream_claim_lock:
            if getattr(self, "_shutdown_worker", None):
                if self._shutdown_worker.is_alive():
                    return False
                prior_outcome = getattr(self._shutdown_worker, "outcome", {}).get(
                    "status", self.SHUTDOWN_UNCLEAN_FAILED
                )
                self._last_shutdown_outcome = prior_outcome
                if prior_outcome in (self.SHUTDOWN_CLEAN_STOP, self.SHUTDOWN_RECOVERED_ABORT):
                    if self._shutdown_stream is not None:
                        self._closed_streams.add(self._shutdown_stream)
                        self._unresolved_streams.discard(self._shutdown_stream)
                        self._unresolved_streams.discard(id(self._shutdown_stream))
                    self._shutdown_worker = None
                    self._shutdown_stream = None
                    self._stream_shutdown_timed_out = False
                    if not self._unresolved_streams:
                        self._stream_cleanup_uncertain = False
                else:
                    if self._shutdown_stream is not None:
                        self._unresolved_streams.add(self._shutdown_stream)
                        self._unresolved_streams.add(id(self._shutdown_stream))
                    self._shutdown_worker = None
                    self._stream_shutdown_timed_out = False
                    self._stream_cleanup_uncertain = True
                    if not device_reset:
                        return False

            # Attempt safe verification/close of remaining unresolved streams
            if getattr(self, "_unresolved_streams", None):
                unresolved_copy = list(self._unresolved_streams)
                for s in unresolved_copy:
                    if isinstance(s, int):
                        continue
                    if (
                        s in self._closed_streams
                        or id(s) in self._closed_streams
                        or getattr(s, "closed", None) is True
                    ):
                        self._unresolved_streams.discard(s)
                        self._unresolved_streams.discard(id(s))

                # Discard orphaned int IDs if corresponding stream object is gone
                obj_streams = {s for s in self._unresolved_streams if not isinstance(s, int)}
                if not obj_streams:
                    self._unresolved_streams.clear()

            has_unresolved = bool(self._unresolved_streams)
            is_uncertain = bool(getattr(self, "_stream_cleanup_uncertain", False))

        # If unresolved streams remain and no hardware reset was performed, fail closed
        if has_unresolved and not device_reset:
            return False

        # If native stream cleanup is uncertain and not reset, require successful stop or fail closed
        if is_uncertain and not device_reset:
            if not force:
                return False
            # When force=True, require a successful stop before clearing uncertainty
            stop_ok = self._safe_sd_stop(timeout=0.05)
            if not stop_ok:
                return False
            with self._stream_claim_lock:
                self._stream_cleanup_uncertain = False

        if getattr(self, "_shm_flush_uncertain", False):
            if hasattr(self, "_shm_transport") and self._shm_transport:
                try:
                    ok = self._shm_transport.trigger_barge_in_clear()
                    if not ok:
                        return False
                except Exception:  # noqa: BLE001
                    return False
            self._shm_flush_uncertain = False

        if getattr(self, "_sapi_purge_failed", False):
            worker = getattr(self, "_sapi_thread", None)
            if worker and worker.is_alive():
                self._sapi_purge_event.set()
                worker.join(timeout=0.100)
                if worker.is_alive():
                    return False
            if (worker is None or not worker.is_alive()) and (force or device_reset):
                self._sapi_purge_failed = False
                self._sapi_thread = None
            else:
                return False

        with self._stream_claim_lock:
            self._unresolved_streams.clear()
            self._shutdown_worker = None
            self._shutdown_stream = None
            self._stream_shutdown_timed_out = False
            self._stream_cleanup_uncertain = False
        return True

    def _dispatch_fallback_close(self, stream: Any = None, default_stop: bool = False) -> None:
        """Coalescing, single-worker fallback dispatcher for queue-full barge-in closures."""
        with self._fallback_lock:
            if stream is not None:
                self._fallback_streams.append(stream)
            if default_stop:
                self._fallback_default_stop = True

            if self._fallback_thread is None or not self._fallback_thread.is_alive():
                def _fallback_worker():
                    while True:
                        with self._fallback_lock:
                            if not self._fallback_streams and not self._fallback_default_stop:
                                self._fallback_thread = None
                                break
                            st = self._fallback_streams.popleft() if self._fallback_streams else None
                            do_def = self._fallback_default_stop if st is None else False
                            if do_def:
                                self._fallback_default_stop = False

                        if st is not None:
                            ok = False
                            try:
                                ok = self._close_stream_safely(st, stop_timeout=0.100)
                            finally:
                                with self._stream_claim_lock:
                                    self._pending_close_streams.discard(st)
                                    self._pending_close_streams.discard(id(st))
                                    if not ok:
                                        self._unresolved_streams.add(st)
                                        self._unresolved_streams.add(id(st))
                                        self._stream_cleanup_uncertain = True
                        elif do_def:
                            stream_err = None
                            try:
                                acquired = self._sd_playback_lock.acquire(timeout=0.040)
                                if not acquired:
                                    self._stream_cleanup_uncertain = True
                                else:
                                    try:
                                        def_s = None
                                        acq_claim = self._stream_claim_lock.acquire(timeout=0.020)
                                        try:
                                            def_s = getattr(self, "_default_stream", None)
                                            self._default_stream = None
                                        finally:
                                            if acq_claim:
                                                self._stream_claim_lock.release()

                                        if def_s is not None:
                                            try:
                                                def_s.abort(ignore_errors=False)
                                            except Exception as ab_ex:  # noqa: BLE001
                                                stream_err = ab_ex
                                            try:
                                                def_s.close(ignore_errors=False)
                                            except Exception as cl_ex:  # noqa: BLE001
                                                stream_err = stream_err or cl_ex
                                        sd.stop()
                                        if stream_err is not None:
                                            self._stream_cleanup_uncertain = True
                                    finally:
                                        self._sd_playback_lock.release()
                            except Exception as e:  # noqa: BLE001
                                logger.warning("Fallback default sounddevice stop failed: %s", e)
                                self._stream_cleanup_uncertain = True
                            finally:
                                acq_fin = self._stream_claim_lock.acquire(timeout=0.050)
                                if acq_fin:
                                    try:
                                        self._default_stop_pending = False
                                        if hasattr(self, "_default_stop_cond"):
                                            self._default_stop_cond.notify_all()
                                    finally:
                                        self._stream_claim_lock.release()
                                else:
                                    self._stream_cleanup_uncertain = True

                                    def _deferred_clear_site3():
                                        with self._stream_claim_lock:
                                            self._default_stop_pending = False
                                            if hasattr(self, "_default_stop_cond"):
                                                self._default_stop_cond.notify_all()

                                    threading.Thread(
                                        target=_deferred_clear_site3,
                                        daemon=True,
                                        name="Jarvis-TTS-StopCondClear3",
                                    ).start()

                t = threading.Thread(
                    target=_fallback_worker,
                    daemon=True,
                    name="Jarvis-TTS-FallbackDispatcher",
                )
                self._fallback_thread = t
                t.start()

    def on_barge_in_flush(self, force: bool = False) -> None:
        """Immediately interrupts speech playback non-blockingly (<10ms) upon barge-in."""
        current_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)
        if not force:
            if is_in_cancellation_flush():
                if current_epoch > 0 and self._last_handled_barge_in_epoch >= current_epoch:
                    return
                if (
                    self._stream_active or getattr(self, "_stream_starting", False)
                ) and current_epoch <= getattr(self, "_session_activation_epoch", 0):
                    return
            elif (
                current_epoch > 0
                and self._last_handled_barge_in_epoch >= current_epoch
                and self._stream_active
            ):
                return
        self._last_handled_barge_in_epoch = current_epoch

        self._session_generation = next(self._session_counter)
        self._stop_event.set()
        self._sapi_purge_event.set()
        self._is_speaking = False
        self._stream_active = False

        if hasattr(self, "_ring_buffer") and self._ring_buffer:
            try:
                self._ring_buffer.barge_in(blocking=False)
            except Exception as e:  # noqa: BLE001
                logger.debug("Ring buffer barge-in suppressed: %s", e)

        if hasattr(self, "_shm_transport") and self._shm_transport:
            try:
                self._shm_transport.trigger_barge_in_clear(blocking=False)
            except Exception as e:  # noqa: BLE001
                logger.debug("SHM trigger_barge_in_clear error: %s", e)
                self._shm_flush_uncertain = True

        if hasattr(self, "_resampler"):
            self._resampler.reset()

        if hasattr(self, "_chunker") and self._chunker_lock.acquire(blocking=False):
            try:
                self._chunker.reset()
            finally:
                self._chunker_lock.release()

        acquired = self._stream_claim_lock.acquire(blocking=False)
        if acquired:
            try:
                stream = self._output_stream
                if stream is not None:
                    self._output_stream = None
                    self._pending_close_streams.add(stream)
                    self._pending_close_streams.add(id(stream))
                    try:
                        self._async_close_queue.put_nowait(stream)
                    except queue.Full:
                        self._dispatch_fallback_close(stream=stream)

                def_stream = getattr(self, "_default_stream", None)
                if def_stream is not None:
                    self._default_stream = None
                    self._pending_close_streams.add(def_stream)
                    self._pending_close_streams.add(id(def_stream))
                    try:
                        self._async_close_queue.put_nowait(def_stream)
                    except queue.Full:
                        self._dispatch_fallback_close(stream=def_stream)

                if stream is None and def_stream is None and not self._default_stop_pending:
                    self._default_stop_pending = True
                    try:
                        self._async_close_queue.put_nowait("STOP_DEFAULT")
                    except queue.Full:
                        self._dispatch_fallback_close(default_stop=True)
            finally:
                self._stream_claim_lock.release()
        else:

            def _deferred_stream_check():
                with self._stream_claim_lock:
                    st = self._output_stream
                    dst = getattr(self, "_default_stream", None)
                    claimed_any = False
                    if (
                        self._stop_event.is_set()
                        or (current_epoch > 0 and self._last_handled_barge_in_epoch >= current_epoch)
                    ):
                        if st is not None:
                            claimed_any = True
                            self._output_stream = None
                            self._pending_close_streams.add(st)
                            self._pending_close_streams.add(id(st))
                            try:
                                self._async_close_queue.put_nowait(st)
                            except queue.Full:
                                self._dispatch_fallback_close(stream=st)
                        if dst is not None:
                            claimed_any = True
                            self._default_stream = None
                            self._pending_close_streams.add(dst)
                            self._pending_close_streams.add(id(dst))
                            try:
                                self._async_close_queue.put_nowait(dst)
                            except queue.Full:
                                self._dispatch_fallback_close(stream=dst)

                    if not claimed_any and not self._default_stop_pending:
                        self._default_stop_pending = True
                        try:
                            self._async_close_queue.put_nowait("STOP_DEFAULT")
                        except queue.Full:
                            self._dispatch_fallback_close(default_stop=True)

            with self._deferred_thread_lock:
                if self._deferred_thread is None or not self._deferred_thread.is_alive():
                    self._deferred_thread = threading.Thread(
                        target=_deferred_stream_check,
                        daemon=True,
                        name="Jarvis-TTS-DeferredStreamCheck",
                    )
                    self._deferred_thread.start()

    def stop(
        self,
        stop_timeout: float = 0.100,
        ramp_timeout: float = 0.030,
        non_blocking: bool = False,
    ) -> bool:
        """Immediately interrupts and halts speech playback across all engines."""
        if non_blocking:
            self._stream_active = False
            self._is_speaking = False
            try:
                from jarvis.core.cancellation import signal_global_barge_in
                signal_global_barge_in(async_callbacks=True, exclude=self.on_barge_in_flush)
            except Exception:  # noqa: BLE001, S110
                pass
            self.on_barge_in_flush(force=True)
            return True

        rem_stop = max(0.001, float(stop_timeout))
        if not self._stop_lock.acquire(timeout=rem_stop):
            return False
        try:
            with self._lifecycle_lock:
                self._stop_in_progress = True
            try:
                return self._do_stop(stop_timeout=stop_timeout, ramp_timeout=ramp_timeout)
            finally:
                with self._lifecycle_lock:
                    self._stop_in_progress = False
                    self._startup_cond.notify_all()
        finally:
            self._stop_lock.release()

    def _do_stop(self, stop_timeout: float = 0.100, ramp_timeout: float = 0.030) -> bool:
        clean_stop = True
        stop_deadline = time.monotonic() + max(0.0, float(stop_timeout))

        def _calc_rem(default_t: float) -> float:
            return min(default_t, max(0.0, stop_deadline - time.monotonic()))

        self._session_generation = next(self._session_counter)
        self._stop_event.set()
        self._sapi_purge_event.set()
        self._stream_active = False
        self._is_speaking = False
        self._last_handled_barge_in_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)

        if hasattr(self, "_chunker") and self._chunker_lock.acquire(blocking=False):
            try:
                self._chunker.reset()
            finally:
                self._chunker_lock.release()
        if hasattr(self, "_resampler"):
            self._resampler.reset()
        if hasattr(self, "_ring_buffer") and self._ring_buffer:
            try:
                self._ring_buffer.barge_in(blocking=False)
            except Exception as e:  # noqa: BLE001
                logger.debug("Ring buffer barge-in suppressed: %s", e)

        if hasattr(self, "_shm_transport") and self._shm_transport:
            try:
                self._shm_transport.trigger_barge_in_clear()
            except Exception:  # noqa: BLE001
                self._shm_flush_uncertain = True
                clean_stop = False

        try:
            from jarvis.core.cancellation import (
                is_barge_in_active,
                signal_global_barge_in,
            )
            if not is_barge_in_active():
                signal_global_barge_in(async_callbacks=True, exclude=self.on_barge_in_flush)
        except Exception:  # noqa: BLE001, S110
            pass

        while not self._sentence_queue.empty():
            try:
                self._sentence_queue.get_nowait()
            except Exception:  # noqa: BLE001
                break

        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except Exception:  # noqa: BLE001
                break

        # Immediately preempt sounddevice playback so any active sd.wait() in speak_sync unblocks
        if not self._safe_sd_stop(timeout=min(0.05, _calc_rem(stop_timeout))):
            clean_stop = False

        if getattr(self, "_stream_starting", False) or getattr(self, "_startup_active", 0) > 0:
            self._barge_in_during_startup = True
            rem_startup = _calc_rem(stop_timeout)
            with self._lifecycle_lock:
                deadline_startup = time.monotonic() + rem_startup
                while (
                    getattr(self, "_stream_starting", False) or getattr(self, "_startup_active", 0) > 0
                ) and time.monotonic() < deadline_startup:
                    rem_w = deadline_startup - time.monotonic()
                    if rem_w <= 0 or not self._startup_cond.wait(timeout=rem_w):
                        break
                if getattr(self, "_stream_starting", False) or getattr(self, "_startup_active", 0) > 0:
                    clean_stop = False

        if getattr(self, "_sync_speech_active", 0) > 0:
            rem_sync = _calc_rem(stop_timeout)
            with self._lifecycle_lock:
                deadline_sync = time.monotonic() + rem_sync
                while getattr(self, "_sync_speech_active", 0) > 0 and time.monotonic() < deadline_sync:
                    rem_w = deadline_sync - time.monotonic()
                    if rem_w <= 0 or not self._startup_cond.wait(timeout=rem_w):
                        break
                if getattr(self, "_sync_speech_active", 0) > 0:
                    clean_stop = False

        stream = None
        rem_claim = _calc_rem(stop_timeout)
        if not self._stream_claim_lock.acquire(timeout=rem_claim):
            clean_stop = False
        else:
            try:
                stream = getattr(self, "_output_stream", None)
                if stream:
                    self._output_stream = None
                    self._pending_close_streams.add(stream)
                    self._pending_close_streams.add(id(stream))
            finally:
                self._stream_claim_lock.release()

        if stream:
            try:
                if getattr(stream, "active", False):
                    ramp_deadline = time.monotonic() + min(ramp_timeout, _calc_rem(ramp_timeout))
                    while (
                        hasattr(self, "_ring_buffer")
                        and self._ring_buffer._consumer_epoch != self._ring_buffer._barge_in_epoch
                        and time.monotonic() < ramp_deadline
                    ):
                        time.sleep(0.002)
                if not self._close_stream_safely(stream, stop_timeout=_calc_rem(stop_timeout)):
                    clean_stop = False
            finally:
                rem_discard = _calc_rem(stop_timeout)
                if self._stream_claim_lock.acquire(timeout=rem_discard):
                    try:
                        self._pending_close_streams.discard(stream)
                        self._pending_close_streams.discard(id(stream))
                    finally:
                        self._stream_claim_lock.release()
                else:
                    clean_stop = False
        elif getattr(self, "_shutdown_worker", None) and self._shutdown_worker.is_alive():
            if not self._close_stream_safely(self._shutdown_stream, stop_timeout=_calc_rem(stop_timeout)):
                clean_stop = False
        else:
            rem_sd = _calc_rem(stop_timeout)
            if not self._safe_sd_stop(timeout=rem_sd):
                clean_stop = False

        if self._sapi_thread:
            if self._sapi_thread.is_alive():
                self._sapi_thread.join(timeout=_calc_rem(0.05))
            if self._sapi_thread.is_alive():
                clean_stop = False

        if getattr(self, "_sapi_purge_failed", False):
            clean_stop = False

        synth = getattr(self, "_synth_thread", None)
        if synth and synth.is_alive():
            synth.join(timeout=_calc_rem(0.02))
            if synth.is_alive():
                clean_stop = False

        play = getattr(self, "_playback_thread", None)
        if play and play.is_alive():
            play.join(timeout=_calc_rem(0.02))
            if play.is_alive():
                clean_stop = False

        if (
            getattr(self, "_stream_cleanup_uncertain", False)
            or getattr(self, "_stream_shutdown_timed_out", False)
            or getattr(self, "_shm_flush_uncertain", False)
            or getattr(self, "_sapi_purge_failed", False)
        ):
            clean_stop = False

        # Wait for any in-flight asynchronous stream closes to finish
        rem_pend = _calc_rem(stop_timeout)
        if not self._stream_claim_lock.acquire(timeout=rem_pend):
            pending = True
            clean_stop = False
        else:
            try:
                pending = bool(self._pending_close_streams)
            finally:
                self._stream_claim_lock.release()

        if pending:
            rem_pending = _calc_rem(stop_timeout)
            deadline_pending = time.monotonic() + rem_pending
            while time.monotonic() < deadline_pending:
                rem_poll = min(0.005, max(0.0, deadline_pending - time.monotonic()))
                if self._stream_claim_lock.acquire(timeout=rem_poll):
                    try:
                        if not self._pending_close_streams:
                            pending = False
                            break
                    finally:
                        self._stream_claim_lock.release()
                time.sleep(0.002)
            if pending:
                clean_stop = False

        return clean_stop

    def close(self, timeout: float = 2.0) -> bool:
        """Closes TTS instance, unregisters callbacks, terminates workers, and releases SHM transport."""
        deadline = time.monotonic() + max(0.0, float(timeout))

        def _get_rem() -> float:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise RuntimeError("TTS close deadline exceeded")
            return rem

        rem_close = _get_rem()
        if not self._close_lock.acquire(timeout=rem_close):
            raise RuntimeError("TTS close deadline exceeded waiting for close lock")
        try:
            if self._closed:
                return True

            with self._lifecycle_lock:
                self._closing = True
                self._stop_event.set()
                self._sapi_purge_event.set()

            # 1. Unregister barge-in audio flush callback
            try:
                from jarvis.core.cancellation import unregister_audio_flush_callback
                rem_unreg = _get_rem()
                ok = unregister_audio_flush_callback(self.on_barge_in_flush, timeout=rem_unreg)
                if not ok:
                    raise RuntimeError("Audio flush callback unregistration timed out")
            except Exception as e:
                raise RuntimeError(
                    f"Audio flush callback unregistration failed during close teardown: {e}"
                ) from e

            # 2. Preempt default sounddevice playback
            self._safe_sd_stop(timeout=min(0.05, _get_rem()))

            # 3. Halt streams and workers
            stop_ok = self.stop(stop_timeout=min(0.100, _get_rem()))
            if not stop_ok:
                raise RuntimeError("TTS stop failed during close teardown")

            # Enforce close deadline after stop
            _get_rem()

            # 4. Shut down async close worker queue
            try:
                self._async_close_queue.put(None, timeout=min(0.05, _get_rem()))
                if self._async_close_worker.is_alive():
                    self._async_close_worker.join(timeout=min(0.100, _get_rem()))
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug("Async close queue shutdown note: %s", e)

            if self._async_close_worker.is_alive():
                raise RuntimeError("Async close worker stalled past close timeout")

            # Enforce close deadline after async worker shutdown
            _get_rem()

            # 5. Join all background worker threads
            threads_to_join = [
                getattr(self, "_synth_thread", None),
                getattr(self, "_playback_thread", None),
                getattr(self, "_sapi_thread", None),
                getattr(self, "_shutdown_worker", None),
                getattr(self, "_startup_worker", None),
                getattr(self, "_fallback_thread", None),
                getattr(self, "_deferred_thread", None),
                getattr(self, "_fallback_sd_stop_thread", None),
            ]
            for th in threads_to_join:
                if th and th.is_alive():
                    rem_th = _get_rem()
                    th.join(timeout=rem_th)
                    if th.is_alive():
                        raise RuntimeError("Worker thread stalled past close timeout")

            # Wait for active synchronous speech to quiesce
            with self._lifecycle_lock:
                deadline_sync = time.monotonic() + _get_rem()
                while getattr(self, "_sync_speech_active", 0) > 0 and time.monotonic() < deadline_sync:
                    rem_w = deadline_sync - time.monotonic()
                    if rem_w <= 0 or not self._startup_cond.wait(timeout=rem_w):
                        break
                if getattr(self, "_sync_speech_active", 0) > 0:
                    raise RuntimeError("Sync speech active past close timeout")

            # Verify no pending stream closures remain unresolved
            with self._stream_claim_lock:
                if self._pending_close_streams:
                    raise RuntimeError("Pending stream closures remained unresolved past close timeout")

            # 6. Close SHM transport only after all workers are dead (preserves transport on failure)
            if getattr(self, "_shm_transport", None):
                self._shm_transport.close()
                self._shm_transport = None

            with self._lifecycle_lock:
                self._closed = True
                self._closing = False
                self._stream_active = False
                self._is_speaking = False
            return True
        finally:
            self._close_lock.release()

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
    def _smooth_and_pad_audio(
        samples: np.ndarray,
        sr: int,
        chunk_text: str,
        apply_dsp: bool = True,
    ) -> np.ndarray:
        """Applies signature MCU J.A.R.V.I.S. DSP filter, 5ms Hann window smoothing,
        peak limiting, and natural acoustic silence pauses."""
        if samples is None or len(samples) == 0:
            return samples

        if apply_dsp:
            samples = apply_cinematic_jarvis_dsp(samples, sr)

        if len(samples) > 240:
            fade_len = min(120, len(samples) // 8)
            fade_in = (0.5 * (1.0 - np.cos(np.pi * np.arange(fade_len) / fade_len))).astype(np.float32)
            samples[:fade_len] *= fade_in
            samples[-fade_len:] *= fade_in[::-1]

        max_val = float(np.max(np.abs(samples)))
        if max_val > 0.95:
            samples = samples * (0.95 / max_val)

        stripped = (chunk_text or "").strip()
        if re.search(r'[.!?\n]$', stripped):
            pause_duration = 0.24
        elif re.search(r'[,;:—–]$', stripped):
            pause_duration = 0.13
        else:
            pause_duration = 0.04

        pause_samples = int(sr * pause_duration)
        if pause_samples > 0:
            silence = np.zeros(pause_samples, dtype=np.float32)
            return np.concatenate([samples, silence])
        return samples

    def _split_into_audio_chunks(self, text: str) -> list[str]:
        """Splits text into natural clause phrases and sentences."""
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

            if re.search(r'[.!?]$', current.strip()):
                if len(words) >= 3 or not chunks:
                    chunks.append(current.strip())
                    current = ""
                else:
                    chunks[-1] = chunks[-1] + " " + current.strip()
                    current = ""
            elif re.search(r'[,;:—–\n]$', current.strip()) and (len(words) >= 5 or len(words) >= 12):
                chunks.append(current.strip())
                current = ""

        if current.strip():
            if chunks and len(current.strip().split()) < 3:
                chunks[-1] = chunks[-1] + " " + current.strip()
            else:
                chunks.append(current.strip())

        return [c for c in chunks if c]

    def _resolve_output_stream_params(self) -> tuple[int | None, int]:
        """Resolves target audio output device index and native hardware sample rate."""
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

                if def_name:
                    for d in wasapi_devs:
                        d_name = d.get("name", "").lower()
                        if d_name in def_name or def_name in d_name:
                            target_sr = int(d.get("default_samplerate", 48000))
                            return d["index"], target_sr

                for d in wasapi_devs:
                    d_name = d.get("name", "").lower()
                    if "headphone" in d_name or "speaker" in d_name:
                        target_sr = int(d.get("default_samplerate", 48000))
                        return d["index"], target_sr

                if wasapi_devs:
                    return wasapi_devs[0]["index"], int(wasapi_devs[0].get("default_samplerate", 48000))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not auto-resolve WASAPI output endpoint: %s", e)

        return None, 24000

    def _stream_audio_callback(self, outdata, frames, time_info, status):
        """Hardware WASAPI / PortAudio streaming callback reading from the lock-free audio ring buffer."""
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

            if not getattr(self, "_prebuffer_filled", True) and not has_pending_ramp:
                cushion = max(1024, frames * 2)
                if self._ring_buffer.available_samples >= cushion or (
                    synth_done and self._ring_buffer.available_samples > 0
                ):
                    self._prebuffer_filled = True
                else:
                    outdata.fill(0.0)
                    return

            n = self._ring_buffer.read_into(outdata, frames=frames)

            if n < frames and (synth_active or q_has_work):
                self._underrun_count += 1
                if n == 0:
                    self._prebuffer_filled = False
        except Exception:  # noqa: BLE001
            outdata.fill(0.0)

    def start_streaming_session(self) -> bool:
        """Initializes a double-buffered streaming session with concurrent pre-synthesis."""
        entry_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)
        if getattr(self, "_closed", False) or getattr(self, "_closing", False):
            return False
        if getattr(self, "_stop_in_progress", False):
            return False
        if getattr(self, "_sync_speech_active", 0) > 0:
            return False
        if getattr(self, "_shm_flush_uncertain", False):
            return False
        if self.engine == "sapi5" and getattr(self, "_sapi_purge_failed", False):
            return False

        with self._stream_claim_lock:
            if getattr(self, "_pending_close_streams", None) and len(self._pending_close_streams) > 0:
                return False
            if getattr(self, "_default_stop_pending", False):
                if hasattr(self, "_default_stop_cond"):
                    self._default_stop_cond.wait(timeout=0.050)
                if getattr(self, "_default_stop_pending", False):
                    return False
            if self._stream_active:
                return False

        if not self._stop_lock.acquire(timeout=0.2):
            return False

        gen = None
        try:
            for w in (
                getattr(self, "_synth_thread", None),
                getattr(self, "_playback_thread", None),
                getattr(self, "_sapi_thread", None),
                getattr(self, "_startup_worker", None),
            ):
                if w and w.is_alive():
                    w.join(timeout=0.05)
                    if w.is_alive():
                        return False

            with self._lifecycle_lock:
                if (
                    self._stop_in_progress
                    or self._startup_in_progress
                    or self._sync_speech_active > 0
                    or self._closed
                    or self._closing
                    or int(GLOBAL_EPOCH_STATE.barge_in_epoch) != entry_epoch
                    or (self.engine == "sapi5" and getattr(self, "_sapi_purge_failed", False))
                ):
                    return False
                self._startup_in_progress = True
                self._startup_active += 1
                self._stream_starting = True
                self._stop_event.clear()
                self._sapi_purge_event.clear()
                self._sapi_worker_error = None
                self._session_generation = next(self._session_counter)
                self._session_activation_epoch = entry_epoch
                gen = self._session_generation

                if int(GLOBAL_EPOCH_STATE.barge_in_epoch) != entry_epoch:
                    self._stop_event.set()
                    self._sapi_purge_event.set()
                    self._stream_starting = False
                    self._startup_in_progress = False
                    self._startup_active = max(0, self._startup_active - 1)
                    return False
        finally:
            self._stop_lock.release()

        try:
            return self._do_start_streaming_session(current_gen=gen)
        finally:
            with self._lifecycle_lock:
                self._stream_starting = False
                self._startup_in_progress = False
                self._startup_active = max(0, self._startup_active - 1)
                self._startup_cond.notify_all()

    def _do_start_streaming_session(self, current_gen: int | None = None) -> bool:
        if (
            self._stop_event.is_set()
            or (current_gen is not None and self._session_generation != current_gen)
            or int(GLOBAL_EPOCH_STATE.barge_in_epoch) != getattr(self, "_session_activation_epoch", 0)
        ):
            self._stream_active = False
            self._is_speaking = False
            return False

        with self._stream_claim_lock:
            if getattr(self, "_shutdown_worker", None):
                if self._shutdown_worker.is_alive():
                    self._stream_active = False
                    self._is_speaking = False
                    self._stream_shutdown_timed_out = True
                    self._stream_cleanup_uncertain = True
                    return False
                prior_outcome = getattr(self._shutdown_worker, "outcome", {}).get(
                    "status", self.SHUTDOWN_UNCLEAN_FAILED
                )
                self._last_shutdown_outcome = prior_outcome
                if prior_outcome in (self.SHUTDOWN_CLEAN_STOP, self.SHUTDOWN_RECOVERED_ABORT):
                    if self._shutdown_stream is not None:
                        self._closed_streams.add(self._shutdown_stream)
                    self._shutdown_worker = None
                    self._shutdown_stream = None
                    self._stream_shutdown_timed_out = False
                    self._stream_cleanup_uncertain = False
                else:
                    if self._shutdown_stream is not None:
                        self._unresolved_streams.add(self._shutdown_stream)
                        self._unresolved_streams.add(id(self._shutdown_stream))
                    self._shutdown_worker = None
                    self._stream_shutdown_timed_out = False
                    self._stream_cleanup_uncertain = True
                    self._stream_active = False
                    self._is_speaking = False
                    return False

            if getattr(self, "_stream_cleanup_uncertain", False):
                self._stream_active = False
                self._is_speaking = False
                return False

            if getattr(self, "_default_stop_pending", False):
                if hasattr(self, "_default_stop_cond"):
                    self._default_stop_cond.wait(timeout=0.050)
                if getattr(self, "_default_stop_pending", False):
                    self._stream_active = False
                    self._is_speaking = False
                    return False
            if getattr(self, "_pending_close_streams", None) and len(self._pending_close_streams) > 0:
                self._stream_active = False
                self._is_speaking = False
                return False

        if hasattr(self, "_chunker"):
            if not self._chunker_lock.acquire(timeout=0.050):
                self._stream_active = False
                self._is_speaking = False
                return False
            try:
                self._chunker.reset()
            finally:
                self._chunker_lock.release()

        self._is_speaking = True
        self._stream_active = True
        self._prebuffer_filled = False
        self._synth_has_started = False
        self._session_word_count = 0
        self._spoken_clauses = []
        self._enqueued_clauses = []
        if hasattr(self, "_resampler"):
            self._resampler.reset()
        if hasattr(self, "_ring_buffer"):
            self._ring_buffer.clear()

        self._stream_device, self._stream_sample_rate = self._resolve_output_stream_params()

        if self.engine == "kokoro" and self._kokoro:
            while not self._sentence_queue.empty():
                try:
                    self._sentence_queue.get_nowait()
                except Exception:  # noqa: BLE001
                    break

            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except Exception:  # noqa: BLE001
                    break

            worker_gen = current_gen
            stream_startup_done = threading.Event()

            def _quiesce_workers():
                self._stream_active = False
                self._stop_event.set()
                stream_startup_done.set()
                try:
                    self._sentence_queue.put_nowait(None)
                except Exception:  # noqa: BLE001, S110
                    pass
                try:
                    self._audio_queue.put_nowait(None)
                except Exception:  # noqa: BLE001, S110
                    pass
                if getattr(self, "_synth_thread", None) and self._synth_thread.is_alive():
                    self._synth_thread.join(timeout=0.05)
                if getattr(self, "_playback_thread", None) and self._playback_thread.is_alive():
                    self._playback_thread.join(timeout=0.05)

            def _synth_worker():
                if not pin_current_thread_to_mask(MASK_PCORE_COMPUTE, THREAD_PRIORITY_ABOVE_NORMAL):
                    _set_thread_realtime_priority("KokoroSynthWorker")
                while (
                    self._stream_active
                    and not self._stop_event.is_set()
                    and (worker_gen is None or self._session_generation == worker_gen)
                ):
                    try:
                        raw_sentence = self._sentence_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue

                    if raw_sentence is None:
                        self._audio_queue.put(None)
                        break

                    if isinstance(raw_sentence, tuple):
                        sentence, item_gen = raw_sentence
                        if item_gen is not None and (
                            (worker_gen is not None and item_gen != worker_gen)
                            or self._session_generation != item_gen
                        ):
                            continue
                    else:
                        sentence = raw_sentence

                    if (
                        self._stop_event.is_set()
                        or (worker_gen is not None and self._session_generation != worker_gen)
                    ):
                        self._audio_queue.put(None)
                        break

                    clean_text = self._clean_text_for_speech(sentence)
                    if not clean_text:
                        continue

                    sub_chunks = self._split_into_audio_chunks(clean_text)
                    for chunk in sub_chunks:
                        if (
                            self._stop_event.is_set()
                            or not self._stream_active
                            or (worker_gen is not None and self._session_generation != worker_gen)
                        ):
                            break
                        try:
                            producer_epoch = self._ring_buffer.barge_in_epoch
                            shm_producer_epoch = (
                                self._shm_transport.barge_in_epoch
                                if getattr(self, "_shm_transport", None)
                                else None
                            )
                            samples, sr = self._kokoro.create(
                                chunk,
                                voice=self._resolve_voice(),
                                speed=self.kokoro_speed,
                                lang=self.kokoro_lang,
                            )
                            if (
                                self._ring_buffer.barge_in_epoch != producer_epoch
                                or self._stop_event.is_set()
                                or (worker_gen is not None and self._session_generation != worker_gen)
                            ):
                                if hasattr(self, "_resampler"):
                                    self._resampler.reset()
                                break

                            padded_samples = self._smooth_and_pad_audio(
                                samples, sr, chunk, apply_dsp=self.dsp_filter
                            )

                            if getattr(self, "_stream_sample_rate", 24000) == 48000:
                                try:
                                    padded_samples = self._resampler.process(padded_samples)
                                except Exception:  # noqa: BLE001, S110
                                    pass

                            if (
                                self._ring_buffer.barge_in_epoch != producer_epoch
                                or self._stop_event.is_set()
                                or (worker_gen is not None and self._session_generation != worker_gen)
                            ):
                                if hasattr(self, "_resampler"):
                                    self._resampler.reset()
                                break

                            if (
                                not self._stop_event.is_set()
                                and (worker_gen is None or self._session_generation == worker_gen)
                            ):
                                written = self._ring_buffer.write(padded_samples, producer_epoch=producer_epoch)
                                if written == 0 and self._ring_buffer.barge_in_epoch != producer_epoch:
                                    if hasattr(self, "_resampler"):
                                        self._resampler.reset()
                                    break
                                if getattr(self, "_shm_transport", None):
                                    try:
                                        self._shm_transport.write_pcm(padded_samples, producer_epoch=shm_producer_epoch)
                                    except Exception:  # noqa: BLE001, S110
                                        pass
                                self._audio_queue.put((padded_samples, sr))
                                try:
                                    from jarvis.audio.dsp_tap import AudioDspTap
                                    AudioDspTap.get_instance().process_and_broadcast(padded_samples[:512])
                                except Exception:  # noqa: BLE001, S110
                                    pass
                        except Exception as e:  # noqa: BLE001
                            logger.error("Kokoro synth error: %s", e)
                    if (
                        not self._stop_event.is_set()
                        and self._stream_active
                        and (worker_gen is None or self._session_generation == worker_gen)
                    ):
                        self._spoken_clauses.append(sentence)

            def _playback_worker():
                if not elevate_audio_thread_mmcss("Pro Audio"):
                    _set_thread_realtime_priority("WASAPIPlaybackWorker")
                stream_startup_done.wait(timeout=3.0)
                while (
                    self._stream_active
                    and not self._stop_event.is_set()
                    and (worker_gen is None or self._session_generation == worker_gen)
                ):
                    try:
                        item = self._audio_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue

                    if (
                        item is None
                        or self._stop_event.is_set()
                        or (worker_gen is not None and self._session_generation != worker_gen)
                    ):
                        break

                    if not use_stream_callback:
                        samples, sr = item
                        started_play = False
                        try:
                            with self._sd_playback_lock:
                                if (
                                    self._stop_event.is_set()
                                    or getattr(self, "_closing", False)
                                    or getattr(self, "_closed", False)
                                    or getattr(self, "_default_stop_pending", False)
                                    or (worker_gen is not None and self._session_generation != worker_gen)
                                ):
                                    break
                                sd.play(samples, sr)
                                started_play = True
                                try:
                                    if hasattr(sd, "get_stream"):
                                        with self._stream_claim_lock:
                                            self._default_stream = sd.get_stream()
                                except Exception:  # noqa: BLE001, S110
                                    pass
                            if started_play:
                                sd.wait()
                        except Exception as e:  # noqa: BLE001
                            logger.error("Playback error: %s", e)
                        finally:
                            with self._stream_claim_lock:
                                self._default_stream = None

                if not use_stream_callback:
                    self._is_speaking = False

            use_stream_callback = True
            self._synth_thread = threading.Thread(target=_synth_worker, daemon=True, name="Jarvis-TTS-Synth")
            self._playback_thread = threading.Thread(target=_playback_worker, daemon=True, name="Jarvis-TTS-Play")
            self._synth_has_started = True
            self._synth_thread.start()
            self._playback_thread.start()

            stream = None
            stream_box: list[Any] = [None]
            stream_err: list[Any] = [None]
            stream_started = threading.Event()
            caller_abandoned = False

            def _open_stream_worker():
                nonlocal caller_abandoned
                s = None
                try:
                    s = sd.OutputStream(
                        device=self._stream_device,
                        samplerate=self._stream_sample_rate,
                        channels=2,
                        dtype='float32',
                        blocksize=1024,
                        latency='low',
                        callback=self._stream_audio_callback,
                    )
                    # Call native s.start() completely OUTSIDE _stream_claim_lock so hangs do not block caller
                    if not self._stop_event.is_set() and (current_gen is None or self._session_generation == current_gen):
                        s.start()
                except Exception as ex:  # noqa: BLE001
                    stream_err[0] = ex
                finally:
                    should_close_now = False
                    with self._stream_claim_lock:
                        if caller_abandoned:
                            should_close_now = (
                                s is not None
                                and s not in self._pending_close_streams
                                and id(s) not in self._pending_close_streams
                            )
                        else:
                            stream_box[0] = s
                    stream_started.set()
                    if should_close_now and s is not None:
                        self._close_stream_safely(s, stop_timeout=0.100)

            startup_thread = threading.Thread(
                target=_open_stream_worker,
                daemon=True,
                name="Jarvis-TTS-StreamStartup",
            )
            self._startup_worker = startup_thread
            startup_thread.start()

            startup_timeout = getattr(self, "stream_startup_timeout", 2.0)
            signaled = stream_started.wait(timeout=startup_timeout)

            stream_to_close = None
            with self._stream_claim_lock:
                if signaled and not caller_abandoned:
                    stream = stream_box[0]
                    stream_box[0] = None
                else:
                    caller_abandoned = True
                    if stream_box[0] is not None:
                        stream_to_close = stream_box[0]
                        stream_box[0] = None

            if not signaled:
                logger.error(
                    "Native OutputStream startup stalled after %.1fs; failing closed.",
                    startup_timeout,
                )
                if stream_to_close is not None:
                    self._close_stream_safely(stream_to_close, stop_timeout=0.100)
                self._stream_cleanup_uncertain = True
                _quiesce_workers()
                self._stream_active = False
                self._is_speaking = False
                return False
            if stream_err[0] is not None:
                logger.warning(
                    "Could not open hardware OutputStream callback on device %s at %dHz: %s. Falling back to standard playback.",
                    self._stream_device,
                    self._stream_sample_rate,
                    stream_err[0],
                )
                should_close_on_error = False
                with self._stream_claim_lock:
                    if self._output_stream is stream:
                        self._output_stream = None
                        should_close_on_error = True
                    elif (
                        stream is not None
                        and stream not in self._pending_close_streams
                        and id(stream) not in self._pending_close_streams
                    ):
                        should_close_on_error = True
                cleanup_ok = True
                if should_close_on_error and stream is not None:
                    cleanup_ok = self._close_stream_safely(stream, stop_timeout=0.100)

                if (
                    self._stop_event.is_set()
                    or (current_gen is not None and self._session_generation != current_gen)
                    or not cleanup_ok
                    or getattr(self, "_stream_cleanup_uncertain", False)
                    or getattr(self, "_stream_shutdown_timed_out", False)
                ):
                    _quiesce_workers()
                    self._stream_active = False
                    self._is_speaking = False
                    return False

                use_stream_callback = False
                self._use_stream_callback = False
                stream_startup_done.set()
                return True
            else:
                should_close_local = False
                with self._stream_claim_lock:
                    if (
                        self._stop_event.is_set()
                        or (current_gen is not None and self._session_generation != current_gen)
                        or stream in self._pending_close_streams
                        or id(stream) in self._pending_close_streams
                    ):
                        if (
                            stream not in self._pending_close_streams
                            and id(stream) not in self._pending_close_streams
                        ):
                            should_close_local = True
                    else:
                        self._output_stream = stream

                if should_close_local and stream is not None:
                    self._close_stream_safely(stream, stop_timeout=0.100)

                if (
                    self._stop_event.is_set()
                    or (current_gen is not None and self._session_generation != current_gen)
                    or stream in self._pending_close_streams
                    or id(stream) in self._pending_close_streams
                ):
                    stream_to_close = None
                    with self._stream_claim_lock:
                        if self._output_stream is stream:
                            self._output_stream = None
                            stream_to_close = stream
                    if stream_to_close is not None:
                        self._close_stream_safely(stream_to_close, stop_timeout=0.100)
                    _quiesce_workers()
                    self._stream_active = False
                    self._is_speaking = False
                    return False

            if self._stop_event.is_set() or (current_gen is not None and self._session_generation != current_gen):
                _quiesce_workers()
                self._stream_active = False
                self._is_speaking = False
                return False

            use_stream_callback = True
            self._use_stream_callback = True
            stream_startup_done.set()
            return True

        else:
            if self._sapi_thread is not None and self._sapi_thread.is_alive():
                logger.warning("Prior SAPI worker is still active; rejecting new SAPI session.")
                return False

            while not self._sentence_queue.empty():
                try:
                    self._sentence_queue.get_nowait()
                except Exception:  # noqa: BLE001
                    break

            sapi_init_done = threading.Event()
            sapi_err: list[Any] = [None]
            worker_gen = current_gen

            def _sapi_worker_loop():
                import pythoncom
                import win32com.client
                sapi_inst = None
                co_initialized = False
                try:
                    try:
                        pythoncom.CoInitialize()
                        co_initialized = True
                        sapi_inst = win32com.client.Dispatch("SAPI.SpVoice")
                        sapi_inst.Rate = self.sapi_rate
                        if self.sapi_voice:
                            for i in range(sapi_inst.GetVoices().Count):
                                v = sapi_inst.GetVoices().Item(i)
                                if self.sapi_voice.lower() in v.GetDescription().lower():
                                    sapi_inst.Voice = v
                                    break
                        sapi_init_done.set()
                    except Exception as ex:  # noqa: BLE001
                        sapi_err[0] = ex
                        sapi_init_done.set()
                        return

                    while (
                        self._stream_active
                        and not self._stop_event.is_set()
                        and (worker_gen is None or self._session_generation == worker_gen)
                    ):
                        if self._sapi_purge_event.is_set():
                            self._safe_purge_sapi(sapi_inst)
                            break
                        try:
                            raw_chunk = self._sentence_queue.get(timeout=0.05)
                        except queue.Empty:
                            continue
                        if raw_chunk is None:
                            break
                        if isinstance(raw_chunk, tuple):
                            chunk, item_gen = raw_chunk
                            if item_gen is not None and (
                                (worker_gen is not None and item_gen != worker_gen)
                                or self._session_generation != item_gen
                            ):
                                continue
                        else:
                            chunk = raw_chunk

                        if (
                            self._stop_event.is_set()
                            or not self._stream_active
                            or (worker_gen is not None and self._session_generation != worker_gen)
                        ):
                            break
                        if self._sapi_purge_event.is_set():
                            self._safe_purge_sapi(sapi_inst)
                            break
                        clean = self._clean_text_for_speech(chunk)
                        if clean:
                            try:
                                sapi_inst.Speak(clean, 1)
                                while (
                                    not self._stop_event.is_set()
                                    and self._stream_active
                                    and not self._sapi_purge_event.is_set()
                                    and (worker_gen is None or self._session_generation == worker_gen)
                                ):
                                    if sapi_inst.WaitUntilDone(50):
                                        break
                            except Exception as ex:  # noqa: BLE001
                                logger.error("SAPI worker playback error: %s", ex)
                                self._sapi_worker_error = ex
                                break
                            if (
                                self._sapi_purge_event.is_set()
                                or self._stop_event.is_set()
                                or (worker_gen is not None and self._session_generation != worker_gen)
                            ):
                                self._safe_purge_sapi(sapi_inst)
                                break
                except Exception as ex:  # noqa: BLE001
                    logger.error("SAPI worker unhandled error: %s", ex)
                    self._sapi_worker_error = ex
                finally:
                    if sapi_inst is not None and (self._sapi_purge_event.is_set() or self._stop_event.is_set()):
                        self._safe_purge_sapi(sapi_inst)
                    sapi_inst = None
                    try:
                        del v
                    except Exception:  # noqa: BLE001, S110
                        pass
                    if co_initialized:
                        try:
                            pythoncom.CoUninitialize()
                        except Exception as ex:  # noqa: BLE001
                            logger.debug("SAPI worker CoUninitialize note: %s", ex)

            self._sapi_thread = threading.Thread(
                target=_sapi_worker_loop,
                daemon=True,
                name="Jarvis-TTS-SAPIWorker",
            )
            self._sapi_thread.start()
            init_signaled = sapi_init_done.wait(timeout=1.0)
            with self._lifecycle_lock:
                if (
                    not init_signaled
                    or sapi_err[0] is not None
                    or self._stop_event.is_set()
                    or (current_gen is not None and self._session_generation != current_gen)
                ):
                    self._session_generation = next(self._session_counter)
                    self._stop_event.set()
                    self._sapi_purge_event.set()
                    self._stream_active = False
                    self._is_speaking = False
                    if self._sapi_thread and self._sapi_thread.is_alive():
                        self._sapi_thread.join(timeout=0.05)
                    return False
            return True

    def feed_token(self, token: str, expected_gen: int | None = None) -> list[str]:
        """Streams an incremental LLM token into the speculative chunker."""
        entry_gen = expected_gen if expected_gen is not None else self._session_generation
        if (
            not self._stream_active
            or self._stop_event.is_set()
            or (expected_gen is not None and self._session_generation != expected_gen)
        ):
            return []

        with self._chunker_lock:
            if (
                not self._stream_active
                or self._stop_event.is_set()
                or self._session_generation != entry_gen
            ):
                return []
            emitted_chunks = self._chunker.feed(token)
            if (
                not self._stream_active
                or self._stop_event.is_set()
                or self._session_generation != entry_gen
            ):
                self._chunker.reset()
                return []

        for chunk in emitted_chunks:
            self.queue_sentence(chunk, expected_gen=entry_gen)
        return emitted_chunks

    def flush_tokens(self, expected_gen: int | None = None) -> list[str]:
        """Flushes any remaining text in the speculative chunker into the synthesis queue."""
        entry_gen = expected_gen if expected_gen is not None else self._session_generation
        if (
            not self._stream_active
            or self._stop_event.is_set()
            or (expected_gen is not None and self._session_generation != expected_gen)
        ):
            return []

        with self._chunker_lock:
            if (
                not self._stream_active
                or self._stop_event.is_set()
                or self._session_generation != entry_gen
            ):
                return []
            remaining_chunks = self._chunker.flush()
            if (
                not self._stream_active
                or self._stop_event.is_set()
                or self._session_generation != entry_gen
            ):
                self._chunker.reset()
                return []

        for chunk in remaining_chunks:
            self.queue_sentence(chunk, expected_gen=entry_gen)
        return remaining_chunks

    def queue_sentence(self, sentence: str, expected_gen: int | None = None) -> None:
        """Enqueues and immediately begins pre-synthesizing a sentence in real time."""
        text = sentence.strip()
        if (
            not text
            or self._stop_event.is_set()
            or (expected_gen is not None and self._session_generation != expected_gen)
        ):
            return

        if (
            self.engine == "sapi5"
            and (self._sapi_thread is None or not self._sapi_thread.is_alive())
            and not self.start_streaming_session()
        ):
            self._stream_active = False
            self._is_speaking = False
            return

        self._session_word_count = getattr(self, "_session_word_count", 0) + len(text.split())
        if not hasattr(self, "_enqueued_clauses"):
            self._enqueued_clauses = []
        self._enqueued_clauses.append(text)

        target_gen = expected_gen if expected_gen is not None else self._session_generation
        if (self.engine == "kokoro" and self._kokoro and self._stream_active) or (
            self._sapi_thread and self._sapi_thread.is_alive()
        ):
            self._sentence_queue.put((text, target_gen))
            self._is_speaking = True
        else:
            self._stream_active = False
            self._is_speaking = False

    def finish_streaming_session(self) -> bool:
        """Waits for all queued speech to finish; returns False on stall or close failure."""
        if self.engine == "kokoro" and self._kokoro:
            self.flush_tokens()
            self._sentence_queue.put(None)

            word_count = getattr(self, "_session_word_count", 0)
            est_speech_seconds = (word_count / 1.5) + 20.0
            max_session_ceiling = max(self.stream_timeout_seconds, est_speech_seconds, 60.0)
            deadline = time.monotonic() + max_session_ceiling
            workers = [self._synth_thread, self._playback_thread]
            timed_out = False
            stall_timeout = 15.0

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

            use_stream_cb = getattr(self, "_use_stream_callback", True)
            if use_stream_cb:
                last_playback_progress = time.monotonic()
                last_available_samples = self._ring_buffer.available_samples

                while (not self._ring_buffer.is_empty) and not self._stop_event.is_set() and not timed_out:
                    now = time.monotonic()
                    if now >= deadline:
                        timed_out = True
                        break

                    current_samples = self._ring_buffer.available_samples
                    if current_samples != last_available_samples:
                        last_playback_progress = now
                        last_available_samples = current_samples
                    elif now - last_playback_progress > stall_timeout:
                        logger.warning("WASAPI audio output stalled for %.1fs without consuming samples.", stall_timeout)
                        timed_out = True
                        break

                    time.sleep(0.01)
            else:
                if hasattr(self, "_ring_buffer"):
                    self._ring_buffer.clear()
                if self._playback_thread:
                    while self._playback_thread.is_alive() and not self._stop_event.is_set() and not timed_out:
                        now = time.monotonic()
                        if now >= deadline:
                            timed_out = True
                            break
                        self._playback_thread.join(timeout=0.05)

            if not self._stop_event.is_set() and not timed_out:
                time.sleep(0.05)

            close_ok = True
            with self._stream_claim_lock:
                stream = self._output_stream
                if stream:
                    self._output_stream = None
                    self._pending_close_streams.add(stream)
                    self._pending_close_streams.add(id(stream))

            if stream:
                try:
                    close_ok = self._close_stream_safely(stream, stop_timeout=0.100)
                finally:
                    with self._stream_claim_lock:
                        self._pending_close_streams.discard(stream)
                        self._pending_close_streams.discard(id(stream))

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
            return not timed_out and close_ok

        else:
            self.flush_tokens()
            self._sentence_queue.put(None)
            sapi_ok = True
            word_count = getattr(self, "_session_word_count", 0)
            est_speech_seconds = (word_count / 1.5) + 20.0
            max_session_ceiling = max(self.stream_timeout_seconds, est_speech_seconds, 60.0)

            if self._sapi_thread and self._sapi_thread.is_alive():
                self._sapi_thread.join(timeout=max_session_ceiling)
                if self._sapi_thread.is_alive():
                    logger.warning(
                        "SAPI worker did not drain queued speech within %.1fs ceiling; requesting purge.",
                        max_session_ceiling,
                    )
                    sapi_ok = False
                    self._sapi_purge_event.set()
                    self._sapi_thread.join(timeout=1.0)
                    if self._sapi_thread.is_alive():
                        logger.error("SAPI worker failed to quiesce after purge request.")
                        sapi_ok = False

            if getattr(self, "_sapi_worker_error", None) is not None:
                sapi_ok = False
            self._is_speaking = False
            self._stream_active = False
            return sapi_ok

    def speak_sync(self, text: str) -> None:
        """Synchronously speaks text with Kokoro or SAPI."""
        entry_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)
        if getattr(self, "_closed", False) or getattr(self, "_closing", False):
            return
        if getattr(self, "_stop_in_progress", False):
            return
        if getattr(self, "_startup_in_progress", False):
            return
        if getattr(self, "_sync_speech_active", 0) > 0 or getattr(self, "_stream_active", False):
            return
        if getattr(self, "_shm_flush_uncertain", False) or getattr(self, "_stream_cleanup_uncertain", False):
            return
        if self.engine == "sapi5" and getattr(self, "_sapi_purge_failed", False):
            return

        with self._stream_claim_lock:
            if getattr(self, "_pending_close_streams", None) and len(self._pending_close_streams) > 0:
                return
            if getattr(self, "_default_stop_pending", False):
                if hasattr(self, "_default_stop_cond"):
                    self._default_stop_cond.wait(timeout=0.050)
                if getattr(self, "_default_stop_pending", False):
                    return
            if self._stream_active:
                return

        with self._lifecycle_lock:
            if (
                self._closed
                or self._closing
                or self._stop_in_progress
                or self._startup_in_progress
                or self._sync_speech_active > 0
                or self._stream_active
                or getattr(self, "_shm_flush_uncertain", False)
                or getattr(self, "_stream_cleanup_uncertain", False)
                or (self.engine == "sapi5" and getattr(self, "_sapi_purge_failed", False))
                or int(GLOBAL_EPOCH_STATE.barge_in_epoch) != entry_epoch
            ):
                return
            with self._stream_claim_lock:
                if getattr(self, "_pending_close_streams", None) and len(self._pending_close_streams) > 0:
                    return
                if getattr(self, "_default_stop_pending", False):
                    if hasattr(self, "_default_stop_cond"):
                        self._default_stop_cond.wait(timeout=0.050)
                    if getattr(self, "_default_stop_pending", False):
                        return
            self._sync_speech_active += 1
            self._session_generation = next(self._session_counter)
            self._stop_event.clear()
            self._sapi_purge_event.clear()
            self._session_activation_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)
            initial_gen = self._session_generation

        try:
            clean_text = self._clean_text_for_speech(text)
            if not clean_text:
                return

            with self._lifecycle_lock:
                if (
                    self._session_generation != initial_gen
                    or self._stop_event.is_set()
                    or getattr(self, "_closing", False)
                    or getattr(self, "_closed", False)
                    or getattr(self, "_stop_in_progress", False)
                    or int(GLOBAL_EPOCH_STATE.barge_in_epoch) != entry_epoch
                ):
                    return
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
                            lang=self.kokoro_lang,
                        )
                        padded_samples = self._smooth_and_pad_audio(
                            samples, sr, chunk, apply_dsp=self.dsp_filter
                        )
                        started_play = False
                        with self._sd_playback_lock:
                            if (
                                self._stop_event.is_set()
                                or getattr(self, "_closing", False)
                                or getattr(self, "_closed", False)
                                or getattr(self, "_default_stop_pending", False)
                                or int(GLOBAL_EPOCH_STATE.barge_in_epoch) != entry_epoch
                            ):
                                break
                            try:
                                from jarvis.audio.dsp_tap import AudioDspTap
                                AudioDspTap.get_instance().process_and_broadcast(padded_samples[:512])
                            except Exception:  # noqa: BLE001, S110
                                pass
                            sd.play(padded_samples, sr)
                            started_play = True
                            try:
                                if hasattr(sd, "get_stream"):
                                    with self._stream_claim_lock:
                                        self._default_stream = sd.get_stream()
                            except Exception:  # noqa: BLE001, S110
                                pass
                        if started_play:
                            try:
                                sd.wait()
                            finally:
                                with self._stream_claim_lock:
                                    self._default_stream = None
                except Exception as e:  # noqa: BLE001
                    logger.error("Kokoro speak_sync error: %s. Falling back to SAPI5.", e)
                    if not self._stop_event.is_set():
                        self._speak_local_sapi(clean_text)
                finally:
                    self._is_speaking = False
            else:
                if not self._stop_event.is_set():
                    self._speak_local_sapi(clean_text)
        finally:
            with self._lifecycle_lock:
                self._sync_speech_active = max(0, self._sync_speech_active - 1)
                self._startup_cond.notify_all()

    def _speak_local_sapi(self, text: str):
        """Speaks via local Windows SAPI5 engine in thread-local COM apartment."""
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        self._is_speaking = True
        sapi = None
        v = None
        try:
            sapi = win32com.client.Dispatch("SAPI.SpVoice")
            sapi.Rate = self.sapi_rate
            if self.sapi_voice:
                for i in range(sapi.GetVoices().Count):
                    v = sapi.GetVoices().Item(i)
                    if self.sapi_voice.lower() in v.GetDescription().lower():
                        sapi.Voice = v
                        break
            if not self._stop_event.is_set():
                sapi.Speak(text, 1)
                while not self._stop_event.is_set():
                    if sapi.WaitUntilDone(50):
                        break
            if self._stop_event.is_set():
                try:
                    sapi.Speak("", 2)
                except Exception as e:  # noqa: BLE001
                    logger.debug("SAPI stop speak note: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.error("SAPI5 speech error: %s", e)
        finally:
            self._is_speaking = False
            sapi = None
            v = None
            try:
                pythoncom.CoUninitialize()
            except Exception as e:  # noqa: BLE001
                logger.debug("CoUninitialize note: %s", e)

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
