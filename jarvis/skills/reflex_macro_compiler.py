"""Deterministic UIA and CDP Workflow-to-Reflex Compiler for Jarvis 2.0.

Compiles verified sequences of tool invocations and physical conditions into
deterministic DAG macros registered in SQLite for <15ms zero-token local execution.
"""
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from jarvis.skills.base import SkillRegistry

FORBIDDEN_MACRO_SKILLS: set[str] = {
    "execute_powershell",
    "execute_powershell_destructive",
    "delete_file",
    "send_email_external",
    "release_file_lock",
}


def _ensure_db_initialized(db_path: str) -> None:
    """Ensures parent directory and SQLite table exist."""
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compiled_reflex_macros (
                macro_name TEXT PRIMARY KEY,
                trigger_regex TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            ) WITHOUT ROWID
        """)
        conn.commit()
    finally:
        conn.close()


@SkillRegistry.register(
    name="compile_workflow_reflex",
    description="Compiles a verified multi-step tool sequence into a <15ms local DynamicReflexEngine macro.",
    risk_level=1,
)
def compile_workflow_reflex(
    trigger_phrase_pattern: str,
    macro_name: str,
    steps_json: str,
    db_path: str = "data/jarvis_memory.db",
) -> dict[str, Any]:
    """Validates and registers a deterministic multi-step workflow into SQLite."""
    # 1. Validate regex safety
    try:
        compiled_re = re.compile(trigger_phrase_pattern, re.IGNORECASE)
    except re.error as err:
        return {"status": "error", "reason": f"Invalid trigger regex: {err}"}

    # 2. Parse and validate steps JSON
    try:
        steps_raw = json.loads(steps_json) if isinstance(steps_json, str) else steps_json
        if not isinstance(steps_raw, list) or not steps_raw:
            return {"status": "error", "reason": "steps_json must be a non-empty JSON list"}
    except json.JSONDecodeError as err:
        return {"status": "error", "reason": f"Invalid JSON in steps: {err}"}

    # 3. Forbid high-blast-radius actions in autonomous macros
    validated_steps: list[dict[str, Any]] = []
    for idx, step in enumerate(steps_raw):
        skill = step.get("skill_name", "")
        if not skill:
            return {"status": "error", "reason": f"Step {idx} is missing 'skill_name'"}
        if skill in FORBIDDEN_MACRO_SKILLS:
            return {"status": "rejected", "reason": f"Skill '{skill}' is forbidden in compiled reflexes."}

        validated_steps.append({
            "step_index": idx,
            "skill_name": skill,
            "static_args": step.get("static_args", {}),
            "expected_post_condition": step.get("expected_post_condition", {}),
            "timeout_ms": min(int(step.get("timeout_ms", 1500)), 5000),
        })

    _ensure_db_initialized(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO compiled_reflex_macros
               (macro_name, trigger_regex, steps_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (macro_name, compiled_re.pattern, json.dumps(validated_steps), time.time()),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "compiled",
        "macro_name": macro_name,
        "step_count": len(validated_steps),
        "trigger_pattern": compiled_re.pattern,
    }


@SkillRegistry.register(
    name="execute_workflow_reflex",
    description="Executes a compiled multi-step reflex macro by name with physical post-condition verification.",
    risk_level=1,
)
def execute_workflow_reflex(
    macro_name: str,
    db_path: str = "data/jarvis_memory.db",
) -> dict[str, Any]:
    """Retrieves and sequentially executes the steps of a compiled macro."""
    _ensure_db_initialized(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT steps_json, failure_count FROM compiled_reflex_macros WHERE macro_name = ?",
            (macro_name,),
        )
        row = cur.fetchone()
        if not row:
            return {"status": "not_found", "reason": f"Macro '{macro_name}' does not exist"}

        steps_json, failure_count = row
        steps = json.loads(steps_json)
    finally:
        conn.close()

    t0 = time.perf_counter()
    step_results: list[dict[str, Any]] = []

    for step in steps:
        skill_name = step["skill_name"]
        static_args = step.get("static_args", {})
        expected_post = step.get("expected_post_condition", {})

        tool = SkillRegistry.get_tool(skill_name)
        if not tool:
            _record_macro_outcome(db_path, macro_name, success=False)
            return {
                "status": "failed",
                "macro_name": macro_name,
                "failed_step": step["step_index"],
                "reason": f"Tool '{skill_name}' not found in registry",
                "completed_steps": step_results,
            }

        try:
            res = tool(**static_args)
            step_results.append({"step": step["step_index"], "skill": skill_name, "result": res})
        except Exception as err:
            _record_macro_outcome(db_path, macro_name, success=False)
            return {
                "status": "failed",
                "macro_name": macro_name,
                "failed_step": step["step_index"],
                "reason": f"Execution error in '{skill_name}': {err}",
                "completed_steps": step_results,
            }

        # Verify physical post-condition if specified
        if expected_post:
            if isinstance(res, dict):
                for k, expected_v in expected_post.items():
                    actual_v = res.get(k)
                    if actual_v != expected_v:
                        _record_macro_outcome(db_path, macro_name, success=False)
                        return {
                            "status": "post_condition_failed",
                            "macro_name": macro_name,
                            "failed_step": step["step_index"],
                            "reason": f"Expected {k}='{expected_v}', got '{actual_v}'",
                            "completed_steps": step_results,
                        }

    _record_macro_outcome(db_path, macro_name, success=True)
    dur_ms = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "status": "success",
        "macro_name": macro_name,
        "duration_ms": dur_ms,
        "steps_executed": len(step_results),
        "results": step_results,
        "summary": f"Workflow '{macro_name}' completed {len(step_results)} steps successfully in {dur_ms}ms, Sir.",
    }


def _record_macro_outcome(db_path: str, macro_name: str, success: bool) -> None:
    """Updates success or failure counters for telemetry and circuit breaking."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            if success:
                conn.execute(
                    "UPDATE compiled_reflex_macros SET success_count = success_count + 1 WHERE macro_name = ?",
                    (macro_name,),
                )
            else:
                conn.execute(
                    "UPDATE compiled_reflex_macros SET failure_count = failure_count + 1 WHERE macro_name = ?",
                    (macro_name,),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


@SkillRegistry.register(
    name="list_compiled_reflex_macros",
    description="Lists all compiled DynamicReflexEngine workflow macros and their reliability metrics.",
    risk_level=0,
)
def list_compiled_reflex_macros(db_path: str = "data/jarvis_memory.db") -> dict[str, Any]:
    """Returns metadata for all registered workflow macros."""
    _ensure_db_initialized(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT macro_name, trigger_regex, success_count, failure_count, created_at FROM compiled_reflex_macros"
        ).fetchall()
        macros = [dict(row) for row in rows]
    finally:
        conn.close()
    return {"status": "ok", "count": len(macros), "macros": macros}


@SkillRegistry.register(
    name="delete_compiled_reflex_macro",
    description="Deletes a compiled workflow macro from SQLite.",
    risk_level=1,
)
def delete_compiled_reflex_macro(
    macro_name: str, db_path: str = "data/jarvis_memory.db"
) -> dict[str, Any]:
    """Removes a macro by name."""
    _ensure_db_initialized(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM compiled_reflex_macros WHERE macro_name = ?", (macro_name,))
        deleted = cur.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "deleted" if deleted else "not_found",
        "macro_name": macro_name,
    }
