"""Test for energy-gated wake word detection and zero-idle CPU optimization."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
import numpy as np
from jarvis.core.config_loader import load_config
from jarvis.audio.wake_word import WakeWordDetector


def test_wake_word_energy_gate():
    config = load_config()
    detector = WakeWordDetector(config)
    detector._init_model()
    assert detector.model is not None, "Wake word model failed to load"
    print("Wake word model initialized successfully.")

    # 1. Test Silent Chunks (Idle energy gate)
    # 50 chunks of ambient silence (4.0 seconds of audio)
    silent_chunks = [np.random.normal(0, 0.0008, 1280).astype(np.float32) for _ in range(50)]
    detector._is_running = True
    
    t0 = time.time()
    for chunk in silent_chunks:
        # Pass dummy indata to _audio_callback
        indata = chunk.reshape(-1, 1)
        detector._audio_callback(indata, 1280, None, None)
    elapsed_idle = time.time() - t0
    
    print(f"50 silent chunks processed in {elapsed_idle*1000:.2f}ms (Average {elapsed_idle/50*1000:.3f}ms per chunk)")
    assert elapsed_idle < 0.20, f"Idle processing took too long: {elapsed_idle}s (energy gate not working)"

    # 2. Test Active Chunk (Speech energy)
    speech_chunk = np.random.normal(0, 0.05, 1280).astype(np.float32).reshape(-1, 1)
    detector._audio_callback(speech_chunk, 1280, None, None)
    assert detector._active_chunks_remaining > 0, "Energy gate failed to activate on simulated speech"
    print(f"Energy gate activated successfully! Active countdown: {detector._active_chunks_remaining}")

    # 3. Test VAD Caching
    vad1 = detector.get_vad()
    vad2 = detector.get_vad()
    assert vad1 is vad2, "VAD instance must be cached and reused"
    print("VAD instance caching verified successfully.")

    print("\nALL WAKE WORD OPTIMIZATION TESTS PASSED CLEANLY!")


if __name__ == "__main__":
    test_wake_word_energy_gate()
