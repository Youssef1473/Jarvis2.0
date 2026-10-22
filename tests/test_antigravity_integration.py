"""Integration test suite for Antigravity Google AI Pro CLI bridge in Jarvis 2.0."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from jarvis.core.config_loader import load_config
from jarvis.core.antigravity_bridge import AntigravityBridge
from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


class TestAntigravityIntegration(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate(self.config)
        self.bridge = AntigravityBridge(self.config)

    def test_bridge_discovery_and_model_resolution(self):
        """Verifies Antigravity CLI model catalog mappings and alias resolution."""
        models = self.bridge.list_models()
        self.assertGreaterEqual(len(models), 6)
        self.assertIn("gemini-3.8-flash-high", models)
        self.assertIn("gemini-3.8-flash-medium", models)
        self.assertIn("gemini-3.8-flash-low", models)
        self.assertIn("claude-sonnet-4-6", models)
        self.assertIn("claude-opus-4-6-thinking", models)
        self.assertIn("gemini-4-argon-high", models)
        self.assertNotIn("gemini-3.1-pro-low", models)
        self.assertNotIn("gemini-3.1-pro-high", models)
        self.assertNotIn("gemini-3.7-flash-low", models)
        self.assertNotIn("gemini-3.6-flash-low", models)

        # Verify aliases
        self.assertEqual(self.bridge.resolve_model("pro"), "gemini-3.8-flash-high")
        self.assertEqual(self.bridge.resolve_model("gemini-pro"), "gemini-3.8-flash-high")
        self.assertEqual(self.bridge.resolve_model("flash"), "gemini-3.8-flash-low")
        self.assertEqual(self.bridge.resolve_model("claude"), "claude-sonnet-4-6")
        self.assertEqual(self.bridge.resolve_model("sonnet"), "claude-sonnet-4-6")
        self.assertEqual(self.bridge.resolve_model("opus"), "claude-opus-4-6-thinking")
        self.assertEqual(self.bridge.resolve_model("argon"), "gemini-4-argon-high")

    def test_bridge_binary_discovery_mocked(self):
        """Verifies is_available behavior when binary is present vs absent."""
        with patch.object(AntigravityBridge, "_find_agy_executable", return_value="mock_agy.exe"), \
             patch("os.path.exists", return_value=True):
            mock_bridge = AntigravityBridge(self.config)
            self.assertTrue(mock_bridge.is_available())
        with patch.object(AntigravityBridge, "_find_agy_executable", return_value=None):
            mock_bridge = AntigravityBridge(self.config)
            self.assertFalse(mock_bridge.is_available())

    def test_antigravity_batch_generation(self):
        """Verifies real response generation with Google Antigravity session (live smoke test)."""
        if os.environ.get("JARVIS_RUN_LIVE_INTEGRATION") != "1":
            self.skipTest("Live Antigravity integration requires JARVIS_RUN_LIVE_INTEGRATION=1")
        if not self.bridge.is_available():
            self.skipTest("Live Antigravity integration requires agy binary installed on host")
        res = self.bridge.generate_response(
            prompt="Respond with exactly two words: System Online.",
            model="gemini-3.8-flash-low",
            timeout=30.0
        )
        self.assertEqual(res.get("status"), "SUCCESS")
        self.assertIn("Online", res.get("response", ""))
        self.assertGreater(res.get("duration_seconds", 0), 0)

    def test_antigravity_batch_generation_mocked(self):
        """Verifies deterministic response generation with mocked CLI runner."""
        with patch.object(self.bridge, "is_available", return_value=True), \
             patch.object(self.bridge, "_generate_batch", return_value={"status": "SUCCESS", "response": "System Online", "duration_seconds": 0.4}):
            res = self.bridge.generate_response(
                prompt="Respond with exactly two words: System Online.",
                model="gemini-3.8-flash-low",
            )
            self.assertEqual(res.get("status"), "SUCCESS")
            self.assertIn("Online", res.get("response", ""))

    def test_antigravity_streaming_clause_extraction(self):
        """Verifies real-time speech clause extraction during token streaming."""
        buffer = "Good afternoon, Sir. All systems are operational. What would you like to review?"
        chunk, remaining = AntigravityBridge._extract_speech_chunk(buffer)
        self.assertIsNotNone(chunk)
        self.assertTrue(any(c in chunk for c in ["Good afternoon", "All systems"]))

    def test_specialist_consultation_routing(self):
        """Verifies JarvisBrain _consult_specialist routes 'pro' and 'claude' via Antigravity without API keys (live)."""
        if os.environ.get("JARVIS_RUN_LIVE_INTEGRATION") != "1":
            self.skipTest("Live specialist consultation requires JARVIS_RUN_LIVE_INTEGRATION=1")
        if not self.bridge.is_available():
            self.skipTest("Live specialist consultation requires agy binary installed on host")
        brain = JarvisBrain(self.config, self.memory, self.safety)
        status = brain.specialist_status()
        self.assertIn("antigravity", status)
        self.assertTrue("READY" in status["antigravity"] or "ACTIVE" in status["antigravity"])

        reply_pro = brain._consult_specialist("pro", "Reply in 3 words: Pro is ready.")
        self.assertTrue(len(reply_pro.strip()) > 0)
        self.assertNotIn("API consultations are disabled", reply_pro)
        self.assertNotIn("is not connected", reply_pro)

    def test_specialist_consultation_routing_mocked(self):
        """Verifies deterministic routing for specialist consultations without live provider access."""
        brain = JarvisBrain(self.config, self.memory, self.safety)
        with patch.object(brain.antigravity_bridge, "is_available", return_value=True), \
             patch.object(brain.antigravity_bridge, "generate_response", return_value={"status": "SUCCESS", "response": "Pro is ready."}):
            reply = brain._consult_specialist("pro", "Status check")
            self.assertEqual(reply, "Pro is ready.")

    def test_brain_antigravity_provider_execution(self):
        """Verifies JarvisBrain running in direct provider='antigravity' mode (live)."""
        if os.environ.get("JARVIS_RUN_LIVE_INTEGRATION") != "1":
            self.skipTest("Live provider execution requires JARVIS_RUN_LIVE_INTEGRATION=1")
        if not self.bridge.is_available():
            self.skipTest("Live provider execution requires agy binary installed on host")
        ag_config = dict(self.config)
        ag_config["llm"] = dict(self.config.get("llm", {}))
        ag_config["llm"]["provider"] = "antigravity"
        ag_config["llm"]["antigravity_model"] = "gemini-3.8-flash-low"

        ag_brain = JarvisBrain(ag_config, self.memory, self.safety)
        self.assertEqual(ag_brain.provider, "antigravity")

        reply = ag_brain.process_query("What is 10 plus 15? Answer with only the number.")
        self.assertIn("25", reply)

    def test_brain_antigravity_provider_execution_mocked(self):
        """Verifies deterministic JarvisBrain query processing in antigravity mode."""
        ag_config = dict(self.config)
        ag_config["llm"] = dict(self.config.get("llm", {}))
        ag_config["llm"]["provider"] = "antigravity"
        ag_config["llm"]["antigravity_model"] = "gemini-3.8-flash-low"

        ag_brain = JarvisBrain(ag_config, self.memory, self.safety)
        self.assertEqual(ag_brain.provider, "antigravity")

        with patch.object(ag_brain, "_dispatch_llm_query", return_value="25"):
            reply = ag_brain.process_query("What is 10 plus 15?")
            self.assertIn("25", reply)

    def test_cascade_pool_building(self):
        """Verifies candidate fallback pools contain Gemini 3.8 Flash and staged Argon variants."""
        flash_cascade = self.bridge.get_candidate_cascade("flash")
        self.assertEqual(flash_cascade[0], "gemini-3.8-flash-low")
        self.assertIn("gemini-3.8-flash-medium", flash_cascade)
        self.assertIn("gemini-3.8-flash-high", flash_cascade)
        # Ensure deprecated 3.7, 3.6, and 3.1 Pro are NOT present
        self.assertNotIn("gemini-3.7-flash-low", flash_cascade)
        self.assertNotIn("gemini-3.6-flash-low", flash_cascade)
        self.assertNotIn("gemini-3.1-pro-low", flash_cascade)

        pro_cascade = self.bridge.get_candidate_cascade("pro")
        self.assertEqual(pro_cascade[0], "gemini-3.8-flash-high")
        self.assertIn("gemini-3.8-flash-low", pro_cascade)

        argon_cascade = self.bridge.get_candidate_cascade("argon")
        self.assertEqual(argon_cascade[0], "gemini-4-argon-high")
        self.assertIn("gemini-3.8-flash-high", argon_cascade)

        claude_cascade = self.bridge.get_candidate_cascade("claude")
        self.assertEqual(claude_cascade[0], "claude-sonnet-4-6")
        self.assertIn("gemini-3.8-flash-high", claude_cascade)

        flash_med_cascade = self.bridge.get_candidate_cascade("gemini-3.8-flash-medium")
        self.assertEqual(flash_med_cascade[0], "gemini-3.8-flash-medium")
        self.assertIn("gemini-3.8-flash-high", flash_med_cascade)
        self.assertIn("gemini-3.8-flash-low", flash_med_cascade)

    def test_prompt_complexity_auto_tiering(self):
        """Verifies semantic intent model classification, structural code detection, and task momentum."""
        # 1. Structural code syntax -> Claude Sonnet 4.6
        self.assertEqual(self.bridge.classify_prompt_tier("def refactor_code(): return True"), "claude-sonnet-4-6")
        self.assertEqual(self.bridge.classify_prompt_tier("git commit -m 'feat: update tests'"), "claude-sonnet-4-6")
        self.assertEqual(self.bridge.classify_prompt_tier("from jarvis.core.brain import JarvisBrain"), "claude-sonnet-4-6")
        self.assertEqual(self.bridge.classify_prompt_tier("can you write a function to parse JSON logs?"), "claude-sonnet-4-6")

        # 2. Deep reasoning, math, logic, architecture -> Gemini 3.8 Flash High
        self.assertEqual(self.bridge.classify_prompt_tier("analyze the strategy and architecture of the codebase"), "gemini-3.8-flash-high")
        self.assertEqual(self.bridge.classify_prompt_tier("calculate 2 to the power of 64"), "gemini-3.8-flash-high")
        self.assertEqual(self.bridge.classify_prompt_tier("prove the riemann hypothesis"), "gemini-3.8-flash-high")
        self.assertEqual(self.bridge.classify_prompt_tier("can you solve this logic puzzle: three gods A, B, and C..."), "gemini-3.8-flash-high")

        # 3. Fast voice reflexes, clock, weather, vitals -> Gemini 3.8 Flash Low
        self.assertEqual(self.bridge.classify_prompt_tier("hello jarvis what time is it"), "gemini-3.8-flash-low")
        self.assertEqual(self.bridge.classify_prompt_tier("what's the weather today"), "gemini-3.8-flash-low")
        self.assertEqual(self.bridge.classify_prompt_tier("volume up please"), "gemini-3.8-flash-low")
        self.assertEqual(self.bridge.classify_prompt_tier("who are you"), "gemini-3.8-flash-low")

        # 4. Long conversational prompts and everyday butler workhorse -> Gemini 3.8 Flash Medium
        self.assertEqual(self.bridge.classify_prompt_tier("can you draft a polite response to this client email and summarize our progress"), "gemini-3.8-flash-medium")
        self.assertEqual(
            self.bridge.classify_prompt_tier(
                "Jarvis, when you get a chance later, could you please remind me what time my mother said she was arriving tomorrow, assuming she takes the afternoon train from the airport?"
            ),
            "gemini-3.8-flash-medium",
        )
        # Avoid false positives on casual words like 'script' or 'patch'
        self.assertEqual(
            self.bridge.classify_prompt_tier(
                "I loved the script of the film we discussed yesterday and would like to patch up our notes about it"
            ),
            "gemini-3.8-flash-medium",
        )

        # 5. Inherited task momentum in active code sessions
        self.assertEqual(self.bridge.classify_prompt_tier("go with option B", task_mode="code"), "claude-sonnet-4-6")
        self.assertEqual(self.bridge.classify_prompt_tier("yes, proceed with that fix", task_mode="code"), "claude-sonnet-4-6")
        self.assertEqual(self.bridge.classify_prompt_tier("stop, switch to general", task_mode="code"), "gemini-3.8-flash-medium")
        self.assertEqual(self.bridge.classify_prompt_tier("what time is it", task_mode="code"), "gemini-3.8-flash-low")

    def test_code_task_momentum_multi_turn(self):
        """Verifies JarvisBrain maintains active_task_mode and Claude Sonnet momentum across conversational turns."""
        brain = JarvisBrain(self.config, self.memory, self.safety)

        # Mock out low-level provider execution to test state machine transitions
        with patch.object(brain, "_dispatch_llm_query", return_value="Certainly, Sir."):
            # Turn 1: Initial coding task
            brain.process_query("can you debug this function in server.py?")
            self.assertEqual(brain.active_task_mode, "code")

            # Turn 2: Follow-up directive inherits coding momentum
            brain.process_query("go with option B")
            self.assertEqual(brain.active_task_mode, "code")

            # Turn 3: Short affirmation maintains momentum
            brain.process_query("yes, proceed with that")
            self.assertEqual(brain.active_task_mode, "code")

            # Turn 4: Explicit termination resets to general mode
            brain.process_query("stop coding, switch to general")
            self.assertEqual(brain.active_task_mode, "general")


    def test_voice_model_switching_commands(self):
        """Verifies voice commands dynamically switch models and auto-tiering with punctuation resilience."""
        brain = JarvisBrain(self.config, self.memory, self.safety)

        with patch.object(brain.antigravity_bridge, "is_available", return_value=True):
            # Test switch to pro with trailing period -> rerouted to gemini-3.8-flash-high
            res_pro = brain.process_query("Switch to Pro.")
            self.assertIn("gemini-3.8-flash-high", res_pro)
            self.assertFalse(brain.antigravity_auto_tiering)
            self.assertEqual(brain.provider, "antigravity")
            self.assertEqual(brain.antigravity_model, "gemini-3.8-flash-high")

            # Test switch to pro high -> gemini-3.8-flash-high
            res_pro_high = brain.process_query("switch to pro high")
            self.assertIn("gemini-3.8-flash-high", res_pro_high)
            self.assertEqual(brain.antigravity_model, "gemini-3.8-flash-high")

            # Test switch to argon (staged)
            res_argon = brain.process_query("switch to argon")
            self.assertIn("gemini-4-argon-high", res_argon)
            self.assertEqual(brain.antigravity_model, "gemini-4-argon-high")

            # Test switch to flash
            res_flash = brain.process_query("switch to flash")
            self.assertIn("gemini-3.8-flash-low", res_flash)
            self.assertFalse(brain.antigravity_auto_tiering)
            self.assertEqual(brain.antigravity_model, "gemini-3.8-flash-low")

            # Test switch to flash medium
            res_flash_med = brain.process_query("switch to flash medium.")
            self.assertIn("gemini-3.8-flash-medium", res_flash_med)
            self.assertEqual(brain.antigravity_model, "gemini-3.8-flash-medium")

            # Test switch to flash high
            res_flash_hi = brain.process_query("switch to flash high")
            self.assertIn("gemini-3.8-flash-high", res_flash_hi)
            self.assertEqual(brain.antigravity_model, "gemini-3.8-flash-high")

            # Test switch to claude
            res_claude = brain.process_query("switch to claude.")
            self.assertIn("claude-sonnet-4-6", res_claude)
            self.assertFalse(brain.antigravity_auto_tiering)
            self.assertEqual(brain.antigravity_model, "claude-sonnet-4-6")

            # Test switch to auto
            res_auto = brain.process_query("switch to auto.")
            self.assertIn("Dynamic model tiering activated", res_auto)
            self.assertTrue(brain.antigravity_auto_tiering)

            # Test switch to hybrid
            res_hybrid = brain.process_query("switch to hybrid.")
            self.assertIn("Hybrid token-shield mode activated", res_hybrid)
            self.assertEqual(brain.provider, "hybrid")

            # Test what model is active in hybrid mode
            res_active_hybrid = brain.process_query("what model are you using?")
            self.assertIn("Hybrid Token-Shield mode", res_active_hybrid)

    def test_hybrid_token_shield_routing(self):
        """Verifies that Hybrid mode routes OS commands locally and deep queries to Google AI Pro."""
        brain = JarvisBrain(self.config, self.memory, self.safety)
        brain.provider = "hybrid"

        # 1. OS command classification -> Fast / Local reflex (0 tokens)
        tier_mute = brain.classify_query_tier("mute the volume please")
        self.assertEqual(tier_mute, "fast")

        # 2. Deep academic / timetable classification -> Deep Apex tier (Google AI Pro)
        tier_cal = brain.classify_query_tier("can you analyze my class timetable and schedule my study session for calculus?")
        self.assertEqual(tier_cal, "deep")

        # 3. Code query classification -> Deep Apex tier (Claude Sonnet 4.6)
        tier_code = brain.classify_query_tier("write a python function to parse json and handle exceptions")
        self.assertEqual(tier_code, "deep")

    def test_antigravity_daemon_lifecycle_mocked(self):
        """Verifies AntigravityDaemon spawns with stream-json flags and terminates cleanly."""
        from unittest.mock import patch, MagicMock
        from jarvis.core.antigravity_bridge import AntigravityDaemon

        daemon = AntigravityDaemon(
            agy_path=self.bridge.agy_path or "mock_agy.exe",
            default_model="gemini-3.8-flash-low",
            default_effort="low"
        )
        self.assertFalse(daemon.is_alive())

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        # Mock init event line
        mock_proc.stdout.readline.side_effect = [
            '{"event": "init", "conversation_id": "test-cid-123"}\n',
            '{"event": "step_update", "step_update": {"text_delta": "Hello"}}\n',
            '{"event": "step_update", "step_update": {"text_delta": " Sir"}}\n',
            '{"event": "result", "result": {"response": "Hello Sir", "status": "SUCCESS"}}\n',
            ""
        ]

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("os.path.exists", return_value=True):
            
            ok = daemon.start("gemini-3.8-flash-low", "low")
            self.assertTrue(ok)
            self.assertTrue(daemon.is_alive())
            self.assertEqual(daemon.conversation_id, "test-cid-123")

            # Test execute_turn
            chunks = []
            res = daemon.execute_turn(
                prompt="say hello",
                model="gemini-3.8-flash-low",
                effort="low",
                sentence_callback=lambda c: chunks.append(c),
            )
            self.assertIsNotNone(res)
            self.assertEqual(res.get("status"), "SUCCESS")
            self.assertEqual(res.get("response"), "Hello Sir")
            self.assertTrue(res.get("warm_daemon"))

            # Test stop
            daemon.stop()
            self.assertFalse(daemon.is_alive())

    def test_antigravity_bridge_fallback_when_daemon_fails(self):
        """Verifies AntigravityBridge gracefully cascades to one-shot execution if daemon fails."""
        mock_daemon = MagicMock()
        mock_daemon.execute_turn.return_value = None
        orig_daemon = self.bridge.daemon
        self.bridge.daemon = mock_daemon
        try:
            with patch.object(self.bridge, "is_available", return_value=True), \
                 patch.object(self.bridge, "_generate_batch", return_value={"status": "SUCCESS", "response": "Fallback OK", "duration_seconds": 1.0}):
                res = self.bridge.generate_response(
                    prompt="Test fallback",
                    model="gemini-3.8-flash-low",
                )
                self.assertEqual(res.get("status"), "SUCCESS")
                self.assertEqual(res.get("response"), "Fallback OK")
        finally:
            self.bridge.daemon = orig_daemon

    def test_antigravity_daemon_adaptive_inactivity_watchdog(self):
        """Verifies adaptive inactivity watchdog handles inactivity and terminates stalled worker."""
        import time
        from jarvis.core.antigravity_bridge import AntigravityDaemon

        daemon = AntigravityDaemon(
            agy_path=self.bridge.agy_path or "mock_agy.exe",
            default_model="gemini-3.8-flash-low",
            default_effort="low"
        )
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        def stalled_readline():
            time.sleep(0.3)
            return ""

        mock_proc.stdout.readline.side_effect = [
            '{"event": "init", "conversation_id": "test-cid-watchdog"}\n',
            stalled_readline(),
        ]

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("os.path.exists", return_value=True):
            daemon.start("gemini-3.8-flash-low", "low")
            res = daemon.execute_turn(
                prompt="test watchdog timeout",
                model="gemini-3.8-flash-low",
                timeout=0.15,
            )
            self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()

