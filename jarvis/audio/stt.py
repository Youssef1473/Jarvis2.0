"""Speech-to-Text transcription module using faster-whisper with CUDA acceleration and CPU fallback."""
from dataclasses import dataclass
import os
import sys
from pathlib import Path
import logging
import re
import time
from typing import Dict, Any, Optional, Tuple, List
import numpy as np

from jarvis.audio.affinity import pin_and_elevate_current_thread

logger = logging.getLogger("Jarvis.STT")


@dataclass
class ProsodyMetrics:
    """Acoustic prosody and speech delivery cadence extracted from Whisper decoding."""
    speech_rate_syl_sec: float = 4.2
    active_duration_sec: float = 0.0
    total_syllables: int = 0
    word_count: int = 0
    rms_energy: float = 0.0
    tempo_category: str = "moderate"
    timestamp: float = 0.0


def count_syllables(word: str) -> int:
    """Estimates the syllable count of an English word using phonetic vowel-clustering heuristics."""
    w = word.lower().strip(".:,;!?\"'()[]{}")
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    # Strip common silent endings
    if w.endswith("e") and not w.endswith("le") and not w.endswith("ee"):
        w = w[:-1]
    # Count vowel groups
    matches = re.findall(r"[aeiouy]+", w)
    return max(1, len(matches))


def classify_tempo(rate: float) -> str:
    """Classifies speech rate into qualitative cognitive urgency categories."""
    if rate >= 5.8:
        return "urgent"
    elif rate >= 5.0:
        return "fast"
    elif rate <= 2.8:
        return "slow"
    return "moderate"



def _setup_nvidia_dll_paths():
    """Adds nvidia CUDA/cuBLAS/cuDNN library paths from site-packages if present."""
    if sys.platform != "win32":
        return
    try:
        venv_site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
        if venv_site.exists():
            for bin_dir in venv_site.glob("*/bin"):
                if bin_dir.is_dir():
                    try:
                        os.add_dll_directory(str(bin_dir))
                        os.environ["PATH"] = f"{bin_dir};{os.environ.get('PATH', '')}"
                        logger.info(f"Registered NVIDIA DLL path: {bin_dir}")
                    except Exception:
                        pass
        # Also check PyTorch's bundled CUDA/cuBLAS runtime libraries
        torch_lib = Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib"
        if torch_lib.is_dir():
            try:
                os.add_dll_directory(str(torch_lib))
                os.environ["PATH"] = f"{torch_lib};{os.environ.get('PATH', '')}"
                logger.info(f"Registered PyTorch CUDA DLL path: {torch_lib}")
            except Exception:
                pass
    except Exception:
        pass


