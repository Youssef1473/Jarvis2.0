---
name: audio-dsp-engineering
description: >-
  Architectural specifications, P-core CPU affinity pinning (0xFF), WASAPI ring-buffer
  management, and neural audio DSP standards for Kokoro ONNX and faster-whisper CUDA in Jarvis 2.0.
  Guarantees zero audio thread starvation, <1us barge-in interruption, and artifact-free speech.
---

# Neural Audio DSP & Real-Time Audio Pipeline Engineering

This skill defines the engineering standards, concurrency models, and digital signal processing (DSP) rules for the Jarvis 2.0 audio engine (`jarvis/audio/`).

```
┌────────────────────────────────────────────────────────────────────────┐
│                          Audio Pipeline Architecture                   │
│                                                                        │
│   Microphone Input (WASAPI / PyAudio)                                 │
│        │                                                               │
│        ▼                                                               │
│   Adaptive VAD & Energy Gate (Dynamic silence scaling up to 3.2s)      │
│        │                                                               │
│        ▼                                                               │
│   Faster-Whisper CUDA Float16 (P-Cores 0xFF, THREAD_PRIORITY_HIGHEST)  │
│        │                                                               │
│        ▼                                                               │
│   JarvisBrain Streaming Clause Extractor                               │
│        │                                                               │
│        ▼ non-blocking queue.Queue                                      │
│   Kokoro-82M ONNX Synthesizer (bm_george 0.98x Bettany cadence)        │
│        │                                                               │
│        ▼ 5ms Hann Window Cross-Fade + Peak Limiting <= 0.95            │
│   WASAPI Playback Worker (AudioRingBuffer 24kHz 120s capacity)         │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Cardinal Invariant: Audio Thread Starvation Is a Fatal Bug

The audio engine operates on dedicated worker threads with P-core CPU affinity (affinity mask `0xFF`, thread priority `THREAD_PRIORITY_HIGHEST`).

- **Strict Prohibition:** Never run blocking network I/O, heavy SQLite queries, or LLM inference on the audio synthesis (`KokoroSynthWorker`) or playback (`WASAPIPlaybackWorker`) threads.
- All speech streaming must pass through non-blocking queues (`queue.Queue`) with timeout bounds.
- Instant speech interruption via `TextToSpeech.stop()` must clear `AudioRingBuffer` atomically in `<1 microsecond`.

```python
# Atomic barge-in interruption pattern in jarvis/audio/tts.py
def stop(self):
    """Halts playback instantly (<1us) upon user barge-in."""
    self._stop_event.set()
    self.ring_buffer.clear()  # Atomic pointer reset
    with self._queue_lock:
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except queue.Empty:
                break
```

---

## 2. Acoustic Cadence & Hann Boundary Cross-Fading

Synthesized neural audio chunks must apply a 5ms Hann window attenuation ($\sin^2(\theta)$) to head/tail boundaries and peak limiting (${\le}0.95$) to eliminate boundary DC step transients, clicks, and muffled clipping:

```python
import numpy as np

def apply_hann_boundary_crossfade(samples: np.ndarray, sample_rate: int = 24000, fade_ms: float = 5.0) -> np.ndarray:
    """Applies a 5ms Hann window to chunk boundaries to eliminate DC clicks."""
    fade_len = int(sample_rate * (fade_ms / 1000.0))
    if len(samples) < fade_len * 2:
        return samples

    # Smooth rising window on head, falling on tail
    t = np.linspace(0, np.pi / 2, fade_len)
    fade_in = np.sin(t) ** 2
    fade_out = np.cos(t) ** 2

    samples[:fade_len] *= fade_in
    samples[-fade_len:] *= fade_out

    # Peak limiter to eliminate audio clipping
    max_val = np.max(np.abs(samples))
    if max_val > 0.95:
        samples = (samples / max_val) * 0.95

    return samples
```

### Acoustic Pause Insertion
Streamed playback must insert natural acoustic silence pauses to prevent machine-gun breathless delivery:
- **240ms** inter-sentence breath pause on sentence terminators (`[.!?]`).
- **130ms** inter-clause pause on clause delimiters (`[,;:]`).
- **40ms** micro-pause between continuous streaming chunks.
- Kokoro speech speed must be calibrated to **$1.03\times$** for authentic Paul Bettany British diction with signature MCU J.A.R.V.I.S. DSP filtering (110Hz HPF + 7.5ms micro-reflection).

---

## 3. Kokoro Latent Style Blending & Cinematic DSP

Custom voice personas (`jarvis_bettany`, `jarvis_classic`, `jarvis_authoritative`, `jarvis_crisp`, `friday`) must utilize normalized linear combinations of $510 \times 1 \times 256$ float style tensors:

$$\mathbf{v}_{\text{blend}} = \sum w_i \mathbf{v}_i \quad \text{where} \quad \sum w_i = 1.0$$

The signature Bettany persona (`jarvis_bettany`) interpolates in StyleTTS-2 latent space:
$$\mathbf{v}_{\text{bettany}} = 0.50 \cdot \mathbf{v}_{\text{fable}} + 0.30 \cdot \mathbf{v}_{\text{george}} + 0.20 \cdot \mathbf{v}_{\text{daniel}}$$

### Signature MCU J.A.R.V.I.S. DSP Chain
Applies post-processing in $<0.3\text{ms}$:
1. **110Hz High-Pass filter** ($N=2$ Butterworth) to eliminate muddy sub-bass boominess.
2. **7.5ms comb micro-reflection** (180 samples at 24kHz, $-21.4\text{dB}$ / 0.085 wet) for the signature holographic sheen.
3. **Peak limiting** to $\le 0.95$.

Cache blended tensors in memory with LRU eviction for sub-1ms timbre switches without reloading ONNX graphs.

---

## 4. Adaptive Silence Scaling & Hesitation Damping

Speech capture in `jarvis/audio/stt.py` must scale pause tolerance dynamically to accommodate thinking pauses:

$$\text{dynamic\_silence\_s} = \min(\text{thinking\_pause\_s}, \text{base\_silence\_s} + (\text{spoken\_duration\_s} \times 0.35))$$

- Maximum pause tolerance ceiling: **3.2s**.
- Decay silence counter by 2 on intermediate acoustic energy ($0.12 \le \text{VAD} < 0.30$ or RMS $\ge 0.0038$) to prevent premature speech cutoffs while Sir is phrasing a thought.

---

## 5. Content-Aware Speech Drain & Liveness Tracking

`finish_streaming_session()` must dynamically scale playback drain ceilings:

$$\text{ceiling} = \max(\text{stream\_timeout\_seconds}, (\text{word\_count} / 1.5) + 20.0, 60.0)$$

- Maintain a 120-second circular audio buffer: `AudioRingBuffer(capacity_samples=24000 * 120)`.
- Playback must employ **15.0s forward-progress liveness tracking** on ring buffer consumption to eliminate premature speech truncation on long responses while preserving instant `<1us` barge-in interruptibility.

---

## 6. Verification Gates

Verify the entire audio pipeline and affinity pinning suite:

```powershell
.venv\Scripts\python.exe -m unittest tests/test_audio_affinity.py tests/test_audio_streaming_pipeline.py tests/test_tts.py
```
