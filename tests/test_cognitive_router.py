"""Unit tests for CognitiveRouter in Jarvis 2.0."""
import unittest
from unittest.mock import MagicMock

from jarvis.core.cognition.cognitive_router import CognitiveRouter, OS_ACTION_TOOLS, SELF_BUILD_TRIGGERS


class TestCognitiveRouter(unittest.TestCase):
    def setUp(self):
        self.router = CognitiveRouter(default_model="gemini-3.8-flash-medium")

    def test_classify_query_tier_screen_vision(self):
        """Screen vision intent is unconditionally classified as deep tier."""
        self.assertEqual(self.router.classify_query_tier("what is this", has_screen_intent=True), "deep")
        self.assertEqual(self.router.classify_query_tier("time", has_screen_intent=True), "deep")

    def test_classify_query_tier_analytical_questions(self):
        """Analytical and explanatory question openers route to deep tier."""
        self.assertEqual(self.router.classify_query_tier("why does Python have a GIL?"), "deep")
        self.assertEqual(self.router.classify_query_tier("explain how WASAPI ring buffers operate"), "deep")
        self.assertEqual(self.router.classify_query_tier("compare SQLite WAL mode to rollback journal"), "deep")
        self.assertEqual(self.router.classify_query_tier("how come the audio worker needs P-core pinning?"), "deep")

    def test_classify_query_tier_deep_indicators(self):
        """Coding, scheduling, and diagnostic indicators route to deep tier."""
        self.assertEqual(self.router.classify_query_tier("refactor the audio chunker"), "deep")
        self.assertEqual(self.router.classify_query_tier("check my exam schedule on Tuesday"), "deep")
        self.assertEqual(self.router.classify_query_tier("investigate the memory leak in the watchdog"), "deep")
        self.assertEqual(self.router.classify_query_tier("run the unit test suite"), "deep")

    def test_classify_query_tier_long_prompts(self):
        """Long or multi-sentence prompts default to deep tier."""
        long_prompt = "first do this and then after that make sure that you check the secondary file and notify me"
        self.assertEqual(self.router.classify_query_tier(long_prompt), "deep")

        multi_sentence = "Hello Jarvis. How is the system running today?"
        self.assertEqual(self.router.classify_query_tier(multi_sentence), "deep")

    def test_classify_query_tier_fast_reflexes(self):
        """Volume, device vitals, time, and greetings route to fast tier."""
        self.assertEqual(self.router.classify_query_tier("mute the volume"), "fast")
        self.assertEqual(self.router.classify_query_tier("what is the time"), "fast")
        self.assertEqual(self.router.classify_query_tier("battery status"), "fast")
        self.assertEqual(self.router.classify_query_tier("hello jarvis"), "fast")
        self.assertEqual(self.router.classify_query_tier("pause music"), "fast")

    def test_classify_query_tier_ambiguous_defaults_to_deep(self):
        """Ambiguous queries default to deep tier to preserve maximum intellect."""
        self.assertEqual(self.router.classify_query_tier("tomorrow afternoon"), "deep")

    def test_resolve_target_model_self_build_triggers(self):
        """Self-build and self-evolution triggers preserve the explicit configured model."""
        mock_bridge = MagicMock()
        mock_bridge.classify_prompt_tier.return_value = "claude-sonnet-4-6"

        for trigger in ("add feature for audio", "build yourself a new tool", "teach yourself a trick"):
            target = self.router.resolve_target_model(
                user_text=trigger,
                active_task_mode="general",
                active_configured_model="gemini-3.8-flash-high",
                antigravity_bridge=mock_bridge,
            )
            self.assertEqual(target, "gemini-3.8-flash-high")
            mock_bridge.classify_prompt_tier.assert_not_called()

    def test_resolve_target_model_claude_exhaustion_fallback(self):
        """When Claude is exhausted, router cascades to Flash High (complex) or Flash Medium (moderate)."""
        mock_bridge = MagicMock()
        mock_bridge.classify_prompt_tier.return_value = "claude-sonnet-4-6"
        mock_bridge.is_claude_exhausted.return_value = True

        # Complex task -> Flash High
        mock_bridge.is_task_complex.return_value = True
        target_complex = self.router.resolve_target_model(
            user_text="write an AST refactoring pipeline",
            active_task_mode="code",
            active_configured_model="claude-sonnet-4-6",
            antigravity_bridge=mock_bridge,
        )
        self.assertEqual(target_complex, "gemini-3.8-flash-high")

        # Moderate task -> Flash Medium
        mock_bridge.is_task_complex.return_value = False
        target_mod = self.router.resolve_target_model(
            user_text="fix the typo in the string",
            active_task_mode="code",
            active_configured_model="claude-sonnet-4-6",
            antigravity_bridge=mock_bridge,
        )
        self.assertEqual(target_mod, "gemini-3.8-flash-medium")

    def test_resolve_target_model_flash_pool_exhaustion_fallback(self):
        """When Gemini Flash pool is exhausted, router fails over across quota boundary to Claude Sonnet."""
        mock_bridge = MagicMock()
        mock_bridge.classify_prompt_tier.return_value = "gemini-3.8-flash-low"
        mock_bridge.is_claude_exhausted.return_value = False
        mock_bridge.is_gemini_flash_exhausted.return_value = True

        target = self.router.resolve_target_model(
            user_text="what time is it",
            active_task_mode="general",
            active_configured_model="gemini-3.8-flash-medium",
            antigravity_bridge=mock_bridge,
        )
        self.assertEqual(target, "claude-sonnet-4-6")

    def test_parse_model_switch_command(self):
        """Verifies voice model switching patterns."""
        self.assertEqual(self.router.parse_model_switch_command("switch to hybrid mode"), ("hybrid", None))
        self.assertEqual(self.router.parse_model_switch_command("activate token shield"), ("hybrid", None))
        self.assertEqual(self.router.parse_model_switch_command("switch to auto tiering"), ("auto", None))
        self.assertEqual(self.router.parse_model_switch_command("enable dynamic mode"), ("auto", None))
        self.assertEqual(self.router.parse_model_switch_command("switch to github copilot"), ("github", None))
        self.assertEqual(self.router.parse_model_switch_command("use gh"), ("github", None))
        self.assertEqual(self.router.parse_model_switch_command("switch to pro"), ("model", "gemini-3.8-flash-high"))
        self.assertEqual(self.router.parse_model_switch_command("switch to argon 4 medium"), ("model", "gemini-4-argon-medium"))
        self.assertEqual(self.router.parse_model_switch_command("set model to flash low"), ("model", "gemini-3.8-flash-low"))
        self.assertEqual(self.router.parse_model_switch_command("switch to claude"), ("model", "claude-sonnet-4-6"))
        self.assertEqual(self.router.parse_model_switch_command("switch to opus"), ("model", "claude-opus-4-6-thinking"))
        self.assertIsNone(self.router.parse_model_switch_command("just a regular question"))

    def test_determine_hybrid_route_screen_vision(self):
        """Screen vision with Gemini available routes to gemini_vision."""
        route = self.router.determine_hybrid_route(
            user_text="what is on my screen",
            active_tool_names=frozenset(),
            has_screen_intent=True,
            active_task_mode="general",
            has_antigravity=True,
            has_ollama=True,
            has_groq=True,
            has_gemini=True,
            has_copilot=True,
        )
        self.assertEqual(route, "gemini_vision")

    def test_determine_hybrid_route_os_actions(self):
        """Local OS action tools with Ollama available route to ollama_os (0 tokens)."""
        route = self.router.determine_hybrid_route(
            user_text="adjust the volume to 50%",
            active_tool_names=frozenset({"adjust_volume"}),
            has_screen_intent=False,
            active_task_mode="general",
            has_antigravity=True,
            has_ollama=True,
            has_groq=True,
            has_gemini=True,
            has_copilot=True,
        )
        self.assertEqual(route, "ollama_os")

    def test_determine_hybrid_route_deep_task(self):
        """Deep tasks route to Antigravity Apex."""
        route = self.router.determine_hybrid_route(
            user_text="refactor the memory engine to use WAL",
            active_tool_names=frozenset(),
            has_screen_intent=False,
            active_task_mode="code",
            has_antigravity=True,
            has_ollama=True,
            has_groq=True,
            has_gemini=True,
            has_copilot=True,
        )
        self.assertEqual(route, "antigravity_deep")

    def test_determine_hybrid_route_fast_reflex(self):
        """Fast conversational reflexes route to Groq LPU."""
        route = self.router.determine_hybrid_route(
            user_text="hello jarvis how are you",
            active_tool_names=frozenset(),
            has_screen_intent=False,
            active_task_mode="general",
            has_antigravity=True,
            has_ollama=True,
            has_groq=True,
            has_gemini=True,
            has_copilot=True,
        )
        self.assertEqual(route, "groq_reflex")


if __name__ == "__main__":
    unittest.main()
