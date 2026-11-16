"""Tests for procedural sound effects generation and presence."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.audio.sound_effects import SoundEffects
from jarvis.core.config_loader import load_config


def test_sound_effects_generation():
    config = load_config()
    _sfx = SoundEffects(config)

    sounds_dir = Path(config["_base_dir"]) / "data" / "sounds"
    assert (sounds_dir / "wake.wav").exists(), "wake.wav not generated"
    assert (sounds_dir / "think.wav").exists(), "think.wav not generated"
    assert (sounds_dir / "done.wav").exists(), "done.wav not generated"
    assert (sounds_dir / "alert.wav").exists(), "alert.wav not generated"

    print("All procedural sound effects verified successfully!")


if __name__ == "__main__":
    test_sound_effects_generation()
