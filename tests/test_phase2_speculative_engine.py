"""Unit tests for Phase 2: Speculative Read-Only Tool Execution Engine."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from jarvis.context.flow_state_mirror import EnvironmentEpochTracker, ResourceScope
from jarvis.skills.base import SkillRegistry, SpeculationForbiddenError
from jarvis.speculative.engine import (
    EpochVector,
    EnvironmentEpochTracker as SpeculativeEpochTracker,
    SpeculativeExecutionEngine,
)


class TestPhase2SpeculativeEngine(unittest.TestCase):
    def setUp(self):
        EnvironmentEpochTracker.reset()
        self.engine = SpeculativeExecutionEngine.get_instance()
        from jarvis.speculative.engine import ExecutionMetrics
        self.engine.metrics = ExecutionMetrics()
        self.engine.discard_unclaimed()

    def tearDown(self):
        self.engine.discard_unclaimed()
        EnvironmentEpochTracker.reset()

    def test_speculation_forbidden_on_mutating_tool(self):
        """Verifies that speculating on read_only=False skills raises SpeculationForbiddenError immediately."""
        @SkillRegistry.register(name="mutating_action", read_only=False)
        def mutating_action(arg: str):
            return f"Mutated {arg}"

        with self.assertRaises(SpeculationForbiddenError):
            self.engine.speculate("mutating_action", arg="system_state")

    def test_pre_completed_cache_hit(self):
        """Verifies 0ms cache hit when tool was speculatively executed before claim."""
        call_count = 0

        @SkillRegistry.register(name="fast_readonly_tool", read_only=True)
        def fast_readonly_tool(query: str):
            nonlocal call_count
            call_count += 1
            return f"Result for {query}"

        # 1. Speculate ahead
        key = self.engine.speculate("fast_readonly_tool", query="git_status")
        self.assertIsNotNone(key)

        # Allow worker thread to complete execution
        for _ in range(50):
            with self.engine._entries_lock:
                entry = self.engine._active_entries.get(key)
            if entry and entry.future.done():
                break
            time.sleep(0.01)

        # 2. Claim
        result = self.engine.execute_or_claim("fast_readonly_tool", query="git_status")
        self.assertEqual(result, "Result for git_status")
        self.assertEqual(call_count, 1)  # Only ran once in background
        self.assertEqual(self.engine.metrics.cache_hits_completed, 1)

    def test_single_flight_coalescing(self):
        """Verifies execute_or_claim waits and claims an in-flight future without re-running."""
        call_count = 0

        @SkillRegistry.register(name="slow_readonly_tool", read_only=True)
        def slow_readonly_tool(param: int):
            nonlocal call_count
            call_count += 1
            time.sleep(0.08)
            return param * 10

        self.engine.speculate("slow_readonly_tool", param=5)
        # Immediate claim while in flight
        result = self.engine.execute_or_claim("slow_readonly_tool", param=5)
        self.assertEqual(result, 50)
        self.assertEqual(call_count, 1)
        self.assertEqual(self.engine.metrics.cache_hits_inflight_coalesced, 1)

    def test_toctou_invalidation_on_scope_bump(self):
        """Verifies that an environment epoch bump invalidates stale speculative entries."""
        call_count = 0

        @SkillRegistry.register(name="scoped_read_tool", read_only=True, scopes=ResourceScope.FILESYSTEM)
        def scoped_read_tool(val: str):
            nonlocal call_count
            call_count += 1
            return f"Read {val}"

        self.engine.speculate("scoped_read_tool", val="manifest.json")
        time.sleep(0.02)

        # Environment mutated: files changed
        EnvironmentEpochTracker.bump(ResourceScope.FILESYSTEM)

        # Calling execute_or_claim should detect stale epoch and fall back to fresh sync execution
        result = self.engine.execute_or_claim("scoped_read_tool", val="manifest.json")
        self.assertEqual(result, "Read manifest.json")
        self.assertEqual(call_count, 2)  # 1 speculative (discarded) + 1 synchronous fallback
        self.assertGreaterEqual(self.engine.metrics.stale_invalidations, 1)

    def test_toctou_invalidation_on_file_stat_change(self):
        """Verifies that file modification invalidates the speculative cache for tracked path args."""
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            tf.write("initial data")
            temp_path = tf.name

        try:
            call_count = 0

            @SkillRegistry.register(
                name="file_reader_tool",
                read_only=True,
                scopes=ResourceScope.FILESYSTEM,
                path_arg_names=("filepath",),
            )
            def file_reader_tool(filepath: str):
                nonlocal call_count
                call_count += 1
                return Path(filepath).read_text(encoding="utf-8")

            # Speculate on initial state
            self.engine.speculate("file_reader_tool", filepath=temp_path)
            time.sleep(0.02)

            # Mutate file
            time.sleep(0.01)
            Path(temp_path).write_text("modified data", encoding="utf-8")

            # Claim: should invalidate and re-read modified data
            result = self.engine.execute_or_claim("file_reader_tool", filepath=temp_path)
            self.assertEqual(result, "modified data")
            self.assertEqual(call_count, 2)
            self.assertGreaterEqual(self.engine.metrics.stale_invalidations, 1)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_epoch_vector_immutability_and_compatibility(self):
        """Verifies EpochVector is frozen, uses slots, and performs strict 4-dim equality."""
        v1 = EpochVector(fs_usn_epoch=10, git_head_crc=12345, terminal_seq=3, active_hwnd=9999)
        v2 = EpochVector(fs_usn_epoch=10, git_head_crc=12345, terminal_seq=3, active_hwnd=9999)
        v3 = EpochVector(fs_usn_epoch=11, git_head_crc=12345, terminal_seq=3, active_hwnd=9999)

        self.assertTrue(v1.is_compatible_with(v2))
        self.assertFalse(v1.is_compatible_with(v3))

        # Immutability check
        with self.assertRaises((AttributeError, TypeError)):
            v1.fs_usn_epoch = 99  # type: ignore[misc]

    def test_environment_epoch_tracker_dimensions(self):
        """Verifies EnvironmentEpochTracker increments all 4 dimensions correctly."""
        tracker = SpeculativeEpochTracker()
        snap0 = tracker.snapshot()
        self.assertEqual(snap0.fs_usn_epoch, 0)
        self.assertEqual(snap0.git_head_crc, 0)
        self.assertEqual(snap0.terminal_seq, 0)
        self.assertEqual(snap0.active_hwnd, 0)

        # 1. Bump FS
        tracker.bump_fs_epoch()
        self.assertEqual(tracker.snapshot().fs_usn_epoch, 1)
        tracker.bump_fs_epoch(42)
        self.assertEqual(tracker.snapshot().fs_usn_epoch, 42)

        # 2. Update Git head (4-byte Blake2b)
        tracker.update_git_head("refs/heads/main")
        self.assertNotEqual(tracker.snapshot().git_head_crc, 0)
        self.assertIsInstance(tracker.snapshot().git_head_crc, int)

        # 3. Bump terminal
        tracker.bump_terminal_seq()
        self.assertEqual(tracker.snapshot().terminal_seq, 1)

        # 4. Set active HWND
        tracker.set_active_hwnd(123456)
        self.assertEqual(tracker.snapshot().active_hwnd, 123456)

    def test_toctou_invalidation_on_epoch_vector_mutation(self):
        """Verifies modifying any EpochVector dimension invalidates speculative cache."""
        call_count = 0

        @SkillRegistry.register(name="epoch_sensitive_read", read_only=True)
        def epoch_sensitive_read(arg: str):
            nonlocal call_count
            call_count += 1
            return f"Data for {arg}"

        # 1. Speculate
        self.engine.speculate("epoch_sensitive_read", arg="config")
        time.sleep(0.02)

        # 2. Mutate active_hwnd on engine tracker
        self.engine.epoch_tracker.set_active_hwnd(88888)

        # 3. Claim: should invalidate and re-execute synchronously
        result = self.engine.execute_or_claim("epoch_sensitive_read", arg="config")
        self.assertEqual(result, "Data for config")
        self.assertEqual(call_count, 2)
        self.assertGreaterEqual(self.engine.metrics.stale_invalidations, 1)


if __name__ == "__main__":
    unittest.main()
