"""Verification test suite for Council Batch 2: Notes, Hardware Watchdog, and Deep Research."""
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from jarvis.skills.base import SkillRegistry
import jarvis.skills.notes
import jarvis.skills.system_watchdog
import jarvis.skills.deep_research
import yaml
from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


def test_notes_skill():
    print("\n--- 1. Testing Spoken Note-Taker Suite ---")
    # Test taking a note
    res_take = SkillRegistry.execute(
        "take_quick_note",
        content="Take a note that the architecture review was approved by the council",
        category="Engineering"
    )
    print("Take note result:", res_take)
    assert "Noted, Sir" in res_take
    assert "architecture review was approved" in res_take

    # Test reading notes
    res_read = SkillRegistry.execute("read_recent_notes", count=3)
    print("Read notes result:\n", res_read)
    assert "Your Recent Notes" in res_read
    assert "architecture review was approved" in res_read

    # Test searching notes
    res_search = SkillRegistry.execute("search_notes", keyword="council")
    print("Search notes result:\n", res_search)
    assert "Found" in res_search and "council" in res_search.lower()


def test_system_watchdog():
    print("\n--- 2. Testing Hardware Watchdog Sentinel ---")
    # Immediate diagnostic scan
    res_health = SkillRegistry.execute("check_hardware_health")
    print("Hardware Diagnostic:\n", res_health)
    assert "Workstation Hardware Diagnostic" in res_health
    assert "CPU Load" in res_health
    assert "Memory Usage" in res_health

    # Start background watchdog
    res_start = SkillRegistry.execute("start_hardware_watchdog", interval_seconds=15)
    print("Start watchdog result:", res_start)
    assert "Hardware watchdog sentinel engaged" in res_start or "already active" in res_start

    # Double start should be harmless
    res_start2 = SkillRegistry.execute("start_hardware_watchdog", interval_seconds=15)
    assert "already active" in res_start2

    # Stop watchdog
    res_stop = SkillRegistry.execute("stop_hardware_watchdog")
    print("Stop watchdog result:", res_stop)
    assert "disengaged" in res_stop


def test_deep_research():
    res_research = SkillRegistry.execute("deep_research", topic="Python 3.13 free threaded GIL", max_sources=2)
    safe_excerpt = res_research[:350].encode('ascii', errors='replace').decode('ascii')
    assert "Deep research completed" in res_research or "Could not locate" in res_research
    assert isinstance(res_research, str) and len(res_research) > 0


def test_intent_routing_coverage():
    print("\n--- 4. Testing Brain Query Intent Routing Coverage ---")
    with open("config/config.yaml", "r") as f:
        cfg = yaml.safe_load(f)
    cfg["llm"]["provider"] = "hybrid"

    brain = JarvisBrain(cfg, MemoryManager(cfg), SafetyGate(cfg))

    queries = {
        "Jarvis, take a note that we deploy tomorrow": "take_quick_note",
        "Jarvis, read my recent notes": "read_recent_notes",
        "Jarvis, search notes for deploy": "search_notes",
        "Jarvis, check hardware health": "check_hardware_health",
        "Jarvis, start hardware watchdog": "start_hardware_watchdog",
        "Jarvis, deep research on artificial general intelligence": "deep_research",
    }

    for q, expected_tool in queries.items():
        tools = brain._select_tools_for_query(q)
        print(f"Query: '{q}' -> Selected: {set(tools)}")
        assert expected_tool in tools, f"Expected {expected_tool} for query '{q}', got {tools}"

    print("\nALL COUNCIL BATCH 2 TESTS PASSED 100%!")


if __name__ == "__main__":
    test_notes_skill()
    test_system_watchdog()
    test_deep_research()
    test_intent_routing_coverage()
