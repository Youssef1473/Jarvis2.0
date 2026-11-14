# Jarvis 2.0 — Holdout Engineering Lesson Benchmark Registry (ENG-OS-02 Phase 3D1B)

> [!IMPORTANT]
> This synthetic benchmark registry is strictly for independent evaluation of multi-lesson retrieval,
> lifecycle metadata filtering, and applicability boundary handling.
> The production engineering registry lives in `docs/agent/LESSONS.md`.

---

## LES-001: Antigravity Sandbox Loopback Socket Limitation & Test Masking

- **Lesson ID:** `LES-001`
- **Subsystem Domain:** Testing / Execution Environment Boundaries / IPC Telemetry
- **Date Recorded:** 2026-11-04
- **Status:** ACTIVE_LEGACY
- **Observed Problem:**
  Executing pytest tests binding UDP/TCP loopback sockets on 127.0.0.1 under Antigravity Standard Sandbox results in dropped packets and WinError 10013.
- **Reproduction Evidence:**
  Reproduced in sandboxed pytest run on test_milestone1_security_hardening.py.
- **Verified Root Cause & Unresolved Mechanisms:**
  Windows AppContainer / sandboxed network isolation restricts 127.0.0.1 socket IPC.
- **Prevention Rules:**
  Decouple unit test logic from real OS network sockets using mock transports.
- **Supporting Files & Commands:**
  `tests/test_milestone1_security_hardening.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Applies to all tests in `tests/` that bind 127.0.0.1 UDP or TCP sockets when executed inside Antigravity Standard Sandbox Mode.
  - *Required Environment:* Sandbox
  - *Excluded Environment:* Host, Unrestricted Network
  - *Required Scope:* Test

---

## LES-002: SQLite WAL SingleWriterActor Deadlock on Exclusive Transaction Lock

- **Lesson ID:** `LES-002`
- **Subsystem Domain:** SQLite Persistence / Memory Architecture
- **Date Recorded:** 2026-11-04
- **Status:** ACTIVE
- **Observed Problem:**
  Concurrent write requests deadlocked SingleWriterActor when an uncommitted transaction held the database lock while awaiting another thread event.
- **Reproduction Evidence:**
  Reproduced via concurrent SQLite stress harness under WAL mode.
- **Verified Root Cause & Unresolved Mechanisms:**
  Cross-thread lock acquisition without explicit timeout inside SingleWriterActor actor mailbox.
- **Prevention Rules:**
  Acquire exclusive write lock strictly inside SingleWriterActor, enforce 5-second busy timeout, and commit transactions before signaling completion.
- **Supporting Files & Commands:**
  `jarvis/core/memory/sqlite_base.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Applies to all SQLite database write operations and SingleWriterActor serialization.

---

## LES-003: SQLite Vec KNN Search Memory Contention Under High Dimensions

- **Lesson ID:** `LES-003`
- **Subsystem Domain:** SQLite Persistence / Vector Search
- **Date Recorded:** 2026-11-04
- **Status:** ACTIVE
- **Observed Problem:**
  Performing unbatched KNN vector similarity queries with 768-dimensional embeddings caused high memory consumption and GC stalls.
- **Reproduction Evidence:**
  Stress test querying 10,000 vectors via sqlite-vec extension in memory.
- **Verified Root Cause & Unresolved Mechanisms:**
  In-memory sqlite-vec query buffer allocated full result vectors before slicing top-k.
- **Prevention Rules:**
  Limit sqlite-vec KNN search query batches to a maximum of 64 vectors and apply dimension quantization before similarity scoring.
- **Supporting Files & Commands:**
  `jarvis/core/memory/interaction_memory.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Applies to vector indexing and sqlite-vec KNN semantic retrieval.

---

## LES-004: SQLite Read Pool Connection Starvation Under Concurrent Read Transactions

- **Lesson ID:** `LES-004`
- **Subsystem Domain:** SQLite Persistence / Reader Pool
- **Date Recorded:** 2026-11-04
- **Status:** ACTIVE
- **Observed Problem:**
  Burst read queries exhausted the read-only connection pool, resulting in connection pool timeout exceptions.
- **Reproduction Evidence:**
  Concurrent read load test with 20 parallel worker threads against a 2-connection pool.
- **Verified Root Cause & Unresolved Mechanisms:**
  Read pool size was too small for high-concurrency telemetry streaming.
- **Prevention Rules:**
  Configure read-only connection pool with minimum 4 connections and recycle idle reader connections after 60 seconds.
- **Supporting Files & Commands:**
  `jarvis/core/memory/sqlite_base.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Applies to SQLite read-only connection pool configuration.

