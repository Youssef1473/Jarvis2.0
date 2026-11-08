# ENG-OS-03B2R — Receipt Canonical Payload & Safe Candidate Bootstrap
**Remediation and Verification Report**

- **Date:** 2026-11-06
- **Engineer & Architect:** Principal Database Reliability Engineer
- **Component:** Execution Evidence Storage & Candidate Registry (`tools/execution_evidence.py`, `tools/continuity_manager.py`, `tests/test_persistence_concurrency.py`)
- **Git Branch:** `feat/engineering-maturity-milestone`
- **Git HEAD SHA:** `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`
- **Status:** **COMPLETED & VERIFIED** (100% test pass rate across 272 continuous learning tests)

---

## 1. Executive Summary

Milestone **ENG-OS-03B2R** resolves the two targeted persistence integrity issues identified during the `ENG-OS-03B3` audit without altering the underlying authoritative SQLite architecture:
1. **Receipt Canonical Payload Integrity & Fail-Closed Verification (`tools/execution_evidence.py`):**
   - Enforced non-null canonical `payload_json` on receipt creation and registration.
   - Refactored `verify_receipt()` to validate against the canonical SQLite `payload_json` first.
   - Missing or unpopulated SQLite payload now explicitly fails closed with `RECEIPT_PAYLOAD_UNPOPULATED`.
   - Content digest divergence between canonical payload and registry row triggers `RECEIPT_PAYLOAD_MISMATCH`.
   - Identity mismatches trigger `RECEIPT_TAMPERED`.
   - Filesystem JSON artifacts remain projections: missing artifacts report `RECEIPT_ARTIFACT_MISSING`, and corrupted or tampered artifacts report `RECEIPT_TAMPERED` / `RECEIPT_MALFORMED`.
   - The returned `ExecutionReceipt` object is constructed directly from the canonical SQLite payload.
2. **Idempotent Receipt Migration Engine (`EvidenceStore.migrate_receipt_payloads`):**
   - Backfills legacy rows where `payload_json IS NULL` exclusively when the corresponding JSON artifact exists, parses, and matches the registry `receipt_id`, `execution_id`, and `content_digest`.
   - Preserves missing, corrupted, or conflicting artifacts without modification.
   - 100% idempotent and restartable.
3. **Safe Non-Destructive Candidate Bootstrap (`tools/continuity_manager.py`):**
   - Replaced unsafe `INSERT OR REPLACE` fallback with `_safe_bootstrap_candidate()`.
   - Uses `BEGIN IMMEDIATE` transactions to prevent destructive overwrites of existing canonical SQLite rows by unindexed disk artifacts.
   - If a candidate already exists in SQLite, read operations reread SQLite and return the canonical database payload.
   - Concurrent bootstrap races catch `sqlite3.IntegrityError`, roll back, and return the authoritative SQLite row.
   - Preserves conflicting disk files without modifying the database.
4. **Connection Durability & Standardized Connection Factories:**
   - Implemented centralized `_connect()` factories in both `FailureCandidatePipeline` and `EvidenceStore`.
   - Every connection explicitly executes:
     - `PRAGMA journal_mode = WAL;`
     - `PRAGMA synchronous = NORMAL;`
     - `PRAGMA busy_timeout = <timeout_ms>;`
   - Verified that newly opened connections reliably report `synchronous == 1` (`NORMAL`).

---

## 2. Technical Implementation Details

### A. Receipt Store Integrity (`tools/execution_evidence.py`)

#### Connection Factory
```python
def _connect(self, timeout: float = 15.0, isolation_level: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(self.registry_db_path, timeout=timeout, isolation_level=isolation_level)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)};")
    return conn
```

