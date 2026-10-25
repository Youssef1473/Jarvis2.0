"""Unit tests for UnifiedRunner task completion accuracy and failure reporting (CORE-RUN-01A).

Verifies DEF-REL-01:
- Prevents false "Action completed, Sir." announcements on tool timeouts, exceptions, safety halts, and loop cycles.
- Preserves "Action completed, Sir." when tools genuinely succeed, even if their return string contains "Error:".
- Preserves model-generated conversational replies when provided.
- Guarantees strict per-session execution tracker isolation across turns.
"""
from __future__ import annotations

import threading
import unittest
from collections.abc import Callable
from typing import Any

from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
from jarvis.core.providers.base import (
    BaseProvider,
    LLMResponse,
    ToolCall,
    ToolExecutionResult,
    UnifiedRunner,
)
from jarvis.core.safety import SafetyGate
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
        return LLMResponse(content="")


class TestUnifiedRunnerReliability(unittest.TestCase):
    """Tests for accurate completion reporting in UnifiedRunner."""

    def setUp(self):
        # Register test skills
        @SkillRegistry.register(name="test_timeout_tool", description="Tool that exceeds timeout")
        def test_timeout_tool():
            import time
            time.sleep(0.5)
            return "done"

        @SkillRegistry.register(name="test_failing_tool", description="Tool that raises an exception")
        def test_failing_tool():
            raise RuntimeError("Hardware connection lost")

        @SkillRegistry.register(
            name="test_search_logs_tool",
            description="Tool that legitimately returns an error message as clean output",
        )
        def test_search_logs_tool():
            return "Error: No log entries found"

        @SkillRegistry.register(name="test_successful_tool", description="Normal succeeding tool")
        def test_successful_tool(step: int = 0):
            return f"CPU temperature is 45C (step {step})"

        @SkillRegistry.register(name="test_mimic_security_denial", description="Tool mimicking security denial")
        def test_mimic_security_denial():
            return "Error: Security gate denied execution of tool 'simulated'."

        @SkillRegistry.register(name="test_mimic_loop_cycle", description="Tool mimicking loop cycle")
        def test_mimic_loop_cycle():
            return "Loop cycle detected on tool 'simulated' with identical arguments. Halting recursion."

        @SkillRegistry.register(name="test_mimic_cancellation", description="Tool mimicking cancellation")
        def test_mimic_cancellation():
            return "Action cancelled: Operation was aborted by user interrupt."

        @SkillRegistry.register(name="test_mimic_exception", description="Tool mimicking exception")
        def test_mimic_exception():
            return "Error executing tool 'simulated': simulated error message"

        @SkillRegistry.register(name="test_mimic_not_found", description="Tool mimicking tool not found")
        def test_mimic_not_found():
            return "Error: Tool 'simulated' not found."

    def test_tool_timeout_reports_timeout_failure_not_completed(self):
        """1. When a tool exceeds execution timeout, UnifiedRunner must report timeout failure
        and MUST NOT announce 'Action completed, Sir.'.
        """
        stalled_call = ToolCall(id="call_timeout", name="test_timeout_tool", arguments={})
        turn_1 = LLMResponse(content="", tool_calls=[stalled_call])
        turn_2 = LLMResponse(content="", tool_calls=[])  # Empty trailing reply

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2, tool_timeout=0.08)

        final_reply, history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "run slow tool"}],
        )

        self.assertNotIn("Action completed, Sir.", final_reply)
        self.assertIn("timed out", final_reply)
        self.assertIn("test_timeout_tool", final_reply)

        # Tool history must record timeout error
        tool_entry = next((m for m in history if m.get("role") == "tool"), None)
        self.assertIsNotNone(tool_entry)
        self.assertIn("exceeded maximum execution timeout", tool_entry["content"])

    def test_safety_gate_halt_reports_halt_reason_not_completed(self):
        """2. When SafetyGate halts a dangerous action, UnifiedRunner must report the halt reason
        and MUST NOT announce 'Action completed, Sir.'.
        """
        dangerous_call = ToolCall(
            id="call_danger",
            name="execute_powershell",
            arguments={"command": "Remove-Item -Recurse C:\\Windows"},
        )
        turn_1 = LLMResponse(content="", tool_calls=[dangerous_call])
        turn_2 = LLMResponse(content="", tool_calls=[])

        # Real production SafetyGate detecting destructive command
        gate = SafetyGate()
        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(safety_gate=gate, max_iterations=2)

        narrated: list[str] = []
        final_reply, _history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "delete windows"}],
            sentence_callback=lambda s: narrated.append(s),
        )

        self.assertNotIn("Action completed, Sir.", final_reply)
        self.assertIn("Action was halted", final_reply)
        self.assertIn("Blocked a command matching Jarvis's destructive-command rules", final_reply)

    def test_unhandled_exception_reports_error_not_completed(self):
        """3. When a tool raises an unhandled exception, UnifiedRunner must report execution failure
        and MUST NOT announce 'Action completed, Sir.'.
        """
        failing_call = ToolCall(id="call_fail", name="test_failing_tool", arguments={})
        turn_1 = LLMResponse(content="", tool_calls=[failing_call])
        turn_2 = LLMResponse(content="", tool_calls=[])

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2)

        final_reply, _history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "test fail"}],
        )

        self.assertNotIn("Action completed, Sir.", final_reply)
        self.assertIn("error executing test_failing_tool", final_reply)

    def test_loop_cycle_detection_reports_cycle_halt_not_completed(self):
        """4. When a recursive tool loop cycle is halted, UnifiedRunner must report cycle halt
        and MUST NOT announce 'Action completed, Sir.'.
        """
        tool_call = ToolCall(id="call_rep", name="test_successful_tool", arguments={})
        turn_1 = LLMResponse(content="", tool_calls=[tool_call])
        turn_2 = LLMResponse(content="", tool_calls=[tool_call])  # identical arguments -> loop cycle

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=5)

        final_reply, _history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "loop test"}],
        )

        self.assertNotIn("Action completed, Sir.", final_reply)
        self.assertEqual(final_reply, "Action stopped: A repetitive action loop was halted, Sir.")

    def test_tool_returning_error_string_preserves_success_when_execution_succeeded(self):
        """5. When a tool successfully returns clean output that happens to start with 'Error:',
        UnifiedRunner MUST NOT misclassify it as an execution failure, and announces 'Action completed, Sir.'.
        """
        log_call = ToolCall(id="call_logs", name="test_search_logs_tool", arguments={})
        turn_1 = LLMResponse(content="", tool_calls=[log_call])
        turn_2 = LLMResponse(content="", tool_calls=[])  # Empty trailing reply

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2)

        final_reply, history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "search logs"}],
        )

        # The tool executed cleanly without exception or timeout; its answer was "Error: No log entries found".
        # UnifiedRunner must correctly announce "Action completed, Sir."
        self.assertEqual(final_reply, "Action completed, Sir.")
        tool_entry = next((m for m in history if m.get("role") == "tool"), None)
        self.assertIsNotNone(tool_entry)
        self.assertEqual(tool_entry["content"], "Error: No log entries found")

    def test_true_success_announces_action_completed(self):
        """6. When a tool execution succeeds and the model generates no trailing text,
        UnifiedRunner announces 'Action completed, Sir.'.
        """
        ok_call = ToolCall(id="call_ok", name="test_successful_tool", arguments={})
        turn_1 = LLMResponse(content="", tool_calls=[ok_call])
        turn_2 = LLMResponse(content="", tool_calls=[])

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2)

        final_reply, _history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "get cpu temp"}],
        )

        self.assertEqual(final_reply, "Action completed, Sir.")

    def test_model_text_reply_preserved_over_default(self):
        """7. When the model provides conversational trailing text, it MUST be preserved
        and not replaced by 'Action completed, Sir.'.
        """
        ok_call = ToolCall(id="call_ok", name="test_successful_tool", arguments={})
        turn_1 = LLMResponse(content="", tool_calls=[ok_call])
        turn_2 = LLMResponse(content="Your CPU is currently at 45C, Sir.", tool_calls=[])

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2)

        final_reply, _history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "check cpu"}],
        )

        self.assertEqual(final_reply, "Your CPU is currently at 45C, Sir.")

    def test_multi_tool_partial_failure_reports_failure_not_completed(self):
        """8. When multiple tools are invoked and one fails, UnifiedRunner must report the failure
        and MUST NOT announce 'Action completed, Sir.'.
        """
        call_ok = ToolCall(id="call_1", name="test_successful_tool", arguments={})
        call_fail = ToolCall(id="call_2", name="test_failing_tool", arguments={})
        turn_1 = LLMResponse(content="", tool_calls=[call_ok, call_fail])
        turn_2 = LLMResponse(content="", tool_calls=[])

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2)

        final_reply, _history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "run multiple"}],
        )

        self.assertNotIn("Action completed, Sir.", final_reply)
        self.assertIn("error executing test_failing_tool", final_reply)

    def test_session_tracker_isolation_across_sequential_runs(self):
        """9. Verifies that failure state from run 1 does not leak into run 2 on the same runner."""
        runner = UnifiedRunner(max_iterations=2)

        # Run 1: Failure
        fail_call = ToolCall(id="c1", name="test_failing_tool", arguments={})
        provider_1 = MockProvider([
            LLMResponse(content="", tool_calls=[fail_call]),
            LLMResponse(content="", tool_calls=[]),
        ])
        reply_1, _ = runner.execute_session(provider_1, [{"role": "user", "content": "run 1"}])
        self.assertIn("error executing test_failing_tool", reply_1)

        # Run 2: Success on the exact same runner instance
        ok_call = ToolCall(id="c2", name="test_successful_tool", arguments={})
        provider_2 = MockProvider([
            LLMResponse(content="", tool_calls=[ok_call]),
            LLMResponse(content="", tool_calls=[]),
        ])
        reply_2, _ = runner.execute_session(provider_2, [{"role": "user", "content": "run 2"}])
        self.assertEqual(reply_2, "Action completed, Sir.")

    def test_legitimate_tool_outputs_resembling_framework_errors_are_not_misclassified(self):
        """10. Verifies that legitimate tool outputs resembling framework error prefixes
        (orchestrator denial, loop cycle, cancellation, execution error, tool not found)
        are NOT misclassified as framework failures when the tool executed cleanly.
        """
        runner = UnifiedRunner(max_iterations=2)

        mimic_tools = [
            ("test_mimic_security_denial", "Error: Security gate denied execution of tool 'simulated'."),
            ("test_mimic_loop_cycle", "Loop cycle detected on tool 'simulated' with identical arguments. Halting recursion."),
            ("test_mimic_cancellation", "Action cancelled: Operation was aborted by user interrupt."),
            ("test_mimic_exception", "Error executing tool 'simulated': simulated error message"),
            ("test_mimic_not_found", "Error: Tool 'simulated' not found."),
        ]

        for tool_name, expected_output in mimic_tools:
            res = runner.execute_tool_with_status(tool_name, {})
            self.assertTrue(
                res.success,
                f"Tool '{tool_name}' was erroneously classified as failed! failure_category={res.failure_category}",
            )
            self.assertIsNone(res.failure_category)
            self.assertEqual(res.result, expected_output)

        # Full session verification with one of the mimic tools
        mimic_call = ToolCall(id="call_mimic", name="test_mimic_security_denial", arguments={})
        turn_1 = LLMResponse(content="", tool_calls=[mimic_call])
        turn_2 = LLMResponse(content="", tool_calls=[])  # Empty trailing reply

        provider = MockProvider([turn_1, turn_2])
        final_reply, history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "run mimic tool"}],
        )

        self.assertEqual(final_reply, "Action completed, Sir.")
        tool_entry = next((m for m in history if m.get("role") == "tool"), None)
        self.assertIsNotNone(tool_entry)
        self.assertEqual(tool_entry["content"], "Error: Security gate denied execution of tool 'simulated'.")

    def test_iteration_limit_exhaustion_with_successful_tools_reports_stopped_not_completed(self):
        """11. When UnifiedRunner exhausts max_iterations while tools succeed but the model
        never emits a final conversational response (tool_calls=[]), it must report an
        accurate stopped/incomplete status and MUST NOT announce 'Action completed, Sir.'.
        """
        call_1 = ToolCall(id="c1", name="test_successful_tool", arguments={"step": 1})
        call_2 = ToolCall(id="c2", name="test_successful_tool", arguments={"step": 2})

        turn_1 = LLMResponse(content="", tool_calls=[call_1])
        turn_2 = LLMResponse(content="", tool_calls=[call_2])
        # Note: Turn 3 with tool_calls=[] is never reached because max_iterations=2 cuts execution short

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2)

        final_reply, history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "infinite tool task"}],
        )

        self.assertNotIn("Action completed, Sir.", final_reply)
        self.assertEqual(
            final_reply,
            "Action stopped: Maximum iteration limit reached before task completion, Sir.",
        )

        # Verify both tools executed cleanly in history
        tool_entries = [m for m in history if m.get("role") == "tool"]
        self.assertEqual(len(tool_entries), 2)
        self.assertEqual(tool_entries[0]["content"], "CPU temperature is 45C (step 1)")
        self.assertEqual(tool_entries[1]["content"], "CPU temperature is 45C (step 2)")

    def test_iteration_limit_exhaustion_with_intermediate_thought_reports_stopped(self):
        """12. Verifies that intermediate thought narration during tool turns is not mistaken
        for a final conversational response when the task is cut short by the iteration ceiling.
        """
        call_1 = ToolCall(id="c1", name="test_successful_tool", arguments={"step": 1})
        call_2 = ToolCall(id="c2", name="test_successful_tool", arguments={"step": 2})

        turn_1 = LLMResponse(content="Investigating temperature sensors...", tool_calls=[call_1])
        turn_2 = LLMResponse(content="Reading second sensor bank...", tool_calls=[call_2])

        provider = MockProvider([turn_1, turn_2])
        runner = UnifiedRunner(max_iterations=2)

        final_reply, _history = runner.execute_session(
            provider=provider,
            messages=[{"role": "user", "content": "check sensors"}],
        )

        self.assertNotIn("Action completed, Sir.", final_reply)
        self.assertEqual(
            final_reply,
            "Action stopped: Maximum iteration limit reached before task completion, Sir.",
        )

    def test_overlapping_concurrent_tool_executions_preserve_individual_status(self):
        """13. Deterministic concurrency test: overlaps two tool executions on the same runner
        and ToolOrchestrator instance (one failing and one succeeding), synchronized via
        threading.Event primitives (zero sleeps).
        Verifies that status metadata is strictly per-invocation and neither execution receives
        or overwrites the other's status.
        """
        fail_started = threading.Event()
        succ_completed = threading.Event()

        @SkillRegistry.register(
            name="test_concurrent_failing_tool",
            description="Tool that starts first, pauses, and raises an exception",
        )
        def test_concurrent_failing_tool():
            fail_started.set()
            # Wait deterministically until tool B has completed its execution
            if not succ_completed.wait(timeout=5.0):
                raise TimeoutError("Concurrency sync error: tool B did not complete in time")
            raise RuntimeError("Forced failure in tool A")

        @SkillRegistry.register(
            name="test_concurrent_succeeding_tool",
            description="Tool that executes while tool A is in-flight and succeeds",
        )
        def test_concurrent_succeeding_tool():
            # Wait deterministically until tool A is actively in-flight
            if not fail_started.wait(timeout=5.0):
                raise TimeoutError("Concurrency sync error: tool A did not start in time")
            return "Tool B clean success"

        orchestrator = ToolOrchestrator()
        runner = UnifiedRunner(tool_orchestrator=orchestrator)

        result_a: list[ToolExecutionResult] = []
        result_b: list[ToolExecutionResult] = []

        def worker_a():
            res = runner.execute_tool_with_status("test_concurrent_failing_tool", {})
            result_a.append(res)

        def worker_b():
            res = runner.execute_tool_with_status("test_concurrent_succeeding_tool", {})
            result_b.append(res)
            # Release tool A now that tool B has finished its execution
            succ_completed.set()

        t_a = threading.Thread(target=worker_a, name="worker_tool_a")
        t_b = threading.Thread(target=worker_b, name="worker_tool_b")

        # Start thread A (which will signal fail_started and block)
        t_a.start()
        # Ensure thread A is in-flight before launching thread B
        self.assertTrue(fail_started.wait(timeout=5.0), "Thread A failed to start")

        # Start thread B while thread A is paused mid-execution
        t_b.start()

        # Join both threads cleanly
        t_b.join(timeout=5.0)
        self.assertFalse(t_b.is_alive(), "Thread B deadlocked")
        t_a.join(timeout=5.0)
        self.assertFalse(t_a.is_alive(), "Thread A deadlocked")

        self.assertEqual(len(result_a), 1)
        self.assertEqual(len(result_b), 1)

        res_a = result_a[0]
        res_b = result_b[0]

        # Tool A must reflect its own failure
        self.assertFalse(res_a.success, "Tool A was erroneously classified as success!")
        self.assertEqual(res_a.failure_category, "exception")
        self.assertIn("Forced failure in tool A", res_a.result)

        # Tool B must reflect its own success and must NOT be polluted by Tool A
        self.assertTrue(res_b.success, "Tool B was erroneously classified as failure!")
        self.assertIsNone(res_b.failure_category)
        self.assertEqual(res_b.result, "Tool B clean success")


if __name__ == "__main__":
    unittest.main()
