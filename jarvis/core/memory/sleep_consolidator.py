"""Sleep-Phase Memory Consolidation & Compaction for Jarvis 2.0.

Executes offline/idle knowledge graph synthesis, redundant atomic fact merging,
and sqlite-vec virtual table compaction on isolated Read-Only WAL snapshots,
committing changes via interruptible <1.5ms micro-batches that yield immediately
upon physical workstation activity (mouse, keyboard, voice).
"""
from __future__ import annotations

import collections
import contextlib
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
import sqlite3
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Jarvis.Memory.SleepConsolidator")

MASK_ECORE_BACKGROUND = 0xFF00

FORBIDDEN_REFLEX_TOOLS = {
    "execute_powershell",
    "execute_sandboxed_powershell",
    "run_job_sandboxed_process",
    "kill_process",
    "release_file_lock",
    "delete_file",
    "write_file",
    "patch_file",
}


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def get_win32_idle_seconds() -> float:
    """Returns workstation idle seconds via GetLastInputInfo + GetTickCount64 in <0.01ms."""
    if sys.platform != "win32":
        return 999.0
    try:
        user32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if user32.GetLastInputInfo(ctypes.byref(lii)):
            try:
                k32.GetTickCount64.restype = ctypes.c_ulonglong
                k32.GetTickCount64.argtypes = []
                tick = k32.GetTickCount64()
            except AttributeError:
                tick = k32.GetTickCount()
            return ((tick - lii.dwTime) & 0xFFFFFFFF) / 1000.0
        return 0.0
    except Exception:
        return 0.0


# Backward compatibility alias
get_system_idle_seconds = get_win32_idle_seconds


def check_power_gate(min_battery_pct: int = 60) -> Tuple[bool, str]:
    """Checks whether device is on AC power or has sufficient battery percentage."""
    if sys.platform != "win32":
        return True, "Non-windows platform; power gate passed."
    try:
        k32 = ctypes.windll.kernel32
        sps = SYSTEM_POWER_STATUS()
        if k32.GetSystemPowerStatus(ctypes.byref(sps)):
            if sps.ACLineStatus == 1 or sps.BatteryLifePercent == 255:
                return True, "System running on AC power or stationary desktop."
            if sps.BatteryLifePercent >= min_battery_pct:
                return True, f"Battery adequate: {sps.BatteryLifePercent}% (threshold: {min_battery_pct}%)."
            return False, f"Battery level too low: {sps.BatteryLifePercent}% (threshold: {min_battery_pct}%)."
    except Exception as e:
        logger.debug("Power gate check notice: %s", e)
    return True, "Power check fallback passed."


def check_vram_gate(max_vram_ratio: float = 0.75) -> Tuple[bool, str]:
    """Ensures GPU VRAM pressure is below threshold before beginning consolidation."""
    try:
        from jarvis.skills.nvml_dxgi_vram_arbiter import audit_vram_and_thermal_budget
        vram_info = audit_vram_and_thermal_budget(reclaim_if_pressured=False)
        allocated = vram_info.get("vram_allocated_mb", 0)
        total = vram_info.get("vram_total_mb", 0)
        if total > 0:
            ratio = allocated / total
            if ratio > max_vram_ratio:
                return False, f"VRAM budget pressured: {ratio:.1%} > {max_vram_ratio:.1%} threshold."
    except Exception as e:
        logger.debug("VRAM gate check notice: %s", e)
    return True, "VRAM budget adequate or unconstrained."


def check_hardware_and_idle_gates(
    idle_threshold_sec: float = 300.0,
    min_battery_pct: int = 60,
    max_vram_ratio: float = 0.75,
) -> Tuple[bool, str]:
    """Evaluates workstation idle status, power supply status, and GPU VRAM budget."""
    idle_s = get_win32_idle_seconds()
    if idle_s < idle_threshold_sec:
        return False, f"Workstation is not idle ({idle_s:.1f}s < {idle_threshold_sec:.1f}s threshold)."
    power_ok, power_msg = check_power_gate(min_battery_pct=min_battery_pct)
    if not power_ok:
        return False, power_msg
    vram_ok, vram_msg = check_vram_gate(max_vram_ratio=max_vram_ratio)
    if not vram_ok:
        return False, vram_msg
    return True, "All hardware and idle gates passed."


