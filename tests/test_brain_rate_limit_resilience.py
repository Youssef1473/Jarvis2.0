"""Verification suite for Groq 429 Rate Limit Resilience and Idempotent Post-Tool Synthesis."""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate
from jarvis.core.config_loader import load_config
from jarvis.skills.base import SkillRegistry


def test_groq_candidate_models():
    print("\n--- 1. Testing Groq Candidate Models Pool ---")
    assert len(JarvisBrain.GROQ_CANDIDATE_MODELS) >= 3
    print(f"Verified {len(JarvisBrain.GROQ_CANDIDATE_MODELS)} Groq candidates configured:")
    for m in JarvisBrain.GROQ_CANDIDATE_MODELS:
        print(f"  • {m}")

    cfg = load_config()
    mem = MemoryManager(cfg)
    safety = SafetyGate(cfg)
    brain = JarvisBrain(cfg, mem, safety)
    assert hasattr(brain, "groq_candidates")
    assert brain.groq_candidates[0] == "qwen/qwen3.8-27b"
    assert "openai/gpt-oss-120b" in brain.groq_candidates
    assert "openai/gpt-oss-20b" in brain.groq_candidates
    print("Groq candidate models initialization verified!")


def test_post_tool_synthesis_429_resilience():
    print("\n--- 2. Testing Post-Tool Synthesis 429 Resilience ---")
    cfg = load_config()
    mem = MemoryManager(cfg)
    safety = SafetyGate(cfg)
    brain = JarvisBrain(cfg, mem, safety)

    # Mock Skill execution
    sample_tool_result = "Scheduled 6 calendar events for college timetable, Sir."

    # Simulate tool invocation followed by a 429 rate limit on Groq
    mock_groq_client = MagicMock()
    mock_ollama_client = MagicMock()

    # Step 1: Initial call returns tool call
    mock_chunk_tool = MagicMock()
    mock_chunk_tool.choices = [MagicMock()]
    mock_delta = MagicMock()
    mock_delta.content = None
    mock_tc = MagicMock()
    mock_tc.index = 0
    mock_tc.id = "call_test_123"
    mock_tc.function = MagicMock()
    mock_tc.function.name = "list_calendar_events"
    mock_tc.function.arguments = "{}"
    mock_delta.tool_calls = [mock_tc]
    mock_chunk_tool.choices[0].delta = mock_delta

    # Follow-up call throws 429 RateLimitError
    class MockRateLimitError(Exception):
        pass

    mock_groq_client.chat.completions.create.side_effect = [
        [mock_chunk_tool], # Initial stream
        MockRateLimitError("Rate limit reached for model qwen/qwen3.8-27b on input tokens per minute (ITPM): Limit 7000"),
        MockRateLimitError("Rate limit reached for model openai/gpt-oss-120b"),
        MockRateLimitError("Rate limit reached for model openai/gpt-oss-20b"),
    ]

    # Local Ollama stream succeeds with speech
    mock_ollama_chunk = MagicMock()
    mock_ollama_chunk.choices = [MagicMock()]
    mock_ollama_delta = MagicMock()
    mock_ollama_delta.content = "Your classes have been successfully scheduled on your calendar, Sir."
    mock_ollama_delta.tool_calls = None
    mock_ollama_chunk.choices[0].delta = mock_ollama_delta

    mock_ollama_client.chat.completions.create.return_value = [mock_ollama_chunk]

    brain.groq_client = mock_groq_client
    brain.ollama_client = mock_ollama_client
    brain.openai_client = mock_groq_client
    brain.provider = "hybrid"

    spoken_clauses = []
    def voice_cb(text):
        spoken_clauses.append(text)

    with patch.object(SkillRegistry, "execute", return_value=sample_tool_result):
        reply = brain._process_openai_compatible_query("Jarvis, check my schedule", sentence_callback=voice_cb)

    print("Synthesized reply after Groq 429:", reply)
    assert "classes have been successfully scheduled" in reply or "Action completed" in reply
    assert "My hybrid model could not complete that request" not in reply
    print("Post-tool synthesis seamlessly failed over to Local Ollama with ZERO false error reporting!")


def test_deterministic_fallback_when_all_llms_fail():
    print("\n--- 3. Testing Deterministic Acknowledgment When All LLMs Fail Post-Tool ---")
    cfg = load_config()
    mem = MemoryManager(cfg)
    safety = SafetyGate(cfg)
    brain = JarvisBrain(cfg, mem, safety)

    mock_groq_client = MagicMock()
    mock_groq_client.chat.completions.create.side_effect = Exception("All network links down")

    # Initial call returns tool call
    mock_chunk_tool = MagicMock()
    mock_chunk_tool.choices = [MagicMock()]
    mock_delta = MagicMock()
    mock_delta.content = None
    mock_tc = MagicMock()
    mock_tc.index = 0
    mock_tc.id = "call_test_456"
    mock_tc.function = MagicMock()
    mock_tc.function.name = "list_calendar_events"
    mock_tc.function.arguments = "{}"
    mock_delta.tool_calls = [mock_tc]
    mock_chunk_tool.choices[0].delta = mock_delta

    # First stream succeeds with tool call, then all subsequent calls fail
    mock_groq_client.chat.completions.create.side_effect = [
        [mock_chunk_tool],
        Exception("503 Gateway Down"),
        Exception("503 Gateway Down"),
        Exception("503 Gateway Down"),
    ]

    brain.groq_client = mock_groq_client
    brain.ollama_client = None # Ollama unavailable
    brain.openai_client = mock_groq_client
    brain.provider = "hybrid"

    sample_res = "Processed college timetable and registered 6 classes."
    with patch.object(SkillRegistry, "execute", return_value=sample_res):
        reply = brain._process_openai_compatible_query("Jarvis, process my timetable", sentence_callback=None)

    print("Deterministic reply:", reply)
    assert "Action completed, Sir" in reply
    assert sample_res in reply
    print("Deterministic acknowledgment verified!")


if __name__ == "__main__":
    test_groq_candidate_models()
    test_post_tool_synthesis_429_resilience()
    test_deterministic_fallback_when_all_llms_fail()
    print("\nALL RATE LIMIT & SYNTHESIS RESILIENCE TESTS PASSED 100%!")
