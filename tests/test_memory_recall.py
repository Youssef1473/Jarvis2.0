"""Test for persistent episodic memory and dialogue recall."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager
from jarvis.skills.productivity import recall_past_conversation, set_memory_manager


def test_memory_recall():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db = os.path.join(tmp_dir, "test_memory_recall.db")
        config = load_config()
        config["memory"]["db_path"] = tmp_db
        memory = MemoryManager(config)
        set_memory_manager(memory)

        try:
            # Seed sample interactions for deterministic test baseline
            memory.log_interaction(
                "What is the weather today?",
                "The weather in London is currently sunny and 18 degrees Celsius, Sir."
            )
            memory.log_interaction(
                "Schedule a review meeting",
                "I have added the review meeting to your schedule, Sir."
            )

            # 1. Test get_recent_interactions
            recent = memory.get_recent_interactions(limit=5)
            print(f"Retrieved {len(recent)} recent interactions.")
            assert len(recent) == 2, f"Expected 2 seeded interactions, got {len(recent)}"

            # 2. Test search_interactions
            weather_results = memory.search_interactions("weather")
            print(f"Found {len(weather_results)} interactions matching 'weather'.")
            assert len(weather_results) > 0, "Expected at least one weather interaction"

            # 3. Test recall_past_conversation tool
            tool_output = recall_past_conversation("weather")
            print("\n--- Tool Output Sample ---")
            print(tool_output[:200] + "...")
            assert "Past Dialogue Logs Found" in tool_output
            assert "weather in london" in tool_output.lower()

            print("\nALL MEMORY RECALL TESTS PASSED CLEANLY!")
        finally:
            set_memory_manager(None)
            memory.close()


if __name__ == "__main__":
    test_memory_recall()
