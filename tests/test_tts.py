"""Test for Kokoro-ONNX audio synthesis and CPU threading."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.config_loader import load_config
from jarvis.audio.tts import TextToSpeech


def test_tts():
    config = load_config()
    tts = TextToSpeech(config)
    print("Testing Kokoro-TTS initialization & synthesis...")
    assert tts.engine == "kokoro", f"Expected kokoro engine, got {tts.engine}"
    assert tts._kokoro is not None, "Kokoro engine instance should not be None"
    print(f"Kokoro engine ready with {tts.cpu_threads} CPU threads.")
    
    # Test generation of speech samples
    samples, sr = tts._kokoro.create(
        "Jarvis systems operational, Sir.",
        voice=tts.kokoro_voice,
        speed=tts.kokoro_speed,
        lang=tts.kokoro_lang
    )
    assert len(samples) > 0, "Synthesized audio samples must not be empty"
    print(f"Kokoro synthesized {len(samples)} samples at {sr}Hz successfully!")


if __name__ == "__main__":
    test_tts()

