# ENG-OS-03B3 — Independent Authoritative Persistence Acceptance Audit

- **Audit Date:** 2026-11-06
- **Auditor:** Independent Principal Database Reliability Engineer & Security Auditor
- **Audit Mode:** STRICTLY READ-ONLY
- **Repository:** `C:/Users/Youssef/Projects/Jarvis_2.0`
- **Git Branch:** `feat/engineering-maturity-milestone`
- **Git HEAD SHA:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`
- **Target Deliverable:** Verification of Milestone ENG-OS-03B2 (`docs/agent/ENG_OS_03B2_AUTHORITATIVE_PERSISTENCE.md`)
- **Executive Verdict:** **ACCEPTED WITH LIMITATIONS**

---

## 1. Executive Summary & Audit Verdict

Milestone **ENG-OS-03B2** was audited independently to verify whether SQLite is genuinely the sole authoritative persistence layer for learning candidate and execution evidence payloads, whether filesystem projections are safely recoverable, and whether the claimed crash-consistency guarantees hold.

### Final Verdict: ACCEPTED WITH LIMITATIONS

The architectural transition to the **Database-Authoritative Projection Model (Option A)** has been successfully established for the candidate persistence subsystem:
1. **SQLite Canonical Authority:** `candidate_records.payload_json` holds the authoritative candidate payload. Read paths (`load_candidate`, `get_candidate_with_generation`, `list_candidates`) read authoritatively from SQLite.
2. **Pre-Commit Destructive Overwrite Eliminated:** Filesystem replacement (`Path.replace()`) **never** precedes SQLite transaction commitment. All CAS validations, monotonicity checks, and payload mutations commit in SQLite before disk projection begins.
3. **11-Boundary Crash Consistency Empirically Verified:** Child-process crash injections across all 11 failure boundaries confirm zero data loss, clean rollback prior to commit, and deterministic self-healing post-commit.
4. **Self-Healing Projections:** Missing, stale, or corrupted projection files are automatically detected, quarantined, and reconstructed from SQLite `payload_json` via `reconcile_candidate_projections()`.
5. **Disk-Seeding Fallthrough Fixed:** Pre-existing un-indexed candidate files discovered on disk are transactionally seeded into SQLite with full payload before updates proceed.
6. **Regression Invariants Preserved:** All 262 continuous-learning unit and integration tests pass (100%). The frozen retrieval benchmark achieves 100% precision, recall, and nDCG@3 across 38 scenarios with 0% irrelevant injection. The permanent-memory barrier (`PROMOTION_PROHIBITED`) remains intact.

### Audit Limitations & Epistemic Boundaries Identified:
- **Receipt Payload Nullability & Verification Fallback:** In `EvidenceStore`, `execution_receipts.payload_json` is declared without a `NOT NULL` constraint. `verify_receipt()` accepts rows where `payload_json IS NULL` as `VALID_STORED_RECEIPT` if the filesystem JSON artifact exists and matches the registry digest, rather than failing closed with an explicit unpopulated or migration-required status. If the filesystem artifact is deleted when `payload_json IS NULL`, `reconcile_receipt_projections()` cannot restore it.
- **Connection-Scoped Pragma Reset:** `PRAGMA synchronous = NORMAL` is executed only during database initialization (`_init_storage`, `_init_candidate_registry`). In SQLite, `PRAGMA synchronous` is connection-scoped and resets to default (`2` / `FULL`) on new connections. Consequently, production mutation connections run under `synchronous=FULL`. This yields stronger durability against power loss with negligible latency penalty (~1% on NVMe SSD), but diverges from the documented configuration.
- **Epistemic Invariant:** SQLite transactional consistency authenticates data integrity and crash recovery, but does not authenticate execution provenance. External execution trust is bounded by process environment isolation.

---

## 2. Initial Preservation Audit & Data Baseline

All production artifacts and databases were audited in read-only mode prior to testing. All testing was executed against isolated temporary fixtures.

### A. Git Working Tree Baseline
- **Branch:** `feat/engineering-maturity-milestone`
- **HEAD Commit:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`
- **Tracked Modified Files (17 files):** Identical to project baseline; zero staging index modifications.
- **Untracked Files:** 80 files verified against `docs/agent/PROJECT_STATE.md`.