---

## LES-005: SQLite FTS5 BM25 Index Corruption During Incremental Vacuum Consolidation

- **Lesson ID:** `LES-005`
- **Subsystem Domain:** SQLite Persistence / Full Text Search
- **Date Recorded:** 2026-11-04
- **Status:** ACTIVE
- **Observed Problem:**
  Executing `PRAGMA incremental_vacuum` while full-text search indexing was active corrupted the FTS5 shadow tables.
- **Reproduction Evidence:**
  FTS5 integrity check failure following background database compaction.
- **Verified Root Cause & Unresolved Mechanisms:**
  Shadow table page reorganization interrupted active FTS5 segment merger.
- **Prevention Rules:**
  Execute database consolidation and incremental vacuum strictly during idle sleep cycles, and run FTS5 integrity verification afterwards.
- **Supporting Files & Commands:**
  `jarvis/core/memory/sleep_consolidator.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Applies to SQLite FTS5 full-text indexing and sleep consolidation.

---

## LES-006: Win32 Job Object Kill-On-Close Race During Unsuspended Process Spawning

- **Lesson ID:** `LES-006`
- **Subsystem Domain:** Windows Process Containment / Subprocess Isolation
- **Date Recorded:** 2026-11-04
- **Status:** ACTIVE
- **Observed Problem:**
  Child processes spawned without `CREATE_SUSPENDED` escaped containment if the parent crashed before Job Object assignment completed.
- **Reproduction Evidence:**
  Process tree inspection revealed orphaned background child processes after forced parent crash.
- **Verified Root Cause & Unresolved Mechanisms:**
  Asynchronous assignment race condition between process creation and Job Object handle binding.
- **Prevention Rules:**
  Always spawn child processes suspended with `CREATE_SUSPENDED`, assign to Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, and resume main thread only after assignment succeeds.
- **Supporting Files & Commands:**
  `jarvis/skills/job_object_sandbox.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Applies to all Windows child process spawning and Job Object isolation.

---

## LES-007: Legacy WASAPI Audio Playback Buffer Size 512 Sample Starvation

- **Lesson ID:** `LES-007`
- **Subsystem Domain:** Audio Threading / WASAPI Playback
- **Date Recorded:** 2026-10-15
- **Status:** DEPRECATED
- **Observed Problem:**
  Setting WASAPI buffer size to 512 samples caused audio glitching and playback underruns under CPU load spikes.
- **Reproduction Evidence:**
  Continuous audio playback glitching at 512 sample buffer sizes.
- **Verified Root Cause & Unresolved Mechanisms:**
  512-sample buffer window (10.6ms at 48kHz) was insufficient to absorb Windows scheduler jitter.
- **Prevention Rules:**
  [DEPRECATED] Do not configure buffer size to 512 samples. Deprecated by ADR-005; 1024 or 2048 sample buffers are now required.
- **Supporting Files & Commands:**
  `jarvis/audio/tts.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* LOW
  - *Applicability:* Deprecated legacy recommendation.

---

## LES-008: SQLite Synchronous OFF Pragma Usage Causing Database File Corruption

- **Lesson ID:** `LES-008`
- **Subsystem Domain:** SQLite Persistence / Pragma Configuration
- **Date Recorded:** 2026-10-20
- **Status:** SUPERSEDED
- **Observed Problem:**
  Setting `PRAGMA synchronous = OFF` to maximize benchmark throughput resulted in unrecoverable database corruption on power loss.
- **Reproduction Evidence:**
  Corrupted database header after simulated crash during heavy write transactions.
- **Verified Root Cause & Unresolved Mechanisms:**
  Operating system write cache was not flushed to disk before transaction was reported committed.
- **Prevention Rules:**
  [SUPERSEDED] Superseded by ADR-001. All SQLite databases must enforce `PRAGMA synchronous = NORMAL` under WAL mode.
- **Supporting Files & Commands:**
  `jarvis/core/memory/sqlite_base.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Superseded legacy configuration.

