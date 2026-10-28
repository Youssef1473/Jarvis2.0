"""Procedural sound effects generator and audio cue player for Jarvis 2.0."""
import math
import struct
import wave
import winsound
from pathlib import Path
from typing import Any


class SoundEffects:
    """Manages audio cues (wake, think, done, alert) with zero external assets required."""

    def __init__(self, config: dict[str, Any]):
        base_dir = Path(config.get("_base_dir", "."))
        self.sounds_dir = base_dir / config.get("sounds", {}).get("dir", "data/sounds")
        self.enabled = config.get("sounds", {}).get("enabled", True)
        self.sounds_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_sound_files()

    def _generate_wav(self, file_path: Path, tones: list[tuple[float, float, float]], sample_rate: int = 44100):
        """
        Generates a clean procedural WAV file.
        tones is a list of tuples: (frequency_hz, duration_seconds, amplitude)
        """
        file_path.parent.mkdir(parents=True, exist_ok=True)
        total_samples = []

        for freq, duration, amp in tones:
            num_samples = int(sample_rate * duration)
            for i in range(num_samples):
                t = i / sample_rate
                # Apply an attack/decay envelope to prevent audio popping
                env = math.sin(math.pi * (i / num_samples)) ** 1.5
                val = amp * env * math.sin(2.0 * math.pi * freq * t)
                total_samples.append(val)

        with wave.open(str(file_path), "wb") as wf:
            wf.setnchannels(1)  # Mono
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            # Pack samples as 16-bit signed PCM
            raw_data = bytearray()
            for s in total_samples:
                clamped = max(-1.0, min(1.0, s))
                raw_data.extend(struct.pack("<h", int(clamped * 32767)))
            wf.writeframes(raw_data)

    def _ensure_sound_files(self):
        """Generates futuristic Stark-style cues if files do not exist."""
        wake_file = self.sounds_dir / "wake.wav"
        if not wake_file.exists():
            # Futuristic two-tone rising chime (F#5 -> C#6)
            self._generate_wav(wake_file, [(740.0, 0.08, 0.4), (1108.7, 0.15, 0.5)])

        think_file = self.sounds_dir / "think.wav"
        if not think_file.exists():
            # Subtle futuristic data pulse (880Hz brief blip)
            self._generate_wav(think_file, [(880.0, 0.05, 0.25)])

        done_file = self.sounds_dir / "done.wav"
        if not done_file.exists():
            # High-tech resolving confirmation (987Hz -> 1318Hz)
            self._generate_wav(done_file, [(987.7, 0.06, 0.35), (1318.5, 0.16, 0.45)])

        alert_file = self.sounds_dir / "alert.wav"
        if not alert_file.exists():
            # Attention tone (523Hz -> 440Hz)
            self._generate_wav(alert_file, [(523.25, 0.12, 0.5), (440.0, 0.18, 0.5)])

    def play(self, sound_name: str, async_mode: bool = True):
        """Plays a sound cue by name (wake, think, done, alert)."""
        if not self.enabled:
            return

        sound_path = self.sounds_dir / f"{sound_name}.wav"
        if not sound_path.exists():
            return

        flags = winsound.SND_FILENAME
        if async_mode:
            flags |= winsound.SND_ASYNC

        try:
            winsound.PlaySound(str(sound_path), flags)
        except Exception:
            pass

    def play_wake(self):
        self.play("wake")

    def play_think(self):
        self.play("think")

    def play_done(self):
        self.play("done")

    def play_alert(self):
        self.play("alert")
