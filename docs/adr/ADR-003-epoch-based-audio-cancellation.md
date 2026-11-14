# ADR-003: Epoch-Based Audio Cancellation and Barge-In Invalidation

## Status
PROPOSED

## Date
2026-11-02

## Scope
Voice interruption (barge-in), neural text-to-speech synthesis pipelines (in [`tts.py`](../../jarvis/audio/tts.py)), WASAPI playback streaming (in [`ring_buffer.py`](../../jarvis/audio/ring_buffer.py)), and command abort handling.

---

## Context and Problem Statement
When a user speaks while Jarvis is producing audio output ("speech barge-in"), in-flight neural speech synthesis, audio buffering, and physical audio playback must halt. Because audio rendering operates across dedicated real-time operating system threads (WASAPI 100Hz audio loops) and asynchronous worker queues, conventional cancellation approaches present severe hazards:
- Terminating threads abruptly (`TerminateThread`) leaks OS handles, corrupts C-heap allocators, and destabilizes the CPython runtime.
- Synchronous mutex locks acquired inside real-time audio streaming callbacks introduce thread priority inversions and audio buffer under-runs (clicks, pops, stutter).
- Slow cancellation drains leave stale audio chunks queued in ring buffers, causing old sentences to play over new voice responses.

---

## Decision
Implement a process-wide shared mutable monotonic epoch counter (`GLOBAL_EPOCH_STATE.barge_in_epoch`) with cooperative token snapshot comparison.

1. **Shared State Representation:** Define `SharedEpochState` as a `ctypes.Structure` with field `barge_in_epoch` (`ctypes.c_uint64`) instantiated as a process-wide singleton `GLOBAL_EPOCH_STATE` in [`cancellation.py`](../../jarvis/core/cancellation.py).
2. **Cancellation Token Snapshotting:** Subsystems mint lightweight `BargeInCancellationToken` instances. Upon instantiation, the token captures `_snapshot_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)`.
3. **Signal Dispatch:** When barge-in or execution abort occurs, `signal_global_barge_in()` performs the increment `GLOBAL_EPOCH_STATE.barge_in_epoch += 1`.
4. **Cooperative Worker Invalidation:** Workers evaluate `token.is_cancelled` (which tests `int(GLOBAL_EPOCH_STATE.barge_in_epoch) != self._snapshot_epoch`) at loop iteration boundaries:
   - **TTS Synthesis Worker ([`tts.py`](../../jarvis/audio/tts.py)):** Verifies epoch before inferring sentence chunks, discarding stale text units when an epoch advance is detected.
   - **Audio Ring Buffer ([`ring_buffer.py`](../../jarvis/audio/ring_buffer.py)):** Maintains an internal `_barge_in_epoch` counter and evicts queued PCM audio frames when consumer and producer epochs diverge.
   - **Antigravity IPC Daemon ([`antigravity_bridge.py`](../../jarvis/core/antigravity_bridge.py)):** Evaluates epoch during token streaming to abort outbound LLM streaming responses.
5. **Asynchronous Flush Callbacks:** Registered audio flush callbacks (`_FLUSH_CALLBACKS`) are dispatched on a background daemon thread (`Jarvis-AsyncBargeInCallbacks`) so the real-time audio input callback thread is not blocked by slow callback execution.

---