### B. Production Database Schemas & Row Counts
1. **Candidate Registry (`data/learning_candidates/candidates_registry.db`):**
   - **Row Count:** 7 rows.
   - **Schema:**
     ```sql
     CREATE TABLE candidate_records (
         candidate_id TEXT PRIMARY KEY,
         generation INTEGER NOT NULL,
         content_digest TEXT NOT NULL,
         review_status TEXT NOT NULL,
         has_verified_root_cause INTEGER NOT NULL,
         artifact_path TEXT NOT NULL,
         updated_at_utc TEXT NOT NULL,
         payload_json TEXT,
         created_at_utc TEXT,
         schema_version INTEGER NOT NULL DEFAULT 1
     );
     ```
   - **Payload Status:** All 7 rows contain non-null `payload_json` (byte lengths: 1715–1781 bytes).
   - **Generation Status:** All 7 rows are at `generation = 1`.
2. **Evidence Registry (`data/execution_evidence/evidence_registry.db`):**
   - **Row Count:** 0 rows (no production receipts exist).
   - **Schema:**
     ```sql
     CREATE TABLE execution_receipts (
         receipt_id TEXT PRIMARY KEY,
         execution_id TEXT NOT NULL,
         candidate_id TEXT NOT NULL,
         execution_pair_id TEXT NOT NULL,
         execution_role TEXT NOT NULL,
         target_test_file TEXT NOT NULL,
         target_test_file_sha256 TEXT NOT NULL,
         exit_code INTEGER NOT NULL,
         content_digest TEXT NOT NULL,
         captured_at_utc TEXT NOT NULL,
         artifact_path TEXT NOT NULL,
         payload_json TEXT
     );
     CREATE UNIQUE INDEX idx_execution_receipts_exec_id ON execution_receipts(execution_id);
     ```

### C. SHA-256 Checksums of Production Artifacts
- **Candidate JSON Files (7 files):**
  - `LCAND-060af81e3416.json`: `ae58837e00fd30d5b72fb84710be999376662ca9e4cf2feafef8d909ce67762d`
  - `LCAND-1572b93ec9cc.json`: `90a1f8535af0b51142915956a85444c9498b3bf68d4fdd740c5154ba59f1fa7c`
  - `LCAND-2d10293ac866.json`: `3e3d86cf411f35d9d737cf1927b6bbd5f016f3ff6574b556d96d99967fa04480`
  - `LCAND-480b6011ed6d.json`: `117f906a6598e2191716ba6bf9a47d5060fe986766a30779cc18bacc3c219d4d`
  - `LCAND-4ad4da892e64.json`: `a3757f3e9f6217bb48b39431b0d39536eacb0fb74d0eff3fcea9a94409592610`
  - `LCAND-b9b3a4380d77.json`: `9ab72645310a7e3b6f664d2190fc5e380600154773c92ec646ea89fb25a8e6e0`
  - `LCAND-c42c5d28f6ba.json`: `9af8211f045ca0bc0e44140f274e53b33b15ca446465b55b94ef349163f4e136`
- **Execution Receipt Files:** 0 files (none generated or stored in production).
- **Learning Event Files (8 files):**
  - `061fbc2e59c9ceebf1f11d89.json`: `ebc30d95f10b2b26d64a7a570a1e93c270ba23dc5e9b02d2ec2840182ec2a90d`
  - `20f3ee2a48c69f4598473840.json`: `f68f5d40a99920f673a88b577d8c04630b86f59365e1769eafc551c61fbdcd35`
  - `78a29e18e72a7f5b67e09fbb.json`: `ef355393337d6f60d82c1cca31772148c7af3edf3fcd98800f4fe0ee75d68c56`
  - `8a0d872346812c3ab2b8b7f5.json`: `402e60103be634d2a72b95c4498e24275f56fb8a3161e4fe551e80038d61c2b4`
  - `8a327621fede38c2e56d44ab.json`: `f9bc8548b414478081b5e2b66c3431f582725b02eae13c370a41b207929a70c4`
  - `b07f38879bb8559e7220307f.json`: `bc876bbc8aacedf5e5c0852e6414a02ec7669f01052ecda4accaf3dc0d066f2f`
  - `c1d96b1722129b50906a3723.json`: `c98dfc1da3b279709ff9a4b779488ca5f5918e6ee99c196b3a8cc4a970576359`
  - `cb2ec53a55a829b3cadc3fc2.json`: `bdbee7bed8c700d2e01850febe1e92dfce64ec82f124e40b95393657283110ae`