#### Fail-Closed Receipt Verification (`verify_receipt`)
```python
def verify_receipt(self, receipt_id: str) -> tuple[bool, str, ExecutionReceipt | None]:
    if not receipt_id:
        return False, "EMPTY_RECEIPT_ID", None

    with self._connect(timeout=5.0) as conn:
        cur = conn.execute("""
            SELECT execution_id, content_digest, artifact_path, payload_json
            FROM execution_receipts
            WHERE receipt_id = ?;
        """, (receipt_id,))
        row = cur.fetchone()

    if not row:
        return False, "RECEIPT_NOT_IN_REGISTRY", None

    reg_exec_id, reg_digest, rel_artifact, payload_json = row

    # 1. Canonical SQLite payload validation
    if payload_json is None or not str(payload_json).strip():
        return False, "RECEIPT_PAYLOAD_UNPOPULATED: Canonical SQLite payload is missing or NULL.", None

    try:
        canonical_data = json.loads(payload_json)
        canonical_receipt = ExecutionReceipt.from_dict(canonical_data)
    except (json.JSONDecodeError, OSError, ValueError, KeyError) as exc:
        return False, f"RECEIPT_MALFORMED: Canonical SQLite payload is malformed: {exc}", None

    if canonical_receipt.receipt_id != receipt_id or canonical_receipt.execution_id != reg_exec_id:
        return False, "RECEIPT_TAMPERED: Identity mismatch between registry row and canonical payload.", None

    if canonical_receipt.compute_content_digest() != reg_digest:
        return False, "RECEIPT_PAYLOAD_MISMATCH: Canonical payload digest does not match registry content_digest.", None

    # 2. Projection validation
    artifact_path = (self.evidence_dir / rel_artifact).resolve()
    if not artifact_path.exists() or not artifact_path.is_file():
        return False, f"RECEIPT_ARTIFACT_MISSING: '{artifact_path}' not found.", None

    try:
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
        disk_receipt = ExecutionReceipt.from_dict(data)
        disk_digest = disk_receipt.compute_content_digest()

        if disk_receipt.execution_id != reg_exec_id or disk_receipt.receipt_id != receipt_id:
            return False, f"RECEIPT_TAMPERED: Execution ID mismatch between artifact ({disk_receipt.execution_id}) and registry ({reg_exec_id}).", None

        if disk_digest != reg_digest or disk_digest != disk_receipt.content_digest:
            return False, "RECEIPT_TAMPERED: Artifact content digest mismatch with registry.", None

        return True, "VALID_STORED_RECEIPT", canonical_receipt
    except (json.JSONDecodeError, OSError, ValueError, KeyError) as exc:
        return False, f"RECEIPT_MALFORMED: Artifact projection file is malformed: {exc}", None
```

#### Migration & Backfill Engine (`migrate_receipt_payloads`)
```python
def migrate_receipt_payloads(self) -> dict[str, Any]:
    report = {"unfilled_found": 0, "backfilled": 0, "skipped_missing": [], "skipped_corrupt": [], "skipped_mismatch": []}
    with self._connect(timeout=15.0) as conn:
        cur = conn.cursor()
        cur.execute("SELECT receipt_id, execution_id, content_digest, artifact_path FROM execution_receipts WHERE payload_json IS NULL;")
        unfilled = cur.fetchall()
        report["unfilled_found"] = len(unfilled)
        for rcpt_id, exec_id, exp_digest, rel_art in unfilled:
            target_path = (self.evidence_dir / rel_art).resolve()
            if not target_path.exists() or not target_path.is_file():
                report["skipped_missing"].append(rcpt_id)
                continue
            try:
                raw_text = target_path.read_text(encoding="utf-8")
                rcpt = ExecutionReceipt.from_dict(json.loads(raw_text))
                if rcpt.receipt_id != rcpt_id or rcpt.execution_id != exec_id or rcpt.compute_content_digest() != exp_digest:
                    report["skipped_mismatch"].append(rcpt_id)
                    continue
                payload_str = json.dumps(rcpt.to_dict(), indent=2, ensure_ascii=False)
                conn.execute("UPDATE execution_receipts SET payload_json = ? WHERE receipt_id = ? AND payload_json IS NULL;", (payload_str, rcpt_id))
                report["backfilled"] += 1
            except Exception:
                report["skipped_corrupt"].append(rcpt_id)
                continue
        conn.commit()
    return report
```

---

### B. Safe Candidate Bootstrap (`tools/continuity_manager.py`)

#### Centralized Connection Factory
```python
def _connect(self, timeout: float = 15.0, isolation_level: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(self.registry_db_path, timeout=timeout, isolation_level=isolation_level)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)};")
    return conn
```

