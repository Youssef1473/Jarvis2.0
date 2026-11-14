"""Verification test suite for Jarvis 2.0 Flagship Features Suite."""
import sys
from pathlib import Path

from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

import yaml

from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate
from jarvis.skills.base import SkillRegistry


def test_protocols():
    from unittest.mock import patch

    from jarvis.core.security_boundaries import TurnTaintTracker

    TurnTaintTracker.get_instance().reset_turn()
    print("\n--- 1. Testing Multi-Action Protocols ---")
    with patch("jarvis.skills.protocols.launch_application", return_value="Application launched"):
        res_work = SkillRegistry.execute("execute_protocol", protocol_name="work")
        print("Work protocol:", res_work)
        assert "Work protocol initiated" in res_work

        TurnTaintTracker.get_instance().reset_turn()
        res_night = SkillRegistry.execute("execute_protocol", protocol_name="night")
        print("Night protocol:", res_night)
        assert "Night protocol engaged" in res_night


def test_system_maintenance():
    import pytest

    from jarvis.core.security_boundaries import (
        DualKeyApprovalManager,
        SecurityBoundaryViolation,
        TurnTaintTracker,
    )
    TurnTaintTracker.get_instance().reset_turn()
    print("\n--- 2. Testing System Maintenance & RAM Hogs ---")
    # Read-only diagnostics do not require authorization
    hogs = SkillRegistry.execute("inspect_ram_hogs")
    print("RAM Hogs excerpt:\n", hogs[:150])
    assert "Top Memory Consumers" in hogs

    TurnTaintTracker.get_instance().reset_turn()
    # 1. Unapproved optimize_workstation must fail closed
    with pytest.raises(SecurityBoundaryViolation):
        SkillRegistry.execute("optimize_workstation")

    # 2. Approved optimize_workstation executes cleanly
    approval_mgr = DualKeyApprovalManager.get_instance()
    approval_mgr.set_approval_handler(lambda req: True)
    try:
        opt = SkillRegistry.execute("optimize_workstation")
        print("Optimization result:", opt)
        assert "optimization complete" in opt.lower()
    finally:
        approval_mgr.set_approval_handler(None)


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

    from unittest.mock import patch
    with patch("os.startfile"):
        f_res = SkillRegistry.execute("open_user_folder", folder_name="downloads")
        print("Folder open result:", f_res)
        assert "Opened your downloads folder" in f_res


def test_intent_routing_coverage():
    print("\n--- 6. Testing Brain Query Intent Routing ---")
    with open("config/config.yaml", "r") as f:
        cfg = yaml.safe_load(f)
    cfg["llm"]["provider"] = "hybrid"

    brain = JarvisBrain(cfg, MemoryManager(cfg), SafetyGate(cfg), antigravity_resolver=lambda: None)

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