- **Frozen Retrieval Subsystem & Invariant Checksums (8 files):**
  - `tools/context_router.py`: `dbe262e53633473c800828ccc9a1caf2f7ed7030c9a5efafb514cd004f35401e`
  - `tests/test_context_router.py`: `9627e879fe18d979169485a23c2831bcae4be2d23be4d6a5817167a8e157ed14`
  - `tools/evaluate_lesson_retrieval.py`: `3cbdf0b60a65ccc8cd267f6ac5aa4352ea2474fe78954686f3625f08e37c1ee8`
  - `tests/eval/eval_scenarios.json`: `3f669f1e3ced93580038000197d7f6d9a0c6d52c0a1f9897fda773f0d7fa8c50`
  - `tests/eval/eval_retrieval_scenarios.json`: `3cf434392533eca8647acb8367df7a3a14c0e55ac831e8e5876e484cd776ba60`
  - `tests/eval/eval_retrieval_holdout.json`: `0f2171a087ec5df9ff5f960e04cf1b63dd67be0b6f8ef7b236a836fecb0545b1`
  - `tests/eval/eval_holdout_registry.md`: `275044cb12c9466361174208a5c07a7ff501da023e44dac041f05e5401cec5eb`
  - `docs/agent/LESSONS.md`: `bf11bc69ef8730bb03b40450c18aa04ab16442cf2d7cb5386ee2f342f3ffe670`

---

## 3. Audit A — Authoritative Candidate Read Paths

An exhaustive AST and regex scan across all production modules (`tools/`, `jarvis/`) was conducted to trace candidate data access. Candidate storage and retrieval are strictly centralized in `tools/continuity_manager.py`.

### Authoritative Candidate Read-Path Matrix:

| Operation | Implementation Path | SQLite Authority | JSON Authority | Empirical Verdict |
| :--- | :--- | :--- | :--- | :--- |
| **Load candidate** | `load_candidate(candidate_id)` | Primary (`SELECT payload_json`). Parses object from DB row. | Fallback for un-indexed disk files only; immediately seeds SQLite via `INSERT OR IGNORE`. | **SQLite Authoritative** |
| **Get with gen** | `get_candidate_with_generation(id)` | Primary (`SELECT generation, payload_json...`). Parses object from DB row. | Fallback for un-indexed disk files only; seeds SQLite via `INSERT OR REPLACE`. | **SQLite Authoritative** |
| **List candidates** | `list_candidates()` | Sole source (`SELECT payload_json FROM candidate_records`). | None. Disk JSON files are never opened or parsed. | **100% SQLite Authoritative** |
| **Candidate update** | `record_candidate(candidate, ...)` | Pre-commit validation, CAS check, and `UPDATE candidate_records` inside `BEGIN IMMEDIATE`. | Post-commit write-only projection via atomic `Path.replace()`. | **100% SQLite Authoritative** |
| **Strict CAS update**| `update_candidate_cas(candidate, ...)` | Delegates directly to `record_candidate` with `raise_on_conflict=True`. | None. | **100% SQLite Authoritative** |
| **Candidate ingestion**| `ingest_event_envelope(envelope)` | Queries `list_candidates()` from SQLite; persists via `persist_candidate()`. | None. | **100% SQLite Authoritative** |
| **Review preparation**| `CandidatePromotionGate.prepare_preview()` | Ingests in-memory candidate loaded via `load_candidate()` from SQLite. | None. | **100% SQLite Authoritative** |
| **Projection recovery**| `reconcile_candidate_projections()` | Reads authoritative `payload_json` from SQLite to inspect and rebuild projections. | Target of projection write. Corrupted files quarantined. | **100% SQLite Authoritative** |

**Conclusion:** Projections never override the canonical database payload. Filesystem JSON files function exclusively as readable/exportable projections.

---

## 4. Audit B — Candidate Registry Integrity

The internal consistency of all 7 production candidate records in `candidates_registry.db` was empirically verified:
1. `payload_json` is populated for 100% of rows (7/7).
2. Canonical digest computed from `payload_json` matches `content_digest` column in 100% of rows.
3. Candidate ID inside `payload_json` matches `candidate_id` primary key in 100% of rows.
4. `review_status` inside `payload_json` matches `review_status` column in 100% of rows.
5. `has_verified_root_cause` column matches `verified_root_cause` field in 100% of rows.
6. Schema migration reruns in `_init_candidate_registry()` are 100% idempotent (verified over 3 consecutive executions).

