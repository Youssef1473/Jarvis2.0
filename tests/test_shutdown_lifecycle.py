"""Unit and regression tests for comprehensive Jarvis tool shutdown and process termination."""
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, patch
import psutil

from jarvis.core.config_loader import load_config
from jarvis.core.orchestrator import JarvisOrchestrator
from jarvis.core.services import ManagedServiceManager, kill_orphaned_jarvis_processes
from jarvis.core.providers.base import UnifiedRunner
from jarvis.audio.stt import SpeechToText
from jarvis.audio.tts import TextToSpeech
from jarvis.skills.system_watchdog import start_hardware_watchdog, stop_hardware_watchdog, _watchdog_active
from jarvis.skills.timers import start_timer, cancel_timer, _active_timers
from jarvis.core.skills.photo_manager import shutdown_photo_manager


class TestShutdownLifecycle(unittest.TestCase):
    """Verifies that shutting down Jarvis stops every tool, child process, sentinel, and GPU allocation."""

    def setUp(self):
        self.config = load_config()

    def test_shutdown_command_triggers(self):
        """Verifies natural language shutdown, exit, and tool-kill triggers."""
        shutdown_phrases = [
            "exit",
            "quit",
            "goodbye",
            "shut down",
            "power down",
            "power off",
            "go offline",
            "terminate",
            "close jarvis",
            "stop jarvis",
            "shut down every tool",
            "shut down all tools",
            "kill jarvis",
            "kill all tools",
            "turn off",
        ]
        for phrase in shutdown_phrases:
            self.assertTrue(
                JarvisOrchestrator.is_shutdown_command(phrase),
                f"Expected '{phrase}' to trigger shutdown."
            )

        # Ensure regular code instructions do not trigger shutdown
        non_shutdown = [
            "def stop_server(): pass",
            "class ToolRunner: pass",
            "execute python script to stop testing",
            "how do i exit vim",
        ]
        for phrase in non_shutdown:
            self.assertFalse(
                JarvisOrchestrator.is_shutdown_command(phrase),
                f"Expected '{phrase}' NOT to trigger shutdown."
            )

    def test_managed_service_manager_child_sweep(self):
        """Spawns an actual background child process and ensures stop_all terminates it cleanly."""
        sm = ManagedServiceManager()

        # Spawn a dummy child process that sleeps
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pid = proc.pid
        self.assertTrue(psutil.pid_exists(pid))

        # Register with service manager
        sm.register("MockWorker", proc)

        # Trigger stop_all
        sm.stop_all(timeout_seconds=2.0)
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass

        # Verify child process is dead
        time.sleep(0.2)
        self.assertFalse(
            psutil.pid_exists(pid) and psutil.Process(pid).is_running()
            if psutil.pid_exists(pid) else False,
            f"Process PID {pid} was not terminated by ManagedServiceManager.stop_all!"
        )

    def test_sentinels_and_timers_shutdown(self):
        """Ensures watchdog and timers disengage completely during shutdown."""
        # 1. Start timer
        start_timer(duration="10 minutes", label="Unit-Test-Timer")
        self.assertGreater(len(_active_timers), 0)

        # 2. Start hardware watchdog
        start_hardware_watchdog(interval_seconds=60)

        # Cancel timers
        cancel_timer("all")
        self.assertEqual(len(_active_timers), 0)

        # Stop watchdog
        res = stop_hardware_watchdog()
        self.assertIn("disengaged", res.lower())

        # Stop photo manager
        shutdown_photo_manager(timeout=1.0)

    def test_unified_runner_pool_shutdown(self):
        """Verifies UnifiedRunner ThreadPoolExecutor halts without leaking worker threads."""
        runner = UnifiedRunner()
        self.assertFalse(runner._tool_executor._shutdown)
        runner.shutdown(wait=False)
        self.assertTrue(runner._tool_executor._shutdown)

    def test_stt_and_tts_model_unload(self):
        """Verifies STT and TTS stop() methods release model and GPU allocations."""
        stt = SpeechToText(self.config)
        stt.stop()
        self.assertIsNone(stt.model)

        tts = TextToSpeech(self.config)
        tts.stop()
        self.assertFalse(tts.is_speaking)

    def test_kill_orphaned_jarvis_processes_safety(self):
        """Verifies orphan process sweeper runs safely without touching current or IDE processes."""
        current_pid = os.getpid()
        killed = kill_orphaned_jarvis_processes(exclude_pids={current_pid})
        # Should not kill current test process
        self.assertTrue(psutil.pid_exists(current_pid))
        self.assertIsInstance(killed, list)

    def test_brain_shutdown_lifecycle(self):
        """Verifies JarvisBrain.shutdown() halts managed service subprocesses cleanly."""
        from jarvis.core.brain import JarvisBrain
        from jarvis.core.memory import MemoryManager
        from jarvis.core.safety import SafetyGate
        mem = MemoryManager(self.config)
        safety = SafetyGate(self.config)
        brain = JarvisBrain(self.config, mem, safety)
        
        # Test explicit shutdown
        brain.shutdown(timeout_seconds=0.5)
        self.assertEqual(len(brain.service_manager._services), 0)


if __name__ == "__main__":
    unittest.main()
