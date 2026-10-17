"""Verification test suite for Jarvis 2.0 Flagship Features Suite."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from jarvis.skills.base import SkillRegistry
import jarvis.skills.protocols
import jarvis.skills.system_maintenance
import jarvis.skills.media_dj
import jarvis.skills.briefing
import jarvis.skills.project_manager
import yaml
from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


def test_protocols():
    print("\n--- 1. Testing Multi-Action Protocols ---")
    res_work = SkillRegistry.execute("execute_protocol", protocol_name="work")
    print("Work protocol:", res_work)
    assert "Work protocol initiated" in res_work

    res_night = SkillRegistry.execute("execute_protocol", protocol_name="night")
    print("Night protocol:", res_night)
    assert "Night protocol engaged" in res_night


def test_system_maintenance():
    print("\n--- 2. Testing System Maintenance & RAM Hogs ---")
    hogs = SkillRegistry.execute("inspect_ram_hogs")
    print("RAM Hogs excerpt:\n", hogs[:150])
    assert "Top Memory Consumers" in hogs

    opt = SkillRegistry.execute("optimize_workstation")
    print("Optimization result:", opt)
    assert "optimization complete" in opt.lower()


def test_media_dj():
    print("\n--- 3. Testing Media DJ ---")
    res = SkillRegistry.execute("play_youtube_media", query="lo-fi hip hop on youtube")
    print("Media DJ result:", res)
    assert "Streaming" in res and "lo-fi" in res


def test_daily_briefing():
    print("\n--- 4. Testing Cinematic Daily Briefing ---")
    briefing = SkillRegistry.execute("get_daily_briefing")
    print("Daily Briefing excerpt:\n", briefing[:250])
    assert "Good day, Sir" in briefing or "nominal" in briefing


def test_project_manager():
    print("\n--- 5. Testing Project & Folder Manager ---")
    p_res = SkillRegistry.execute("open_project", project_name="Jarvis_2.0")
    print("Project open result:", p_res)
    assert "opened in Visual Studio Code" in p_res or "Jarvis_2.0" in p_res

    f_res = SkillRegistry.execute("open_user_folder", folder_name="downloads")
    print("Folder open result:", f_res)
    assert "Opened your downloads folder" in f_res


def test_intent_routing_coverage():
    print("\n--- 6. Testing Brain Query Intent Routing ---")
    with open("config/config.yaml", "r") as f:
        cfg = yaml.safe_load(f)
    cfg["llm"]["provider"] = "hybrid"

    brain = JarvisBrain(cfg, MemoryManager(cfg), SafetyGate(cfg))

    queries = {
        "Jarvis, initiate work protocol": "execute_protocol",
        "Jarvis, give me my morning briefing": "get_daily_briefing",
        "Jarvis, who is eating all my RAM?": "inspect_ram_hogs",
        "Jarvis, optimize my PC": "optimize_workstation",
        "Jarvis, play Interstellar soundtrack on YouTube": "play_youtube_media",
        "Jarvis, open project Jarvis in VS Code": "open_project",
        "Jarvis, open downloads folder": "open_user_folder",
    }

    for q, expected_tool in queries.items():
        tools = brain._select_tools_for_query(q)
        print(f"Query: '{q}' -> Selected: {set(tools)}")
        assert expected_tool in tools, f"Expected {expected_tool} for query '{q}', got {tools}"

    print("\nALL 6 FLAGSHIP FEATURE TESTS PASSED 100%!")


if __name__ == "__main__":
    test_protocols()
    test_system_maintenance()
    test_media_dj()
    test_daily_briefing()
    test_project_manager()
    test_intent_routing_coverage()
