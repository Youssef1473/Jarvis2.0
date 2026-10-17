"""Unit tests for the Execution Abort / End Key Kill-Switch in Jarvis 2.0."""
import unittest
from unittest.mock import MagicMock, patch

from jarvis.core.cancellation import (
    GLOBAL_EPOCH_STATE,
    BargeInCancellationToken,
    get_global_cancellation_token,
    signal_global_barge_in,
    signal_execution_abort,
)
from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator


class TestExecutionAbortKillSwitch(unittest.TestCase):
    """Verifies atomic cancellation, tool execution halting, and orchestrator kill-switch behavior."""

    def test_cancellation_token_epoch_invalidation(self):
        """Mints a token, signals abort, and verifies instant token invalidation."""
        token = get_global_cancellation_token()
        self.assertFalse(token.is_cancelled)

        # Trigger execution abort
        signal_execution_abort()
        self.assertTrue(token.is_cancelled)

        with self.assertRaises(RuntimeError) as ctx:
            token.raise_if_cancelled()
        self.assertIn("preempted by voice barge-in", str(ctx.exception))

    def test_tool_orchestrator_halts_on_cancelled_token(self):
        """Verifies ToolOrchestrator immediately halts execution if cancellation token is marked."""
        token = get_global_cancellation_token()
        orchestrator = ToolOrchestrator()
        orchestrator.cancel_token = token

        # Signal abort
        signal_execution_abort()

        with patch("jarvis.skills.base.SkillRegistry.execute") as mock_exec:
            res = orchestrator.execute_step("launch_application", {"app_name": "notepad"})
            self.assertIn("Action cancelled", res)
            self.assertIn("aborted by user interrupt", res)
            mock_exec.assert_not_called()

    def test_brain_execute_tool_raises_when_cancelled(self):
        """Verifies Brain._execute_tool_with_orchestrator raises if cancel token is invalid."""
        from jarvis.core.brain import JarvisBrain

        brain = object.__new__(JarvisBrain)
        brain._active_cancel_token = get_global_cancellation_token()
        brain.tool_orchestrator = None

        # Invalidate token
        signal_execution_abort()

        with self.assertRaises(RuntimeError):
            brain._execute_tool_with_orchestrator("adjust_volume", {"percent": 50})

    def test_orchestrator_abort_active_command(self):
        """Verifies Orchestrator.abort_active_command halts TTS, advances epoch, and resets state."""
        from jarvis.core.orchestrator import JarvisOrchestrator

        orch = object.__new__(JarvisOrchestrator)
        orch.tts = MagicMock()
        orch.tts.is_speaking = True
        orch.hud = MagicMock()
        orch.ui_service = MagicMock()
        orch._current_state = "thinking"
        orch._is_processing_command = True

        init_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)
        was_busy = orch.abort_active_command()

        self.assertTrue(was_busy)
        orch.tts.stop.assert_called_once()
        self.assertEqual(orch.get_state(), "idle")
        self.assertGreater(int(GLOBAL_EPOCH_STATE.barge_in_epoch), init_epoch)

    def test_orchestrator_ptt_press_triggers_killswitch(self):
        """Verifies pressing PTT (End / Fin key) invokes abort_active_command."""
        from jarvis.core.orchestrator import JarvisOrchestrator

        orch = object.__new__(JarvisOrchestrator)
        orch._is_muted = False
        orch.sample_rate = 16000
        orch.config = {"audio": {"ptt_mode": "hold"}}
        orch._is_recording = False
        orch._lock = MagicMock()
        orch._lock.__enter__ = MagicMock()
        orch._lock.__exit__ = MagicMock()
        orch._recorded_frames = []
        orch.hotkey_listener = MagicMock()
        orch.hotkey_listener.mouse_target_btn = None
        orch.sfx = MagicMock()
        orch.hud = MagicMock()
        orch.ui_service = MagicMock()

        with patch.object(orch, "abort_active_command") as mock_abort:
            with patch("sounddevice.InputStream") as mock_sd:
                orch._on_ptt_press()
                mock_abort.assert_called_once()


if __name__ == "__main__":
    unittest.main()
