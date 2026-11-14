"""Jarvis Engineering OS — Cross-Session Continuity & Evidence-Gated Learning Manager (ENG-OS-02 Phase 3D2).

Integrates persistent project continuity mechanisms with the evidence-gated continuous-learning pipeline:
Observed Event -> Evidence Collection -> Candidate Generation -> Root-Cause Verification ->
Independent Review -> Explicit Human Approval -> Controlled Lesson Promotion -> Future Session Retrieval.

Core Responsibilities:
1. Workstream A (Project Continuity & Staleness Engine):
   - Programmatically validates docs/agent/PROJECT_STATE.md against live Git reality.
   - Detects drift in branch, HEAD commit, tracked modified files, and untracked inventory.
   - Detects staleness and broken references (files mentioned in state docs that do not exist on disk).
   - Produces an explicit uncertainty state (FAILED_SAFE / UNVERIFIED_STATE) if state artifacts are missing/corrupt.
2. Workstream B (Failure-to-Candidate Pipeline):
   - Ingests EventEnvelope JSONs recorded by tools/failure_collector.py.
   - Enforces three-layer epistemic separation:
     * Layer A: Directly observed facts (tool name, exit code, failure signal, evidence refs).
     * Layer B: Hypotheses and alternative explanations (defaulting strictly to INCONCLUSIVE).
     * Layer C: Verified root cause and reproduction procedure (requires explicit empirical proof).
   - Deduplicates candidates across sessions using deterministic content hashes and event IDs.
   - Persists candidates into a persistent cross-session candidate repository without polluting LESSONS.md.
3. Workstream C (Approval & Promotion Gate):
   - Generates non-writing promotion preview diffs and unforgeable Action Manifest Digests.
   - Binds human approval tokens to exact candidate content and diff hashes; any post-preview modification invalidates approval.
   - Strictly fails closed against in-process autonomous promotion (PROMOTION_PROHIBITED).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

# Intra-process candidate persistence synchronization lock
_PERSISTENCE_LOCK = threading.RLock()


class StaleVersionConflictError(ValueError):
    """Raised when a candidate update specifies a stale generation or mismatched digest."""


class TerminalStateError(ValueError):
    """Raised when an update is attempted on a candidate in a terminal state (REJECTED, SUPERSEDED)."""


class CandidateMonotonicityError(ValueError):
    """Raised when an update attempts to downgrade review_status or verified_root_cause."""


def canonical_candidate_digest(candidate: LessonCandidate | dict[str, Any]) -> str:
    """Computes a deterministic canonical SHA-256 digest over all candidate fields."""
    raw = candidate.to_dict() if isinstance(candidate, LessonCandidate) else dict(candidate)
    canonical_json = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

from tools.failure_collector import (
    EventEnvelope,
)
from tools.lesson_candidate import (
    LessonCandidate,
    _prepare_promotion_preview_with_root,
    check_candidate_deduplication,
    compute_candidate_hash,
    create_candidate_from_event,
    is_monotonic_transition,
    prepare_promotion_preview,
)

DEFAULT_CANDIDATES_DIR = WORKSPACE_ROOT / "data" / "learning_candidates"
DEFAULT_EVENTS_DIR = WORKSPACE_ROOT / "data" / "learning_events"
DEFAULT_PROJECT_STATE_FILE = WORKSPACE_ROOT / "docs" / "agent" / "PROJECT_STATE.md"
DEFAULT_LESSONS_FILE = WORKSPACE_ROOT / "docs" / "agent" / "LESSONS.md"


def get_approved_event_directories(
    custom_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> list[Path]:
    """Returns an ordered, deduplicated list of safe directories for failure events.

    Order of priority:
    1. Explicit custom directory (if specified and safe)
    2. JARVIS_LEARNING_EVENTS_DIR environment variable (if specified and safe)
    3. Workspace repository default <workspace_root>/data/learning_events
    4. Active session artifact directories <appDataDir>/brain/*/learning_events
    """
    dirs: list[Path] = []
    seen: set[str] = set()

    def _add_if_safe(p: Path | str | None):
        if not p:
            return
        try:
            cand = Path(p).resolve()
            cand_str = str(cand).lower()
            if cand_str not in seen and cand.exists() and cand.is_dir():
                seen.add(cand_str)
                dirs.append(cand)
        except (OSError, ValueError):
            pass

    if custom_dir:
        _add_if_safe(custom_dir)

    env_dir = os.environ.get("JARVIS_LEARNING_EVENTS_DIR")
    if env_dir:
        _add_if_safe(env_dir)

    root = Path(workspace_root or WORKSPACE_ROOT).resolve()
    _add_if_safe(root / "data" / "learning_events")

    # Discover any active session brain artifact directories when not confined to explicit workspace/custom dir
    if workspace_root is None and custom_dir is None:
        brain_dir = Path.home() / ".gemini" / "antigravity" / "brain"
        if brain_dir.exists():
            for p in brain_dir.glob("*/learning_events"):
                _add_if_safe(p)

    return dirs


# =============================================================================
# Workstream A: Project Continuity & Staleness Engine
# =============================================================================