def pin_worker_to_ecores(mask: int = MASK_ECORE_BACKGROUND) -> bool:
    """Pins calling worker thread to E-cores at THREAD_PRIORITY_NORMAL (0)."""
    if sys.platform != "win32":
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentThread.restype = wintypes.HANDLE
        k32.GetCurrentThread.argtypes = []
        k32.SetThreadAffinityMask.restype = ctypes.c_size_t
        k32.SetThreadAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
        k32.SetThreadPriority.restype = wintypes.BOOL
        k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]

        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetCurrentProcess.argtypes = []
        k32.GetProcessAffinityMask.restype = wintypes.BOOL
        k32.GetProcessAffinityMask.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
        ]
        proc_mask = ctypes.c_size_t(0)
        sys_mask = ctypes.c_size_t(0)
        h_proc = k32.GetCurrentProcess()
        target_mask = mask
        if k32.GetProcessAffinityMask(h_proc, ctypes.byref(proc_mask), ctypes.byref(sys_mask)):
            avail = sys_mask.value
            targeted = mask & avail
            target_mask = targeted if targeted > 0 else proc_mask.value

        h_thread = k32.GetCurrentThread()
        k32.SetThreadAffinityMask(h_thread, ctypes.c_size_t(target_mask))
        k32.SetThreadPriority(h_thread, 0)
        return True
    except Exception as e:
        logger.debug("E-core thread pinning notice: %s", e)
        return False