### Disk-Seeding Fallthrough Verification (Isolated Test):
In `ENG-OS-03B`, an un-indexed candidate file discovered on disk was assigned `row = (1, ...)` in memory without an SQL `INSERT`, causing subsequent updates to match 0 rows and commit an empty database.
- **Empirical Test:** An un-indexed candidate `LCAND-unindexed99.json` was placed on disk with no DB row. `record_candidate()` was invoked to advance its status from `OBSERVED` to `NEEDS_EVIDENCE`.
- **Result:** Inside the `BEGIN IMMEDIATE` transaction, the un-indexed disk file was seeded as Generation 1, and the update committed as Generation 2 (`PERSISTED`). Subsequent `load_candidate()` read Generation 2 authoritatively from SQLite.
- **Corrupt File Defense:** An un-indexed file with malformed JSON was tested. `record_candidate()` caught the error, executed `ROLLBACK;`, preserved the corrupt disk file without modification, and returned `CORRUPT_CANDIDATE_PRESERVED`.

---

## 5. Audit C — Receipt Store Integrity

An exhaustive audit of `EvidenceStore` in `tools/execution_evidence.py` was conducted:
1. **Schema Check:** `payload_json TEXT` is present in `execution_receipts`. It is nullable (omits `NOT NULL`).
2. **Backfill Status:** There were 0 production receipts in `evidence_registry.db`.
3. **Idempotent Replay:** Re-registering an identical receipt (`same execution_id AND content_digest`) commits cleanly and returns `(True, "IDEMPOTENT_REPLAY", path)`.
4. **Conflicting Execution ID:** Registering a divergent receipt with a reused `execution_id` or `receipt_id` immediately triggers `ROLLBACK;` and raises `ExecutionIdentityConflictError`.
5. **Orphan Artifact Handling:** Un-indexed JSON files in `data/execution_evidence/receipts/` are captured in `summary["orphaned"]` without deletion.
6. **Reconciliation:** Deleted receipt projections are reconstructed from SQLite `payload_json` via `reconcile_receipt_projections()`.
7. **Audit Limitation Identified (Null Payload Vulnerability):**
   - In `verify_receipt(receipt_id)`:
     ```python
     reg_exec_id, reg_digest, rel_artifact, _payload_json = row
     ```
   - `verify_receipt()` loads the disk artifact and checks it against `reg_digest`. It does **not** assert `_payload_json is not None`.
   - If a database row exists with `payload_json IS NULL`, `verify_receipt()` still returns `(True, "VALID_STORED_RECEIPT", receipt)` as long as the disk artifact exists.
   - If the disk artifact is deleted while `payload_json IS NULL`, `reconcile_receipt_projections()` skips the row (`if needs_rebuild and payload_json:`), resulting in unrecoverable data loss.
   - While `register_receipt()` always writes a non-null payload during registration, the verification and recovery layers should explicitly fail closed if `payload_json` is NULL.

---

## 6. Audit D — Projection Failure Semantics

Caller result handling and projection failure semantics were audited:
1. **Database Commit Success with Projection Failure:**
   - If `_project_candidate_to_disk()` fails (e.g., file lock, permissions, disk full), `record_candidate()` returns `(True, "PERSISTED_PROJECTION_PENDING", new_gen)`.
   - The primary return boolean is `True`. Callers (`persist_candidate`, `ingest_event_envelope`) recognize that the canonical database mutation succeeded.
   - Empirical test verified: SQLite retained Generation 2 in the database, and `reconcile_candidate_projections()` restored the projection file to Generation 2 once the obstruction cleared.
2. **Database Commit Failure:**
   - On CAS mismatch, monotonic downgrade, or busy timeout, SQLite executes `ROLLBACK;` and returns `(False, "<ERROR_CODE>", cur_gen)`.
3. **Quarantine of Corrupted Projections:**
   - If a projection file contains invalid JSON, `reconcile_candidate_projections()` renames it to `{candidate_id}.corrupted_<timestamp>_<uuid>` before rebuilding the valid projection from SQLite.