class ProjectContinuityEngine:
    """Verifies repository reality against persistent project state documents."""

    @staticmethod
    def get_live_git_state(workspace_root: Path | None = None) -> dict[str, Any]:
        """Captures live git reality: branch, HEAD SHA, tracked modified files, and untracked inventory."""
        root = Path(workspace_root or WORKSPACE_ROOT).resolve()
        try:
            branch = subprocess.check_output(
                ["git", "branch", "--show-current"],
                cwd=str(root),
                text=True,
                stderr=subprocess.PIPE,
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root),
                text=True,
                stderr=subprocess.PIPE,
            ).strip()
            status_lines = subprocess.check_output(
                ["git", "status", "--short", "--untracked-files=all"],
                cwd=str(root),
                text=True,
                stderr=subprocess.PIPE,
            ).splitlines()

            tracked_modified = []
            untracked = []
            staged = []

            for line in status_lines:
                if len(line) < 4:
                    continue
                code = line[:2]
                path_str = line[3:].strip()
                if code.startswith("?") or code.endswith("?"):
                    untracked.append(path_str)
                else:
                    if code[0] in ("M", "A", "D", "R", "C"):
                        staged.append(path_str)
                    if code[1] in ("M", "D"):
                        tracked_modified.append(path_str)

            return {
                "available": True,
                "branch": branch,
                "head": head,
                "tracked_modified_count": len(tracked_modified),
                "untracked_count": len(untracked),
                "staged_count": len(staged),
                "tracked_modified_files": sorted(tracked_modified),
                "untracked_files": sorted(untracked),
                "staged_files": sorted(staged),
            }
        except (subprocess.SubprocessError, OSError) as e:
            return {
                "available": False,
                "error": f"{type(e).__name__}: {e}",
                "branch": None,
                "head": None,
                "tracked_modified_count": 0,
                "untracked_count": 0,
                "staged_count": 0,
                "tracked_modified_files": [],
                "untracked_files": [],
                "staged_files": [],
            }

    @staticmethod
    def parse_project_state(project_state_path: Path | str | None = None) -> dict[str, Any]:
        """Parses recorded baseline metadata and referenced file paths from PROJECT_STATE.md."""
        target = Path(project_state_path or DEFAULT_PROJECT_STATE_FILE)
        if not target.exists() or not target.is_file():
            return {
                "exists": False,
                "error": f"Project state file not found at {target}",
            }

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as e:
            return {
                "exists": False,
                "error": f"Failed reading project state file: {e}",
            }

        m_head = re.search(r"-\s+\*\*Recorded HEAD Commit SHA:\*\*\s*`([a-f0-9]+)`", content)
        m_branch = re.search(r"-\s+\*\*Active Git Branch:\*\*\s*`([^`]+)`", content)
        m_time = re.search(r"-\s+\*\*Validation Timestamp:\*\*\s*([^\n\r]+)", content)
        m_tracked = re.search(r"Unstaged Tracked Modifications \((\d+) files\):", content)
        m_untracked = re.search(r"Exact Untracked File Inventory \((\d+) files", content)

        if not m_head and not m_branch:
            return {
                "exists": False,
                "error": f"Project state file at {target} is malformed or missing required baseline headers.",
            }

        # Extract file paths mentioned in backticks
        raw_paths = re.findall(r"`([a-zA-Z0-9_\-\.\/]+\.[a-zA-Z0-9]+)`", content)
        # Filter down to relative repository paths with directories
        referenced_files = sorted({p for p in raw_paths if "/" in p and not p.startswith("http")})

        return {
            "exists": True,
            "path": str(target),
            "timestamp": m_time.group(1).strip() if m_time else None,
            "recorded_branch": m_branch.group(1).strip() if m_branch else None,
            "recorded_head": m_head.group(1).strip() if m_head else None,
            "recorded_tracked_modified_count": int(m_tracked.group(1)) if m_tracked else None,
            "recorded_untracked_count": int(m_untracked.group(1)) if m_untracked else None,
            "referenced_files": referenced_files,
            "content_length": len(content),
        }

    @classmethod
    def verify_project_state(
        cls,
        project_state_path: Path | str | None = None,
        workspace_root: Path | None = None,
    ) -> dict[str, Any]:
        """Reconciles live Git reality with recorded state and flags drift, staleness, and missing files."""
        root = Path(workspace_root or WORKSPACE_ROOT).resolve()
        live_git = cls.get_live_git_state(root)
        parsed_state = cls.parse_project_state(project_state_path)

        # Fail safe if state document is missing or Git is unavailable
        if not parsed_state.get("exists"):
            return {
                "status": "FAILED_SAFE",
                "recovery_state": "UNVERIFIED_STATE",
                "summary": "Project state recovery failed: state artifact unavailable.",
                "live_git": live_git,
                "discrepancies": [parsed_state.get("error", "Unknown state error")],
                "missing_referenced_files": [],
                "uncertainties": ["All project state assumptions are unverified."],
            }

        if not live_git.get("available"):
            return {
                "status": "FAILED_SAFE",
                "recovery_state": "UNVERIFIED_STATE",
                "summary": "Project state recovery failed: live Git unavailable.",
                "live_git": live_git,
                "discrepancies": [live_git.get("error", "Git execution failed")],
                "missing_referenced_files": [],
                "uncertainties": ["Cannot verify live working tree baseline."],
            }

        discrepancies: list[str] = []
        uncertainties: list[str] = []

        # 1. Branch verification
        if live_git["branch"] != parsed_state["recorded_branch"]:
            discrepancies.append(
                f"Active branch mismatch: live '{live_git['branch']}' != recorded '{parsed_state['recorded_branch']}'"
            )

        # 2. HEAD commit verification
        if live_git["head"] != parsed_state["recorded_head"]:
            discrepancies.append(
                f"HEAD commit mismatch: live '{live_git['head'][:8] if live_git['head'] else None}' != recorded '{parsed_state['recorded_head'][:8] if parsed_state['recorded_head'] else None}'"
            )

        # 3. Tracked modified file count verification
        rec_tracked = parsed_state["recorded_tracked_modified_count"]
        if rec_tracked is not None and live_git["tracked_modified_count"] != rec_tracked:
            discrepancies.append(
                f"Tracked modified file count drift: live {live_git['tracked_modified_count']} files != recorded {rec_tracked} files"
            )

        # 4. Untracked file count verification
        rec_untracked = parsed_state["recorded_untracked_count"]
        if rec_untracked is not None and live_git["untracked_count"] != rec_untracked:
            discrepancies.append(
                f"Untracked inventory count drift: live {live_git['untracked_count']} items != recorded {rec_untracked} items"
            )

        # 5. Check if referenced files exist on disk (Staleness / Broken reference detection)
        missing_referenced_files: list[str] = []
        for ref in parsed_state["referenced_files"]:
            target_file = root / ref
            if not target_file.exists():
                missing_referenced_files.append(ref)

        if missing_referenced_files:
            discrepancies.append(
                f"Stale state document: {len(missing_referenced_files)} referenced files missing from disk: {', '.join(missing_referenced_files[:5])}"
            )

        # Determine recovery status
        if missing_referenced_files:
            status = "STALE"
            summary = "Project state document is STALE (references missing or deleted repository files)."
            uncertainties.append("State document references files that no longer exist on disk.")
        elif discrepancies:
            status = "DIVERGED"
            summary = f"Project state has DIVERGED from live Git reality ({len(discrepancies)} drift factors detected)."
            uncertainties.append("Live working tree has drifted from the recorded baseline manifest.")
        else:
            status = "ALIGNED"
            summary = "Project state is fully ALIGNED with live Git baseline and repository disk reality."

        return {
            "status": status,
            "recovery_state": "VERIFIED_STATE" if status == "ALIGNED" else "RECONCILED_DRIFT",
            "summary": summary,
            "timestamp": parsed_state.get("timestamp"),
            "live_git": live_git,
            "recorded_state": {
                "branch": parsed_state.get("recorded_branch"),
                "head": parsed_state.get("recorded_head"),
                "tracked_modified_count": rec_tracked,
                "untracked_count": rec_untracked,
            },
            "discrepancies": discrepancies,
            "missing_referenced_files": missing_referenced_files,
            "uncertainties": uncertainties,
        }


# =============================================================================
# Workstream B: Failure-to-Candidate Pipeline
# =============================================================================

