"""Test for persistent episodic memory and dialogue recall."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager
from jarvis.skills.productivity import recall_past_conversation


def test_memory_recall():
    config = load_config()
    memory = MemoryManager(config)

    # 1. Test get_recent_interactions
    recent = memory.get_recent_interactions(limit=5)
    print(f"Retrieved {len(recent)} recent interactions.")
    assert len(recent) > 0, "Expected non-empty recent interactions"

    # 2. Test search_interactions
    weather_results = memory.search_interactions("weather")
    print(f"Found {len(weather_results)} interactions matching 'weather'.")
    assert len(weather_results) > 0, "Expected at least one weather interaction"

    # 3. Test recall_past_conversation tool
    tool_output = recall_past_conversation("weather")
    print("\n--- Tool Output Sample ---")
    print(tool_output[:200] + "...")
    assert "Past Dialogue Logs Found" in tool_output

    print("\nALL MEMORY RECALL TESTS PASSED CLEANLY!")


if __name__ == "__main__":
    test_memory_recall()