#### Safe Non-Destructive Bootstrap Strategy
```python
def _safe_bootstrap_candidate(self, candidate_id: str) -> tuple[LessonCandidate | None, int, str]:
    target = self.candidates_dir / f"{candidate_id}.json"
    if not target.exists() or not target.is_file():
        return None, 0, ""

    try:
        raw_text = target.read_text(encoding="utf-8")
        data = json.loads(raw_text)
        disk_cand = LessonCandidate.from_dict(data)
        if disk_cand.candidate_id != candidate_id:
            return None, 0, ""
        disk_digest = canonical_candidate_digest(disk_cand)
        payload_str = json.dumps(disk_cand.to_dict(), indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, OSError, ValueError, KeyError):
        return None, 0, ""

    now_iso = datetime.datetime.now(datetime.UTC).isoformat()
    with self._connect(timeout=15.0, isolation_level=None) as conn:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            cur = conn.execute("SELECT generation, content_digest, payload_json FROM candidate_records WHERE candidate_id = ?;", (candidate_id,))
            existing = cur.fetchone()

            if existing and existing[2]:
                conn.execute("COMMIT;")
                try:
                    auth_cand = LessonCandidate.from_dict(json.loads(existing[2]))
                    return auth_cand, existing[0], existing[1]
                except (json.JSONDecodeError, ValueError, KeyError):
                    return None, existing[0], existing[1]

            conn.execute("""
                INSERT INTO candidate_records (
                    candidate_id, generation, content_digest, payload_json, review_status,
                    has_verified_root_cause, artifact_path, created_at_utc, updated_at_utc, schema_version
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 1);
            """, (
                disk_cand.candidate_id, disk_digest, payload_str, disk_cand.review_status,
                1 if disk_cand.verified_root_cause else 0, target.name, now_iso, now_iso,
            ))
            conn.execute("COMMIT;")
            return disk_cand, 1, disk_digest
        except sqlite3.IntegrityError:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            with self._connect(timeout=5.0) as read_conn:
                cur = read_conn.execute("SELECT generation, content_digest, payload_json FROM candidate_records WHERE candidate_id = ?;", (candidate_id,))
                row = cur.fetchone()
                if row and row[2]:
                    try:
                        auth_cand = LessonCandidate.from_dict(json.loads(row[2]))
                        return auth_cand, row[0], row[1]
                    except (json.JSONDecodeError, ValueError, KeyError):
                        pass
            return None, 0, ""
```

---

## 3. Test Suite Verification (10 Scenarios Added)

Ten targeted concurrency and integrity scenarios were added to `tests/test_persistence_concurrency.py`:

| Scenario ID | Name & Description | Result | Execution Time |
| :---: | :--- | :---: | :---: |
| **Scenario 20** | `test_scenario_20_new_receipt_with_populated_payload`: newly registered receipt has non-null canonical payload and passes verification. | **PASSED** | <0.1s |
| **Scenario 21** | `test_scenario_21_existing_receipt_with_null_payload_rejected`: existing receipt with NULL payload rejected with `RECEIPT_PAYLOAD_UNPOPULATED`. | **PASSED** | <0.1s |
| **Scenario 22** | `test_scenario_22_valid_receipt_backfill`: migration backfills NULL payload from valid matching disk artifact and succeeds. | **PASSED** | <0.1s |
| **Scenario 23** | `test_scenario_23_missing_receipt_artifact`: missing artifact fails verification but is restored via `reconcile_receipt_projections`. | **PASSED** | <0.1s |
| **Scenario 24** | `test_scenario_24_mismatched_receipt_digest`: digest mismatches in canonical payload or artifact trigger `RECEIPT_PAYLOAD_MISMATCH` and `RECEIPT_TAMPERED`. | **PASSED** | <0.1s |
| **Scenario 25** | `test_scenario_25_repeated_migration_idempotent`: repeated migration runs return 0 unpopulated and 0 backfilled (idempotent). | **PASSED** | <0.1s |
| **Scenario 26** | `test_scenario_26_concurrent_candidate_bootstrap`: concurrent independent processes bootstrap unindexed candidate safely without duplicates. | **PASSED** | ~0.5s |
| **Scenario 27** | `test_scenario_27_existing_sqlite_row_vs_stale_json_projection`: SQLite Gen 2 row is not overwritten by stale Gen 1 disk artifact on read. | **PASSED** | <0.1s |
| **Scenario 28** | `test_scenario_28_receipt_verification_after_restart`: receipt verification succeeds across distinct `EvidenceStore` instances. | **PASSED** | <0.1s |
| **Scenario 29** | `test_scenario_29_connection_pragma_values_on_new_connections`: newly opened connections explicitly report `synchronous == 1` (`NORMAL`). | **PASSED** | <0.1s |