---

## LES-009: DualKeyApprovalManager Webhook Taint Leak via Unsanitized Telemetry Stream

- **Lesson ID:** `LES-009`
- **Subsystem Domain:** Security Boundaries / Tool Authorization
- **Date Recorded:** 2026-11-04
- **Status:** ACTIVE
- **Observed Problem:**
  Untrusted external webhook payloads bypassed taint tracking because the telemetry receiver did not register the input with TurnTaintTracker.
- **Reproduction Evidence:**
  Webhook payload containing prompt injection executed unauthorized mutating tool action.
- **Verified Root Cause & Unresolved Mechanisms:**
  Missing `taint_input()` call at the inbound network telemetry handler boundary.
- **Prevention Rules:**
  Tag all external network inputs with TurnTaintTracker before pipeline ingestion and enforce DualKeyApprovalManager confirmation on any tainted turn.
- **Supporting Files & Commands:**
  `jarvis/core/security_boundaries.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Applies to inbound webhook handling, external telemetry, and taint tracking.

---

## LES-010: Public Interface Breaking Change in SkillRegistry Tool Registration Decorator

- **Lesson ID:** `LES-010`
- **Subsystem Domain:** API Compatibility / Public Interfaces
- **Date Recorded:** 2026-11-04
- **Status:** ACTIVE
- **Observed Problem:**
  Renaming `@SkillRegistry.register` parameter broke backward compatibility for downstream test shims.
- **Reproduction Evidence:**
  AttributeError in tests importing SkillRegistry public interfaces.
- **Verified Root Cause & Unresolved Mechanisms:**
  Public method signature was modified without adding backward compatibility keyword alias.
- **Prevention Rules:**
  Preserve public API signatures in `jarvis/skills/base.py`, provide deprecation re-export shims, and verify downstream callers before refactoring.
- **Supporting Files & Commands:**
  `jarvis/skills/base.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Applies to all SkillRegistry public API modifications and tool decorators.

---

## LES-011: WASAPI Ring Buffer Lock Contention During Concurrent Audio Frame Capture

- **Lesson ID:** `LES-011`
- **Subsystem Domain:** Audio Threading / Real-Time DSP
- **Date Recorded:** 2026-11-04
- **Status:** ACTIVE
- **Observed Problem:**
  Holding an OS mutex inside the WASAPI audio frame capture loop caused audio dropout when the Kokoro synthesis worker attempted to read the buffer.
- **Reproduction Evidence:**
  Audio underrun detected by WASAPI playback monitor thread.
- **Verified Root Cause & Unresolved Mechanisms:**
  Blocking lock acquisition in real-time audio thread violating zero-blocking invariant.
- **Prevention Rules:**
  Use lock-free ring buffers (SPSC lock-free queue) between WASAPI capture thread and Kokoro synthesis worker. Real-time audio threads must never block on mutexes.
- **Supporting Files & Commands:**
  `jarvis/audio/tts.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Applies to real-time WASAPI audio threading and Kokoro neural synthesis.

---

## LES-012: Host Standalone Loopback Socket Timeout Under Windows Defender Firewall Rule

- **Lesson ID:** `LES-012`
- **Subsystem Domain:** Testing / Host Environment Network Diagnostics
- **Date Recorded:** 2026-11-04
- **Status:** ACTIVE
- **Observed Problem:**
  Running integration tests directly on host machine with unrestricted network failed due to local Windows Defender firewall blocking unfamiliar ephemeral ports.
- **Reproduction Evidence:**
  Reproduced in standalone Windows host test execution outside sandbox.
- **Verified Root Cause & Unresolved Mechanisms:**
  Inbound ephemeral port blocking by local host firewall policy.
- **Prevention Rules:**
  Bind explicit designated test port ranges (49152-65535) and verify local host firewall exception before executing host standalone network tests.
- **Supporting Files & Commands:**
  `tests/test_host_network.py`
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Applies strictly to tests executed directly on the host machine outside the sandbox.
  - *Required Environment:* Host
  - *Excluded Environment:* Sandbox
  - *Required Scope:* Test
