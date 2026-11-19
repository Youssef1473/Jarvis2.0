"""Unit tests for UnifiedRunner and BaseProvider subsystems."""
import unittest
from collections.abc import Callable
from typing import Any

from jarvis.core.providers.base import (
    BaseProvider,
    LLMResponse,
    ToolCall,
    UnifiedRunner,
)
from jarvis.skills.base import SkillRegistry


class MockProvider(BaseProvider):
    """Mock provider with programmed turns for testing UnifiedRunner."""

    def __init__(self, turns: list[LLMResponse]):
        super().__init__(name="mock", model="mock-model")
        self.turns = turns
        self.turn_index = 0

    def generate_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_instruction: str | None = None,
        sentence_callback: Callable[[str], None] | None = None,
        timeout: float = 30.0,
    ) -> LLMResponse:
        if self.turn_index < len(self.turns):
            res = self.turns[self.turn_index]
            self.turn_index += 1
            if sentence_callback and res.content:
                sentence_callback(res.content)
            return res
        return LLMResponse(content="No further turns.")


class TestUnifiedRunnerAndSandbox(unittest.TestCase):
    """Verifies loop cycle detection, tool execution, and sandbox validation."""

    def test_unified_runner_cycle_detection(self):
        """UnifiedRunner must detect repeated tool signatures and halt recursion immediately."""
        repeated_tool = ToolCall(id="call_1", name="get_active_window", arguments={})
        turn_1 = LLMResponse(content="", tool_calls=[repeated_tool])
        turn_2 = LLMResponse(content="", tool_calls=[repeated_tool]) # identical call -> cycle!

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=5)

        narrations = []
        _final_text, _history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "test"}],
            sentence_callback=lambda s: narrations.append(s),
        )

        self.assertIn("Halting repetitive action cycle, Sir.", narrations)
        self.assertEqual(provider.turn_index, 2)

    def test_unified_runner_narration_and_execution(self):
        """UnifiedRunner must narrate tools in real time and execute registered tools."""
        tool_call = ToolCall(id="call_vol", name="adjust_volume", arguments={"action": "mute"})
        turn_1 = LLMResponse(content="", tool_calls=[tool_call])
        turn_2 = LLMResponse(content="Volume is now muted, Sir.", tool_calls=[])

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=4)

        narrated = []
        final_reply, _history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "mute audio"}],
            sentence_callback=lambda s: narrated.append(s),
        )

        self.assertIn("Adjusting the system volume, Sir.", narrated)
        self.assertEqual(final_reply, "Volume is now muted, Sir.")

    def test_unified_runner_enforces_mandatory_tool_timeout(self):
        """UnifiedRunner must abort stalled tools and return a timeout error without freezing."""
        import time

        @SkillRegistry.register(name="slow_stalled_tool", description="Test stalled tool")
        def slow_stalled_tool():
            time.sleep(0.8)
            return "finished"

        self.addCleanup(lambda: SkillRegistry.unregister("slow_stalled_tool"))

        stalled_call = ToolCall(id="call_slow", name="slow_stalled_tool", arguments={})
        turn_1 = LLMResponse(content="", tool_calls=[stalled_call])
        turn_2 = LLMResponse(content="Recovered after timeout.", tool_calls=[])

        provider = MockProvider([turn_1, turn_2])
        # Set aggressive tool timeout of 0.15s
        runner = UnifiedRunner(max_iterations=3, tool_timeout=0.15)

        start = time.monotonic()
        _final_reply, history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "run slow tool"}],
        )
        elapsed = time.monotonic() - start

        # Must abort in well under 0.6s instead of waiting 0.8s
        self.assertLess(elapsed, 0.6)
        tool_entry = next((m for m in history if m.get("role") == "tool"), None)
        self.assertIsNotNone(tool_entry)
        self.assertTrue(
            "exceeded maximum execution timeout" in tool_entry["content"]
            or "aborted" in tool_entry["content"].lower()
        )


if __name__ == "__main__":
    unittest.main()