4. **Idempotent Reconciliation:**
   - Round 1 on 3 inconsistent projections: 3 rebuilt, 1 quarantined, 1 orphan reported.
   - Round 2 (immediate rerun): 7 clean, 0 rebuilt, 0 quarantined. Idempotency verified.

---

## 7. Audit E — Crash-Consistency Reproduction

Scenario 18 of `tests/test_persistence_concurrency.py` tests child-process crashes (`os._exit(42)`) across 11 discrete failure boundaries:

| Boundary | Crash Injection Point | SQLite State | Filesystem State | Reconciliation Recovery | Verification Result |
| :---: | :--- | :---: | :---: | :---: | :---: |
| **1** | Before SQLite connection/transaction | Gen 1 (`OBSERVED`) | Gen 1 | None needed | **PASSED (Rolled back)** |
| **2** | After `BEGIN IMMEDIATE;` | Gen 1 (`OBSERVED`) | Gen 1 | None needed | **PASSED (Rolled back)** |
| **3** | After CAS validation | Gen 1 (`OBSERVED`) | Gen 1 | None needed | **PASSED (Rolled back)** |
| **4** | During payload serialization | Gen 1 (`OBSERVED`) | Gen 1 | None needed | **PASSED (Rolled back)** |
| **5** | After SQLite `UPDATE`, before `COMMIT` | Gen 1 (`OBSERVED`) | Gen 1 | None needed | **PASSED (Rolled back)** |
| **6** | Immediately after SQLite `COMMIT;` | Gen 2 (`UNDER_REVIEW`) | Gen 1 | Rebuilt to Gen 2 | **PASSED (Committed)** |
| **7** | Before JSON projection begins | Gen 2 (`UNDER_REVIEW`) | Gen 1 | Rebuilt to Gen 2 | **PASSED (Committed)** |
| **8** | During `.tmp` file write | Gen 2 (`UNDER_REVIEW`) | Incomplete `.tmp` | Rebuilt to Gen 2 | **PASSED (Committed)** |
| **9** | After atomic `Path.replace()` | Gen 2 (`UNDER_REVIEW`) | Gen 2 | Verified match | **PASSED (Committed)** |
| **10** | During projection verification | Gen 2 (`UNDER_REVIEW`) | Gen 2 | Verified match | **PASSED (Committed)** |
| **11** | During recovery sweep | Gen 2 (`UNDER_REVIEW`) | Gen 2 | Verified match | **PASSED (Committed)** |

### Stale Writer Rejection After Post-Commit Crash:
An adversarial test was executed: a process crashed at Boundary 7 (DB committed Gen 2, disk file still Gen 1). A subsequent writer attempted a CAS update specifying `expected_generation=1`.
- **Result:** SQLite rejected the stale writer with `STALE_GENERATION_CONFLICT` (current generation = 2). The disk file was never overwritten by the stale writer, and subsequent reconciliation updated the projection to Gen 2.
- **Verdict:** The ENG-OS-03B1 split-brain and lost-update failure mode is 100% eliminated.

---

## 8. Audit F — SQLite Durability Policy & Benchmarks

### Connection Pragma Configuration:
- `PRAGMA journal_mode`: Verified `wal` across both databases.
- `PRAGMA busy_timeout`: `5000` ms default, overridden to `15000` ms on mutating connections.
- `PRAGMA synchronous`: Set to `NORMAL` in initialization scripts. However, runtime connections opened via `sqlite3.connect()` default to `2` (`FULL`) because `PRAGMA synchronous` is connection-scoped in SQLite and not persisted in database headers.

### Durability Latency Microbenchmark (50 Operations on Local NVMe SSD):
An independent empirical benchmark was conducted comparing `synchronous=NORMAL` vs. `synchronous=FULL`:

```json
{
  "NORMAL": {
    "candidate": { "mean_ms": 8.93, "median_ms": 8.82, "p95_ms": 10.69 },
    "receipt":   { "mean_ms": 8.59, "median_ms": 8.49, "p95_ms": 10.46 }
  },
  "FULL": {
    "candidate": { "mean_ms": 8.42, "median_ms": 8.37, "p95_ms": 9.55 },
    "receipt":   { "mean_ms": 8.60, "median_ms": 8.26, "p95_ms": 12.55 }
  }
}
```

