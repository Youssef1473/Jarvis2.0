---
trigger: model_decision
description: Audio pipeline invariants, WASAPI real-time threads, Kokoro neural TTS, P-Core thread affinity partitioning, and barge-in cancellation.
---

# Audio Pipeline & Real-Time Threading Invariants

These invariants govern audio capture, synthesis, playback, and preemptive cancellation in Jarvis 2.0.

## 1. Thread Safety & Audio Concurrency
- Real-time audio synthesis (`KokoroSynthWorker`) and playback (`WASAPIPlaybackWorker`) threads must **never** execute blocking network I/O, disk queries, heavy database transactions, or LLM inference.
- Audio streaming must strictly use non-blocking queues (`queue.Queue`) and shared memory (`ShmAudioRingTransport`).
- Prohibit blind `time.sleep()` calls in audio paths; use synchronization primitives (events, condition variables).

## 2. Windows CPU Affinity Partitioning & MMCSS
- On Intel 12th Gen Alder Lake i5-12500H topology, partition threads across performance (P) and efficiency (E) cores via `jarvis.core.win32_affinity`:
  - **WASAPI Playback Worker:** Pinned to `MASK_PCORE_AUDIO_ISOLATED = 0x0003` (Logical CPUs 0 and 1) with `THREAD_PRIORITY_TIME_CRITICAL` and MMCSS Pro Audio.
  - **Wake-Word Router Worker:** Pinned across physical P-cores via `get_physical_pcore_mask()` (`0x0055` — Logical CPUs 0, 2, 4, 6) with `THREAD_PRIORITY_HIGHEST` and MMCSS Pro Audio.
  - **Compute P-Cores:** `MASK_PCORE_COMPUTE = 0x00FC` (Logical CPUs 2–7) for neural workloads (Kokoro ONNX TTS, Whisper STT).
  - **Background E-Cores:** `MASK_ECORE_BACKGROUND = 0xFF00` (Logical CPUs 8–15) with EcoQoS for background maintenance, I/O, and worker threads.
- **Affinity Partition Overlap:** The WASAPI playback mask (`0x0003`) and wake-word worker mask (`0x0055`) intersect on Logical CPU 0 (`0x0003 & 0x0055 = 0x0001`). Thread affinity restricts eligible cores but does not grant exclusive processor ownership; Windows may schedule both threads on CPU 0. These affinity masks are specific to the declared host topology and require dynamic fallback on non-Alder Lake processors.

## 3. Preemptive Barge-In Cancellation
- Use the epoch-based cancellation mechanism through `GLOBAL_EPOCH_STATE` (in `jarvis.core.cancellation`) to invalidate stale work. Do not assume a fixed cancellation latency; performance targets require reproducible benchmarks.
- When a barge-in interrupt occurs, abort in-flight playback and queued TTS synthesis immediately.

## 4. Hardware Envelope (RTX 3050 Laptop / 4GB)
- Total VRAM usage must remain strictly under 2.5 GB. CUDA is reserved exclusively for `faster-whisper` and `Kokoro-82M`.
- Auxiliary ONNX models (`onnx_intent_router.py`, `onnx_embeddings.py`) must use `CPUExecutionProvider` with `allow_spinning = "0"` and `intra_op_num_threads = 2` to prevent P-core starvation.

