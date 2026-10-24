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
            cooldown_sec=2.0,
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
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        DualKeyApprovalManager.get_instance().reset()
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
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        orchestrator = ToolOrchestrator(taint_tracker=self.tracker)

        # 1. Taint the turn via an untrusted ingestion tool
        self.tracker.mark_tainted("web_search")

        # 2. Attempt critical execution without authorization -> blocked
        res = orchestrator.execute_step("execute_powershell", {"command": "dir"}, step_index=2)
        self.assertIn("Critical Authorization Gate", str(res))

        # 3. Attempt risk_level >= 1 execution without dual key confirmation -> blocked
        res_risk = orchestrator.execute_step("_test_high_risk_tool", {}, step_index=3)
        self.assertIn("Dual-Key Safety Gate", str(res_risk))

        # 4. Attempt with genuine dual key approval grant -> permitted
        approval_mgr = DualKeyApprovalManager.get_instance()
        approval_mgr.reset()
        approval_mgr.grant_approval("_test_high_risk_tool", {})
        res_confirmed = orchestrator.execute_step("_test_high_risk_tool", {}, step_index=4)
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

    def test_tool_orchestrator_read_only_cache(self):
        from jarvis.core.pipeline.dual_track import DualTrackCoordinator
        coordinator = DualTrackCoordinator(cache_ttl_seconds=60.0)
        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=coordinator,
        )

        call_counter = 0
        @SkillRegistry.register(name="_test_cached_ro_tool", description="RO tool", read_only=True)
        def _cached_ro():
            nonlocal call_counter
            call_counter += 1
            return f"computed_val_{call_counter}"

        self.addCleanup(lambda: SkillRegistry.unregister("_test_cached_ro_tool"))

        # First call executes and caches
        res1 = orchestrator.execute_step("_test_cached_ro_tool", {})
        self.assertEqual(res1, "computed_val_1")
        self.assertEqual(call_counter, 1)

        # Second call returns cached without invoking underlying function again
        res2 = orchestrator.execute_step("_test_cached_ro_tool", {})
        self.assertEqual(res2, "computed_val_1")
        self.assertEqual(call_counter, 1)

    def test_execute_step_async_uncoordinated_fallback(self):
        """Verifies execute_step_async executes cleanly without TypeError when
        dual_track_coordinator is None, passes arguments, and dispatches exactly once.
        """
        import asyncio

        call_count = 0
        received_args = []

        @SkillRegistry.register(name="_test_async_fallback_tool", description="Fallback test", risk_level=0)
        def _test_async_fallback(query_arg: str = ""):
            nonlocal call_count
            call_count += 1
            received_args.append(query_arg)
            return f"Processed: {query_arg}"

        self.addCleanup(lambda: SkillRegistry.unregister("_test_async_fallback_tool"))

        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=None,
        )

        # 1. Execute uncoordinated async tool call with user_query
        result = asyncio.run(
            orchestrator.execute_step_async(
                "_test_async_fallback_tool",
                {"query_arg": "sample_query"},
                user_query="what is battery vitals",
                step_index=1,
            )
        )

        # 2. Verify successful return value
        self.assertEqual(result, "Processed: sample_query")

        # 3. Verify exactly one execution (absence of duplicate dispatch)
        self.assertEqual(call_count, 1)
        self.assertEqual(received_args, ["sample_query"])

        # 4. Verify trajectory history is recorded accurately
        self.assertEqual(len(orchestrator._history), 1)
        self.assertEqual(orchestrator._history[0][0], "_test_async_fallback_tool")



    def test_execute_step_async_approval_wait_does_not_block_event_loop(self):
        """Verifies that waiting for human approval inside execute_step_async runs
        off-thread and does not block concurrent coroutines on the asyncio event loop.
        """
        import asyncio
        import threading
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        from jarvis.core.pipeline.dual_track import DualTrackCoordinator

        approval_mgr = DualKeyApprovalManager.get_instance()
        approval_mgr.reset()
        self.tracker.reset_turn()

        @SkillRegistry.register(name="_test_async_gated_tool", description="Gated tool", risk_level=2)
        def _test_gated_action(target: str = ""):
            return f"Executed gated: {target}"

        self.addCleanup(lambda: SkillRegistry.unregister("_test_async_gated_tool"))

        approval_event = threading.Event()
        approval_requested = threading.Event()

        def mock_approval_handler(req):
            approval_requested.set()
            approved = approval_event.wait(timeout=2.0)
            return approved

        approval_mgr.set_approval_handler(mock_approval_handler)
        self.addCleanup(lambda: approval_mgr.set_approval_handler(None))

        coordinator = DualTrackCoordinator(cache_ttl_seconds=60.0)
        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=coordinator,
        )

        async def run_test():
            ticks = 0
            stop_ticker = asyncio.Event()

            async def ticker():
                nonlocal ticks
                while not stop_ticker.is_set():
                    ticks += 1
                    await asyncio.sleep(0.01)

            ticker_task = asyncio.create_task(ticker())

            exec_task = asyncio.create_task(
                orchestrator.execute_step_async(
                    "_test_async_gated_tool",
                    {"target": "async_test"},
                    step_index=1,
                )
            )

            # Wait until approval handler has been entered
            while not approval_requested.is_set():
                await asyncio.sleep(0.01)

            # Let the ticker run for at least 5 ticks while approval is pending
            while ticks < 5:
                await asyncio.sleep(0.01)

            # Now approve the action
            approval_event.set()
            result = await exec_task

            stop_ticker.set()
            await ticker_task

            self.assertEqual(result, "Executed gated: async_test")
            self.assertGreaterEqual(ticks, 5)

        asyncio.run(run_test())

    def test_execute_step_async_approval_denial_fails_closed(self):
        """Verifies that human approval denial in execute_step_async fails closed
        and returns an authorization error without executing the tool.
        """
        import asyncio
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        from jarvis.core.pipeline.dual_track import DualTrackCoordinator

        approval_mgr = DualKeyApprovalManager.get_instance()
        approval_mgr.reset()
        self.tracker.reset_turn()

        executed = False
        @SkillRegistry.register(name="_test_async_deny_tool", description="Deny test", risk_level=2)
        def _test_deny_action():
            nonlocal executed
            executed = True
            return "Should not execute"

        self.addCleanup(lambda: SkillRegistry.unregister("_test_async_deny_tool"))

        def mock_denial_handler(req):
            return False

        approval_mgr.set_approval_handler(mock_denial_handler)
        self.addCleanup(lambda: approval_mgr.set_approval_handler(None))

        coordinator = DualTrackCoordinator(cache_ttl_seconds=60.0)
        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=coordinator,
        )

        async def run_test():
            result = await orchestrator.execute_step_async(
                "_test_async_deny_tool",
                {},
                step_index=1,
            )
            self.assertIn("Critical Authorization Gate", str(result))
            self.assertFalse(executed)

        asyncio.run(run_test())

    def test_execute_step_async_approval_timeout_fails_closed(self):
        """Verifies that approval handler timeout in execute_step_async fails closed
        and returns an authorization error without blocking the event loop indefinitely.
        """
        import asyncio
        import threading
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        from jarvis.core.pipeline.dual_track import DualTrackCoordinator

        approval_mgr = DualKeyApprovalManager.get_instance()
        approval_mgr.reset()
        self.tracker.reset_turn()

        executed = False
        @SkillRegistry.register(name="_test_async_timeout_tool", description="Timeout test", risk_level=2)
        def _test_timeout_action():
            nonlocal executed
            executed = True
            return "Should not execute"

        self.addCleanup(lambda: SkillRegistry.unregister("_test_async_timeout_tool"))

        timeout_event = threading.Event()
        def mock_timeout_handler(req):
            # Wait with a short timeout and return False on timeout
            timed_out = not timeout_event.wait(timeout=0.05)
            return False if timed_out else True

        approval_mgr.set_approval_handler(mock_timeout_handler)
        self.addCleanup(lambda: approval_mgr.set_approval_handler(None))

        coordinator = DualTrackCoordinator(cache_ttl_seconds=60.0)
        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=coordinator,
        )

        async def run_test():
            result = await orchestrator.execute_step_async(
                "_test_async_timeout_tool",
                {},
                step_index=1,
            )
            self.assertIn("Critical Authorization Gate", str(result))
            self.assertFalse(executed)

        asyncio.run(run_test())

    def test_execute_step_async_successful_human_approval_lifecycle(self):
        """Exercises the complete successful asynchronous human approval lifecycle in execute_step_async:
        1. Privilege check in tainted turn enforces approval wait off-thread.
        2. Event loop remains responsive (ticker runs) while approval is pending.
        3. Human approval via approval manager unblocks execution.
        4. Intended tool executes exactly once and receives verified active ContextVar authorization.
        5. Single-use grant cannot authorize repeat execution, altered arguments, another tool, or another turn.
        6. Active authorization ContextVar is completely cleared after success and after exception.
        """
        import asyncio
        import threading
        from jarvis.core.security_boundaries import DualKeyApprovalManager, _active_auth_context
        from jarvis.core.pipeline.dual_track import DualTrackCoordinator

        approval_mgr = DualKeyApprovalManager.get_instance()
        approval_mgr.reset()
        self.tracker.reset_turn()

        exec_records = []
        captured_tokens = []

        @SkillRegistry.register(name="_test_async_lifecycle_tool", description="Privileged tool", risk_level=1)
        def _test_lifecycle_action(target: str = "", mode: str = "normal"):
            nonlocal exec_records, captured_tokens
            # Verify active authorization is present in the worker thread via ContextVar
            is_authed = approval_mgr.verify_and_consume_active_context(
                "_test_async_lifecycle_tool", {"target": target, "mode": mode}
            )
            token = _active_auth_context.get()
            captured_tokens.append(token)
            exec_records.append({"target": target, "mode": mode, "authorized": is_authed})
            if mode == "fail":
                raise RuntimeError("Simulated failure in privileged tool")
            return f"Executed: {target}:{mode}"

        @SkillRegistry.register(name="_test_async_unauthorized_tool", description="Different tool", risk_level=1)
        def _test_unauthed_action(target: str = ""):
            return f"Unauthed executed: {target}"

        self.addCleanup(lambda: SkillRegistry.unregister("_test_async_lifecycle_tool"))
        self.addCleanup(lambda: SkillRegistry.unregister("_test_async_unauthorized_tool"))

        coordinator = DualTrackCoordinator(cache_ttl_seconds=60.0)
        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=coordinator,
        )

        approval_requested = threading.Event()
        approval_decision_event = threading.Event()
        decision_value = False

        def mock_approval_handler(req):
            nonlocal decision_value
            approval_requested.set()
            approval_decision_event.wait(timeout=5.0)
            return decision_value

        approval_mgr.set_approval_handler(mock_approval_handler)
        self.addCleanup(lambda: approval_mgr.set_approval_handler(None))

        async def run_lifecycle_test():
            # 1. Taint turn via untrusted ingestion
            self.tracker.mark_tainted("web_search")
            self.assertTrue(self.tracker.is_tainted)

            # Start background event loop ticker
            ticks = 0
            stop_ticker = asyncio.Event()
            async def ticker():
                nonlocal ticks
                while not stop_ticker.is_set():
                    ticks += 1
                    await asyncio.sleep(0.01)

            ticker_task = asyncio.create_task(ticker())

            # 2. Invoke privileged tool in tainted turn
            exec_task = asyncio.create_task(
                orchestrator.execute_step_async(
                    "_test_async_lifecycle_tool",
                    {"target": "host_cfg", "mode": "normal"},
                    step_index=1,
                )
            )

            # Wait until approval handler is entered off-thread
            while not approval_requested.is_set():
                await asyncio.sleep(0.01)

            # Execution remains pending, tool has NOT executed
            self.assertEqual(len(exec_records), 0)
            self.assertFalse(exec_task.done())

            # 3. Verify event loop remains responsive while approval is pending
            while ticks < 5:
                await asyncio.sleep(0.01)
            self.assertGreaterEqual(ticks, 5)

            # 4. Grant approval through approval manager
            nonlocal decision_value
            decision_value = True
            approval_decision_event.set()

            result = await exec_task
            stop_ticker.set()
            await ticker_task

            # Verify tool executed exactly once
            self.assertEqual(result, "Executed: host_cfg:normal")
            self.assertEqual(len(exec_records), 1)
            self.assertEqual(exec_records[0]["target"], "host_cfg")
            self.assertTrue(exec_records[0]["authorized"])

            # 5. Verify ContextVar active authorization is cleared on caller context after success
            self.assertIsNone(_active_auth_context.get())
            self.assertFalse(
                approval_mgr.verify_and_consume_active_context(
                    "_test_async_lifecycle_tool", {"target": "host_cfg", "mode": "normal"}
                )
            )

            # Set approval handler to deny for boundary validation checks
            approval_mgr.set_approval_handler(lambda req: False)

            # 6a. Verify single-use: Grant cannot authorize a repeated invocation
            repeat_res = await orchestrator.execute_step_async(
                "_test_async_lifecycle_tool",
                {"target": "host_cfg", "mode": "normal"},
                step_index=2,
            )
            self.assertIn("Dual-Key Safety Gate", str(repeat_res))
            self.assertEqual(len(exec_records), 1)

            # 6b. Verify grant cannot authorize altered command arguments
            altered_res = await orchestrator.execute_step_async(
                "_test_async_lifecycle_tool",
                {"target": "altered_cfg", "mode": "normal"},
                step_index=3,
            )
            self.assertIn("Dual-Key Safety Gate", str(altered_res))
            self.assertEqual(len(exec_records), 1)

            # 6c. Verify grant cannot authorize a different tool
            diff_tool_res = await orchestrator.execute_step_async(
                "_test_async_unauthorized_tool",
                {"target": "host_cfg"},
                step_index=4,
            )
            self.assertIn("Dual-Key Safety Gate", str(diff_tool_res))
            self.assertEqual(len(exec_records), 1)

            # 6d. Verify grant cannot authorize execution in another turn
            self.tracker.reset_turn()
            orchestrator.reset()
            self.tracker.mark_tainted("web_search")
            new_turn_res = await orchestrator.execute_step_async(
                "_test_async_lifecycle_tool",
                {"target": "host_cfg", "mode": "normal"},
                step_index=1,
            )
            self.assertIn("Dual-Key Safety Gate", str(new_turn_res))
            self.assertEqual(len(exec_records), 1)

            # 7. Verify active authorization is cleared after a tool exception
            approval_mgr.grant_approval("_test_async_lifecycle_tool", {"target": "host_cfg", "mode": "fail"})
            fail_res = await orchestrator.execute_step_async(
                "_test_async_lifecycle_tool",
                {"target": "host_cfg", "mode": "fail"},
                step_index=2,
            )
            self.assertIn("Simulated failure in privileged tool", str(fail_res))
            self.assertEqual(len(exec_records), 2)
            self.assertTrue(exec_records[1]["authorized"])
            # Verify ContextVar is cleared and grant invalidated post-exception
            self.assertIsNone(_active_auth_context.get())
            self.assertFalse(
                approval_mgr.verify_and_consume_active_context(
                    "_test_async_lifecycle_tool", {"target": "host_cfg", "mode": "fail"}
                )
            )

        asyncio.run(run_lifecycle_test())

    def test_execute_step_async_cancellation_during_pending_approval(self):
        """Verifies cancellation of the asyncio task awaiting execute_step_async while
        human approval evaluation is pending off-thread:
        1. Privilege check in tainted turn triggers approval handler off-thread.
        2. Asyncio task is cancelled while awaiting approval.
        3. Caller observes asyncio.CancelledError and event loop remains responsive.
        4. Late operator approval is safely discarded by request_human_approval.
        5. Underlying tool is never executed and no authorization remains active or reusable.
        6. Approval worker finishes within bounded timeout with zero orphaned requests.
        """
        import asyncio
        import threading
        from jarvis.core.security_boundaries import DualKeyApprovalManager, _active_auth_context
        from jarvis.core.pipeline.dual_track import DualTrackCoordinator

        approval_mgr = DualKeyApprovalManager.get_instance()
        approval_mgr.reset()
        self.tracker.reset_turn()

        tool_executed = False

        @SkillRegistry.register(name="_test_async_cancel_tool", description="Cancel tool test", risk_level=1)
        def _test_cancel_action(target: str = ""):
            nonlocal tool_executed
            tool_executed = True
            return f"Executed cancel tool: {target}"

        self.addCleanup(lambda: SkillRegistry.unregister("_test_async_cancel_tool"))

        approval_requested = threading.Event()
        operator_decision_event = threading.Event()
        handler_finished = threading.Event()
        captured_cancel_event = None

        def mock_blocking_handler(req):
            nonlocal captured_cancel_event
            captured_cancel_event = req.get("cancel_event")
            approval_requested.set()
            # Wait for either late operator response or bounded timeout
            operator_decision_event.wait(timeout=2.0)
            handler_finished.set()
            # Late operator approval decision:
            return True

        approval_mgr.set_approval_handler(mock_blocking_handler)
        self.addCleanup(lambda: approval_mgr.set_approval_handler(None))

        coordinator = DualTrackCoordinator(cache_ttl_seconds=60.0)
        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=coordinator,
        )

        async def run_cancel_test():
            # 1. Taint turn
            self.tracker.mark_tainted("web_search")
            self.assertTrue(self.tracker.is_tainted)

            # Start event-loop ticker to verify loop responsiveness
            ticks = 0
            stop_ticker = asyncio.Event()

            async def ticker():
                nonlocal ticks
                while not stop_ticker.is_set():
                    ticks += 1
                    await asyncio.sleep(0.01)

            ticker_task = asyncio.create_task(ticker())

            # 2. Start privileged tool call in tainted turn
            exec_task = asyncio.create_task(
                orchestrator.execute_step_async(
                    "_test_async_cancel_tool",
                    {"target": "cancel_target"},
                    step_index=1,
                )
            )

            # Wait until approval handler confirms request is pending
            while not approval_requested.is_set():
                await asyncio.sleep(0.01)

            self.assertFalse(exec_task.done())
            self.assertFalse(tool_executed)

            # 3. Cancel the awaiting asyncio task
            exec_task.cancel()

            # 4. Verify caller observes documented cancellation behavior
            with self.assertRaises(asyncio.CancelledError):
                await exec_task

            # Verify event loop remained responsive throughout
            self.assertGreaterEqual(ticks, 1)

            # Verify cancel_event was propagated to the approval request
            self.assertIsNotNone(captured_cancel_event)
            self.assertTrue(captured_cancel_event.is_set())

            # 5. Subsequently resolve approval handler as approved (simulating delayed operator response)
            operator_decision_event.set()

            # 6. Verify worker finishes within bounded timeout
            worker_completed = handler_finished.wait(timeout=1.0)
            self.assertTrue(worker_completed, "Approval worker did not finish within bounded timeout")

            stop_ticker.set()
            await ticker_task

            # 7. Verify underlying tool was never executed
            self.assertFalse(tool_executed)

            # 8. Verify no authorization remains active or reusable
            self.assertIsNone(_active_auth_context.get())
            self.assertFalse(
                approval_mgr.verify_and_consume_active_context(
                    "_test_async_cancel_tool", {"target": "cancel_target"}
                )
            )
            self.assertFalse(
                approval_mgr.has_valid_grant("_test_async_cancel_tool", {"target": "cancel_target"})
            )

        asyncio.run(run_cancel_test())

    def test_execute_step_async_cancellation_race_at_grant_issuance_boundary(self):
        """Deterministic race regression test:
        Verifies that when an approval handler returns True, but the awaiting asyncio task
        is cancelled at the grant-issuance boundary before registration:
        1. Awaiting task observes asyncio.CancelledError.
        2. No usable grant survives the cancelled request, even if the worker finishes after caller is cancelled.
        3. The same tool and arguments cannot execute through a later invocation using a leftover grant.
        4. _active_auth_context remains clear in caller and no authorization is bound.
        5. Pending approval request is explicitly tracked and terminates cleanly.
        """
        import asyncio
        import threading
        from jarvis.core.security_boundaries import DualKeyApprovalManager, _active_auth_context
        from jarvis.core.pipeline.dual_track import DualTrackCoordinator

        approval_mgr = DualKeyApprovalManager.get_instance()
        approval_mgr.reset()
        self.tracker.reset_turn()

        tool_executed = False

        @SkillRegistry.register(name="_test_async_race_tool", description="Race tool test", risk_level=2)
        def _test_race_action(target: str = ""):
            nonlocal tool_executed
            tool_executed = True
            return f"Executed race tool: {target}"

        self.addCleanup(lambda: SkillRegistry.unregister("_test_async_race_tool"))

        def mock_approval_handler(req):
            return True

        approval_mgr.set_approval_handler(mock_approval_handler)
        self.addCleanup(lambda: approval_mgr.set_approval_handler(None))

        worker_at_boundary = threading.Event()
        release_worker = threading.Event()

        def pre_grant_hook():
            worker_at_boundary.set()
            release_worker.wait(timeout=2.0)

        approval_mgr.set_pre_grant_creation_hook(pre_grant_hook)
        self.addCleanup(lambda: approval_mgr.set_pre_grant_creation_hook(None))

        coordinator = DualTrackCoordinator(cache_ttl_seconds=60.0)
        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=coordinator,
        )

        async def run_race_test():
            # 1. Start execution task
            exec_task = asyncio.create_task(
                orchestrator.execute_step_async(
                    "_test_async_race_tool",
                    {"target": "race_payload"},
                    step_index=1,
                )
            )

            # Wait until worker reaches grant-issuance boundary (after handler returned True)
            while not worker_at_boundary.is_set():
                await asyncio.sleep(0.01)

            # Verify pending approval request is explicitly tracked
            self.assertEqual(approval_mgr.get_pending_request_count(), 1)
            self.assertFalse(exec_task.done())
            self.assertFalse(tool_executed)

            # 2. Cancel the awaiting asyncio task while worker is paused at boundary
            exec_task.cancel()

            # 3. Assert awaiting task observes cancellation
            with self.assertRaises(asyncio.CancelledError):
                await exec_task

            # 4. Release worker and allow worker cleanup to finish
            release_worker.set()

            # Wait for worker to finish and verify pending request count returns to 0
            for _ in range(100):
                if approval_mgr.get_pending_request_count() == 0:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(approval_mgr.get_pending_request_count(), 0)

            # 5. Assert no usable grant survives the cancelled request
            self.assertFalse(
                approval_mgr.has_valid_grant("_test_async_race_tool", {"target": "race_payload"})
            )
            self.assertIsNone(
                approval_mgr.consume_grant_object("_test_async_race_tool", {"target": "race_payload"})
            )

            # 6. Assert _active_auth_context remains clear in caller
            self.assertIsNone(_active_auth_context.get())
            self.assertFalse(
                approval_mgr.verify_and_consume_active_context(
                    "_test_async_race_tool", {"target": "race_payload"}
                )
            )

            # 7. Assert same tool and arguments CANNOT execute through a later invocation using leftover grant
            orchestrator.reset()
            # Set approval handler to deny for subsequent invocation
            approval_mgr.set_approval_handler(lambda req: False)
            result2 = await orchestrator.execute_step_async(
                "_test_async_race_tool",
                {"target": "race_payload"},
                step_index=2,
            )
            self.assertIn("Critical Authorization Gate", str(result2))
            self.assertFalse(tool_executed)

        asyncio.run(run_race_test())

    def test_execute_step_async_cancellation_race_post_grant_registration(self):
        """Deterministic race regression test for post-grant-registration pause:
        Verifies that even if grant was already registered under lock, cancellation
        atomically revokes it via cancel_approval_request, leaving zero usable grants.
        """
        import asyncio
        import threading
        from jarvis.core.security_boundaries import DualKeyApprovalManager, _active_auth_context
        from jarvis.core.pipeline.dual_track import DualTrackCoordinator

        approval_mgr = DualKeyApprovalManager.get_instance()
        approval_mgr.reset()
        self.tracker.reset_turn()

        tool_executed = False

        @SkillRegistry.register(name="_test_async_post_race_tool", description="Post race tool", risk_level=2)
        def _test_post_race_action(target: str = ""):
            nonlocal tool_executed
            tool_executed = True
            return f"Executed post race tool: {target}"

        self.addCleanup(lambda: SkillRegistry.unregister("_test_async_post_race_tool"))

        approval_mgr.set_approval_handler(lambda req: True)
        self.addCleanup(lambda: approval_mgr.set_approval_handler(None))

        worker_registered_grant = threading.Event()
        release_worker = threading.Event()
        created_grant_ref = []

        def post_grant_hook(grant):
            created_grant_ref.append(grant)
            worker_registered_grant.set()
            release_worker.wait(timeout=2.0)

        approval_mgr.set_post_grant_creation_hook(post_grant_hook)
        self.addCleanup(lambda: approval_mgr.set_post_grant_creation_hook(None))

        coordinator = DualTrackCoordinator(cache_ttl_seconds=60.0)
        orchestrator = ToolOrchestrator(
            taint_tracker=self.tracker,
            dual_track_coordinator=coordinator,
        )

        async def run_post_race_test():
            exec_task = asyncio.create_task(
                orchestrator.execute_step_async(
                    "_test_async_post_race_tool",
                    {"target": "post_race_target"},
                    step_index=1,
                )
            )

            # Wait until grant has been created and registered
            while not worker_registered_grant.is_set():
                await asyncio.sleep(0.01)

            self.assertEqual(len(created_grant_ref), 1)
            raw_grant = created_grant_ref[0]

            # Cancel awaiting task while worker is paused after grant registration
            exec_task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await exec_task

            # Release worker
            release_worker.set()

            # Wait for worker to finish
            for _ in range(100):
                if approval_mgr.get_pending_request_count() == 0:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(approval_mgr.get_pending_request_count(), 0)

            # Assert grant was invalidated
            self.assertEqual(raw_grant.status, "invalidated")
            self.assertFalse(
                approval_mgr.has_valid_grant("_test_async_post_race_tool", {"target": "post_race_target"})
            )
            self.assertIsNone(
                approval_mgr.consume_grant_object("_test_async_post_race_tool", {"target": "post_race_target"})
            )

            # Attempting to bind the invalidated grant fails closed
            with self.assertRaises(PermissionError):
                approval_mgr.bind_active_grant(raw_grant)

            self.assertIsNone(_active_auth_context.get())
            self.assertFalse(tool_executed)

            # Subsequent execution without approval fails closed
            orchestrator.reset()
            approval_mgr.set_approval_handler(lambda req: False)
            result = await orchestrator.execute_step_async(
                "_test_async_post_race_tool",
                {"target": "post_race_target"},
                step_index=2,
            )
            self.assertIn("Critical Authorization Gate", str(result))
            self.assertFalse(tool_executed)

        asyncio.run(run_post_race_test())

if __name__ == "__main__":
    unittest.main()

