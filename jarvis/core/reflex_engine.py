"""Dynamic Reflex Engine and Habit Learner for Jarvis 2.0.

Provides sub-millisecond local shortcut caching for learned user workflows.
When a novel user command is reasoned and executed through the LLM on Turn 1,
the successful tool trajectory is analyzed and promoted to SQLite persistent memory.
On Turn 2, the command matches the in-memory habit cache and executes directly in <15ms,
bypassing cloud LLM generation entirely.
"""
import contextlib
import datetime
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("Jarvis.ReflexEngine")

# Tools safe to be promoted to automatic fast-path reflexes without human confirmation
REFLEX_ELIGIBLE_TOOLS = {
    "open_browser_tab",
    "close_browser_tab",
    "launch_application",
    "close_application",
    "focus_window",
    "adjust_volume",
    "control_media",
    "minimize_all_windows",
    "take_desktop_screenshot",
    "get_active_window",
    "list_running_applications",
    "get_system_vitals",
    "get_battery_status",
    "read_clipboard",
    "copy_to_clipboard",
    "open_photo",
    "list_photos",
    "get_calendar_events",
    "get_pending_tasks",
    "recall_fact",
    "inspect_screen",
    "see_screen",
    "verify_learned_habits",
    "press_hotkey",
    "save_clipboard_photo",
    "execute_workflow_reflex",
    "inspect_file_locks",
    "inspect_port_holders",
    "audit_vram_and_thermal_budget",
    "get_recent_workspace_mutations",
}


def normalize_trigger_phrase(text: str) -> str:
    """Normalizes conversational text into a canonical lookup key."""
    if not text:
        return ""
    from jarvis.core.reflex_dispatcher import ReflexDispatcher
    clean = ReflexDispatcher.normalize_conversational_text(text)
    return " ".join(clean.split())


_CONVERSATIONAL_PARTICLES = frozenset({
    "my", "the", "this", "that", "a", "an", "for", "me", "to", "now", "please", "can", "you", "just"
})


def extract_significant_tokens(text: str) -> frozenset[str]:
    """Extracts core action/target tokens, stripping minor conversational particles."""
    words = re.findall(r"\b[a-z0-9_-]+\b", text.lower())
    return frozenset(w for w in words if w not in _CONVERSATIONAL_PARTICLES and len(w) > 1)


