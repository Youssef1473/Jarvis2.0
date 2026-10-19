"""Unit tests for Post-9aeec4c Final Polish & Convergence.

Verifies:
1. EpochVector & EnvironmentEpochTracker shared between context_orchestrator and speculative engine.
2. TOU invalidation when environment mutations occur.
3. ReasoningBookmarkStack LIFO ordering, 900s TTL eviction, and multi-phrase resumption matching.
4. Orchestrator delegation to brain/tool_orchestrator and latency profiler turn tracing.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

from jarvis.context.context_orchestrator import (
    ContextOrchestrator,
    EpochVector as ContextEpochVector,
    EnvironmentEpochTracker as ContextEpochTracker,
)
from jarvis.conversation.bookmarks import (
    ReasoningBookmark,
    ReasoningBookmarkStack,
    DEFAULT_BOOKMARK_TTL_NS,
)
from jarvis.core.pipeline.latency_profiler import PipelineLatencyProfiler
from jarvis.skills.base import SkillRegistry
from jarvis.speculative.engine import (
    EpochVector as SpecEpochVector,
    EnvironmentEpochTracker as SpecEpochTracker,
    SpeculativeExecutionEngine,
)


class TestPost9aeec4cConvergence(unittest.TestCase):
    """Test suite for post-9aeec4c convergence invariants."""

    def setUp(self):
        ContextEpochTracker.reset()
        self.engine = SpeculativeExecutionEngine.get_instance()
        self.engine.discard_unclaimed()

    def tearDown(self):
        self.engine.discard_unclaimed()
        ContextEpochTracker.reset()

    def test_epoch_vector_and_tracker_shared_identity(self):
        """Verifies EpochVector and EnvironmentEpochTracker are shared across context and speculative modules."""
        self.assertIs(ContextEpochVector, SpecEpochVector)
        self.assertIs(ContextEpochTracker, SpecEpochTracker)

        v_context = ContextOrchestrator.get_epoch_vector()
        v_engine = self.engine.epoch_tracker.snapshot()
        self.assertEqual(v_context, v_engine)
        self.assertTrue(v_context.is_compatible_with(v_engine))

    def test_context_mutation_invalidates_speculative_cache(self):
        """Verifies ContextOrchestrator.notify_environment_mutation invalidates speculative cache at TOU."""
        call_count = 0

        @SkillRegistry.register(name="test_conv_read", read_only=True)
        def test_conv_read(item: str):
            nonlocal call_count
            call_count += 1
            return f"Data_{item}"

        key = self.engine.speculate("test_conv_read", item="catalog")
        for _ in range(50):
            with self.engine._entries_lock:
                entry = self.engine._active_entries.get(key)
            if entry and entry.future.done():
                break
            time.sleep(0.01)

        # Notify mutation via ContextOrchestrator
        ContextOrchestrator.notify_environment_mutation("fs", 101)

        # Claim: should detect mutated epoch and re-run synchronously
        result = self.engine.execute_or_claim("test_conv_read", item="catalog")
        self.assertEqual(result, "Data_catalog")
        self.assertEqual(call_count, 2)
        self.assertGreaterEqual(self.engine.metrics.stale_invalidations, 1)

    def test_bookmark_lifo_and_900s_ttl(self):
        """Verifies ReasoningBookmarkStack LIFO ordering and 900s TTL eviction."""
        stack = ReasoningBookmarkStack(max_depth=4)
        stack.clear()

        # Check default TTL
        self.assertEqual(DEFAULT_BOOKMARK_TTL_NS, 900_000_000_000)

        # Push two turns
        b1 = stack.push_interrupted_turn(
            turn_id="turn_1",
            original_query="First topic query",
            topic_label="first topic",
            spoken_clauses=["Clause 1"],
            unspoken_clauses=["Remaining 1"],
        )
        b2 = stack.push_interrupted_turn(
            turn_id="turn_2",
            original_query="Second topic query",
            topic_label="second topic",
            spoken_clauses=["Clause 2"],
            unspoken_clauses=["Remaining 2"],
        )

        self.assertEqual(len(stack), 2)
        # Strict LIFO: top is b2
        self.assertEqual(stack.peek_valid().turn_id, "turn_2")
        popped = stack.pop_valid()
        self.assertEqual(popped.turn_id, "turn_2")
        self.assertEqual(stack.peek_valid().turn_id, "turn_1")

        # Expired bookmark check: simulate >900s
        now_ns = time.perf_counter_ns()
        future_ns = now_ns + 901_000_000_000  # 901 seconds later
        self.assertTrue(b1.is_expired(now_ns=future_ns))

    def test_bookmark_multi_phrase_resumption_matching(self):
        """Verifies multi-phrase resumption matching for 'continue', 'go on', 'resume', 'as you were saying'."""
        stack = ReasoningBookmarkStack()

        # Phrases that must trigger explicit resumption
        resumption_phrases = [
            "continue",
            "go on",
            "resume",
            "as you were saying",
            "please continue",
            "continue what you were saying",
            "keep going",
        ]
        for phrase in resumption_phrases:
            self.assertTrue(stack.is_resumption_intent(phrase), f"Failed for '{phrase}'")
            self.assertEqual(stack.classify_barge_in_intent(phrase), "CLARIFY_AND_RESUME", f"Failed for '{phrase}'")

        # Test formatting injection for explicit resumption
        stack.clear()
        stack.push_interrupted_turn(
            turn_id="turn_resume_test",
            original_query="Explain the architecture",
            topic_label="the architecture",
            spoken_clauses=["We use a five stage pipeline."],
            unspoken_clauses=["The output stage uses Kokoro."],
        )
        xml = stack.format_prompt_injection("as you were saying")
        self.assertIn('<active_resumption_bookmark mode="CLARIFY_AND_RESUME">', xml)
        self.assertIn("Sir requested to resume", xml)
        self.assertIn("Continuing with the architecture...", xml)

    def test_orchestrator_delegation_and_profiler_tracing(self):
        """Verifies JarvisOrchestrator delegates to brain and records TurnTrace via latency_profiler."""
        from jarvis.core.orchestrator import JarvisOrchestrator

        with patch.object(JarvisOrchestrator, "__init__", lambda self, cfg: None):
            orch = JarvisOrchestrator({})
            orch.config = {}
            orch.brain = MagicMock()
            orch.tts = MagicMock()
            orch.sfx = MagicMock()
            orch.hud = MagicMock()
            orch.get_state = MagicMock(return_value="idle")
            orch.bookmarks = MagicMock()
            orch.bookmarks.format_prompt_injection.return_value = ""
            orch.latency_profiler = PipelineLatencyProfiler(window_size=16)
            orch.tool_orchestrator = MagicMock()
            orch._is_processing_command = False
            orch._extract_topic_label = JarvisOrchestrator._extract_topic_label

            mock_reply = "Architecture is validated, Sir."
            orch.brain.process_query.return_value = mock_reply
            orch.brain.is_fast_reflex.return_value = False
            orch.tts.finish_streaming_session.return_value = True

            orch.handle_command("explain the pipeline architecture")

            # Check that brain was invoked with flow state and bookmark context
            orch.brain.process_query.assert_called_once()
            call_kwargs = orch.brain.process_query.call_args[1]
            self.assertIn("flow_state_xml", call_kwargs)
            self.assertIn("bookmark_context", call_kwargs)

            # Check that latency profiler completed the turn
            percentiles = orch.latency_profiler.get_percentiles()
            self.assertIn("stt_ms", percentiles)


if __name__ == "__main__":
    unittest.main()