## Concurrency & Synchronization Reality (Grounded Invariants)
- **Existing Implementation Behavior:**
  - The counter mutation `GLOBAL_EPOCH_STATE.barge_in_epoch += 1` executes as an unsynchronized scalar read-modify-write on a `ctypes.Structure` field (`c_uint64`) under CPython bytecode evaluation.
  - There is no mutex, atomic compare-and-swap (CAS), or Win32 `InterlockedIncrement64` protecting the counter arithmetic. (`_CALLBACK_LOCK` synchronizes only flush callback registration and execution, not the counter increment itself).
  - Multiple subsystem threads invoke `signal_global_barge_in()` concurrently without write serialization:
    - Wake-word listener thread ([`wake_word.py:444`](../../jarvis/audio/wake_word.py#L444)).
    - TTS engine stop routines ([`tts.py:1353, 1421`](../../jarvis/audio/tts.py#L1353)).
    - UI command abort handlers ([`orchestrator.py:169`](../../jarvis/core/orchestrator.py#L169)).
    - Direct test suite harnesses.
- **Potential Concurrency Failure Mode (Missed Invalidation Race):**
  - Because read-modify-write updates across multiple writer threads are unsynchronized, concurrent interleaving can theoretically cause a lost update that fails to invalidate newly created tokens.
  - *Illustrative Interleaving (Theoretical Race, Not Proven Production Failure):*
    1. Writer A reads `barge_in_epoch = 0`.
    2. Writer B concurrently reads `barge_in_epoch = 0`.
    3. Writer A computes `0 + 1` and writes `1` to `barge_in_epoch`.
    4. A new worker thread initializes and mints a `BargeInCancellationToken`, capturing snapshot epoch `1`.
    5. Writer B, having read `0` prior to Writer A's write, writes its computed value `1` to `barge_in_epoch`.
    6. The new worker thread evaluates `token.is_cancelled` by comparing current `barge_in_epoch` (`1`) against its snapshot (`1`). Because both values equal `1`, the token does not detect Writer B's cancellation signal, and work scheduled after Writer A's signal continues executing despite Writer B's subsequent signal.
  - Current implementation evidence does not rule out this race condition under heavy multi-threaded contention.
- **Currently Demonstrated Test Guarantees:**
  - Automated tests in [`test_milestone3_audio_invariants.py`](../../tests/test_milestone3_audio_invariants.py) verify that individual barge-in signals advance the counter monotonically and that tokens minted prior to a signal report `is_cancelled == True`.
  - Automated tests in [`test_tts_lifecycle_hardening.py`](../../tests/test_tts_lifecycle_hardening.py) verify that `signal_global_barge_in(async_callbacks=True)` synchronously updates the counter while offloading flush callback dispatch to a daemon thread.
- **Unverified Runtime Behavior:**
  - No thread-safety guarantees exist for free-threaded Python environments (PEP 703) where the GIL is absent.
  - Cancellation latency is strictly cooperative and bounded by worker loop iteration intervals (chunking and buffer submission boundaries), not by hardware-level memory speed. Numerical latency guarantees (<10ns, <1us) are unsubstantiated and excised.

---

## Alternatives Considered
- **Direct Thread Termination (`TerminateThread`):** Rejected as unsafe; corrupts runtime heap state, leaks Win32 handles, and causes unpredictable application crashes.
- **Synchronous Mutex on Audio Callbacks:** Rejected because lock contention in high-frequency (100Hz) WASAPI playback callbacks induces audio stutter and device buffer underruns.
- *(Historical alternatives from initial pre-M-03 architecture: Historical rationale unavailable).*

---

## Consequences and Trade-Offs
- **Positive:**
  - Non-blocking, lock-free evaluation in time-critical audio playback threads.
  - Safe, cooperative eviction of stale audio synthesis work and queued buffers.
  - Asynchronous callback dispatch protects real-time microphone capture loops from blocking.
- **Negative / Costs:**
  - Cancellation is polling-based and cooperative; worker code must actively check token state at frequent intervals.
  - Concurrent barge-in signals across threads may interleave.
  - A long-running synchronous C-extension call (e.g. an uninterrupted ONNX model forward pass) cannot be aborted mid-calculation and must finish the current chunk before checking the epoch.

---

## Binding Invariants
- Audio synthesis and playback loops must evaluate cancellation token state before generating, resampling, or queueing audio buffers.
- Stale audio buffers produced under an older epoch must be dropped before submission to WASAPI endpoints.
- Registered flush callbacks must execute asynchronously or with strict timeouts to prevent deadlocking cancellation signals.

---

## Implementation References
- [`cancellation.py:22`](../../jarvis/core/cancellation.py#L22) (`SharedEpochState`, `GLOBAL_EPOCH_STATE`)
- [`cancellation.py:34`](../../jarvis/core/cancellation.py#L34) (`BargeInCancellationToken`)
- [`cancellation.py:244`](../../jarvis/core/cancellation.py#L244) (`signal_global_barge_in`)

---

## Production Call Sites
- `jarvis/audio/tts.py` (Speech synthesis chunking and stop handling)
- `jarvis/audio/wake_word.py` (Speech barge-in triggering)
- `jarvis/core/orchestrator.py` (Active command execution killswitch)
- `jarvis/core/antigravity_bridge.py` (Antigravity streaming response cancellation)

---

## Verification Evidence
- [`test_milestone3_audio_invariants.py`](../../tests/test_milestone3_audio_invariants.py) (6 of 6 tests passed on 2026-11-02).
- [`test_tts_lifecycle_hardening.py`](../../tests/test_tts_lifecycle_hardening.py) (TTS cancellation stress tests).

---

## Known Limitations and Unresolved Questions
- In free-threaded Python environments (PEP 703) without a GIL, the unsynchronized `ctypes.c_uint64` increment will require explicit Win32 interlocked primitives or atomic wrappers.
- In-flight ONNX inference operations cannot be interrupted mid-chunk; cancellation takes effect between sentence clauses.

---

## Related ADRs
- [ADR-005](ADR-005-win32-hardware-core-affinity.md) (WASAPI playback thread affinity and priority).
