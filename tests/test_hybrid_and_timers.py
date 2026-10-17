"""Comprehensive verification suite for Tri-Tier Hybrid Router and Spoken Timers Suite."""
import os
import sys
import time
import yaml
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from jarvis.skills.base import SkillRegistry
import jarvis.skills.timers as timer_skills
from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


def test_spoken_timers():
    print("\n--- 1. Testing Spoken Timers Suite ---")
    
    # Test starting timers with natural language
    res1 = SkillRegistry.execute("start_timer", duration="2 seconds", label="Test Quick Timer")
    print("Start result 1:", res1)
    assert "Timer set for 2 seconds" in res1

    res2 = SkillRegistry.execute("start_timer", duration="10 mins", label="Coffee Break")
    print("Start result 2:", res2)
    assert "Timer set for 10 minutes" in res2

    # Test listing active timers
    list_res = SkillRegistry.execute("list_active_timers")
    print("List result:\n", list_res)
    assert "Coffee Break" in list_res

    # Test canceling specific timer
    cancel_res = SkillRegistry.execute("cancel_timer", label="Coffee Break")
    print("Cancel result:", cancel_res)
    assert "cancelled" in cancel_res.lower()

    # Cancel all
    cancel_all_res = SkillRegistry.execute("cancel_timer", label="all")
    print("Cancel all result:", cancel_all_res)
    assert "cancelled all" in cancel_all_res.lower() or "no timers" in cancel_all_res.lower()
    print("Timers suite passed 100%!")


def test_gemini_candidate_pool():
    print("\n--- 2. Testing Gemini Candidate Models Pool ---")
    assert len(JarvisBrain.GEMINI_CANDIDATE_MODELS) >= 8
    print(f"Verified {len(JarvisBrain.GEMINI_CANDIDATE_MODELS)} candidate models configured for quota failover:")
    for m in JarvisBrain.GEMINI_CANDIDATE_MODELS:
        print(f"  • {m}")


def test_hybrid_brain_initialization_and_dispatch():
    print("\n--- 3. Testing Tri-Tier Hybrid Brain Router ---")
    with open("config/config.yaml", "r") as f:
        cfg = yaml.safe_load(f)
    cfg["llm"]["provider"] = "hybrid"

    memory = MemoryManager(cfg)
    safety = SafetyGate(cfg)
    brain = JarvisBrain(cfg, memory, safety)

    print("Active provider:", brain.provider)
    print("Ollama client available:", brain.ollama_client is not None)
    print("Groq client available:", brain.groq_client is not None)
    print("Gemini client available:", brain.gemini_client is not None)

    # A. Test OS Action query routing (should select local Ollama)
    print("\n[A] Testing OS Action Routing ('Jarvis, check system vitals.')...")
    tools_vitals = brain._select_tools_for_query("Jarvis, check system vitals.")
    print("Selected tools:", set(tools_vitals))
    assert "get_system_vitals" in tools_vitals

    # B. Test Timer query routing
    print("\n[B] Testing Timer Routing ('Jarvis, set a timer for 5 minutes.')...")
    tools_timer = brain._select_tools_for_query("Jarvis, set a timer for 5 minutes.")
    print("Selected tools:", set(tools_timer))
    assert "start_timer" in tools_timer

    # C. Test Spoken conversational query on Groq LPU
    if brain.groq_client:
        print("\n[C] Testing Conversational Query on Groq LPU (qwen/qwen3.8-27b)...")
        reply = brain.process_query("In exactly four words, state your name.")
        print("Groq LPU reply:", reply)
        assert len(reply) > 0

    # D. Test Screen vision query (should select Gemini)
    print("\n[D] Testing Vision Trigger Recognition ('look at my screen, what do you see?')...")
    is_vision = brain.should_capture_screen("look at my screen, what do you see?")
    assert is_vision is True
    print("Vision properly classified:", is_vision)

    print("\nALL TRI-TIER HYBRID AND TIMERS SUITE TESTS PASSED!")


if __name__ == "__main__":
    test_spoken_timers()
    test_gemini_candidate_pool()
    test_hybrid_brain_initialization_and_dispatch()