class SpeechToText:
    """Wrapper around faster-whisper with automatic GPU/CPU selection and runtime fallback."""

    COMMON_PHONETIC_FIXES = [
        (re.compile(r"\bbrave\s+tower\b", re.IGNORECASE), "Brave tab"),
        (re.compile(r"\bbrave\s+top\b", re.IGNORECASE), "Brave tab"),
        (re.compile(r"\bbrave\s+tap\b", re.IGNORECASE), "Brave tab"),
        (re.compile(r"\bbrave\s+tag\b", re.IGNORECASE), "Brave tab"),
        (re.compile(r"\bbe\s+open\s+it\b", re.IGNORECASE), "will you open it"),
        (re.compile(r"\bchrome\s+tower\b", re.IGNORECASE), "Chrome tab"),
        (re.compile(r"\bchrome\s+top\b", re.IGNORECASE), "Chrome tab"),
        (re.compile(r"\bedge\s+tower\b", re.IGNORECASE), "Edge tab"),
        (re.compile(r"\bvs\s+cold\b", re.IGNORECASE), "VS Code"),
        (re.compile(r"\bvs\s+cope\b", re.IGNORECASE), "VS Code"),
    ]

    @classmethod
    def clean_transcription(cls, text: str) -> str:
        """Applies domain phonetic corrections for common browser and system commands."""
        if not text:
            return ""
        cleaned = text
        for pattern, replacement in cls.COMMON_PHONETIC_FIXES:
            cleaned = pattern.sub(replacement, cleaned)
        return cleaned.strip()

    def __init__(self, config: Dict[str, Any]):
        _setup_nvidia_dll_paths()
        stt_cfg = config.get("stt", {})
        self.model_size = stt_cfg.get("model_size", "small")
        self.preferred_device = stt_cfg.get("device", "cuda")
        self.compute_type = stt_cfg.get("compute_type", "float16")
        self.language = stt_cfg.get("language", "en")
        self.beam_size = int(stt_cfg.get("beam_size", 1))
        self.trim_silence = bool(stt_cfg.get("trim_silence", True))
        self.vad_filter = bool(stt_cfg.get("vad_filter", False))
        self.cpu_threads = int(stt_cfg.get("cpu_threads", 4))
        self.initial_prompt = stt_cfg.get("initial_prompt", "Jarvis, Sir, computer, volume, battery, system.")
        self.model = None
        self.last_prosody: ProsodyMetrics = ProsodyMetrics(timestamp=time.time())
        self._init_model()


    def _init_model(self):
        """Initializes the Faster-Whisper model on CUDA with validation, falling back to CPU."""
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            logger.error("faster_whisper package is not installed.")
            return

        device = self.preferred_device
        compute_type = self.compute_type

        # Dynamic VRAM arbiter check for GPU allocation and compute type adaptation
        if device == "cuda":
            try:
                from jarvis.skills.nvml_dxgi_vram_arbiter import audit_vram_and_thermal_budget
                vram_info = audit_vram_and_thermal_budget(reclaim_if_pressured=True)
                if vram_info.get("status") == "ok":
                    free_mb = vram_info.get("vram_free_mb", 4000.0)
                    if free_mb < 350.0:
                        logger.warning(
                            "VRAM critically low (%.1f MB free < 350MB). Falling back Whisper STT to CPU (int8).",
                            free_mb
                        )
                        device = "cpu"
                        compute_type = "int8"
                    elif free_mb < 700.0:
                        logger.info(
                            "VRAM constrained (%.1f MB free). Adapting Whisper compute_type to int8_float16.",
                            free_mb
                        )
                        compute_type = "int8_float16"
            except Exception as arb_err:
                logger.debug("VRAM arbiter check note in STT init: %s", arb_err)

        # Try CUDA first if requested
        if device == "cuda":
            try:
                logger.info(f"Loading faster-whisper '{self.model_size}' on CUDA ({compute_type})...")
                candidate_model = WhisperModel(self.model_size, device="cuda", compute_type=compute_type)
                # Verify that CUDA DLLs actually load with a 50ms warm-up sample
                dummy_audio = np.zeros(800, dtype=np.float32)
                _ = list(candidate_model.transcribe(dummy_audio, language="en", vad_filter=False)[0])
                self.model = candidate_model
                self.device = "cuda"
                self.compute_type = compute_type
                logger.info("Successfully validated faster-whisper on CUDA GPU.")
                return
            except Exception as e:
                logger.warning(f"CUDA initialization or DLL load failed ({e}). Falling back to CPU...")

        # Fallback to CPU with int8 (capped to 4 threads to prevent CPU spikes)
        try:
            logger.info(f"Loading faster-whisper '{self.model_size}' on CPU (int8, {self.cpu_threads} threads)...")
            self.model = WhisperModel(self.model_size, device="cpu", compute_type="int8", cpu_threads=self.cpu_threads)
            self.device = "cpu"
            self.compute_type = "int8"
            logger.info("Successfully loaded faster-whisper on CPU.")
        except Exception as e:
            logger.error(f"Failed to load faster-whisper on CPU: {e}")
            self.model = None

    def stop(self):
        """Unloads the speech recognition model and releases CUDA GPU memory allocations."""
        if self.model is not None:
            self.model = None
            logger.info("Unloaded faster-whisper model from memory.")
        if "torch" in sys.modules:
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
        import gc
        gc.collect()

    @staticmethod
    def _trim_audio_silence(audio: np.ndarray, sample_rate: int = 16000, threshold: float = 0.008, pad_ms: int = 300) -> np.ndarray:
        """Trims leading and trailing silence from audio array using RMS energy while protecting trailing phonemes."""
        if len(audio) == 0:
            return audio

        frame_size = int(sample_rate * 0.02)
        pad_samples = int(sample_rate * (pad_ms / 1000.0))
        num_frames = len(audio) // frame_size
        if num_frames == 0:
            return audio

        frames = audio[:num_frames * frame_size].reshape(num_frames, frame_size)
        rms = np.sqrt(np.mean(frames ** 2, axis=1))
        active_indices = np.where(rms > threshold)[0]
        if len(active_indices) == 0:
            return audio

        start_frame = max(0, active_indices[0])
        end_frame = min(num_frames, active_indices[-1] + 1)
        start_sample = max(0, start_frame * frame_size - pad_samples)
        end_sample = min(len(audio), end_frame * frame_size + pad_samples)
        return audio[start_sample:end_sample]

    def transcribe(self, audio_data: np.ndarray, sample_rate: int = 16000) -> str:
        """
        Transcribes audio array with beam search decoding, domain prompt conditioning, and automatic fallback.
        """
        pin_and_elevate_current_thread(tag="WhisperSTT")
        if self.model is None:
            logger.error("Whisper model is not initialized.")
            return ""

        if len(audio_data) == 0:
            return ""

        # Normalize audio to float32 between -1.0 and 1.0
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)

        max_abs = np.max(np.abs(audio_data))
        if max_abs > 1.0:
            audio_data = audio_data / max_abs

        # Energy-based silence pre-trimming with safe 300ms boundary protection
        audio_to_transcribe = self._trim_audio_silence(audio_data, sample_rate=sample_rate) if self.trim_silence else audio_data

        try:
            segments, info = self.model.transcribe(
                audio_to_transcribe,
                beam_size=self.beam_size,
                language=self.language,
                temperature=0.0,
                condition_on_previous_text=False,
                initial_prompt=self.initial_prompt,
                vad_filter=self.vad_filter,
                vad_parameters=dict(
                    min_silence_duration_ms=300,
                    speech_pad_ms=200,
                ),
            )
            segment_list = list(segments)
            text = " ".join([segment.text for segment in segment_list]).strip()
            cleaned = self.clean_transcription(text)
            self.last_prosody = self._extract_prosody(audio_to_transcribe, segment_list, cleaned)
            return cleaned
        except Exception as e:
            logger.warning(f"Transcription error encountered: {e}. Switching to CPU...")
            try:
                from faster_whisper import WhisperModel
                self.model = WhisperModel(self.model_size, device="cpu", compute_type="int8", cpu_threads=self.cpu_threads)
                self.device = "cpu"
                self.compute_type = "int8"
                segments, info = self.model.transcribe(
                    audio_to_transcribe,
                    beam_size=self.beam_size,
                    language=self.language,
                    temperature=0.0,
                    condition_on_previous_text=False,
                    initial_prompt=self.initial_prompt,
                    vad_filter=self.vad_filter,
                    vad_parameters=dict(
                        min_silence_duration_ms=300,
                        speech_pad_ms=200,
                    ),
                )
                segment_list = list(segments)
                text = " ".join([segment.text for segment in segment_list]).strip()
                cleaned = self.clean_transcription(text)
                self.last_prosody = self._extract_prosody(audio_to_transcribe, segment_list, cleaned)
                return cleaned
            except Exception as e2:
                logger.error(f"CPU transcription fallback also failed: {e2}")
                return ""

    @classmethod
    def _extract_prosody(
        cls,
        audio_data: np.ndarray,
        segments: list,
        text: str,
    ) -> ProsodyMetrics:
        """Extracts syllables per second and RMS acoustic energy from decoded segments."""
        now = time.time()
        if not text or not segments:
            return ProsodyMetrics(timestamp=now)

        active_duration = sum(
            max(0.0, float(getattr(s, "end", 0.0)) - float(getattr(s, "start", 0.0)))
            for s in segments
        )
        if active_duration <= 0.05:
            active_duration = max(0.2, len(audio_data) / 16000.0)

        words = [w for w in text.split() if w.strip()]
        total_syllables = sum(count_syllables(w) for w in words)
        word_count = len(words)

        speech_rate = total_syllables / max(0.2, active_duration)
        rms = float(np.sqrt(np.mean(audio_data ** 2))) if len(audio_data) > 0 else 0.0
        tempo = classify_tempo(speech_rate)

        return ProsodyMetrics(
            speech_rate_syl_sec=round(speech_rate, 2),
            active_duration_sec=round(active_duration, 2),
            total_syllables=total_syllables,
            word_count=word_count,
            rms_energy=round(rms, 4),
            tempo_category=tempo,
            timestamp=now,
        )

    def transcribe_with_prosody(self, audio_data: np.ndarray, sample_rate: int = 16000) -> Tuple[str, ProsodyMetrics]:
        """Transcribes audio array and returns both the cleaned text and calculated prosody metrics."""
        text = self.transcribe(audio_data, sample_rate=sample_rate)
        return text, self.last_prosody