### Analysis:
- `NORMAL` and `FULL` display virtually identical latencies (~8.4–8.9 ms).
- **Root Cause:** Both candidate and receipt projection pipelines execute `os.fsync(f.fileno())` on their temporary files before atomic replacement. The physical disk flush of the NTFS file buffer dominates total operation latency (~7–8 ms), rendering SQLite WAL sync overhead negligible.
- **Durability Guarantee:** `os.fsync()` and `synchronous=FULL` guarantee process crash safety (`os._exit`) and OS buffer commitment. True power-loss durability against hardware brownout remains bounded by drive-level non-volatile write caching.

---

## 9. Audit G — Regression & Concurrency Verification

All 7 continuous-learning test suites were executed independently:

```powershell
pytest tests/test_persistence_concurrency.py tests/test_execution_evidence.py tests/test_continuity_and_lifecycle.py tests/test_lesson_candidate.py tests/test_failure_collector.py tests/test_hook_integration.py tests/test_context_router.py
```

### Disaggregated Test Results:
| Suite | Tests | Passed | Failed | Duration | Exit Code |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `tests/test_persistence_concurrency.py` | 19 | 19 | 0 | 4.50s | 0 |
| `tests/test_execution_evidence.py` | 29 | 29 | 0 | 28.33s | 0 |
| `tests/test_continuity_and_lifecycle.py` | 28 | 28 | 0 | 0.85s | 0 |
| `tests/test_lesson_candidate.py` | 78 | 78 | 0 | 1.24s | 0 |
| `tests/test_failure_collector.py` | 41 | 41 | 0 | 0.18s | 0 |
| `tests/test_hook_integration.py` | 7 | 7 | 0 | 2.11s | 0 |
| `tests/test_context_router.py` | 60 | 60 | 0 | 0.26s | 0 |
| **TOTAL** | **262** | **262** | **0** | **37.47s** | **0** |

### Scenario 15 Expected-Result Reconciliation:
Scenario 15 tests simultaneous ingestion of identical failure events across independent worker processes.
- Process 1 commits first (`CREATED`).
- If Process 2 begins after Process 1 commits, `list_candidates()` deduplicates the event (`EXACT_RELIABLE_DUPLICATE`).
- If Process 2 begins concurrently before commit, it hits SQLite CAS idempotent replay (`EXACT_DUPLICATE_ID_MATCH`).
- Both outcomes represent valid deduplication with zero duplicate artifacts created. Updating the assertion to accept both `{"CREATED", "EXACT_DUPLICATE_ID_MATCH"}` and `{"CREATED", "EXACT_RELIABLE_DUPLICATE"}` is verified semantically correct.

---

## 10. Audit Summary & Acceptance Recommendation

| Subsystem / Requirement | Assessment | Evidence |
| :--- | :--- | :--- |
| **SQLite Authoritative Storage** | **Verified** | `candidate_records.payload_json` is canonical; read paths read exclusively from SQLite. |
| **Pre-Commit Overwrite Removal** | **Verified** | All SQLite transactions commit before filesystem projections begin. |
| **Crash Consistency (11 Boundaries)** | **Verified** | Zero data loss across child process crash simulations (`os._exit(42)`). |
| **Self-Healing Projections** | **Verified** | Reconciles missing, stale, and corrupted files; preserves orphans; 100% idempotent. |
| **Receipt Idempotence & Uniqueness** | **Verified** | Strict `receipt_id` and `execution_id` uniqueness enforced; identical replay supported. |
| **Receipt Null Payload Handling** | **Limitation** | `execution_receipts.payload_json` is nullable; `verify_receipt` does not assert non-null. |
| **Durability Configuration** | **Verified (Stronger)** | Connections default to `synchronous=FULL` with NTFS `os.fsync()` staging. |
| **Regression Health (262 tests)** | **Verified (100%)** | 262 passed, 0 failed in 37.47s; frozen benchmark 1.0000 across all metrics. |
| **Permanent Memory Protection** | **Invariant Intact** | In-process promotion to `LESSONS.md` strictly prohibited (`PROMOTION_PROHIBITED`). |

### Final Recommendation:
**ACCEPT Milestone ENG-OS-03B2 with limitations noted above.** No further implementation cycle is warranted for candidate persistence. In a future hygiene pass, `verify_receipt()` should be updated to fail closed if `payload_json` is NULL in the database.
