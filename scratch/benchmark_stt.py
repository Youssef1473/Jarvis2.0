import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import time
import numpy as np
from faster_whisper import WhisperModel
from jarvis.audio.stt import _setup_nvidia_dll_paths

_setup_nvidia_dll_paths()

print("Loading Whisper 'small' on CUDA (float16)...")
t0 = time.time()
model_small = WhisperModel("small", device="cuda", compute_type="float16")
print(f"Model loaded in {time.time()-t0:.2f}s")

# Generate 3 seconds of dummy audio (16kHz sine wave)
sr = 16000
duration = 3.0
t = np.linspace(0, duration, int(sr * duration), endpoint=False)
audio = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

print("\n--- Benchmark 1: Current Settings (beam_size=3) ---")
latencies_current = []
for _ in range(3):
    start = time.time()
    segments, _ = model_small.transcribe(audio, beam_size=3, language="en", vad_filter=True)
    list(segments)
    latencies_current.append(time.time() - start)
print(f"Average latency: {np.mean(latencies_current)*1000:.1f}ms")

print("\n--- Benchmark 2: Turbo Settings (beam_size=1, condition_on_prev=False) ---")
latencies_turbo = []
for _ in range(3):
    start = time.time()
    segments, _ = model_small.transcribe(
        audio,
        beam_size=1,
        language="en",
        condition_on_previous_text=False,
        temperature=0.0,
        vad_filter=True,
    )
    list(segments)
    latencies_turbo.append(time.time() - start)
print(f"Average latency: {np.mean(latencies_turbo)*1000:.1f}ms")
speedup = np.mean(latencies_current) / np.mean(latencies_turbo)
print(f"Speedup: {speedup:.2f}x faster!")