class FailureCandidatePipeline:
    """Ingests failure events, binds evidence, and generates candidate lessons with epistemic separation."""

    def __init__(self, candidates_dir: Path | str | None = None):
        self.candidates_dir = Path(candidates_dir or DEFAULT_CANDIDATES_DIR)
        self.registry_db_path = self.candidates_dir / "candidates_registry.db"
        self._init_candidate_registry()

    def _connect(self, timeout: float = 15.0, isolation_level: str | None = None) -> sqlite3.Connection:
        """Creates a SQLite connection with explicit WAL and durability pragmas."""
        conn = sqlite3.connect(self.registry_db_path, timeout=timeout, isolation_level=isolation_level)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)};")
        return conn

    def _init_candidate_registry(self) -> None:
        """Initializes SQLite WAL registry schema, migrates legacy records, and seeds pre-existing candidate artifacts."""
        self.candidates_dir.mkdir(parents=True, exist_ok=True)
        with self._connect(timeout=15.0) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candidate_records (
                    candidate_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    content_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    has_verified_root_cause INTEGER NOT NULL,
                    artifact_path TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_candidate_records_gen
                ON candidate_records(candidate_id, generation);
            """)
            conn.commit()

            # Schema migration: check for missing columns (payload_json, created_at_utc, schema_version)
            cur = conn.cursor()
            cols = [col[1] for col in cur.execute("PRAGMA table_info(candidate_records);").fetchall()]
            if "payload_json" not in cols:
                conn.execute("ALTER TABLE candidate_records ADD COLUMN payload_json TEXT;")
            if "created_at_utc" not in cols:
                conn.execute("ALTER TABLE candidate_records ADD COLUMN created_at_utc TEXT;")
            if "schema_version" not in cols:
                conn.execute("ALTER TABLE candidate_records ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1;")
            conn.commit()

            # Backfill any existing records where payload_json is NULL from disk artifacts
            cur.execute("""
                SELECT candidate_id, generation, content_digest, artifact_path, updated_at_utc
                FROM candidate_records
                WHERE payload_json IS NULL;
            """)
            unfilled = cur.fetchall()
            for cid, gen, dig, rel_art, updated_at in unfilled:
                cand_file = self.candidates_dir / rel_art
                if cand_file.exists() and cand_file.is_file():
                    try:
                        raw = json.loads(cand_file.read_text(encoding="utf-8"))
                        cand = LessonCandidate.from_dict(raw)
                        actual_dig = canonical_candidate_digest(cand)
                        if actual_dig == dig:
                            payload_str = json.dumps(cand.to_dict(), indent=2, ensure_ascii=False)
                            conn.execute("""
                                UPDATE candidate_records
                                SET payload_json = ?,
                                    created_at_utc = COALESCE(created_at_utc, updated_at_utc)
                                WHERE candidate_id = ?;
                            """, (payload_str, cid))
                    except (json.JSONDecodeError, OSError, ValueError, KeyError):
                        continue
            conn.commit()

            # Seed pre-existing candidate JSON artifacts without mutating files
            for cand_file in self.candidates_dir.glob("*.json"):
                cand_id = cand_file.stem
                cur.execute("SELECT 1 FROM candidate_records WHERE candidate_id = ? LIMIT 1;", (cand_id,))
                if cur.fetchone() is None:
                    try:
                        raw_data = json.loads(cand_file.read_text(encoding="utf-8"))
                        cand = LessonCandidate.from_dict(raw_data)
                        digest = canonical_candidate_digest(cand)
                        payload_str = json.dumps(cand.to_dict(), indent=2, ensure_ascii=False)
                        now_iso = datetime.datetime.now(datetime.UTC).isoformat()
                        cur.execute("""
                            INSERT INTO candidate_records (
                                candidate_id, generation, content_digest, payload_json, review_status,
                                has_verified_root_cause, artifact_path, created_at_utc, updated_at_utc, schema_version
                            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 1);
                        """, (
                            cand.candidate_id,
                            digest,
                            payload_str,
                            cand.review_status,
                            1 if cand.verified_root_cause else 0,
                            cand_file.name,
                            now_iso,
                            now_iso,
                        ))
                    except (json.JSONDecodeError, OSError, ValueError, KeyError):
                        # Corrupted or unparseable pre-existing file: leave for quarantine/error handling
                        continue
            conn.commit()

    def load_candidate(self, candidate_id: str) -> LessonCandidate | None:
        """Loads a stored candidate by candidate_id using SQLite as the authoritative source."""
        if not candidate_id:
            return None
        cand, _gen, _digest = self.get_candidate_with_generation(candidate_id)
        return cand

    def get_candidate_with_generation(self, candidate_id: str) -> tuple[LessonCandidate | None, int, str]:
        """Retrieves a candidate along with its current persisted generation and content digest from SQLite."""
        if not candidate_id:
            return None, 0, ""

        with self._connect(timeout=5.0) as conn:
            cur = conn.execute("""
                SELECT generation, content_digest, payload_json, artifact_path
                FROM candidate_records
                WHERE candidate_id = ?;
            """, (candidate_id,))
            row = cur.fetchone()

        if row and row[2]:
            gen, digest, payload_json, _rel_art = row
            try:
                data = json.loads(payload_json)
                cand = LessonCandidate.from_dict(data)
                return cand, gen, digest
            except (json.JSONDecodeError, OSError, ValueError):
                pass

        # If not in registry DB or payload missing, check if file exists on disk and safely bootstrap
        return self._safe_bootstrap_candidate(candidate_id)

    def _safe_bootstrap_candidate(self, candidate_id: str) -> tuple[LessonCandidate | None, int, str]:
        """Safely bootstraps an unindexed disk candidate into SQLite without destructive overwrites.

        Guarantees:
        - Never uses INSERT OR REPLACE.
        - Uses BEGIN IMMEDIATE transaction.
        - If candidate row already exists in SQLite (committed concurrently or earlier):
          Rereads authoritative database payload and returns it.
          Never replaces existing SQLite row with disk file.
        - If candidate row does not exist in SQLite and disk file is valid:
          Inserts row with generation=1 and commits.
        - If concurrent process inserted during bootstrap, handles IntegrityError by rereading SQLite.
        - If disk file is corrupt or invalid:
          Preserves corrupt disk artifact, returns None without database mutation.
        """
        target = self.candidates_dir / f"{candidate_id}.json"
        if not target.exists() or not target.is_file():
            return None, 0, ""

        # Validate disk artifact structure and identity first
        try:
            raw_text = target.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            disk_cand = LessonCandidate.from_dict(data)
            if disk_cand.candidate_id != candidate_id:
                return None, 0, ""
            disk_digest = canonical_candidate_digest(disk_cand)
            payload_str = json.dumps(disk_cand.to_dict(), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, OSError, ValueError, KeyError):
            # Corrupted disk artifact: preserve it, do not insert into SQLite
            return None, 0, ""

        # Transactional safe conditional insertion
        now_iso = datetime.datetime.now(datetime.UTC).isoformat()
        with self._connect(timeout=15.0, isolation_level=None) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # 1. Re-check if candidate exists in SQLite
                cur = conn.execute("""
                    SELECT generation, content_digest, payload_json
                    FROM candidate_records
                    WHERE candidate_id = ?;
                """, (candidate_id,))
                existing = cur.fetchone()

                if existing and existing[2]:
                    # Canonical row already exists!
                    conn.execute("COMMIT;")
                    try:
                        auth_data = json.loads(existing[2])
                        auth_cand = LessonCandidate.from_dict(auth_data)
                        return auth_cand, existing[0], existing[1]
                    except (json.JSONDecodeError, ValueError, KeyError):
                        return None, existing[0], existing[1]

                # 2. Insert as Generation 1 conditionally
                conn.execute("""
                    INSERT INTO candidate_records (
                        candidate_id, generation, content_digest, payload_json, review_status,
                        has_verified_root_cause, artifact_path, created_at_utc, updated_at_utc, schema_version
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 1);
                """, (
                    disk_cand.candidate_id,
                    disk_digest,
                    payload_str,
                    disk_cand.review_status,
                    1 if disk_cand.verified_root_cause else 0,
                    target.name,
                    now_iso,
                    now_iso,
                ))
                conn.execute("COMMIT;")
                return disk_cand, 1, disk_digest
            except sqlite3.IntegrityError:
                # Concurrent insertion won the race: rollback and reread SQLite
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                with self._connect(timeout=5.0) as read_conn:
                    cur = read_conn.execute("""
                        SELECT generation, content_digest, payload_json
                        FROM candidate_records
                        WHERE candidate_id = ?;
                    """, (candidate_id,))
                    row = cur.fetchone()
                    if row and row[2]:
                        try:
                            auth_cand = LessonCandidate.from_dict(json.loads(row[2]))
                            return auth_cand, row[0], row[1]
                        except (json.JSONDecodeError, ValueError, KeyError):
                            pass
                return None, 0, ""
            except BaseException:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                raise

    def cleanup_stale_temp_files(self, max_age_seconds: float = 60.0) -> int:
        """Removes orphaned temporary candidate files older than max_age_seconds."""
        if not self.candidates_dir.exists():
            return 0
        cleaned = 0
        now = time.time()
        for p in self.candidates_dir.glob("*.tmp_*"):
            try:
                st = p.stat()
                if (now - st.st_mtime) >= max_age_seconds:
                    p.unlink()
                    cleaned += 1
            except OSError:
                continue
        return cleaned

    def list_candidates(self) -> list[LessonCandidate]:
        """Lists all stored candidates using SQLite as the authoritative repository."""
        if not self.candidates_dir.exists():
            return []
        self.cleanup_stale_temp_files()
        candidates: list[LessonCandidate] = []
        with self._connect(timeout=5.0) as conn:
            cur = conn.execute("""
                SELECT payload_json
                FROM candidate_records
                ORDER BY candidate_id;
            """)
            rows = cur.fetchall()

        for (payload_json,) in rows:
            if payload_json:
                try:
                    c = LessonCandidate.from_dict(json.loads(payload_json))
                    candidates.append(c)
                except (json.JSONDecodeError, KeyError, ValueError, OSError):
                    continue

        return candidates

    def record_candidate(
        self,
        candidate: LessonCandidate,
        *,
        expected_generation: int | None = None,
        expected_digest: str | None = None,
        raise_on_conflict: bool = False,
    ) -> tuple[bool, str, int]:
        """Persists a LessonCandidate into SQLite as the authoritative source with transactional CAS and disk projection.

        Guarantees:
        - SQLite is the single authoritative source of truth; JSON files are rebuildable projections.
        - SQLite transaction commits payload, generation, and digest BEFORE filesystem replacement.
        - Stale writers fail explicitly when expected_generation or expected_digest diverges.
        - Monotonic progression: terminal states (REJECTED, SUPERSEDED) cannot be modified.
        - Monotonic Layer C: verified root causes cannot be downgraded to unverified.
        - Monotonic review_status: cannot downgrade lifecycle rank.
        - Un-indexed candidate artifacts discovered on disk are seeded with full database INSERT.
        - Post-commit projection failure reports PERSISTED_PROJECTION_PENDING without losing database authority.
        - Idempotent replay: exact matching content returns (True, 'IDEMPOTENT_REPLAY', generation).
        """
        with _PERSISTENCE_LOCK:
            target = self.candidates_dir / f"{candidate.candidate_id}.json"
            rel_artifact = target.name
            new_digest = canonical_candidate_digest(candidate)
            new_payload = json.dumps(candidate.to_dict(), indent=2, ensure_ascii=False)

            max_busy_retries = 5
            base_backoff = 0.05

            for attempt in range(max_busy_retries):
                try:
                    with self._connect(timeout=15.0, isolation_level=None) as conn:
                        conn.execute("BEGIN IMMEDIATE;")
                        try:
                            cur = conn.execute("""
                                SELECT generation, content_digest, payload_json, review_status, has_verified_root_cause, created_at_utc
                                FROM candidate_records
                                WHERE candidate_id = ?;
                            """, (candidate.candidate_id,))
                            row = cur.fetchone()

                            # --- Check for un-indexed disk artifact (Disk-Seeding Fallthrough Fix) ---
                            if not row and target.exists():
                                try:
                                    raw_disk = json.loads(target.read_text(encoding="utf-8"))
                                    disk_cand = LessonCandidate.from_dict(raw_disk)
                                    disk_digest = canonical_candidate_digest(disk_cand)
                                    disk_payload = json.dumps(disk_cand.to_dict(), indent=2, ensure_ascii=False)
                                    now_iso = datetime.datetime.now(datetime.UTC).isoformat()
                                    conn.execute("""
                                        INSERT INTO candidate_records (
                                            candidate_id, generation, content_digest, payload_json, review_status,
                                            has_verified_root_cause, artifact_path, created_at_utc, updated_at_utc, schema_version
                                        ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 1);
                                    """, (
                                        disk_cand.candidate_id,
                                        disk_digest,
                                        disk_payload,
                                        disk_cand.review_status,
                                        1 if disk_cand.verified_root_cause else 0,
                                        rel_artifact,
                                        now_iso,
                                        now_iso,
                                    ))
                                    row = (1, disk_digest, disk_payload, disk_cand.review_status, 1 if disk_cand.verified_root_cause else 0, now_iso)
                                except (json.JSONDecodeError, OSError, ValueError, KeyError):
                                    conn.execute("ROLLBACK;")
                                    return (
                                        False,
                                        "CORRUPT_CANDIDATE_PRESERVED: Existing candidate file is corrupt; refusing overwrite.",
                                        0,
                                    )

                            # --- CASE 1: Brand new candidate record ---
                            if not row:
                                if expected_generation is not None and expected_generation != 0:
                                    conn.execute("ROLLBACK;")
                                    msg = (
                                        f"STALE_GENERATION_CONFLICT: Expected generation {expected_generation} "
                                        f"for new candidate '{candidate.candidate_id}', but candidate does not exist."
                                    )
                                    if raise_on_conflict:
                                        raise StaleVersionConflictError(msg)
                                    return False, msg, 0

                                now_iso = datetime.datetime.now(datetime.UTC).isoformat()
                                conn.execute("""
                                    INSERT INTO candidate_records (
                                        candidate_id, generation, content_digest, payload_json, review_status,
                                        has_verified_root_cause, artifact_path, created_at_utc, updated_at_utc, schema_version
                                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 1);
                                Adversarial-safe insertion
                                """.replace("Adversarial-safe insertion", ""), (
                                    candidate.candidate_id,
                                    new_digest,
                                    new_payload,
                                    candidate.review_status,
                                    1 if candidate.verified_root_cause else 0,
                                    rel_artifact,
                                    now_iso,
                                    now_iso,
                                ))
                                conn.execute("COMMIT;")

                                # Database write is committed and authoritative! Project to filesystem
                                proj_ok = self._project_candidate_to_disk(target, candidate)
                                status = "PERSISTED" if proj_ok else "PERSISTED_PROJECTION_PENDING"
                                return True, status, 1

                            # --- CASE 2: Candidate record exists in SQLite ---
                            cur_gen, cur_digest, _cur_payload, cur_status, cur_root_cause, _created_at = row

                            # 1. Idempotent identical replay check
                            if cur_digest == new_digest and cur_status == candidate.review_status:
                                conn.execute("COMMIT;")
                                if not target.exists():
                                    self._project_candidate_to_disk(target, candidate)
                                return True, "IDEMPOTENT_REPLAY", cur_gen

                            # 2. CAS Generation check
                            if expected_generation is not None and expected_generation != cur_gen:
                                conn.execute("ROLLBACK;")
                                msg = (
                                    f"STALE_GENERATION_CONFLICT: Stale candidate update for '{candidate.candidate_id}'. "
                                    f"Expected generation {expected_generation}, but persisted generation is {cur_gen}."
                                )
                                if raise_on_conflict:
                                    raise StaleVersionConflictError(msg)
                                return False, msg, cur_gen

                            # 3. CAS Content Digest check
                            if expected_digest is not None and expected_digest != cur_digest:
                                conn.execute("ROLLBACK;")
                                msg = (
                                    f"STALE_DIGEST_CONFLICT: Candidate digest mismatch for '{candidate.candidate_id}'. "
                                    f"Expected digest {expected_digest}, but persisted digest is {cur_digest}."
                                )
                                if raise_on_conflict:
                                    raise StaleVersionConflictError(msg)
                                return False, msg, cur_gen

                            # 4. Terminal State check
                            if cur_status in ("REJECTED", "SUPERSEDED") and candidate.review_status != cur_status:
                                conn.execute("ROLLBACK;")
                                msg = f"TERMINAL_STATE_LOCKED: Cannot modify candidate in terminal state '{cur_status}'."
                                if raise_on_conflict:
                                    raise TerminalStateError(msg)
                                return False, msg, cur_gen

                            # 5. Monotonic Layer C Protection
                            if cur_root_cause and not candidate.verified_root_cause:
                                conn.execute("ROLLBACK;")
                                msg = "REFUSED_DOWNGRADE_PRESERVED: Cannot overwrite candidate with verified root cause with unverified candidate."
                                if raise_on_conflict:
                                    raise CandidateMonotonicityError(msg)
                                return False, msg, cur_gen

                            # 6. Monotonic Lifecycle State Protection
                            if not is_monotonic_transition(cur_status, candidate.review_status):
                                conn.execute("ROLLBACK;")
                                msg = f"REFUSED_DOWNGRADE_PRESERVED: Cannot downgrade candidate status from '{cur_status}' to '{candidate.review_status}'."
                                if raise_on_conflict:
                                    raise CandidateMonotonicityError(msg)
                                return False, msg, cur_gen

                            # 7. Check if existing disk artifact is corrupt (quarantine before projection)
                            if target.exists():
                                try:
                                    json.loads(target.read_text(encoding="utf-8"))
                                except (json.JSONDecodeError, OSError, ValueError):
                                    self.quarantine_corrupt_artifact(candidate.candidate_id)

                            # 8. Advance generation in SQLite TRANSACTIONALLY
                            new_gen = cur_gen + 1
                            now_iso = datetime.datetime.now(datetime.UTC).isoformat()
                            cur_upd = conn.execute("""
                                UPDATE candidate_records
                                SET generation = ?,
                                    content_digest = ?,
                                    payload_json = ?,
                                    review_status = ?,
                                    has_verified_root_cause = ?,
                                    updated_at_utc = ?
                                WHERE candidate_id = ? AND generation = ?;
                            """, (
                                new_gen,
                                new_digest,
                                new_payload,
                                candidate.review_status,
                                1 if candidate.verified_root_cause else 0,
                                now_iso,
                                candidate.candidate_id,
                                cur_gen,
                            ))
                            if cur_upd.rowcount == 0:
                                conn.execute("ROLLBACK;")
                                msg = f"STALE_GENERATION_CONFLICT: Concurrent update detected for '{candidate.candidate_id}'."
                                if raise_on_conflict:
                                    raise StaleVersionConflictError(msg)
                                return False, msg, cur_gen

                            conn.execute("COMMIT;")

                            # 9. Database write is committed and authoritative! Project to filesystem
                            proj_ok = self._project_candidate_to_disk(target, candidate)
                            status = "PERSISTED" if proj_ok else "PERSISTED_PROJECTION_PENDING"
                            return True, status, new_gen
                        except BaseException:
                            try:
                                conn.execute("ROLLBACK;")
                            except sqlite3.Error:
                                pass
                            raise
                except sqlite3.OperationalError as exc:
                    if ("locked" in str(exc).lower() or "busy" in str(exc).lower()) and attempt < max_busy_retries - 1:
                        time.sleep(base_backoff * (2 ** attempt))
                        continue
                    raise

            return False, "STORAGE_FAILURE: Database busy timeout exceeded.", 0

    def _project_candidate_to_disk(self, target_path: Path, candidate: LessonCandidate) -> bool:
        """Projects committed candidate payload to filesystem atomically with fsync."""
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            unique_tmp = target_path.parent / f"{target_path.stem}.tmp_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
            content = json.dumps(candidate.to_dict(), indent=2, ensure_ascii=False)
            with open(unique_tmp, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            for attempt in range(5):
                try:
                    unique_tmp.replace(target_path)
                    return True
                except OSError:
                    if attempt == 4:
                        if unique_tmp.exists():
                            try:
                                unique_tmp.unlink()
                            except OSError:
                                pass
                        return False
                    time.sleep(0.01 * (attempt + 1))
            return False
        except OSError:
            return False

    def _write_candidate_file_atomic(self, target_path: Path, candidate: LessonCandidate) -> None:
        """Writes candidate JSON to a unique temporary file, flushes to disk, and atomically replaces target."""
        self._project_candidate_to_disk(target_path, candidate)

    def reconcile_candidate_projections(self) -> dict[str, Any]:
        """Reconciles filesystem JSON projections against authoritative SQLite candidate payloads.

        Guarantees:
        - SQLite is always authoritative.
        - Missing projections are rebuilt from SQLite payload.
        - Stale or mismatched projections are rebuilt.
        - Corrupt disk artifacts are quarantined before rebuilding.
        - Orphaned JSON files are reported without automatic deletion.
        """
        summary: dict[str, Any] = {
            "checked": 0,
            "projected": 0,
            "clean": 0,
            "quarantined": [],
            "orphaned": [],
        }

        with self._connect(timeout=15.0) as conn:
            cur = conn.execute("""
                SELECT candidate_id, generation, content_digest, payload_json, artifact_path
                FROM candidate_records;
            """)
            rows = cur.fetchall()

        summary["checked"] = len(rows)
        known_ids = set()

        for cid, gen, dig, payload_json, rel_art in rows:
            known_ids.add(cid)
            if not payload_json:
                continue

            target = self.candidates_dir / rel_art
            needs_rebuild = False

            if not target.exists():
                needs_rebuild = True
            else:
                try:
                    raw = json.loads(target.read_text(encoding="utf-8"))
                    disk_cand = LessonCandidate.from_dict(raw)
                    disk_dig = canonical_candidate_digest(disk_cand)
                    if disk_dig != dig:
                        needs_rebuild = True
                    else:
                        summary["clean"] += 1
                except (json.JSONDecodeError, OSError, ValueError, KeyError):
                    qpath = self.quarantine_corrupt_artifact(cid)
                    if qpath:
                        summary["quarantined"].append(str(qpath.name))
                    needs_rebuild = True

            if needs_rebuild:
                try:
                    cand = LessonCandidate.from_dict(json.loads(payload_json))
                    if self._project_candidate_to_disk(target, cand):
                        summary["projected"] += 1
                except (json.JSONDecodeError, OSError, ValueError, KeyError):
                    continue

        # Identify orphaned candidate files
        for f in self.candidates_dir.glob("*.json"):
            if f.stem not in known_ids and not f.name.endswith(".db"):
                summary["orphaned"].append(f.name)

        return summary

    def update_candidate_cas(
        self,
        candidate: LessonCandidate,
        expected_generation: int,
        expected_digest: str | None = None,
    ) -> int:
        """Strict CAS update helper. Raises exception on conflict, returns new generation."""
        _, _, new_gen = self.record_candidate(
            candidate,
            expected_generation=expected_generation,
            expected_digest=expected_digest,
            raise_on_conflict=True,
        )
        return new_gen

    def persist_candidate(
        self,
        candidate: LessonCandidate,
        expected_generation: int | None = None,
        expected_digest: str | None = None,
    ) -> tuple[bool, str]:
        """Persists a LessonCandidate into the candidate repository.

        Backward-compatible wrapper returning (bool, str).
        """
        ok, msg, _ = self.record_candidate(
            candidate,
            expected_generation=expected_generation,
            expected_digest=expected_digest,
            raise_on_conflict=False,
        )
        return ok, msg

    def quarantine_corrupt_artifact(self, candidate_id: str) -> Path | None:
        """Quarantines a corrupted candidate artifact by renaming it with a timestamped suffix."""
        target = self.candidates_dir / f"{candidate_id}.json"
        if not target.exists() or not target.is_file():
            return None
        quarantine_target = target.with_name(f"{candidate_id}.corrupted_{int(time.time())}_{uuid.uuid4().hex[:6]}")
        try:
            target.replace(quarantine_target)
            return quarantine_target
        except OSError:
            return None

    def ingest_event_envelope(self, envelope: EventEnvelope) -> tuple[LessonCandidate | None, str]:
        """Converts an EventEnvelope into a LessonCandidate, preserving Layer A facts and inconclusive Layer B."""
        # Create candidate enforcing three-layer epistemic model
        candidate = create_candidate_from_event(envelope)

        # Check deduplication against existing candidates
        existing_candidates = self.list_candidates()
        is_dup, dup_status, existing = check_candidate_deduplication(candidate, existing_candidates)

        if is_dup and existing:
            # Primary Invariant: Replaying an event must NEVER overwrite or downgrade an existing candidate!
            return existing, dup_status

        # Candidate ID collision with conflicting contents: quarantine with distinct ID
        if dup_status == "CANDIDATE_ID_COLLISION_CONFLICT":
            candidate.candidate_id = f"{candidate.candidate_id}_conflict_{uuid.uuid4().hex[:6]}"

        # Evidence conflict: quarantine with distinct ID to preserve divergent evidence
        elif dup_status == "CANDIDATE_EVIDENCE_CONFLICT":
            candidate.candidate_id = f"{candidate.candidate_id}_evdiff_{uuid.uuid4().hex[:6]}"

        # Incomplete identity collision handling
        elif dup_status == "POTENTIAL_DUPLICATE_INCOMPLETE_IDENTITY":
            candidate.candidate_id = f"{candidate.candidate_id}_collision_{os.getpid()}_{uuid.uuid4().hex[:6]}"

        success, msg = self.persist_candidate(candidate)
        if not success:
            return None, msg

        if msg == "IDEMPOTENT_REPLAY":
            return candidate, "EXACT_DUPLICATE_ID_MATCH"

        return candidate, "CREATED"

    def scan_and_ingest_events(
        self,
        events_dir: Path | str | None = None,
        workspace_root: Path | str | None = None,
    ) -> list[dict[str, Any]]:
        """Scans approved event storage directories and ingests unhandled failure events into candidates."""
        if events_dir is not None:
            e_dir = Path(events_dir)
            if not e_dir.exists():
                return [{
                    "error": "DIRECTORY_NOT_FOUND",
                    "path": str(e_dir),
                }]
            scan_dirs = [e_dir]
        else:
            scan_dirs = get_approved_event_directories(workspace_root=workspace_root)
            if not scan_dirs:
                return []

        results: list[dict[str, Any]] = []
        for s_dir in scan_dirs:
            for event_file in sorted(s_dir.glob("*.json")):
                try:
                    raw_data = json.loads(event_file.read_text(encoding="utf-8"))
                    envelope = EventEnvelope(**{k: v for k, v in raw_data.items() if hasattr(EventEnvelope, k)})
                    cand, status = self.ingest_event_envelope(envelope)
                    results.append({
                        "event_file": event_file.name,
                        "directory": str(s_dir),
                        "event_id": envelope.event_id,
                        "candidate_id": cand.candidate_id if cand else None,
                        "status": status,
                    })
                except (json.JSONDecodeError, KeyError, ValueError, OSError, TypeError) as e:
                    results.append({
                        "event_file": event_file.name,
                        "directory": str(s_dir),
                        "status": f"PARSE_ERROR: {e}",
                    })

        return results


# =============================================================================
# Workstream C: Approval & Promotion Gate
# =============================================================================

class CandidatePromotionGate:
    """Manages preview generation, approval binding, and fail-closed promotion controls."""

    @staticmethod
    def prepare_preview(
        candidate: LessonCandidate,
        lessons_file: Path | str | None = None,
        workspace_root: Path | str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Generates a non-writing promotion preview diff and Action Manifest Digest.

        Raises ValueError if candidate lacks Layer C verified root cause.
        """
        # Primary Invariant: Only Layer C verified candidates can produce preview diffs
        if not candidate.verified_root_cause:
            raise ValueError(
                f"Candidate '{candidate.candidate_id}' lacks verified_root_cause (Layer C unverified); preview prohibited."
            )
        if not candidate.reproduction_procedure:
            raise ValueError(
                f"Candidate '{candidate.candidate_id}' lacks reproduction_procedure; preview prohibited."
            )
        if not candidate.corrective_recommendation:
            raise ValueError(
                f"Candidate '{candidate.candidate_id}' lacks corrective_recommendation; preview prohibited."
            )
        if not candidate.scope_of_applicability:
            raise ValueError(
                f"Candidate '{candidate.candidate_id}' lacks scope_of_applicability; preview prohibited."
            )

        if workspace_root:
            return _prepare_promotion_preview_with_root(
                candidate,
                lessons_file=lessons_file,
                root=workspace_root,
                title=title,
            )
        return prepare_promotion_preview(
            candidate,
            lessons_file=lessons_file or DEFAULT_LESSONS_FILE,
            title=title,
        )

    @staticmethod
    def verify_content_integrity_digest(
        candidate: LessonCandidate,
        approval_token: str,
        preview_digest_or_manifest: str | dict[str, Any],
    ) -> tuple[bool, str]:
        """Cryptographic Content Integrity Verification (Tamper Detection).

        SECURITY BOUNDARY SPECIFICATION:
        Matching the Action Manifest Digest establishes Content Integrity (tamper detection
        between preview generation and confirmation), NOT human cryptographic authentication or human identity.
        No model-controlled code or unkeyed hash can establish genuine human authorization.
        Genuine human approval is enforced solely by the architectural fail-closed boundary
        in promote_candidate(): autonomous promotion is strictly prohibited, and permanent writes
        require an external human edit to docs/agent/LESSONS.md.
        """
        if not approval_token or not preview_digest_or_manifest:
            return False, "TOKEN_OR_DIGEST_EMPTY"

        if isinstance(preview_digest_or_manifest, dict):
            expected_digest = preview_digest_or_manifest.get("action_manifest_digest", "")
            if not expected_digest:
                manifest_bytes = json.dumps(
                    preview_digest_or_manifest, sort_keys=True, separators=(",", ":"), default=str
                ).encode("utf-8")
                expected_digest = hashlib.sha256(manifest_bytes).hexdigest()
            manifest_content_hash = (
                preview_digest_or_manifest.get("candidate_content_hash")
                or preview_digest_or_manifest.get("content_hash", "")
            )
            current_content_hash = compute_candidate_hash(candidate)
            if manifest_content_hash and current_content_hash != manifest_content_hash:
                return False, f"CANDIDATE_MUTATED: Content hash {current_content_hash} != manifest {manifest_content_hash}"
        else:
            expected_digest = str(preview_digest_or_manifest).strip()
            _ = compute_candidate_hash(candidate)

        # Check token equality with preview digest
        if approval_token.strip() != expected_digest.strip():
            return False, "TOKEN_DIGEST_MISMATCH: Approval token does not match Action Manifest Digest."

        return True, "APPROVAL_BOUND: Token matches Action Manifest Digest exactly."

    # Backward-compatible alias for existing tests and callers
    verify_approval_token = verify_content_integrity_digest

    @staticmethod
    def promote_candidate(
        candidate: LessonCandidate,
        lessons_file: Path | str | None = None,
        approval_token: str | None = None,
    ) -> tuple[bool, str]:
        """FAIL-CLOSED: Autonomous promotion to permanent memory is strictly disabled.

        Primary Security Invariant:
        No model-controlled execution or script may append to docs/agent/LESSONS.md.
        Candidate promotion is preview-only. Human promotion requires an external human edit.
        """
        return (
            False,
            (
                "PROMOTION_PROHIBITED: Autonomous promotion to permanent memory is strictly disabled. "
                "Candidate promotion is preview-only. Permanent memory writes require external human action."
            ),
        )


# =============================================================================
# Antigravity Hook & CLI Interface
# =============================================================================

def _get_session_marker_path(conversation_id: str | None) -> Path | None:
    """Returns the filesystem path for the ephemeral session continuity marker, or None if conversation ID absent."""
    if not conversation_id or not str(conversation_id).strip():
        return None
    conv_digest = hashlib.sha256(str(conversation_id).strip().encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"jarvis_session_{conv_digest}_continuity.marker"


def run_session_start_hook(
    stdin_stream=sys.stdin,
    stdout_stream=sys.stdout,
    stderr_stream=sys.stderr,
) -> int:
    """Fail-open Antigravity SessionStart hook entry point for initial project continuity recovery.

    Runs once at session boot. Reconciles docs/agent/PROJECT_STATE.md against Git ground truth
    and injects baseline continuity state into the agent's initial context window.
    """
    written = False
    try:
        raw_input = stdin_stream.read()
        payload: dict[str, Any] = {}
        if raw_input and raw_input.strip():
            try:
                payload = json.loads(raw_input)
            except (json.JSONDecodeError, ValueError):
                pass

        conv_id = payload.get("conversationId")
        marker = _get_session_marker_path(conv_id)
        if marker is not None:
            try:
                marker.write_text(str(time.time()), encoding="utf-8")
            except OSError:
                pass

        report = ProjectContinuityEngine.verify_project_state()
        status = report.get("status")

        if status in ("DIVERGED", "STALE"):
            discs = "\n".join(f"  * {d}" for d in report.get("discrepancies", []))
            msg = (
                "[PROJECT_CONTINUITY_DRIFT: REFERENCE METADATA ONLY - INERT DATA]\n"
                f"Repository working tree baseline has {status.lower()} from docs/agent/PROJECT_STATE.md:\n"
                f"- Live Branch: {report.get('live_git', {}).get('branch')}\n"
                f"- Discrepancies:\n{discs}\n"
                "Action: Reconcile PROJECT_STATE.md with live Git baseline before making state assumptions."
            )
            resp = {"injectSteps": [{"ephemeralMessage": msg}]}
            stdout_stream.write(json.dumps(resp) + "\n")
            stdout_stream.flush()
            written = True
            return 0

        if status == "FAILED_SAFE":
            msg = (
                "[PROJECT_CONTINUITY_WARNING: REFERENCE METADATA ONLY - INERT DATA]\n"
                f"Project state snapshot could not be verified: {report.get('summary', 'Unknown error')}\n"
                "Live working tree assumptions are unverified."
            )
            resp = {"injectSteps": [{"ephemeralMessage": msg}]}
            stdout_stream.write(json.dumps(resp) + "\n")
            stdout_stream.flush()
            written = True
            return 0

        stdout_stream.write('{"injectSteps": []}\n')
        stdout_stream.flush()
        written = True
        return 0

    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        stderr_stream.write(f"[session_start_hook] Non-blocking error: {exc}\n")
        if not written:
            stdout_stream.write('{"injectSteps": []}\n')
            stdout_stream.flush()
        return 0


def run_continuity_hook(
    stdin_stream=sys.stdin,
    stdout_stream=sys.stdout,
    stderr_stream=sys.stderr,
) -> int:
    """Fail-open Antigravity PreInvocation hook entry point for project continuity verification.

    Executes non-blocking working tree state verification. If drift or divergence is detected,
    injects an ephemeral warning so the agent reconciles PROJECT_STATE.md with Git ground truth.
    Suppresses redundant checks if SessionStart has already executed.
    """
    written = False
    try:
        raw_input = stdin_stream.read()
        payload: dict[str, Any] = {}
        if raw_input and raw_input.strip():
            try:
                payload = json.loads(raw_input)
            except (json.JSONDecodeError, ValueError):
                pass

        invocation_num = payload.get("invocationNum")
        if isinstance(invocation_num, int) and invocation_num > 0:
            stdout_stream.write('{"injectSteps": []}\n')
            stdout_stream.flush()
            written = True
            return 0

        # At turn 0, check if SessionStart already executed for this session within 300s
        conv_id = payload.get("conversationId")
        marker = _get_session_marker_path(conv_id)
        if marker is not None and marker.exists():
            try:
                if (time.time() - marker.stat().st_mtime) < 300.0:
                    stdout_stream.write('{"injectSteps": []}\n')
                    stdout_stream.flush()
                    written = True
                    return 0
            except OSError:
                pass

        # Update marker for fallback turn 0 execution
        if marker is not None:
            try:
                marker.write_text(str(time.time()), encoding="utf-8")
            except OSError:
                pass

        report = ProjectContinuityEngine.verify_project_state()
        status = report.get("status")

        if status in ("DIVERGED", "STALE"):
            discs = "\n".join(f"  * {d}" for d in report.get("discrepancies", []))
            msg = (
                "[PROJECT_CONTINUITY_DRIFT: REFERENCE METADATA ONLY - INERT DATA]\n"
                f"Repository working tree baseline has {status.lower()} from docs/agent/PROJECT_STATE.md:\n"
                f"- Live Branch: {report.get('live_git', {}).get('branch')}\n"
                f"- Discrepancies:\n{discs}\n"
                "Action: Reconcile PROJECT_STATE.md with live Git baseline before making state assumptions."
            )
            resp = {"injectSteps": [{"ephemeralMessage": msg}]}
            stdout_stream.write(json.dumps(resp) + "\n")
            stdout_stream.flush()
            written = True
            return 0

        if status == "FAILED_SAFE":
            msg = (
                "[PROJECT_CONTINUITY_WARNING: REFERENCE METADATA ONLY - INERT DATA]\n"
                f"Project state snapshot could not be verified: {report.get('summary', 'Unknown error')}\n"
                "Live working tree assumptions are unverified."
            )
            resp = {"injectSteps": [{"ephemeralMessage": msg}]}
            stdout_stream.write(json.dumps(resp) + "\n")
            stdout_stream.flush()
            written = True
            return 0

        stdout_stream.write('{"injectSteps": []}\n')
        stdout_stream.flush()
        written = True
        return 0

    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        stderr_stream.write(f"[continuity_hook] Non-blocking error: {exc}\n")
        if not written:
            stdout_stream.write('{"injectSteps": []}\n')
            stdout_stream.flush()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis Continuity and Evidence-Gated Learning Manager")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # hook-session-start command
    subparsers.add_parser("hook-session-start", help="Antigravity SessionStart hook entry point for continuity")

    # hook-check command
    subparsers.add_parser("hook-check", help="Antigravity PreInvocation hook entry point for continuity")

    # verify-state command
    verify_p = subparsers.add_parser("verify-state", help="Verify live git reality against PROJECT_STATE.md")
    verify_p.add_argument("--state-file", type=str, default=None, help="Path to PROJECT_STATE.md")
    verify_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # ingest command
    ingest_p = subparsers.add_parser("ingest", help="Ingest failure events into candidate repository")
    ingest_p.add_argument("--events-dir", type=str, default=None, help="Path to learning_events directory")
    ingest_p.add_argument("--candidates-dir", type=str, default=None, help="Path to learning_candidates directory")
    ingest_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # list-candidates command
    list_p = subparsers.add_parser("list-candidates", help="List stored lesson candidates")
    list_p.add_argument("--candidates-dir", type=str, default=None, help="Path to learning_candidates directory")
    list_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # preview command
    prev_p = subparsers.add_parser("preview", help="Prepare promotion preview and action manifest digest")
    prev_p.add_argument("candidate_id", type=str, help="ID of candidate to preview")
    prev_p.add_argument("--candidates-dir", type=str, default=None, help="Path to learning_candidates directory")

    args = parser.parse_args()

    if args.command == "hook-session-start":
        return run_session_start_hook()

    if args.command == "hook-check":
        return run_continuity_hook()

    if args.command == "verify-state":
        res = ProjectContinuityEngine.verify_project_state(args.state_file)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print("=" * 80)
            print(f" PROJECT CONTINUITY RECOVERY REPORT: {res['status']}")
            print("=" * 80)
            print(f"Summary:       {res['summary']}")
            print(f"Recovery State:{res['recovery_state']}")
            live = res["live_git"]
            if live.get("available"):
                print(f"Live Git:      Branch: {live['branch']} | HEAD: {live['head'][:8]} | Modified: {live['tracked_modified_count']} | Untracked: {live['untracked_count']}")
            rec = res.get("recorded_state", {})
            print(f"Recorded State:Branch: {rec.get('branch')} | HEAD: {rec.get('head', '')[:8] if rec.get('head') else None} | Modified: {rec.get('tracked_modified_count')} | Untracked: {rec.get('untracked_count')}")
            if res.get("discrepancies"):
                print("-" * 80)
                print("Discrepancies / Detected Drift:")
                for d in res["discrepancies"]:
                    print(f"  * {d}")
            if res.get("missing_referenced_files"):
                print("-" * 80)
                print("Stale Referenced Files:")
                for mf in res["missing_referenced_files"]:
                    print(f"  ! Missing on disk: {mf}")
            print("=" * 80)
        return 0 if res["status"] in ("ALIGNED", "DIVERGED") else 1

    elif args.command == "ingest":
        pipeline = FailureCandidatePipeline(candidates_dir=args.candidates_dir)
        res = pipeline.scan_and_ingest_events(events_dir=args.events_dir)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Ingested {len(res)} failure events into {pipeline.candidates_dir}:")
            for r in res:
                print(f"  * Event {r.get('event_file')}: Candidate={r.get('candidate_id')} ({r.get('status')})")
        return 0

    elif args.command == "list-candidates":
        pipeline = FailureCandidatePipeline(candidates_dir=args.candidates_dir)
        cands = pipeline.list_candidates()
        if args.json:
            print(json.dumps([c.to_dict() for c in cands], indent=2))
        else:
            print(f"Stored Candidates in {pipeline.candidates_dir} ({len(cands)} total):")
            for c in cands:
                print(f"  * [{c.candidate_id}] Review: {c.review_status:<15} | Summary: {c.observed_behavior.get('summary', '')[:50]}")
        return 0

    elif args.command == "preview":
        pipeline = FailureCandidatePipeline(candidates_dir=args.candidates_dir)
        cand = pipeline.load_candidate(args.candidate_id)
        if not cand:
            sys.stderr.write(f"Candidate '{args.candidate_id}' not found.\n")
            return 1
        try:
            prev = CandidatePromotionGate.prepare_preview(cand)
            print(json.dumps(prev, indent=2))
            return 0
        except ValueError as e:
            sys.stderr.write(f"Preview error: {e}\n")
            return 1

    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
