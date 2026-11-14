"""Integration tests for Antigravity PreInvocation Hook lifecycle (ENG-OS-02 Phase 2C).

Verifies:
- .agents/hooks.json schema and PreInvocation hook declaration.
- Command resolution from both workspace root and .agents/ directory.
- Fail-open behavior under malformed inputs, missing fields, or empty stdin.
- Stderr diagnostic isolation (stdout strictly reserved for pure PreInvocation JSON).
- Invocation cycle deduplication (invocationNum > 0 returns empty injectSteps).
- Performance: core vs process startup vs end-to-end hook execution.
"""
from __future__ import annotations

import json
import subprocess
import time
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON_PATH = WORKSPACE_ROOT / ".agents" / "hooks.json"


class TestHookConfiguration(unittest.TestCase):
    """Verifies .agents/hooks.json structure and non-regression of existing hooks."""

    def test_hooks_json_valid_and_intact(self) -> None:
        self.assertTrue(HOOKS_JSON_PATH.exists(), f"Missing {HOOKS_JSON_PATH}")
        data = json.loads(HOOKS_JSON_PATH.read_text(encoding="utf-8"))

        # Verify existing hooks are preserved
        self.assertIn("safety-watchdog", data)
        self.assertIn("python-lint-diagnostic", data)
        self.assertIn("context-router", data)

        cr = data["context-router"]
        self.assertIn("PreInvocation", cr)
        self.assertIsInstance(cr["PreInvocation"], list)
        self.assertEqual(len(cr["PreInvocation"]), 1)

        handler = cr["PreInvocation"][0]
        self.assertEqual(handler.get("type"), "command")
        self.assertIn("context_router.py", handler.get("command", ""))
        self.assertEqual(handler.get("timeout"), 5)


class TestHookProcessExecution(unittest.TestCase):
    """Verifies end-to-end execution of the hook command as spawned by Antigravity."""

    def _execute_hook_process(self, stdin_payload: str, cwd: Path) -> tuple[int, str, str]:
        cmd = [
            "cmd.exe",
            "/c",
            "if exist tools\\context_router.py (python tools\\context_router.py) else (python ..\\tools\\context_router.py)",
        ]
        proc = subprocess.run(
            cmd,
            check=False,
            input=stdin_payload,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=5,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_execution_from_workspace_root(self) -> None:
        payload = json.dumps({
            "lastUserInput": "Refactor WASAPI audio playback thread",
            "invocationNum": 0,
        })
        ret, stdout, stderr = self._execute_hook_process(payload, cwd=WORKSPACE_ROOT)
        self.assertEqual(ret, 0)
        parsed = json.loads(stdout)
        self.assertIn("injectSteps", parsed)
        self.assertEqual(len(parsed["injectSteps"]), 1)
        self.assertIn("AUDIO_THREADING", parsed["injectSteps"][0]["ephemeralMessage"])
        self.assertIn("[ContextRouter] Invocation #0", stderr)

    def test_execution_from_agents_directory(self) -> None:
        payload = json.dumps({
            "lastUserInput": "Refactor SQLite database SingleWriterActor",
            "invocationNum": 0,
        })
        agents_dir = WORKSPACE_ROOT / ".agents"
        ret, stdout, _stderr = self._execute_hook_process(payload, cwd=agents_dir)
        self.assertEqual(ret, 0)
        parsed = json.loads(stdout)
        self.assertIn("injectSteps", parsed)
        self.assertIn("SQLITE_PERSISTENCE", parsed["injectSteps"][0]["ephemeralMessage"])

    def test_fail_open_on_empty_stdin(self) -> None:
        ret, stdout, _stderr = self._execute_hook_process("", cwd=WORKSPACE_ROOT)
        self.assertEqual(ret, 0)
        parsed = json.loads(stdout)
        self.assertIn("injectSteps", parsed)
        # Empty input produces fallback packet with UNKNOWN category
        self.assertIn("UNKNOWN", parsed["injectSteps"][0]["ephemeralMessage"])

    def test_fail_open_on_malformed_json(self) -> None:
        ret, stdout, _stderr = self._execute_hook_process("{not valid json", cwd=WORKSPACE_ROOT)
        self.assertEqual(ret, 0)
        parsed = json.loads(stdout)
        self.assertIn("injectSteps", parsed)
        self.assertIn("UNKNOWN", parsed["injectSteps"][0]["ephemeralMessage"])

    def test_deduplication_on_subsequent_invocations(self) -> None:
        payload = json.dumps({
            "lastUserInput": "Refactor WASAPI audio playback thread",
            "invocationNum": 1,
        })
        ret, stdout, stderr = self._execute_hook_process(payload, cwd=WORKSPACE_ROOT)
        self.assertEqual(ret, 0)
        parsed = json.loads(stdout)
        self.assertEqual(parsed, {"injectSteps": []})
        self.assertIn("duplicate injection suppressed", stderr)


class TestHookPerformanceBreakdown(unittest.TestCase):
    """Measures process startup, core router, and end-to-end hook latency."""

    def test_performance_breakdown(self) -> None:
        from tools.context_router import build_context_packet, serialize_hook_response

        prompt = "Refactor WASAPI audio playback thread and handle Kokoro neural TTS synthesis"
        stdin_payload = json.dumps({"lastUserInput": prompt, "invocationNum": 0})

        # 1. Core Python timing
        core_times = []
        for _ in range(50):
            t0 = time.perf_counter_ns()
            packet = build_context_packet(prompt)
            _ = serialize_hook_response(packet)
            t1 = time.perf_counter_ns()
            core_times.append((t1 - t0) / 1_000_000.0)

        # 2. End-to-end process invocation timing (cmd /c -> python)
        proc_times = []
        cmd = [
            "cmd.exe",
            "/c",
            "if exist tools\\context_router.py (python tools\\context_router.py) else (python ..\\tools\\context_router.py)",
        ]
        for _ in range(10):
            t0 = time.perf_counter_ns()
            p = subprocess.run(
                cmd,
                check=False,
                input=stdin_payload,
                capture_output=True,
                text=True,
                cwd=str(WORKSPACE_ROOT),
                timeout=5,
            )
            t1 = time.perf_counter_ns()
            self.assertEqual(p.returncode, 0)
            proc_times.append((t1 - t0) / 1_000_000.0)

        median_core = sorted(core_times)[len(core_times) // 2]
        median_proc = sorted(proc_times)[len(proc_times) // 2]
        startup_overhead = median_proc - median_core
        self.assertGreaterEqual(startup_overhead, -5.0)

        # Ensure process startup completes well within 5-second timeout (typically <150ms)
        self.assertLess(median_proc, 1000.0, f"Hook process latency too high: {median_proc:.2f}ms")


if __name__ == "__main__":
    unittest.main()