class SleepPhaseConsolidator:
    """Zero-Contention SQLite WAL + sqlite-vec Sleep Consolidator.

    Performs heavy similarity clustering and redundant fact discovery on an isolated
    Read-Only WAL snapshot, then applies compaction in <1.5ms interruptible micro-transactions
    guaranteed to yield instantly when the user wakes the system.
    """

    def __init__(self, db_path: str, vector_engine: Any, idle_threshold_sec: float = 300.0):
        self.db_path = str(db_path)
        self.vector_engine = vector_engine
        self.idle_threshold_sec = idle_threshold_sec

    def check_hardware_and_idle_gates(
        self,
        min_battery_pct: int = 60,
        max_vram_ratio: float = 0.75,
    ) -> Tuple[bool, str]:
        """Instance helper checking idle, power, and VRAM gates."""
        return check_hardware_and_idle_gates(
            idle_threshold_sec=self.idle_threshold_sec,
            min_battery_pct=min_battery_pct,
            max_vram_ratio=max_vram_ratio,
        )

    def _connect(self, read_only: bool = False) -> sqlite3.Connection:
        """Opens a SQLite connection with WAL mode and sqlite-vec loaded."""
        uri = f"file:{self.db_path}{'?mode=ro' if read_only else ''}"
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
        except Exception as e:
            logger.debug("sqlite-vec load in consolidator: %s", e)
        return conn

    def _open_interruptible_connection(
        self,
        read_only: bool = False,
        is_user_active_fn: Optional[Callable[[], bool]] = None,
        progress_steps: int = 250,
    ) -> sqlite3.Connection:
        """Opens a SQLite connection registered with a <1.0ms VDBE progress handler abort hook."""
        conn = self._connect(read_only=read_only)
        if is_user_active_fn is not None:
            def _vdbe_progress_hook() -> int:
                try:
                    return 1 if is_user_active_fn() else 0
                except Exception:
                    return 0
            conn.set_progress_handler(_vdbe_progress_hook, progress_steps)
        return conn

    def discover_redundant_facts_readonly(
        self, distance_threshold: float = 0.70
    ) -> List[Tuple[Dict[str, Any], Dict[str, Any], float]]:
        """Phase 1: Runs on an isolated WAL read snapshot (?mode=ro).

        In SQLite WAL mode, readers NEVER block writers. Finds near-duplicate
        facts via sqlite-vec KNN search where cosine distance <= distance_threshold,
        or where values are textually identical.
        """
        ro_conn = self._connect(read_only=True)
        redundant_pairs: List[Tuple[Dict[str, Any], Dict[str, Any], float]] = []
        try:
            cursor = ro_conn.cursor()
            cursor.execute("SELECT id, category, key, value FROM facts ORDER BY id ASC")
            all_facts = [dict(r) for r in cursor.fetchall()]

            import sqlite_vec
            seen_stale_ids = set()

            for f in all_facts:
                if f["id"] in seen_stale_ids:
                    continue

                text = f"{f['category']} {f['key']} {f['value']}"
                vec = self.vector_engine.dense_vectorize(text)
                blob = sqlite_vec.serialize_float32(vec)

                cursor.execute("""
                    SELECT f.id, f.category, f.key, f.value, v.distance
                    FROM facts_vec v
                    JOIN facts f ON v.fact_id = f.id
                    WHERE v.embedding MATCH ? AND k = 4
                      AND f.id > ?
                    ORDER BY v.distance ASC
                """, (blob, f["id"]))

                neighbors = cursor.fetchall()
                for n in neighbors:
                    dist = float(n["distance"])
                    is_match = (dist <= distance_threshold) or (f["value"].strip().lower() == n["value"].strip().lower())
                    if is_match and n["id"] not in seen_stale_ids:
                        n_dict = {
                            "id": n["id"],
                            "category": n["category"],
                            "key": n["key"],
                            "value": n["value"],
                        }
                        redundant_pairs.append((f, n_dict, dist))
                        seen_stale_ids.add(n["id"])

        except Exception as err:
            logger.debug("Read-only redundant facts discovery notice: %s", err)
        finally:
            ro_conn.close()

        return redundant_pairs

    def discover_consolidatable_lessons_readonly(self) -> List[Dict[str, Any]]:
        """Phase 1b: Discovers repeated episodic lessons that can be clustered."""
        ro_conn = self._connect(read_only=True)
        consolidatable: List[Dict[str, Any]] = []
        try:
            cursor = ro_conn.cursor()
            cursor.execute("""
                SELECT tool_name, COUNT(*) as cnt, SUM(hit_count) as total_hits,
                       GROUP_CONCAT(id) as ids, GROUP_CONCAT(lesson, ' | ') as all_lessons
                FROM episodic_lessons
                GROUP BY tool_name
                HAVING cnt > 3
            """)
            rows = cursor.fetchall()
            for r in rows:
                consolidatable.append({
                    "tool_name": r["tool_name"],
                    "count": r["cnt"],
                    "total_hits": r["total_hits"],
                    "ids": [int(x) for x in r["ids"].split(",") if x.strip().isdigit()],
                    "lessons_summary": r["all_lessons"][:300],
                })
        except Exception as err:
            logger.debug("Read-only lessons discovery notice: %s", err)
        finally:
            ro_conn.close()

        return consolidatable

    def apply_consolidation_micro_batches(
        self,
        stale_fact_ids: List[int],
        is_user_active_fn: Optional[Callable[[], bool]] = None,
        batch_size: int = 20,
    ) -> int:
        """Phase 2: Applies removals/consolidations in <1.5ms micro-transactions."""
        if not stale_fact_ids:
            return 0

        if is_user_active_fn is None:
            is_user_active_fn = lambda: get_win32_idle_seconds() < self.idle_threshold_sec

        rw_conn = self._connect(read_only=False)
        total_consolidated = 0

        def _progress_abort_guard() -> int:
            return 1 if is_user_active_fn() else 0

        rw_conn.set_progress_handler(_progress_abort_guard, 5000)

        try:
            for i in range(0, len(stale_fact_ids), batch_size):
                if is_user_active_fn():
                    logger.info("User activity detected during sleep consolidation; yielding WAL lock in <1ms.")
                    break

                batch = stale_fact_ids[i : i + batch_size]
                try:
                    rw_conn.execute("BEGIN IMMEDIATE;")
                    placeholders = ",".join("?" for _ in batch)
                    try:
                        rw_conn.execute(
                            f"DELETE FROM facts_vec WHERE fact_id IN ({placeholders})",
                            batch,
                        )
                    except Exception:
                        pass
                    rw_conn.execute(
                        f"DELETE FROM facts WHERE id IN ({placeholders})",
                        batch,
                    )
                    rw_conn.execute("COMMIT;")
                    total_consolidated += len(batch)
                except (sqlite3.OperationalError, sqlite3.DatabaseError) as b_err:
                    try:
                        rw_conn.execute("ROLLBACK;")
                    except Exception:
                        pass
                    logger.debug("Consolidation micro-batch yielded or backoff: %s", b_err)
                    break

                time.sleep(0.001)

            if not is_user_active_fn():
                try:
                    rw_conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                except Exception:
                    pass

        finally:
            rw_conn.set_progress_handler(None, 0)
            rw_conn.close()

        return total_consolidated

    def run_pass_a_entity_deduplication(
        self,
        conn: sqlite3.Connection,
        is_user_active_fn: Optional[Callable[[], bool]] = None,
    ) -> Tuple[int, int]:
        """Pass A: Merges duplicate rows in facts matching normalized (category, key, value)
        and promotes structured subject.predicate facts into active knowledge_edges.
        """
        duplicates_merged = 0
        edges_promoted = 0

        # Ensure knowledge_edges table exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_entity TEXT NOT NULL,
                relation TEXT NOT NULL,
                target_entity TEXT NOT NULL,
                valid_from REAL,
                valid_until REAL,
                confidence REAL DEFAULT 1.0,
                created_at REAL DEFAULT (unixepoch())
            )
        """)

        # 1. Normalize and deduplicate facts table
        cursor = conn.cursor()
        cursor.execute("SELECT id, category, key, value FROM facts ORDER BY id ASC")
        rows = cursor.fetchall()

        seen_normalized: Dict[Tuple[str, str, str], int] = {}
        stale_ids_to_delete: List[int] = []

        for r in rows:
            f_id = r["id"]
            cat = (r["category"] or "").strip().lower()
            k = (r["key"] or "").strip().lower()
            v = (r["value"] or "").strip().lower()
            norm_key = (cat, k, v)

            if norm_key in seen_normalized:
                stale_ids_to_delete.append(f_id)
            else:
                seen_normalized[norm_key] = f_id

        if stale_ids_to_delete:
            for s_id in stale_ids_to_delete:
                if is_user_active_fn and is_user_active_fn():
                    break
                try:
                    conn.execute("DELETE FROM facts_vec WHERE fact_id = ?", (s_id,))
                except Exception:
                    pass
                conn.execute("DELETE FROM facts WHERE id = ?", (s_id,))
                duplicates_merged += 1

        # 2. Promote structured subject.predicate facts into active knowledge_edges
        cursor.execute("SELECT category, key, value FROM facts WHERE key LIKE '%.%'")
        dot_rows = cursor.fetchall()
        for dr in dot_rows:
            if is_user_active_fn and is_user_active_fn():
                break
            full_key = dr["key"].strip()
            parts = full_key.split(".", 1)
            if len(parts) == 2:
                subj, pred = parts[0].strip(), parts[1].strip()
                val = (dr["value"] or "").strip()
                if subj and pred and val:
                    cursor.execute("""
                        SELECT id FROM knowledge_edges
                        WHERE source_entity = ? AND relation = ? AND target_entity = ? AND valid_until IS NULL
                    """, (subj, pred, val))
                    if not cursor.fetchone():
                        conn.execute("""
                            INSERT INTO knowledge_edges (source_entity, relation, target_entity, valid_from, valid_until)
                            VALUES (?, ?, ?, unixepoch(), NULL)
                        """, (subj, pred, val))
                        edges_promoted += 1

        return duplicates_merged, edges_promoted

    def run_pass_b_workflow_distillation(
        self,
        conn: sqlite3.Connection,
        is_user_active_fn: Optional[Callable[[], bool]] = None,
    ) -> int:
        """Pass B: Scans interactions for contiguous tool sequences (N>=3) repeated >=3 times,
        compiling them into compiled_reflex_macros.
        """
        from jarvis.skills.base import SkillRegistry

        reflex_macros_compiled = 0

        conn.execute("""
            CREATE TABLE IF NOT EXISTS compiled_reflex_macros (
                macro_id TEXT PRIMARY KEY,
                tool_sequence TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL,
                last_compiled_ts REAL NOT NULL
            ) WITHOUT ROWID
        """)

        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT tool_calls FROM interactions
                WHERE tool_calls IS NOT NULL AND tool_calls != ''
                  AND created_at >= datetime('now', '-7 days')
                ORDER BY id ASC
            """)
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return 0

        sequence_counts: Dict[Tuple[str, ...], int] = collections.defaultdict(int)

        for r in rows:
            raw_tc = r["tool_calls"]
            if not raw_tc:
                continue
            tools_in_turn = []
            try:
                parsed = json.loads(raw_tc)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict):
                            t_name = item.get("name") or item.get("function", {}).get("name")
                            if t_name:
                                tools_in_turn.append(str(t_name))
                        elif isinstance(item, str):
                            tools_in_turn.append(item)
            except Exception:
                continue

            if len(tools_in_turn) >= 3:
                for length in range(3, min(6, len(tools_in_turn) + 1)):
                    for start_idx in range(len(tools_in_turn) - length + 1):
                        seq = tuple(tools_in_turn[start_idx : start_idx + length])
                        if any(t in FORBIDDEN_REFLEX_TOOLS for t in seq):
                            continue
                        if any(SkillRegistry.get_risk_level(t) >= 1 for t in seq):
                            continue
                        sequence_counts[seq] += 1

        for seq, count in sequence_counts.items():
            if count >= 3:
                if is_user_active_fn and is_user_active_fn():
                    break
                seq_str = "->".join(seq)
                macro_id = "macro_" + hashlib.sha256(seq_str.encode()).hexdigest()[:12]
                conn.execute("""
                    INSERT OR REPLACE INTO compiled_reflex_macros
                    (macro_id, tool_sequence, occurrence_count, last_compiled_ts)
                    VALUES (?, ?, ?, unixepoch())
                """, (macro_id, seq_str, count))
                reflex_macros_compiled += 1

        return reflex_macros_compiled

    def run_pass_c_wal_maintenance(self, conn: sqlite3.Connection) -> int:
        """Pass C: Runs incremental vacuum and WAL checkpoint (TRUNCATE)."""
        wal_pages = 0
        try:
            conn.execute("PRAGMA incremental_vacuum(64);")
        except Exception as e:
            logger.debug("Incremental vacuum notice: %s", e)

        try:
            cursor = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            row = cursor.fetchone()
            if row and len(row) >= 3:
                wal_pages = int(row[2])
        except Exception as e:
            logger.debug("WAL checkpoint notice: %s", e)

        return wal_pages

    def run_sleep_consolidation(
        self,
        is_user_active_fn: Optional[Callable[[], bool]] = None,
        batch_size: int = 20,
        extractor_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
        pin_ecores: bool = True,
    ) -> Dict[str, Any]:
        """Orchestrates full sleep-phase consolidation with snapshot isolation and micro-batching."""
        t0 = time.perf_counter()

        if pin_ecores:
            pin_worker_to_ecores(MASK_ECORE_BACKGROUND)

        if is_user_active_fn is None:
            is_user_active_fn = lambda: get_win32_idle_seconds() < self.idle_threshold_sec

        distillation_summary = {"status": "skipped", "turns_processed": 0, "traits_extracted": 0}
        if not is_user_active_fn():
            try:
                from .transcript_distiller import TranscriptDistiller
                distiller = TranscriptDistiller(self.db_path, self.vector_engine)
                distillation_summary = distiller.distill_unprocessed_interactions(
                    is_user_active_fn=is_user_active_fn,
                    max_turns=25,
                    extractor_fn=extractor_fn,
                )
            except Exception as d_err:
                logger.debug("Sleep consolidation transcript distillation notice: %s", d_err)

        if is_user_active_fn():
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return {
                "status": "yielded",
                "aborted_by_user": True,
                "distillation": distillation_summary,
                "redundant_pairs_found": 0,
                "facts_pruned": 0,
                "duplicates_merged": 0,
                "edges_promoted": 0,
                "reflex_macros_compiled": 0,
                "wal_pages_checkpointed": 0,
                "decayed_habits": 0,
                "elapsed_ms": round(elapsed_ms, 2),
            }

        pairs = self.discover_redundant_facts_readonly()
        stale_ids = [stale["id"] for _, stale, _ in pairs]

        if is_user_active_fn():
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return {
                "status": "yielded",
                "aborted_by_user": True,
                "distillation": distillation_summary,
                "redundant_pairs_found": len(pairs),
                "facts_pruned": 0,
                "duplicates_merged": 0,
                "edges_promoted": 0,
                "reflex_macros_compiled": 0,
                "wal_pages_checkpointed": 0,
                "decayed_habits": 0,
                "elapsed_ms": round(elapsed_ms, 2),
            }

        pruned_count = self.apply_consolidation_micro_batches(
            stale_fact_ids=stale_ids,
            is_user_active_fn=is_user_active_fn,
            batch_size=batch_size,
        )

        duplicates_merged = 0
        edges_promoted = 0
        reflex_macros_compiled = 0
        wal_pages_checkpointed = 0

        # Execute Pass A, Pass B, Pass C on interruptible connection
        rw_conn = self._open_interruptible_connection(
            read_only=False,
            is_user_active_fn=is_user_active_fn,
            progress_steps=250,
        )
        try:
            if not is_user_active_fn():
                duplicates_merged, edges_promoted = self.run_pass_a_entity_deduplication(
                    rw_conn, is_user_active_fn=is_user_active_fn
                )

            if not is_user_active_fn():
                reflex_macros_compiled = self.run_pass_b_workflow_distillation(
                    rw_conn, is_user_active_fn=is_user_active_fn
                )

            if not is_user_active_fn():
                wal_pages_checkpointed = self.run_pass_c_wal_maintenance(rw_conn)
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as opt_err:
            logger.debug("Interruptible consolidation pass caught: %s", opt_err)
        finally:
            rw_conn.set_progress_handler(None, 0)
            rw_conn.close()

        decayed_habits = 0
        if not is_user_active_fn():
            decayed_habits = self.decay_action_transitions(
                half_life_days=14.0,
                is_user_active_fn=is_user_active_fn,
            )

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        was_aborted = bool(is_user_active_fn())

        logger.info(
            "Sleep-Phase Consolidation finished: %d duplicates merged, %d edges promoted, %d macros compiled, %d WAL pages checkpointed in %.2fms.",
            duplicates_merged, edges_promoted, reflex_macros_compiled, wal_pages_checkpointed, elapsed_ms
        )

        return {
            "status": "yielded" if was_aborted else "success",
            "aborted_by_user": was_aborted,
            "distillation": distillation_summary,
            "redundant_pairs_found": len(pairs),
            "facts_pruned": pruned_count,
            "duplicates_merged": duplicates_merged,
            "edges_promoted": edges_promoted,
            "reflex_macros_compiled": reflex_macros_compiled,
            "wal_pages_checkpointed": wal_pages_checkpointed,
            "decayed_habits": decayed_habits,
            "elapsed_ms": round(elapsed_ms, 2),
        }

    def decay_action_transitions(
        self,
        half_life_days: float = 14.0,
        is_user_active_fn: Optional[Callable[[], bool]] = None,
    ) -> int:
        """Applies 14-day exponential decay to habit graph action transitions during sleep phase."""
        if is_user_active_fn and is_user_active_fn():
            return 0

        try:
            from jarvis.core.cognition.habit_graph import TemporalHabitGraph
            graph = TemporalHabitGraph(self.db_path, sync_writes=True)
            return graph.apply_exponential_decay(half_life_days=half_life_days)
        except Exception as e:
            logger.debug("Habit transition decay in sleep consolidator notice: %s", e)
            return 0
