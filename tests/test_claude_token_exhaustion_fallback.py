"""Tests for Claude Sonnet Token Exhaustion Fallback and Dynamic Flash Tiering.
Verifies that when Claude Sonnet runs out of tokens or hits rate limits:
- It switches to Gemini 3.8 Flash High for complex tasks.
- It switches to Gemini 3.8 Flash Medium for moderate/standard tasks.
- Exhaustion status is tracked cleanly and can be manually or explicitly reset.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

from jarvis.core.antigravity_bridge import AntigravityBridge
from jarvis.core.brain import JarvisBrain
from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


class TestClaudeTokenExhaustionFallback(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.bridge = AntigravityBridge(self.config)
        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate(self.config)
        self.brain = JarvisBrain(self.config, self.memory, self.safety)

    def test_exhaustion_state_lifecycle(self):
        """Verifies initial state, marking exhausted, and resetting exhaustion."""
        self.assertFalse(self.bridge.is_claude_exhausted())
        self.bridge.mark_claude_exhausted(duration_seconds=60.0, reason="429 Rate limit exceeded")
        self.assertTrue(self.bridge.is_claude_exhausted())
        self.assertEqual(self.bridge._claude_exhaustion_reason, "429 Rate limit exceeded")

        self.bridge.reset_claude_exhaustion()
        self.assertFalse(self.bridge.is_claude_exhausted())
        self.assertEqual(self.bridge._claude_exhaustion_reason, "")

    def test_is_token_exhaustion_error(self):
        """Verifies detection of token, quota, and rate limit errors across providers."""
        self.assertTrue(self.bridge._is_token_exhaustion_error("Error 429: Too Many Requests"))
        self.assertTrue(self.bridge._is_token_exhaustion_error("RESOURCE_EXHAUSTED: quota exceeded"))
        self.assertTrue(self.bridge._is_token_exhaustion_error("Rate limit reached on token per minute"))
        self.assertTrue(self.bridge._is_token_exhaustion_error("Your credit balance is too low"))
        self.assertTrue(self.bridge._is_token_exhaustion_error("Exceeded maximum token limit for this session"))
        self.assertTrue(self.bridge._is_token_exhaustion_error("Model capacity exhausted or overloaded"))

        # Unrelated errors should not trigger token exhaustion
        self.assertFalse(self.bridge._is_token_exhaustion_error("SyntaxError: invalid syntax on line 12"))
        self.assertFalse(self.bridge._is_token_exhaustion_error("Connection refused to 127.0.0.1:9222"))
        self.assertFalse(self.bridge._is_token_exhaustion_error("File not found: es.exe"))

    def test_is_task_complex_classification(self):
        """Verifies distinction between complex engineering tasks vs moderate/routine tasks."""
        # Complex tasks -> True (Flash High)
        self.assertTrue(self.bridge.is_task_complex("Refactor the entire concurrency model to eliminate thread deadlocks"))
        self.assertTrue(self.bridge.is_task_complex("Analyze system architecture and multi-file data structures for self-evolution"))
        self.assertTrue(self.bridge.is_task_complex("Jarvis, optimize yourself across multiple files in Jarvis_2.0"))
        self.assertTrue(self.bridge.is_task_complex("Conduct a security audit of our sandbox threat model and execute formal verification"))
        self.assertTrue(self.bridge.is_task_complex("Derive the mathematical proof for the optimization algorithm and benchmark memory leak with py-spy"))
        self.assertTrue(self.bridge.is_task_complex("Step 1: Inspect the AST. Step 2: Rewrite the parser. Step 3: Verify all test cases."))

        # Moderate/Standard tasks -> False (Flash Medium)
        self.assertFalse(self.bridge.is_task_complex("Show git status"))
        self.assertFalse(self.bridge.is_task_complex("Run project unit tests"))
        self.assertFalse(self.bridge.is_task_complex("What does this helper function return?"))
        self.assertFalse(self.bridge.is_task_complex("Inspect git diff for line 40"))
        self.assertFalse(self.bridge.is_task_complex("Proceed with option a"))
        self.assertFalse(self.bridge.is_task_complex("Fix that bug"))
        self.assertFalse(self.bridge.is_task_complex("Yes, go ahead"))

    def test_classify_prompt_tier_when_claude_exhausted(self):
        """Verifies classify_prompt_tier routes to Flash High or Flash Medium when Claude is exhausted."""
        complex_prompt = "Refactor the entire memory architecture and optimize yourself for multi-thread safety"
        moderate_prompt = "Check git status and run project tests in python"

        # Baseline: Claude available -> Claude Sonnet 4.6
        self.bridge.reset_claude_exhaustion()
        self.assertEqual(self.bridge.classify_prompt_tier(complex_prompt), "claude-sonnet-4-6")
        self.assertEqual(self.bridge.classify_prompt_tier(moderate_prompt), "claude-sonnet-4-6")

        # When Claude is exhausted:
        self.bridge.mark_claude_exhausted(duration_seconds=300.0, reason="Quota exceeded")
        # Complex task -> Flash High
        self.assertEqual(self.bridge.classify_prompt_tier(complex_prompt), "gemini-3.8-flash-high")
        # Moderate task -> Flash Medium
        self.assertEqual(self.bridge.classify_prompt_tier(moderate_prompt), "gemini-3.8-flash-medium")

        # Active code mode follow-up turn when exhausted -> Flash Medium
        self.assertEqual(self.bridge.classify_prompt_tier("proceed with option b", task_mode="code"), "gemini-3.8-flash-medium")

    def test_get_candidate_cascade_priorities(self):
        """Verifies candidate cascade places Flash High or Flash Medium first when Claude is exhausted."""
        complex_prompt = "Redesign the concurrency model and fix thread safety"
        moderate_prompt = "Run unit tests and check git diff"

        # Claude available: Claude is first
        self.bridge.reset_claude_exhaustion()
        cascade_complex = self.bridge.get_candidate_cascade("claude-sonnet-4-6", prompt=complex_prompt)
        self.assertEqual(cascade_complex[0], "claude-sonnet-4-6")
        self.assertEqual(cascade_complex[2], "gemini-3.8-flash-high")

        cascade_mod = self.bridge.get_candidate_cascade("claude-sonnet-4-6", prompt=moderate_prompt)
        self.assertEqual(cascade_mod[0], "claude-sonnet-4-6")
        self.assertEqual(cascade_mod[2], "gemini-3.8-flash-medium")

        # Claude exhausted: Flash fallback is FIRST
        self.bridge.mark_claude_exhausted(duration_seconds=300.0, reason="429 rate limit")
        cascade_exhausted_complex = self.bridge.get_candidate_cascade("claude-sonnet-4-6", prompt=complex_prompt)
        self.assertEqual(cascade_exhausted_complex[0], "gemini-3.8-flash-high")
        self.assertEqual(cascade_exhausted_complex[1], "gemini-3.8-flash-medium")

        cascade_exhausted_mod = self.bridge.get_candidate_cascade("claude-sonnet-4-6", prompt=moderate_prompt)
        self.assertEqual(cascade_exhausted_mod[0], "gemini-3.8-flash-medium")
        self.assertEqual(cascade_exhausted_mod[1], "gemini-3.8-flash-high")

    def test_generate_response_dynamic_exhaustion_failover(self):
        """Verifies generate_response marks Claude exhausted on 429 and immediately cascades to Flash High/Medium."""
        self.bridge.reset_claude_exhaustion()

        # Mock Claude returning a 429 token limit error on first attempt, then Flash succeeding
        def mock_generate_batch(prompt, model, timeout=90.0, conversation_id=None):
            if "claude" in model:
                return {
                    "status": "ERROR",
                    "response": "429 RESOURCE_EXHAUSTED: Rate limit reached for model claude-sonnet-4-6 on tokens per day.",
                    "duration_seconds": 0.5,
                }
            elif model == "gemini-3.8-flash-high":
                return {
                    "status": "SUCCESS",
                    "response": "Architectural refactoring complete via Gemini 3.8 Flash High, Sir.",
                    "duration_seconds": 1.2,
                }
            elif model == "gemini-3.8-flash-medium":
                return {
                    "status": "SUCCESS",
                    "response": "Test suite executed via Gemini 3.8 Flash Medium, Sir.",
                    "duration_seconds": 0.9,
                }
            return {"status": "ERROR", "response": "Unknown model"}

        complex_task = "Analyze system architecture and refactor concurrency model"
        with patch.object(self.bridge, "is_available", return_value=True), \
             patch.object(self.bridge, "_generate_batch", side_effect=mock_generate_batch):
            self.bridge.use_daemon = False  # Test batch cascade
            res = self.bridge.generate_response(prompt=complex_task, model="claude-sonnet-4-6")
            self.assertEqual(res.get("status"), "SUCCESS")
            self.assertIn("Gemini 3.8 Flash High", res.get("response"))
            # Claude is now marked exhausted
            self.assertTrue(self.bridge.is_claude_exhausted())

        # Second turn with moderate task should route directly to Flash Medium
        moderate_task = "Check git status and run tests"
        with patch.object(self.bridge, "is_available", return_value=True), \
             patch.object(self.bridge, "_generate_batch", side_effect=mock_generate_batch):
            res2 = self.bridge.generate_response(prompt=moderate_task, model="claude-sonnet-4-6")
            self.assertEqual(res2.get("status"), "SUCCESS")
            self.assertIn("Gemini 3.8 Flash Medium", res2.get("response"))

    def test_brain_antigravity_query_switches_when_exhausted(self):
        """Verifies JarvisBrain switches target_model to Flash High or Flash Medium when Claude is exhausted."""
        self.brain.antigravity_bridge.mark_claude_exhausted(duration_seconds=300.0, reason="Out of tokens")

        complex_query = "Jarvis, optimize yourself and refactor the entire concurrency architecture"
        moderate_query = "Jarvis, check git status and run the tests"

        with patch.object(self.brain.antigravity_bridge, "generate_response") as mock_gen:
            mock_gen.return_value = {"status": "SUCCESS", "response": "Acknowledged, Sir.", "duration_seconds": 0.5}

            # Complex query -> target_model becomes gemini-3.8-flash-high
            self.brain._process_antigravity_query(complex_query)
            args, kwargs = mock_gen.call_args
            self.assertEqual(kwargs.get("model"), "gemini-3.8-flash-high")

            # Moderate query -> target_model becomes gemini-3.8-flash-medium
            self.brain._process_antigravity_query(moderate_query)
            args2, kwargs2 = mock_gen.call_args
            self.assertEqual(kwargs2.get("model"), "gemini-3.8-flash-medium")

    def test_explicit_user_directive_resets_exhaustion(self):
        """Verifies that an explicit user request ('use claude', 'with sonnet') resets exhaustion cooldown."""
        self.brain.antigravity_bridge.mark_claude_exhausted(duration_seconds=300.0, reason="Out of tokens")
        self.assertTrue(self.brain.antigravity_bridge.is_claude_exhausted())

        with patch.object(self.brain.antigravity_bridge, "generate_response") as mock_gen:
            mock_gen.return_value = {"status": "SUCCESS", "response": "Using Claude Sonnet, Sir.", "duration_seconds": 0.5}
            self.brain._process_antigravity_query("Jarvis, use claude to inspect this function")
            args, kwargs = mock_gen.call_args
            self.assertEqual(kwargs.get("model"), "claude-sonnet-4-6")
            # Exhaustion was reset
            self.assertFalse(self.brain.antigravity_bridge.is_claude_exhausted())

    def test_gemini_flash_unified_exhaustion_lifecycle(self):
        """Verifies that the unified Gemini Flash pool tracks and clears quota exhaustion across all variants."""
        self.assertFalse(self.bridge.is_gemini_flash_exhausted())
        self.bridge.mark_gemini_flash_exhausted(duration_seconds=120.0, reason="429 Flash Quota Reached")
        self.assertTrue(self.bridge.is_gemini_flash_exhausted())
        self.assertEqual(self.bridge._gemini_flash_exhaustion_reason, "429 Flash Quota Reached")

        self.bridge.reset_gemini_flash_exhaustion()
        self.assertFalse(self.bridge.is_gemini_flash_exhausted())
        self.assertEqual(self.bridge._gemini_flash_exhaustion_reason, "")

    def test_classify_prompt_tier_when_gemini_flash_exhausted(self):
        """Verifies classify_prompt_tier routes to Claude Sonnet when the unified Gemini Flash pool is exhausted."""
        reflex_prompt = "Jarvis, what's the weather today?"
        reasoning_prompt = "Analyze the trade-offs between monolithic and microservice architecture"
        general_prompt = "Draft a polite response to this email"

        # Baseline: Flash available
        self.bridge.reset_gemini_flash_exhaustion()
        self.bridge.reset_claude_exhaustion()
        self.assertEqual(self.bridge.classify_prompt_tier(reflex_prompt), "gemini-3.8-flash-low")
        self.assertEqual(self.bridge.classify_prompt_tier(reasoning_prompt), "gemini-3.8-flash-high")
        self.assertEqual(self.bridge.classify_prompt_tier(general_prompt), "gemini-3.8-flash-medium")

        # When Flash is exhausted: all route across quota boundary to Claude Sonnet 4.6
        self.bridge.mark_gemini_flash_exhausted(duration_seconds=300.0, reason="Quota reached")
        self.assertEqual(self.bridge.classify_prompt_tier(reflex_prompt), "claude-sonnet-4-6")
        self.assertEqual(self.bridge.classify_prompt_tier(reasoning_prompt), "claude-sonnet-4-6")
        self.assertEqual(self.bridge.classify_prompt_tier(general_prompt), "claude-sonnet-4-6")

    def test_get_candidate_cascade_when_gemini_flash_exhausted(self):
        """Verifies candidate cascade avoids redundant Flash models and prioritizes Claude when Flash pool is exhausted."""
        self.bridge.reset_claude_exhaustion()
        self.bridge.mark_gemini_flash_exhausted(duration_seconds=300.0, reason="Quota reached")
        cascade = self.bridge.get_candidate_cascade("gemini-3.8-flash-medium")
        self.assertEqual(cascade[0], "claude-sonnet-4-6")
        self.assertEqual(cascade[1], "claude-opus-4-6-thinking")

    def test_brain_fails_over_to_claude_when_flash_exhausted(self):
        """Verifies JarvisBrain switches target_model to Claude Sonnet when Gemini Flash is exhausted."""
        self.brain.antigravity_bridge.mark_gemini_flash_exhausted(duration_seconds=300.0, reason="Out of Flash tokens")
        self.brain.antigravity_bridge.reset_claude_exhaustion()

        with patch.object(self.brain.antigravity_bridge, "generate_response") as mock_gen:
            mock_gen.return_value = {"status": "SUCCESS", "response": "Acknowledged, Sir.", "duration_seconds": 0.5}
            self.brain._process_antigravity_query("Jarvis, summarize the morning briefing")
            args, kwargs = mock_gen.call_args
            self.assertEqual(kwargs.get("model"), "claude-sonnet-4-6")

            # Explicit user directive resets exhaustion
            self.brain._process_antigravity_query("Jarvis, use flash high to analyze this")
            args2, kwargs2 = mock_gen.call_args
            self.assertEqual(kwargs2.get("model"), "gemini-3.8-flash-high")
            self.assertFalse(self.brain.antigravity_bridge.is_gemini_flash_exhausted())


if __name__ == "__main__":
    unittest.main()