---

## 4. Empirical Verification Evidence

### A. Full Continuous-Learning Test Suite (272 Tests Across 7 Files)
```powershell
pytest tests/test_persistence_concurrency.py tests/test_execution_evidence.py tests/test_continuity_and_lifecycle.py tests/test_lesson_candidate.py tests/test_failure_collector.py tests/test_hook_integration.py tests/test_context_router.py
```
- **Result:** **272 passed in 40.59s** (Exit code: `0`)
- **Suite Breakdown:**
  - `tests/test_persistence_concurrency.py`: **29 passed** in 5.45s
  - `tests/test_execution_evidence.py`: **29 passed** in 29.76s
  - `tests/test_continuity_and_lifecycle.py`: **28 passed** in 0.85s
  - `tests/test_lesson_candidate.py`: **78 passed** in 1.24s
  - `tests/test_failure_collector.py`: **41 passed** in 0.18s
  - `tests/test_hook_integration.py`: **7 passed** in 2.11s
  - `tests/test_context_router.py`: **60 passed** in 0.26s

### B. Frozen Retrieval Benchmark (38 Scenarios)
```powershell
python tools/evaluate_lesson_retrieval.py
```
- **Holdout Evaluation (28 Scenarios):** 1.0000 Precision, 1.0000 Capacity-Bounded Recall, 1.0000 nDCG@3, 0.0000 Irrelevant Injection.
- **Baseline Evaluation (10 Scenarios):** 1.0000 Precision, 1.0000 Recall, 1.0000 nDCG@3, 0.0000 Irrelevant Injection.
- **Exit Code:** `0`

### C. Static Diagnostics & Code Integrity
- `python tools/lint.py check tools/continuity_manager.py tools/execution_evidence.py tests/test_persistence_concurrency.py`: **All checks passed!** (Exit code: `0`)
- `python tools/diagnostics.py check tools/continuity_manager.py tools/execution_evidence.py tests/test_persistence_concurrency.py`: **Ruff: PASSED, AST-Grep: PASSED** (Exit code: `0`)
- `python tools/continuity_manager.py verify-state`: **PROJECT CONTINUITY REPORT: ALIGNED** (Exit code: `0`)

---

## 5. Artifact & Subsystem Preservation

- **Candidates Registry & Files (7 items):** All SHA-256 hashes verified identical to baseline.
- **Learning Events (8 items):** All SHA-256 hashes verified identical to baseline.
- **Frozen Retrieval Files (7 items):** Zero edits; checksums verified identical to baseline.
- **Permanent Memory (`docs/agent/LESSONS.md`):** Zero edits; SHA-256: `bf11bc69ef8730bb03b40450c18aa04ab16442cf2d7cb5386ee2f342f3ffe670`.
- **Permanent-Memory Barrier:** `PROMOTION_PROHIBITED` strictly enforced; candidate promotion remains prohibited in-process.

---

## 6. Summary of Remediations

| Issue Identified in ENG-OS-03B3 | Remediation Applied in ENG-OS-03B2R | Verification Status |
| :--- | :--- | :---: |
| **Receipt `payload_json` Nullability** | `verify_receipt` enforces fail-closed verification (`RECEIPT_PAYLOAD_UNPOPULATED`). | **RESOLVED** (Scenario 21) |
| **Receipt Migration Fallback** | Added `migrate_receipt_payloads()` with strict identity and digest pre-validation. | **RESOLVED** (Scenario 22 & 25) |
| **Unsafe Candidate `INSERT OR REPLACE`** | Replaced with non-destructive conditional bootstrap (`_safe_bootstrap_candidate`). | **RESOLVED** (Scenario 26 & 27) |
| **Connection `synchronous` Pragma Reset** | Centralized `_connect()` factories set `PRAGMA synchronous = NORMAL` on every connection. | **RESOLVED** (Scenario 29) |
