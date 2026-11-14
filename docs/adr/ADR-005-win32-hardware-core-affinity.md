# ADR-005: Windows Hybrid CPU Core Affinity Partitioning

## Status
PROPOSED

## Date
2026-11-02

## Scope
Thread scheduling, CPU core affinity partitioning, Windows Multimedia Class Scheduler Service (MMCSS) priority elevation, and EcoQoS power management on Windows hybrid CPU architectures.

---

## Context and Problem Statement
Jarvis 2.0 operates concurrently across heterogeneous computing domains: real-time 100Hz WASAPI audio streaming, ONNX neural speech synthesis, continuous microphone capture, SQLite vector indexing, and asynchronous web socket APIs.

On modern Windows laptops with Intel 12th/13th/14th Generation hybrid architectures (specifically the primary deployment platform: Intel Core i5-12500H with 4 Performance Cores / 8 SMT threads and 8 Efficiency Cores), the default Windows thread scheduler distributes tasks dynamically across all cores. Under high system load (such as heavy background indexing or code linting), this unconstrained scheduling causes:
1. Real-time audio playback threads to be starved of CPU cycles or descheduled from P-Cores, resulting in audible audio stutter, buffer under-runs, and latency spikes.
2. Time-critical neural TTS inference to be scheduled on low-power E-Cores, causing speech generation delays exceeding acceptable interactive thresholds.
3. Wasteful thermal power dissipation when background I/O tasks spin up high-voltage P-Cores unnecessarily.

---

## Decision
Enforce deterministic thread affinity partitioning and MMCSS registration via Win32 APIs in [`win32_affinity.py`](../../jarvis/core/win32_affinity.py).

1. **Topology Bitmask Partitioning (Intel Alder Lake i5-12500H Profile):**
   - **`MASK_PCORE_AUDIO_ISOLATED` (`0x0003` — Logical CPUs 0–1):** Affinity restriction pinning real-time WASAPI audio playback loops to P-Core 0 (Logical CPUs 0 and 1). This restricts playback execution to P-Core 0, but does not grant exclusive CPU ownership.
   - **`MASK_PCORE_COMPUTE` (`0x00FC` — Logical CPUs 2–7):** Affinity restriction directing compute-intensive neural workloads (Kokoro ONNX neural speech synthesis, faster-whisper speech transcription, and LLM orchestration) to P-Cores 1, 2, and 3.
   - **`MASK_ECORE_BACKGROUND` (`0xFF00` — Logical CPUs 8–15):** Affinity restriction confining background I/O, perceptual hashing, and background daemon execution to the 8 Efficiency Cores.
2. **Real-Time Priority & MMCSS Elevation:**
   - Real-time audio threads invoke `kernel32.SetThreadAffinityMask` with `MASK_PCORE_AUDIO_ISOLATED` and elevate OS priority via `kernel32.SetThreadPriority(h_thread, THREAD_PRIORITY_TIME_CRITICAL)`.
   - Threads register with the Windows Multimedia Class Scheduler Service via `avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ...)` and set MMCSS priority to `AVRT_PRIORITY_CRITICAL`, guaranteeing Windows kernel scheduler preemption over standard desktop processes.
3. **Background Power Throttling (EcoQoS):**
   - Background threads pinned to E-Cores enable Windows EcoQoS power throttling (`THREAD_POWER_THROTTLING_EXECUTION_SPEED`) via `kernel32.SetThreadInformation`, optimizing thermal headroom and battery consumption.

---

## Distinction: Verified Code Call Sites vs. Conceptual Scope
Code inspection establishes the exact boundary where affinity masks are explicitly applied:

- **Explicit Call Sites in Production Code:**
  1. *`MASK_PCORE_AUDIO_ISOLATED` (`0x0003`):* Explicitly applied via `elevate_audio_thread_mmcss()` ([`win32_affinity.py:296`](../../jarvis/core/win32_affinity.py#L296)) inside `_playback_worker()` ([`tts.py:2180`](../../jarvis/audio/tts.py#L2180)), setting MMCSS Pro Audio, disabling EcoQoS, and pinning to Logical CPUs 0 and 1 with `THREAD_PRIORITY_TIME_CRITICAL`.
  2. *`MASK_PCORE_COMPUTE` (`0x00FC`):* Explicitly applied in [`tts.py:2050`](../../jarvis/audio/tts.py#L2050) on the dedicated Kokoro ONNX neural speech synthesis worker thread.
  3. *`MASK_ECORE_BACKGROUND` (`0xFF00` with `eco_qos=True`):* Explicitly applied in:
     - `init_ecore_thread_pool()` ([`win32_affinity.py:387`](../../jarvis/core/win32_affinity.py#L387)) for background thread pool executor threads.
     - `sleep_consolidator.py:597` (Memory sleep consolidation worker).
     - `dhash_monitor.py:255` (Perceptual screen hash monitor).
     - `window_hook_daemon.py:162` (Win32 window hook message pump daemon).
     - `cadence_tracker.py:218` (Cadence telemetry tracker).
  4. *Physical P-Core Elevation (`get_physical_pcore_mask()`, e.g. `0x0055`):* Explicitly applied in `_worker_loop()` ([`wake_word.py:342`](../../jarvis/audio/wake_word.py#L342)) via `pin_and_elevate_current_thread(tag="MicRouterWorker")`. This pins the wake-word audio router thread across all physical P-cores (Logical CPUs 0, 2, 4, and 6) with `THREAD_PRIORITY_HIGHEST` and MMCSS Pro Audio. It does **not** receive `MASK_PCORE_AUDIO_ISOLATED` (`0x0003`).
- **Affinity Partition Overlap on Logical CPU 0:**
  - The WASAPI playback worker mask (`0x0003`, Logical CPUs 0 and 1) and the wake-word worker mask (`0x0055`, Logical CPUs 0, 2, 4, and 6) intersect:
    `0x0003 & 0x0055 = 0x0001` (Logical CPU 0).
  - Both affinity masks permit execution on Logical CPU 0.
  - Thread affinity (`SetThreadAffinityMask`) restricts which logical processors a thread is eligible to run on; it does **not** grant exclusive core reservation or hardware CPU ownership.
  - The Windows NT scheduler may schedule both the WASAPI playback thread and the wake-word worker thread on Logical CPU 0 concurrently.
  - Consequently, the architecture represents an *affinity partition* with an overlapping logical processor, rather than guaranteed exclusive hardware isolation.
  - Note: No empirical benchmark has established whether this overlap causes any measurable latency impact or performance degradation.
- **Subsystems Without Direct Pinning Calls:**
  - `SingleWriterActor` ([`sqlite_base.py`](../../jarvis/core/memory/sqlite_base.py)) and the FastAPI web server ([`server.py`](../../jarvis/ui/server.py)) do **not** invoke `pin_current_thread_to_mask` directly in their initialization. While designed conceptually to execute on E-cores, they receive E-core affinity only when dispatched through the E-core `ThreadPoolExecutor` or when scheduled by the OS.

---

## Alternatives Considered
- **Default Windows Scheduler (No Affinity Masks):** Rejected due to reproducible audio dropouts and non-deterministic TTS synthesis latency during concurrent background tasks.
- **Equal Round-Robin Thread Affinity:** Rejected because asymmetric CPU architectures exhibit dramatically different single-thread performance, instruction cache sizes, and memory latency between P-cores and E-cores.
- *(Historical alternatives from initial pre-M-05 architecture: Historical rationale unavailable).*

---

## Consequences and Trade-Offs
- **Positive:**
  - Mitigation of audio playback glitches and buffer underruns by partitioning real-time audio threads away from background E-Core workloads.
  - Predictable, low-latency neural speech synthesis on dedicated P-Cores.
  - Optimized laptop thermals and fan noise by keeping background maintenance tasks confined to E-Cores.
- **Negative / Costs:**
  - The static affinity bitmasks (`0x0003`, `0x00FC`, `0xFF00`) are calibrated specifically to the 4P+8E topology of the Intel Core i5-12500H.
  - Porting to disparate processor architectures (e.g. AMD Ryzen, Intel Core Ultra with LP-E cores, or desktop 8P+16E processors) requires dynamic topology discovery to avoid pinning threads to non-optimal logical processors.

---

## Binding Invariants
- WASAPI playback worker threads must apply `MASK_PCORE_AUDIO_ISOLATED` and register with MMCSS Pro Audio upon startup.
- Neural synthesis workers must not run on background E-Core masks.
- Tasks explicitly pinned to `MASK_ECORE_BACKGROUND` must enable EcoQoS power throttling.

---

## Implementation References
- [`win32_affinity.py:28`](../../jarvis/core/win32_affinity.py#L28) (Affinity mask constants)
- [`win32_affinity.py:90`](../../jarvis/core/win32_affinity.py#L90) (`set_thread_affinity`)
- [`win32_affinity.py:120`](../../jarvis/core/win32_affinity.py#L120) (`enable_mmcss_pro_audio`)
- [`win32_affinity.py:386`](../../jarvis/core/win32_affinity.py#L386) (`init_ecore_thread_pool`)

---

## Production Call Sites
- `jarvis/audio/tts.py` (WASAPI playback thread affinity, MMCSS registration, and Kokoro neural TTS compute affinity)
- `jarvis/audio/wake_word.py` (Wake-word audio router thread physical P-core pinning and MMCSS elevation)
- `jarvis/core/memory/sleep_consolidator.py` (Memory consolidator worker E-Core affinity)
- `jarvis/vision/dhash_monitor.py` (Screen dHash monitor E-Core affinity)
- `jarvis/vision/window_hook_daemon.py` (Window hook daemon E-Core affinity)

---

## Verification Evidence
- [`test_milestone5_windows_hardware.py`](../../tests/test_milestone5_windows_hardware.py) (13 of 13 tests passed on 2026-11-02).

---

## Known Limitations and Unresolved Questions
- CPU masks assume Intel Alder Lake 4P+8E logical processor mapping; on machines with Hyper-Threading disabled in BIOS or different core layouts, the affinity engine requires dynamic discovery fallbacks.
- **Affinity Partition Overlap on Logical CPU 0:** The WASAPI playback worker mask (`0x0003`) and wake-word capture mask (`0x0055`) intersect on Logical CPU 0 (`0x0003 & 0x0055 = 0x0001`). Thread affinity restricts eligible cores but does not guarantee exclusive CPU reservation; Windows may schedule both threads on CPU 0. The impact (if any) of scheduler contention on CPU 0 between playback and wake-word capture has not been empirically benchmarked.

---

## Related ADRs
- [ADR-003](ADR-003-epoch-based-audio-cancellation.md) (Audio cancellation and barge-in invalidation).