class DynamicReflexEngine:
    """Manages learned habits, in-memory reflex lookups, and SQLite persistence."""

    def __init__(self, db_path: Path | str = "data/jarvis_memory.db"):
        if not isinstance(db_path, (str, Path)) or hasattr(db_path, "_mock_name"):
            db_path = "data/jarvis_memory.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._macro_cache: dict[str, dict[str, Any]] = {}
        self._init_db()
        self._load_cache()

    @contextlib.contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA mmap_size=268435456;")
        conn.execute("PRAGMA cache_size=-64000;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Initializes learned_habits schema in SQLite."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learned_habits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_phrase TEXT UNIQUE NOT NULL,
                    skill_name TEXT NOT NULL,
                    skill_kwargs TEXT NOT NULL,
                    hit_count INTEGER DEFAULT 1,
                    turn1_latency_ms REAL DEFAULT 0.0,
                    reflex_latency_ms REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_executed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_habits_phrase ON learned_habits(trigger_phrase)")
            conn.commit()

    def _load_cache(self):
        """Loads all active habits and compiled macros from SQLite into fast in-memory dictionary."""
        with self._lock:
            self._cache.clear()
            self._macro_cache.clear()
            try:
                with self._get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT id, trigger_phrase, skill_name, skill_kwargs, hit_count,
                               turn1_latency_ms, reflex_latency_ms, created_at, last_executed
                        FROM learned_habits
                    """)
                    for row in cursor.fetchall():
                        try:
                            kwargs = json.loads(row["skill_kwargs"])
                        except Exception:
                            kwargs = {}
                        is_compound = row["skill_name"] == "__compound__"
                        self._cache[row["trigger_phrase"]] = {
                            "id": row["id"],
                            "trigger_phrase": row["trigger_phrase"],
                            "skill_name": row["skill_name"],
                            "skill_kwargs": kwargs,
                            "is_compound": is_compound,
                            "hit_count": row["hit_count"],
                            "turn1_latency_ms": float(row["turn1_latency_ms"] or 0.0),
                            "reflex_latency_ms": float(row["reflex_latency_ms"] or 0.0),
                            "created_at": row["created_at"],
                            "last_executed": row["last_executed"],
                        }

                    # Load compiled workflow macros into fast regex cache
                    try:
                        cursor.execute("SELECT macro_name, trigger_regex, failure_count FROM compiled_reflex_macros WHERE failure_count < 3")
                        for row in cursor.fetchall():
                            try:
                                compiled_pat = re.compile(row["trigger_regex"], re.IGNORECASE)
                                self._macro_cache[row["macro_name"]] = {
                                    "macro_name": row["macro_name"],
                                    "regex": compiled_pat,
                                }
                            except Exception:
                                pass
                    except sqlite3.OperationalError:
                        pass
            except Exception as e:
                logger.error(f"Error loading learned habits cache: {e}")

    def check_habit(self, query: str) -> dict[str, Any] | None:
        """Checks if normalized query matches a learned habit or compiled macro in <0.2ms."""
        norm_key = normalize_trigger_phrase(query)
        if not norm_key:
            return None

        with self._lock:
            # 1. Exact canonical normalized match
            habit = self._cache.get(norm_key)
            if habit:
                return dict(habit)

            # 2. Resilient suffix/prefix match (e.g. "open brave and go to github" vs "go to github in brave")
            for phrase, h in self._cache.items():
                if norm_key == phrase or (len(phrase) > 12 and (norm_key.endswith(phrase) or phrase.endswith(norm_key))):
                    return dict(h)

            # 3. Fast token-set match (matches conversational variations like "open my calculator" vs "open calculator")
            query_tokens = extract_significant_tokens(norm_key)
            if len(query_tokens) >= 2:
                for phrase, h in self._cache.items():
                    phrase_tokens = extract_significant_tokens(phrase)
                    if query_tokens == phrase_tokens:
                        return dict(h)

            # 4. Fast Compiled Reflex Macro match (<0.05ms in-memory regex match)
            for m_name, m_data in self._macro_cache.items():
                if m_data["regex"].search(query) or m_data["regex"].search(norm_key):
                    return {
                        "id": -1,
                        "trigger_phrase": query,
                        "skill_name": "execute_workflow_reflex",
                        "skill_kwargs": {"macro_name": m_name, "db_path": str(self.db_path)},
                        "is_compound": True,
                        "hit_count": 1,
                        "turn1_latency_ms": 0.0,
                        "reflex_latency_ms": 0.0,
                        "is_macro": True,
                    }

        return None

    def record_habit(
        self,
        trigger_query: str,
        skill_name: str,
        skill_kwargs: dict[str, Any],
        turn1_latency_ms: float = 0.0,
    ) -> bool:
        """Promotes a successful single-tool execution into the persistent habit cache."""
        norm_key = normalize_trigger_phrase(trigger_query)
        if not norm_key or len(norm_key) < 4:
            return False

        # Only promote safe, deterministic, reflex-eligible tools
        if skill_name not in REFLEX_ELIGIBLE_TOOLS:
            logger.debug(f"Tool '{skill_name}' is not reflex-eligible. Skipping habit promotion.")
            return False

        # Clean kwargs of unpickleable or transient elements
        clean_kwargs = {k: v for k, v in skill_kwargs.items() if isinstance(v, (str, int, float, bool, list, dict))}
        kwargs_json = json.dumps(clean_kwargs, sort_keys=True)

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO learned_habits (
                        trigger_phrase, skill_name, skill_kwargs, hit_count,
                        turn1_latency_ms, reflex_latency_ms, created_at, last_executed
                    ) VALUES (?, ?, ?, 1, ?, 0.0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(trigger_phrase) DO UPDATE SET
                        skill_name = excluded.skill_name,
                        skill_kwargs = excluded.skill_kwargs,
                        hit_count = learned_habits.hit_count + 1,
                        turn1_latency_ms = CASE WHEN learned_habits.turn1_latency_ms <= 0 THEN excluded.turn1_latency_ms ELSE learned_habits.turn1_latency_ms END,
                        last_executed = CURRENT_TIMESTAMP
                """, (norm_key, skill_name, kwargs_json, round(turn1_latency_ms, 2)))
                conn.commit()
                habit_id = cursor.lastrowid

            with self._lock:
                self._cache[norm_key] = {
                    "id": habit_id,
                    "trigger_phrase": norm_key,
                    "skill_name": skill_name,
                    "skill_kwargs": clean_kwargs,
                    "hit_count": 1,
                    "turn1_latency_ms": round(turn1_latency_ms, 2),
                    "reflex_latency_ms": 0.0,
                    "created_at": datetime.datetime.now().isoformat(),
                    "last_executed": datetime.datetime.now().isoformat(),
                }

            logger.info(f"Learned habit promoted: '{norm_key}' -> {skill_name}({clean_kwargs}) [Turn 1: {turn1_latency_ms:.1f}ms]")
            return True
        except Exception as e:
            logger.warning(f"Failed to record learned habit: {e}")
            return False

    def record_compound_habit(
        self,
        trigger_query: str,
        steps: list[dict[str, Any]],
        turn1_latency_ms: float = 0.0,
    ) -> bool:
        """Promotes a multi-step sequential action pipeline into the persistent habit cache."""
        norm_key = normalize_trigger_phrase(trigger_query)
        if not norm_key or len(norm_key) < 4:
            return False

        if not steps or len(steps) < 2 or len(steps) > 5:
            return False

        # Verify every step is safe and reflex-eligible
        for s in steps:
            s_name = s.get("skill_name")
            if not s_name or s_name not in REFLEX_ELIGIBLE_TOOLS:
                logger.debug(f"Compound step '{s_name}' is not reflex-eligible. Skipping promotion.")
                return False

        clean_steps = []
        for s in steps:
            s_kwargs = s.get("skill_kwargs", {})
            clean_k = {k: v for k, v in s_kwargs.items() if isinstance(v, (str, int, float, bool, list, dict))}
            clean_steps.append({
                "skill_name": s["skill_name"],
                "skill_kwargs": clean_k,
            })

        kwargs_json = json.dumps(clean_steps, sort_keys=True)

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO learned_habits (
                        trigger_phrase, skill_name, skill_kwargs, hit_count,
                        turn1_latency_ms, reflex_latency_ms, created_at, last_executed
                    ) VALUES (?, '__compound__', ?, 1, ?, 0.0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(trigger_phrase) DO UPDATE SET
                        skill_name = '__compound__',
                        skill_kwargs = excluded.skill_kwargs,
                        hit_count = learned_habits.hit_count + 1,
                        turn1_latency_ms = CASE WHEN learned_habits.turn1_latency_ms <= 0 THEN excluded.turn1_latency_ms ELSE learned_habits.turn1_latency_ms END,
                        last_executed = CURRENT_TIMESTAMP
                """, (norm_key, kwargs_json, round(turn1_latency_ms, 2)))
                conn.commit()
                habit_id = cursor.lastrowid

            with self._lock:
                self._cache[norm_key] = {
                    "id": habit_id,
                    "trigger_phrase": norm_key,
                    "skill_name": "__compound__",
                    "skill_kwargs": clean_steps,
                    "is_compound": True,
                    "hit_count": 1,
                    "turn1_latency_ms": round(turn1_latency_ms, 2),
                    "reflex_latency_ms": 0.0,
                    "created_at": datetime.datetime.now().isoformat(),
                    "last_executed": datetime.datetime.now().isoformat(),
                }

            logger.info(f"Learned compound habit promoted: '{norm_key}' -> {len(clean_steps)} steps [Turn 1: {turn1_latency_ms:.1f}ms]")
            return True
        except Exception as e:
            logger.warning(f"Failed to record compound habit: {e}")
            return False

    def record_hit(self, habit_id: int, reflex_latency_ms: float = 0.0):
        """Updates habit execution telemetry after a fast-path reflex hit."""
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    UPDATE learned_habits
                    SET hit_count = hit_count + 1,
                        reflex_latency_ms = ?,
                        last_executed = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (round(reflex_latency_ms, 2), habit_id))
                conn.commit()

            with self._lock:
                for h in self._cache.values():
                    if h.get("id") == habit_id:
                        h["hit_count"] = h.get("hit_count", 0) + 1
                        h["reflex_latency_ms"] = round(reflex_latency_ms, 2)
                        break
        except Exception as e:
            logger.debug(f"Error updating habit hit telemetry: {e}")

    def list_learned_habits(self) -> list[dict[str, Any]]:
        """Returns all registered habits sorted by hit count."""
        with self._lock:
            items = list(self._cache.values())
        items.sort(key=lambda x: x.get("hit_count", 0), reverse=True)
        return items

    def clear_habits(self) -> int:
        """Clears all learned habits from database and cache."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM learned_habits")
            conn.commit()
            count = cursor.rowcount
        with self._lock:
            self._cache.clear()
        return count
