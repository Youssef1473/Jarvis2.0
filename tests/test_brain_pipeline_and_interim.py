"""Tests for Brain Pipeline, Tool Orchestrator, and Acoustic Interim Bridge in Jarvis 2.0."""
import time
import unittest
from unittest.mock import MagicMock, patch

from jarvis.core.pipeline.interim_bridge import InterimSpeechBridge, _INTERIM_PHRASE_MAP
from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
from jarvis.core.security_boundaries import TurnTaintTracker, SecurityBoundaryViolation
from jarvis.skills.base import SkillRegistry


class TestInterimSpeechBridge(unittest.TestCase):
    """Verifies non-blocking acoustic interim bridging and delay/cooldown timings."""

    def test_fast_tool_disarms_timer_zero_interim_calls(self):
        spoken = []
        bridge = InterimSpeechBridge(
            speak_fn=lambda phrase: spoken.append(phrase),
            delay_step1_sec=0.15,
            delay_step_n_sec=0.05,
            cooldown_sec=0.5,
        )

        # Fast execution completes before timer expires
        bridge.on_tool_start("web_search", step_index=1)
        time.sleep(0.02)
        bridge.on_tool_end()

        # Wait past the delay threshold to confirm cancelled timer never fired
        time.sleep(0.20)
        self.assertEqual(spoken, [])
        self.assertEqual(bridge.spoken_count, 0)

    def test_slow_tool_fires_bettany_phrase(self):
        spoken = []
        bridge = InterimSpeechBridge(
            speak_fn=lambda phrase: spoken.append(phrase),
            delay_step1_sec=0.05,
            delay_step_n_sec=0.02,
            cooldown_sec=0.3,
        )

        bridge.on_tool_start("web_search", step_index=1)
        time.sleep(0.12)  # Wait for timer to fire
        bridge.on_tool_end()

        self.assertEqual(len(spoken), 1)
        self.assertEqual(spoken[0], _INTERIM_PHRASE_MAP["web_search"])
        self.assertEqual(bridge.spoken_count, 1)

    def test_cooldown_suppresses_rapid_subsequent_interim_speech(self):
        spoken = []
        bridge = InterimSpeechBridge(
            speak_fn=lambda phrase: spoken.append(phrase),
            delay_step1_sec=0.03,
            delay_step_n_sec=0.02,
            cooldown_sec=0.4,
        )

        # Step 1 fires
        bridge.on_tool_start("inspect_file_locks", step_index=1)
        time.sleep(0.08)
        bridge.on_tool_end()
        self.assertEqual(len(spoken), 1)

        # Step 2 within cooldown (< 0.4s) should not arm or fire
        bridge.on_tool_start("release_file_lock", step_index=2)
        time.sleep(0.08)
        bridge.on_tool_end()
        self.assertEqual(len(spoken), 1)  # Still 1, suppressed by cooldown

    def test_aborts_speech_if_system_already_speaking(self):
        spoken = []
        is_speaking_flag = True
        bridge = InterimSpeechBridge(
            speak_fn=lambda phrase: spoken.append(phrase),
            is_speaking_fn=lambda: is_speaking_flag,
            delay_step1_sec=0.03,
            cooldown_sec=0.5,
        )

        bridge.on_tool_start("deep_research", step_index=1)
        time.sleep(0.08)
        bridge.on_tool_end()

        self.assertEqual(spoken, [])
        self.assertEqual(bridge.spoken_count, 0)


class TestToolOrchestrator(unittest.TestCase):
    """Verifies orchestration loop guards, taint tracking, and tool execution."""

    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()

        # Register mock tools for test
        @SkillRegistry.register(name="_test_echo_tool", description="Test tool", risk_level=0)
        def _test_echo(text: str = ""):
            return f"Echo: {text}"

        @SkillRegistry.register(name="_test_high_risk_tool", description="High risk tool", risk_level=1)
        def _test_high_risk():
            return "Danger executed"

        self.addCleanup(lambda: SkillRegistry.unregister("_test_echo_tool"))
        self.addCleanup(lambda: SkillRegistry.unregister("_test_high_risk_tool"))

    def test_normal_tool_execution(self):
        orchestrator = ToolOrchestrator(taint_tracker=self.tracker)
        result = orchestrator.execute_step("_test_echo_tool", {"text": "hello"}, step_index=1)
        self.assertEqual(result, "Echo: hello")

    def test_cycle_detection_halts_recursion(self):
        orchestrator = ToolOrchestrator(taint_tracker=self.tracker, max_identical_cycles=2)

        res1 = orchestrator.execute_step("_test_echo_tool", {"text": "same"}, step_index=1)
        self.assertEqual(res1, "Echo: same")

        res2 = orchestrator.execute_step("_test_echo_tool", {"text": "same"}, step_index=2)
        self.assertEqual(res2, "Echo: same")

        res3 = orchestrator.execute_step("_test_echo_tool", {"text": "same"}, step_index=3)
        self.assertIn("Loop cycle detected", res3)

    def test_taint_blocks_high_privilege_without_dual_key(self):
        orchestrator = ToolOrchestrator(taint_tracker=self.tracker)

        # 1. Taint the turn via an untrusted ingestion tool
        self.tracker.mark_tainted("web_search")

        # 2. Attempt high-privilege execution without dual key confirmation -> blocked
        res = orchestrator.execute_step("execute_powershell", {"command": "dir"}, step_index=2)
        self.assertIn("Dual-Key Safety Gate", str(res))

        # 3. Attempt risk_level >= 1 execution without dual key confirmation -> blocked
        res_risk = orchestrator.execute_step("_test_high_risk_tool", {}, step_index=3)
        self.assertIn("Dual-Key Safety Gate", str(res_risk))

        # 4. Attempt with dual_key_confirmed=True -> permitted
        res_confirmed = orchestrator.execute_step("_test_high_risk_tool", {}, step_index=4, dual_key_confirmed=True)
        self.assertEqual(res_confirmed, "Danger executed")

    def test_tool_status_callback_events(self):
        events = []
        def _callback(evt):
            events.append(evt)

        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            on_status_callback=_callback,
        )
        res = orchestrator.execute_step("_test_echo_tool", {"text": "telemetry"}, step_index=1)
        self.assertEqual(res, "Echo: telemetry")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["status"], "running")
        self.assertEqual(events[0]["tool"], "_test_echo_tool")
        self.assertEqual(events[1]["status"], "completed")
        self.assertEqual(events[1]["tool"], "_test_echo_tool")


if __name__ == "__main__":
    unittest.main()

