"""Unit tests for UnifiedRunner task completion accuracy and failure reporting (CORE-RUN-01A).

Verifies DEF-REL-01:
- Prevents false "Action completed, Sir." announcements on tool timeouts, exceptions, safety halts, and loop cycles.
- Preserves "Action completed, Sir." when tools genuinely succeed, even if their return string contains "Error:".
- Preserves model-generated conversational replies when provided.
- Guarantees strict per-session execution tracker isolation across turns.
"""
from __future__ import annotations

import concurrent.futures
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


class TestWorkerPoolRecoveryDEFREL04(unittest.TestCase):
    """Tests for Worker-Pool Recovery on Tool Timeout (CORE-RUN-01B, DEF-REL-04)."""

    def setUp(self):
        self._cleanup_events: list[threading.Event] = []
        self._runners_to_shutdown: list[UnifiedRunner] = []

    def tearDown(self):
        # Guaranteed cleanup: release any blocking events first so worker threads can exit
        for ev in self._cleanup_events:
            ev.set()
        # Shut down any runners instantiated in tests
        for runner in self._runners_to_shutdown:
            runner.shutdown(wait=False)

    def test_tool_raises_own_timeout_error_classified_as_tool_exception(self):
        """Tool raising TimeoutError internally is classified as 'exception', not runner deadline timeout."""
        @SkillRegistry.register(name="test_internal_timeout_tool", description="Raises internal TimeoutError")
        def test_internal_timeout_tool():
            raise TimeoutError("Third-party API request timed out after 3.0s")

        runner = UnifiedRunner(tool_timeout=10.0)
        self._runners_to_shutdown.append(runner)

        res = runner.execute_tool_with_status("test_internal_timeout_tool", {})
        self.assertFalse(res.success)
        self.assertEqual(res.failure_category, "exception")
        self.assertIn("Third-party API request timed out after 3.0s", res.result)
        self.assertNotIn("timed out to protect real-time system responsiveness", res.result)

    def test_queued_task_cancelled_on_deadline_timeout_never_executes(self):
        """Queued task whose deadline expires is cancelled and never subsequently executes."""
        blocker_event = threading.Event()
        self._cleanup_events.append(blocker_event)
        started_count = [0]
        started_lock = threading.Lock()
        first_worker_started = threading.Event()

        @SkillRegistry.register(name="test_pool_occupier_tool", description="Occupies worker thread until released")
        def test_pool_occupier_tool(worker_id: int = 0):
            with started_lock:
                started_count[0] += 1
                if started_count[0] == 6:
                    first_worker_started.set()
            blocker_event.wait(timeout=5.0)
            return f"occupier {worker_id} done"

        queued_executed = threading.Event()

        @SkillRegistry.register(name="test_queued_tool", description="Queued task that should be cancelled")
        def test_queued_tool():
            queued_executed.set()
            return "queued done"

        runner = UnifiedRunner(tool_timeout=5.0)
        self._runners_to_shutdown.append(runner)

        # Occupy all 6 workers in general pool with distinct arguments
        threads = []
        for i in range(6):
            t = threading.Thread(
                target=runner.execute_tool_with_status,
                args=("test_pool_occupier_tool", {"worker_id": i}),
            )
            threads.append(t)
            t.start()

        # Wait until all 6 workers are actively in test_pool_occupier_tool
        self.assertTrue(first_worker_started.wait(timeout=5.0), "Workers failed to occupy pool")

        # Now submit the 7th tool with a 0.1s timeout (it will sit queued in _work_queue)
        res = runner.execute_tool_with_status("test_queued_tool", {}, timeout=0.1)
        self.assertFalse(res.success)
        self.assertEqual(res.failure_category, "timeout")

        # Now release the occupiers so workers become free
        blocker_event.set()
        for t in threads:
            t.join(timeout=3.0)

        # Verify that test_queued_tool never executed because future.cancel() pruned it from _work_queue
        self.assertFalse(queued_executed.wait(timeout=0.3), "Queued tool executed despite being timed out!")

    def test_cooperative_cancellation_token_stops_iterative_tool(self):
        """Iterative tool observing ToolCancellationToken halts immediately when cancelled."""
        from jarvis.core.cancellation import get_current_tool_cancellation_token

        completed_steps = []

        @SkillRegistry.register(name="test_iterative_cooperative_tool", description="Iterative tool checking token")
        def test_iterative_cooperative_tool():
            tok = get_current_tool_cancellation_token()
            for step in range(1, 10):
                if tok is not None and tok.is_cancelled:
                    return f"halted at step {step}"
                completed_steps.append(step)
                if step == 2 and tok is not None:
                    # Trigger cancellation mid-loop
                    tok.cancel()
            return "completed all steps"

        runner = UnifiedRunner(tool_timeout=5.0)
        self._runners_to_shutdown.append(runner)

        runner.execute_tool_with_status("test_iterative_cooperative_tool", {})
        self.assertEqual(completed_steps, [1, 2], "Iterative tool continued running after cancellation!")

    def test_subprocess_job_object_tree_cessation_on_tool_timeout(self):
        """execute_powershell child process tree is reaped via Win32 Job Object on timeout."""
        import jarvis.skills.dev_shell  # noqa: F401
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        approval_mgr = DualKeyApprovalManager.get_instance()
        approval_mgr.grant_approval("execute_powershell", {"command": "Start-Sleep -Seconds 10"})

        runner = UnifiedRunner(tool_timeout=0.4)
        self._runners_to_shutdown.append(runner)

        import time
        start_t = time.monotonic()
        # Start a 10-second powershell sleep with dual_key_confirmed=True
        res = runner.execute_tool_with_status(
            "execute_powershell",
            {"command": "Start-Sleep -Seconds 10"},
            dual_key_confirmed=True,
        )
        elapsed = time.monotonic() - start_t

        self.assertFalse(res.success)
        self.assertEqual(res.failure_category, "timeout")
        # Ensure runner returned within tight deadline tolerance (0.4s timeout, allowing reasonable process launch margin)
        self.assertLess(elapsed, 6.0, f"Subprocess did not terminate promptly; elapsed={elapsed:.2f}s")

    def test_subprocess_launch_prevented_by_expired_or_cancelled_token(self):
        """Expired cancellation token prevents subprocess launch entirely."""
        import time

        import jarvis.skills.dev_shell  # noqa: F401
        from jarvis.core.cancellation import (
            ToolCancellationToken,
            bind_tool_cancellation_token,
            reset_tool_cancellation_token,
        )
        from jarvis.skills.dev_shell import execute_powershell

        # Mint an already-expired token (deadline in the past)
        expired_token = ToolCancellationToken(deadline=time.monotonic() - 1.0)
        self.assertTrue(expired_token.is_cancelled)

        ctx_tok = bind_tool_cancellation_token(expired_token)
        try:
            start_t = time.monotonic()
            out = execute_powershell("Start-Sleep -Seconds 5")
            elapsed = time.monotonic() - start_t

            self.assertIn("cancelled before launch: execution deadline expired", out)
            self.assertLess(elapsed, 0.1, f"Execution was not aborted immediately; elapsed={elapsed:.3f}s")
        finally:
            reset_tool_cancellation_token(ctx_tok)

        # Also verify explicitly cancelled token before launch
        cancelled_token = ToolCancellationToken(deadline=time.monotonic() + 10.0)
        cancelled_token.cancel()
        self.assertTrue(cancelled_token.is_cancelled)

        ctx_tok2 = bind_tool_cancellation_token(cancelled_token)
        try:
            start_t = time.monotonic()
            out2 = execute_powershell("Start-Sleep -Seconds 5")
            elapsed2 = time.monotonic() - start_t

            self.assertIn("cancelled before launch", out2)
            self.assertLess(elapsed2, 0.1, f"Cancelled execution was not aborted immediately; elapsed={elapsed2:.3f}s")
        finally:
            reset_tool_cancellation_token(ctx_tok2)

    def test_subprocess_normal_bounded_execution_succeeds(self):
        """Normal execution within deadline succeeds without spurious cancellation."""
        import jarvis.skills.dev_shell  # noqa: F401
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        approval_mgr = DualKeyApprovalManager.get_instance()
        approval_mgr.grant_approval("execute_powershell", {"command": "Write-Output 'BoundedSuccess'"})

        runner = UnifiedRunner(tool_timeout=15.0)
        self._runners_to_shutdown.append(runner)

        res = runner.execute_tool_with_status(
            "execute_powershell",
            {"command": "Write-Output 'BoundedSuccess'"},
            dual_key_confirmed=True,
        )
        self.assertTrue(res.success, f"Subprocess failed: {res.result}")
        self.assertIn("BoundedSuccess", res.result)

    def test_uncooperative_worker_timeout_reports_failure_without_caller_hang(self):
        """Uncooperative worker timeout reports failure promptly without caller blocking."""
        blocker = threading.Event()
        self._cleanup_events.append(blocker)

        @SkillRegistry.register(name="test_uncooperative_hang_tool", description="Blocks on event")
        def test_uncooperative_hang_tool():
            blocker.wait(timeout=10.0)
            return "unblocked"

        runner = UnifiedRunner(tool_timeout=0.3)
        self._runners_to_shutdown.append(runner)

        import time
        start_t = time.monotonic()
        res = runner.execute_tool_with_status("test_uncooperative_hang_tool", {})
        elapsed = time.monotonic() - start_t

        self.assertFalse(res.success)
        self.assertEqual(res.failure_category, "timeout")
        self.assertLess(elapsed, 1.5, f"Caller hung on uncooperative worker; elapsed={elapsed:.2f}s")
        blocker.set()

    def test_general_pool_saturation_and_renewal_recovers_fresh_commands(self):
        """Saturated general pool undergoes controlled renewal, enabling fresh commands to succeed immediately."""
        blocker = threading.Event()
        self._cleanup_events.append(blocker)

        @SkillRegistry.register(name="test_saturation_worker_tool", description="Blocks worker")
        def test_saturation_worker_tool(worker_id: int = 0):
            blocker.wait(timeout=10.0)
            return f"free {worker_id}"

        runner = UnifiedRunner(tool_timeout=0.2)
        self._runners_to_shutdown.append(runner)

        initial_gen = runner._generation_id

        # Occupy all 6 workers with uncooperative tasks using distinct args
        threads = []
        for i in range(6):
            t = threading.Thread(
                target=runner.execute_tool_with_status,
                args=("test_saturation_worker_tool", {"worker_id": i}),
            )
            threads.append(t)
            t.start()

        # Wait for them to time out and leave pool saturated
        for t in threads:
            t.join(timeout=2.0)

        # General pool is now saturated with timed-out workers.
        # Submitting a new task must trigger controlled pool renewal!
        fresh_res = runner.execute_tool_with_status("test_successful_tool", {"step": 99})
        self.assertTrue(fresh_res.success, f"Fresh tool failed after saturation: {fresh_res.result}")
        self.assertIn("CPU temperature is 45C (step 99)", fresh_res.result)
        self.assertGreater(runner._generation_id, initial_gen, "Executor generation was not incremented")

        # Cleanup: release initial blocked workers
        blocker.set()

    def test_repeated_saturation_exceeds_cap_and_fails_closed_with_degraded_capacity(self):
        """Repeated saturation reaching the configured generation cap fails closed with degraded capacity error."""
        runner = UnifiedRunner(tool_timeout=5.0)
        self._runners_to_shutdown.append(runner)

        # Artificially set max_retired_generations to 1 and simulate saturation
        runner._max_retired_generations = 1
        fake_retired_exec = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fake_ev = threading.Event()
        fake_retired_exec.submit(fake_ev.wait)
        runner._retired_executors.append((fake_retired_exec, 1))
        runner._general_active_tasks = 6  # simulate full saturation
        runner._general_timed_out_workers = 6

        res = runner.execute_tool_with_status("test_successful_tool", {})
        self.assertFalse(res.success)
        self.assertEqual(res.failure_category, "degraded_capacity")
        self.assertIn("capacity exhausted", res.result)
        fake_ev.set()
        fake_retired_exec.shutdown(wait=False)

    def test_concurrent_submissions_during_renewal_no_lost_work_or_races(self):
        """Concurrent callers submitting during renewal do not crash, lose work, or reuse retired executors."""
        runner = UnifiedRunner(tool_timeout=3.0)
        self._runners_to_shutdown.append(runner)

        results = []
        threads = []
        for i in range(12):
            def submitter(idx: int):
                res = runner.execute_tool_with_status("test_successful_tool", {"step": idx})
                results.append(res)
            t = threading.Thread(target=submitter, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(results), 12)
        for res in results:
            self.assertTrue(res.success, f"Concurrent submission failed: {res.result}")

    def test_uia_pool_saturation_no_overlapping_replacement_fails_closed(self):
        """Saturated/degraded UIA pool does not spawn replacement pool; fails closed with degraded capacity."""
        runner = UnifiedRunner(tool_timeout=5.0)
        self._runners_to_shutdown.append(runner)

        # Simulate degraded UIA state with 2 active workers (max cap for UIA)
        with runner._executor_lock:
            runner._uia_degraded = True
            runner._uia_active_tasks = 2

        # UIA skills are listed in UIA_SKILL_NAMES (e.g. 'click_ui_element')
        res = runner.execute_tool_with_status("click_ui_element", {})
        self.assertFalse(res.success)
        self.assertEqual(res.failure_category, "degraded_capacity")
        self.assertIn("UIA worker pool is degraded", res.result)
        # Ensure UIA executor was NOT replaced
        self.assertEqual(runner._uia_executor._max_workers, 2)

    def test_async_caller_cancellation_propagates_to_running_tool(self):
        """Cancelling the asynchronous caller task triggers cancellation in the running tool."""
        import asyncio

        started = threading.Event()
        observed_cancelled = threading.Event()
        blocker = threading.Event()
        self._cleanup_events.append(blocker)

        @SkillRegistry.register(name="test_async_cancellable_tool", description="Checks cancellation")
        def test_async_cancellable_tool():
            from jarvis.core.cancellation import get_current_tool_cancellation_token
            tok = get_current_tool_cancellation_token()
            started.set()
            for _ in range(50):
                if tok is not None and tok.is_cancelled:
                    observed_cancelled.set()
                    return "cancelled observed"
                if blocker.wait(timeout=0.05):
                    break
            return "done without cancel"

        runner = UnifiedRunner(tool_timeout=5.0)
        self._runners_to_shutdown.append(runner)

        async def run_async_test():
            task = asyncio.create_task(runner.execute_tool_async("test_async_cancellable_tool", {}))
            # Wait for tool to start
            while not started.is_set():
                await asyncio.sleep(0.01)
            # Cancel the task
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run_async_test())
        # Verify the running worker observed the cancellation event
        self.assertTrue(observed_cancelled.wait(timeout=2.0), "Worker did not observe async cancellation!")
        blocker.set()

    def test_post_timeout_late_result_fencing_discards_output(self):
        """Tool finishing after execution timeout has its output fenced and discarded."""
        release_late = threading.Event()
        self._cleanup_events.append(release_late)

        @SkillRegistry.register(name="test_late_finishing_tool", description="Finishes after timeout")
        def test_late_finishing_tool():
            release_late.wait(timeout=5.0)
            return "late secret data"

        runner = UnifiedRunner(tool_timeout=0.1)
        self._runners_to_shutdown.append(runner)

        # Call with 0.1s timeout
        res = runner.execute_tool_with_status("test_late_finishing_tool", {})
        self.assertFalse(res.success)
        self.assertEqual(res.failure_category, "timeout")

        # Now release the late tool
        release_late.set()

    def test_clean_runner_shutdown_with_blocked_test_workers(self):
        """Runner shutdown with in-flight uncooperative workers is bounded (<50ms)."""
        blocker = threading.Event()
        self._cleanup_events.append(blocker)

        @SkillRegistry.register(name="test_shutdown_hang_tool", description="Blocks on event")
        def test_shutdown_hang_tool():
            blocker.wait(timeout=10.0)
            return "done"

        runner = UnifiedRunner(tool_timeout=10.0)
        # Dispatch a tool offloaded to worker
        t = threading.Thread(target=runner.execute_tool_with_status, args=("test_shutdown_hang_tool", {}))
        t.start()

        import time
        start_t = time.monotonic()
        # Shutdown with wait=False must return promptly
        runner.shutdown(wait=False)
        elapsed = time.monotonic() - start_t

        self.assertLess(elapsed, 0.2, f"Shutdown took too long; elapsed={elapsed:.2f}s")
        # Cleanup
        blocker.set()
        t.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
