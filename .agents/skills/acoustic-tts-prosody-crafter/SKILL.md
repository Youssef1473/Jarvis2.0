---
name: acoustic-tts-prosody-crafter
description: >-
  Rules and DSP guidelines for authoring spoken prompts, reflex responses, and speculative clause chunking
  for Kokoro-82M neural TTS (Paul Bettany persona). Load whenever modifying jarvis/audio/, jarvis/core/brain.py,
  or user-facing spoken responses.
---

# Acoustic TTS Prosody & Bettany Cadence Playbook

This skill defines the acoustic formatting laws required to produce natural, authentic Paul Bettany voice delivery using Kokoro-82M ONNX in Jarvis 2.0.

---

## 1. Punctuation-as-Synthesizer-Control Law

`jarvis/audio/chunker.py` splits streaming LLM token streams at punctuation boundaries and injects calibrated silence durations into the audio stream:
- **Period / Question / Exclamation (`.!?`)**: Injects a **240ms breath pause**.
- **Comma / Semicolon / Colon / Em-Dash (`,;:—`)**: Injects a **130ms clause pause**.
- **Micro-Pause**: Injects a **40ms micro-pause**.

### Clause Length Discipline
- Keep every spoken clause between **6 and 16 words**.
- Insert an em-dash (`—`) before a dry ironic tail clause so Kokoro drops pitch naturally after the pause.
- Never feed multi-sentence run-ons or unpunctuated stream dumps to Kokoro; unpunctuated streams cause breathless, machine-gun delivery.

---

## 2. Phonetic & Lexical Normalization Rules

Neural TTS models synthesize text phonetically. Always format for the ear, not the eye:
- **Contractions:** Use spoken contractions (`"I've"`, `"that's"`, `"we're"`, `"doesn't"`) unless emphasizing a specific warning (*"I would not advise that, Sir"*).
- **Identifiers & File Paths:** Never pass raw `snake_case`, file paths, or hex codes directly to Kokoro. Expand `.py` to `"dot py"`, `0xFF00` to `"the E-core mask"`, and `<= 10ms` to `"under ten milliseconds"`.
- **Acronyms:** Kokoro pronounces uppercase `"HUD"` and `"RAM"` as words, but spells out `"UIA"`, `"CDP"`, and `"SQL"` when properly spaced or cased. Use `"sequel lite"` or `"S-Q-L-ite"` if phoneme drift occurs.
- **Abbreviation Guard:** Guard against false-positive clause splits on `v2.0`, `3.14ms`, `Dr.`, `Mr.`, `vs.`, or `e.g.`.

---

## 3. Audio Ring Buffer & DSP Invariants

- **Lock-Free Read Path:** Never introduce blocking locks on `AudioRingBuffer.read()` in `jarvis/audio/ring_buffer.py`. The WASAPI playback callback executes on P-Core 0 (`0x0003`) and will suffer buffer underruns if blocked.
- **DSP Chain Invariants:**
  - 110Hz High-Pass cut to eliminate low-frequency rumble.
  - 7.5ms comb micro-reflection at -21.4 dB wet for the signature J.A.R.V.I.S. holographic room acoustic.
  - 5ms Hann window (`sin^2`) head/tail attenuation on chunk boundaries.
  - Peak limiter clamped at `<= 0.95`.
- **Barge-in Mute Transient Protection:** When `TextToSpeech.stop()` is triggered, apply a 64-sample (2.6ms) linear fade-to-zero ramp on the final DMA callback frame before clearing the buffer to eliminate DC offset speaker clicks.
