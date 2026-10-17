"""Tests for SQLite memory manager."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager


def test_memory_operations():
    config = load_config()
    # Use temporary test db
    config["memory"]["db_path"] = "data/test_memory.db"
    mem = MemoryManager(config)

    # 1. Test Facts
    mem.remember_fact("favorite_editor", "VS Code")
    fact = mem.get_fact("favorite_editor")
    assert fact == "VS Code", f"Expected 'VS Code', got {fact}"

    # 2. Test Tasks
    task_msg = mem.add_task("Complete Jarvis architecture", "Tonight")
    assert "added" in task_msg
    pending = mem.get_pending_tasks()
    assert len(pending) > 0
    task_id = pending[-1]["id"]
    comp_msg = mem.complete_task(task_id)
    assert "completed" in comp_msg

    # 3. Test Interactions
    mem.log_interaction("Hello Jarvis", "Good evening, Sir.")

    print("Memory tests passed successfully!")

    # Cleanup test db
    test_db = mem.db_path
    if test_db.exists():
        try:
            os.remove(test_db)
        except Exception:
            pass


if __name__ == "__main__":
    test_memory_operations()
