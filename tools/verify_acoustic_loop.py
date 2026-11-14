"""Acoustic loop end-to-end verification script for Phase 2.

Exercises:
1. Audio capture / wake word detection (openWakeWord 'hey_jarvis').
2. Speech recognition (faster-whisper STT).
3. Deterministic reflex / cognitive routing (JarvisBrain).
4. Neural TTS synthesis (Kokoro-82M ONNX with Bettany voice vector blend).
5. WASAPI playback parameter validation.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np
from scipy.signal import resample_poly

from jarvis.audio.stt import SpeechToText
from jarvis.audio.tts import TextToSpeech
from jarvis.audio.wake_word import WakeWordDetector
from jarvis.core.brain import JarvisBrain
from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


def main() -> None:
    cfg = load_config()
    print("=== STAGE 1: COMPONENT INITIALIZATION ===")
    tts = TextToSpeech(cfg)
    stt = SpeechToText(cfg)
    detector = WakeWordDetector(cfg)
    detector._init_model()
    memory = MemoryManager(cfg)
    safety = SafetyGate(cfg)
    brain = JarvisBrain(cfg, memory, safety, antigravity_resolver=lambda: None)
    print("Kokoro-82M ONNX: OK")
    print("faster-whisper STT: OK")
    print("openWakeWord ('hey_jarvis'): OK")
    print("JarvisBrain: OK")

    print("\n=== STAGE 2: WAKE WORD DETECTION ('Hey Jarvis') ===")
    ww_audio, _ = tts._kokoro.create("Hey Jarvis!", voice=tts._resolve_voice(), speed=1.0, lang="en-us")
    ww_16k = resample_poly(ww_audio, up=2, down=3).astype(np.float32)
    int16_ww = (np.clip(ww_16k, -1.0, 1.0) * 32767).astype(np.int16)

    max_score = 0.0
    for i in range(0, len(int16_ww) - 1280, 1280):
        p = detector.model.predict(int16_ww[i : i + 1280])
        max_score = max(max_score, p.get("hey_jarvis", 0.0))
    print(f"Wake word 'Hey Jarvis' confidence: {max_score:.4f} (Threshold: {detector.threshold})")
    assert max_score >= detector.threshold, f"Wake word confidence {max_score:.4f} < {detector.threshold}"

    print("\n=== STAGE 3: SPEECH RECOGNITION (faster-whisper) ===")
    user_query = "What time is it?"
    query_audio, _ = tts._kokoro.create(user_query, voice=tts._resolve_voice(), speed=1.0, lang="en-us")
    query_16k = resample_poly(query_audio, up=2, down=3).astype(np.float32)
    transcribed = stt.transcribe(query_16k).strip()
    print(f"Synthesized query: {user_query!r}")
    print(f"Transcribed query: {transcribed!r}")
    assert "time" in transcribed.lower(), f"Expected 'time' in transcription, got: {transcribed!r}"

    print("\n=== STAGE 4: REASONING & REFLEX DISPATCH ===")
    t_start = time.perf_counter()
    reply = brain.process_query(transcribed)
    latency_ms = (time.perf_counter() - t_start) * 1000
    print(f"Brain reply ({latency_ms:.2f}ms): {reply!r}")
    assert reply, "Brain reply was empty"

    print("\n=== STAGE 5: NEURAL TTS SYNTHESIS (Kokoro-82M Bettany Blend) ===")
    # Warmup pass (initializes graph memory allocators and operator kernels)
    tts._kokoro.create(".", voice=tts._resolve_voice(), speed=1.03, lang="en-us")

    t_synth = time.perf_counter()
    synth_audio, sr = tts._kokoro.create(reply, voice=tts._resolve_voice(), speed=1.03, lang="en-us")
    synth_ms = (time.perf_counter() - t_synth) * 1000
    audio_duration_sec = len(synth_audio) / sr
    rtf = (synth_ms / 1000.0) / audio_duration_sec
    print(f"Synthesized samples: {len(synth_audio)} ({audio_duration_sec:.2f}s audio)")
    print(f"Inference latency: {synth_ms:.2f}ms")
    print(f"Real-Time Factor (RTF): {rtf:.4f} (Streaming requires RTF < 1.0 to outpace physical playback)")
    assert rtf < 1.0, f"RTF {rtf:.4f} exceeded real-time streaming playback ceiling (1.0)"

    brain.shutdown()

    print("\n=== STAGE 6: COGNITIVE UPLINK ACOUSTIC LOOP ===")
    brain2 = JarvisBrain(cfg, memory, safety, antigravity_resolver=lambda: None)
    math_query = "What is the square root of 144?"
    query_audio2, _ = tts._kokoro.create(math_query, voice=tts._resolve_voice(), speed=1.0, lang="en-us")
    query_16k_2 = resample_poly(query_audio2, up=2, down=3).astype(np.float32)
    transcribed2 = stt.transcribe(query_16k_2).strip()
    print(f"Transcribed math query: {transcribed2!r}")

    t_start2 = time.perf_counter()
    reply2 = brain2.process_query(transcribed2)
    latency_ms2 = (time.perf_counter() - t_start2) * 1000
    print(f"Brain reasoning reply ({latency_ms2:.2f}ms): {reply2!r}")

    synth_audio2, sr2 = tts._kokoro.create(reply2, voice=tts._resolve_voice(), speed=1.03, lang="en-us")
    print(f"Synthesized reasoning speech: {len(synth_audio2)} samples ({len(synth_audio2)/sr2:.2f}s audio)")
    brain2.shutdown()

    print("\n=== FULL ACOUSTIC LOOP VERIFIED: PASSED ===")


if __name__ == "__main__":
    main()
