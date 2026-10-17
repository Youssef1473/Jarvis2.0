"""Unit tests for Phase 4: WinHook SPSC Ring, Habit Distillation & Resumption Bookmarks."""
import time
import unittest

from jarvis.context.flow_state_mirror import VisualPrivacyGuard
from jarvis.conversation.bookmarks import ReasoningBookmarkStack
from jarvis.desktop.uia_delta_worker import UIADeltaWorker, TOTAL_SHM_SIZE, RING_CAPACITY
from jarvis.memory.habit_distiller import AutonomousHabitDistiller


class TestPhase4WinHookAndHabits(unittest.TestCase):
    def setUp(self):
        VisualPrivacyGuard.set_lock(False)
        self.worker = UIADeltaWorker()
        self.raw_buf = bytearray(TOTAL_SHM_SIZE)
        self.worker.attach_buffer(self.raw_buf)
        self.distiller = AutonomousHabitDistiller(repetition_threshold=3)
        self.bookmark_stack = ReasoningBookmarkStack()
        self.bookmark_stack.clear()

    def tearDown(self):
        self.worker.stop()
        VisualPrivacyGuard.set_lock(False)
        self.distiller.reset()
        self.bookmark_stack.clear()

    def test_spsc_ring_buffer_wraparound_and_burst_handling(self):
        """Verifies ring buffer processes 1,500 burst events through the 1,024-slot ring with wrap-around."""
        burst_count = 1500

        # Push burst of 1,500 events
        for i in range(burst_count):
            self.worker.push_frame_for_test(
                hwnd=1000 + i,
                event_id=0x0003,  # EVENT_SYSTEM_FOREGROUND
                title=f"Editor Window {i}",
                privacy=0,
            )
            # Periodic drain simulating worker loop
            if i % 100 == 0:
                self.worker.poll_once()

        # Final drain
        self.worker.poll_once()

        self.assertEqual(self.worker.events_processed, burst_count)
        self.assertEqual(self.worker.active_title, f"Editor Window {burst_count - 1}")

    def test_privacy_lock_atomic_trip(self):
        """Verifies that an event with privacy=1 atomically locks VisualPrivacyGuard."""
        self.assertFalse(VisualPrivacyGuard.is_locked())

        # Push frame with privacy=1 (e.g. Bitwarden window)
        self.worker.push_frame_for_test(
            hwnd=9999,
            event_id=0x0003,
            title="Bitwarden - Master Password",
            privacy=1,
        )

        self.worker.poll_once()

        # VisualPrivacyGuard MUST be locked immediately
        self.assertTrue(VisualPrivacyGuard.is_locked())

    def test_3gram_habit_detection(self):
        """Verifies that executing a workflow triple 3 times produces a DistilledHabitProposal."""
        triplet = ("git_pull", "pip_install", "run_pytest")

        # Repetition 1
        p1 = self.distiller.record_action(triplet[0])
        p2 = self.distiller.record_action(triplet[1])
        p3 = self.distiller.record_action(triplet[2])
        self.assertIsNone(p3)

        # Repetition 2
        self.distiller.record_action(triplet[0])
        self.distiller.record_action(triplet[1])
        p_rep2 = self.distiller.record_action(triplet[2])
        self.assertIsNone(p_rep2)

        # Repetition 3 (threshold reached!)
        self.distiller.record_action(triplet[0])
        self.distiller.record_action(triplet[1])
        proposal = self.distiller.record_action(triplet[2])

        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.occurrence_count, 3)
        self.assertIn("git_pull", proposal.macro_name)
        self.assertIn("run_pytest", proposal.macro_name)
        self.assertIn("git_pull -> pip_install -> run_pytest", proposal.proposal_text)

    def test_barge_in_reasoning_bookmark_restoration(self):
        """Verifies reasoning bookmark stack pushes interrupted turns and enriches pivot turns."""
        turn_id = "turn_1042"
        query = "Refactor the memory manager to use async SQLite"
        plan = "1. Inspect schema. 2. Rewrite connections. 3. Run migrations."
        resolved_outputs = {
            "read_file": "class MemoryManager: pass",
            "git_status": "branch main, clean",
        }

        # 1. User barges in: freeze onto stack
        bookmark = self.bookmark_stack.push_interrupted_turn(
            turn_id=turn_id,
            user_query=query,
            partial_llm_plan=plan,
            resolved_tool_outputs=resolved_outputs,
        )
        self.assertEqual(len(self.bookmark_stack), 1)
        self.assertEqual(bookmark.turn_id, turn_id)

        # 2. Pivot turn: "No, use Option B instead"
        pivot_query = "No, keep synchronous SQLite WAL and just add thread-local connection pool"
        enriched_prompt, restored_tools = self.bookmark_stack.inject_resumed_context(pivot_query)

        self.assertEqual(len(self.bookmark_stack), 0)
        self.assertIn("read_file", restored_tools)
        self.assertIn("git_status", restored_tools)
        self.assertIn("User interrupted prior turn 'turn_1042'", enriched_prompt)
        self.assertIn(pivot_query, enriched_prompt)


if __name__ == "__main__":
    unittest.main()
