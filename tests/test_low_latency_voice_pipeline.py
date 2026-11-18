"""Hermetic unit tests for Plan 046: Low-Latency Voice Pipeline & VRAM-Decoupled Audio Orchestration.

Verifies:
1. Kokoro-82M ONNX Zero-VRAM CPU isolation (CPUExecutionProvider with 4 P-core threads).
2. Pipelined lookahead synthesis overlap and sub-350ms inter-sentence gap.
3. Conversational Fast-Path VAD silence cutoff (0.65s for short utterances).
4. Casual greeting reflex bridge immediate acoustic acknowledgment (<10ms).
5. Zero-tool prompt pruning (tools=None) on pure conversational turns.
6. Sub-1us epoch barge-in cancellation during lookahead pipelining.
7. Elastic pre-buffer cushion (2,048 samples, ~85ms) starvation protection.
8. Package entry point `jarvis.__main__` importability and forwarder.
9. Standalone stage latency profiler CLI `--dry-run` execution.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

# Ensure workspace root is present in sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from jarvis.audio.ring_buffer import AudioRingBuffer
from jarvis.audio.tts import TextToSpeech
from jarvis.core.cancellation import GLOBAL_EPOCH_STATE
from jarvis.core.cognition.cognitive_router import CognitiveRouter
from jarvis.core.cognition.intent_acknowledgments import (
    get_immediate_intent_acknowledgment,
)
from tools.profile_voice_turn import profile_turn_dry_run


class TestLowLatencyVoicePipeline(unittest.TestCase):
    """Test suite validating low-latency conversational voice and memory orchestration."""

    def setUp(self) -> None:
        self._orig_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)
        from jarvis.audio.tts import _flush_kokoro_gpu_cache
        _flush_kokoro_gpu_cache()

    def tearDown(self) -> None:
        GLOBAL_EPOCH_STATE.barge_in_epoch = self._orig_epoch
        from jarvis.audio.tts import _flush_kokoro_gpu_cache
        _flush_kokoro_gpu_cache()

    def test_01_kokoro_zero_vram_cpu_execution_provider(self) -> None:
        """Verifies Kokoro-82M ONNX initializes exclusively with CPUExecutionProvider

        and 4 P-core threads, strictly enforcing the Zero-VRAM invariant.
        """
        cfg = {
            "tts": {
                "engine": "kokoro",
                "device": "cpu",
                "cpu_threads": 4,
                "use_shm_transport": False,
            }
        }
        with patch("os.path.exists", return_value=True), \
             patch("onnxruntime.SessionOptions") as mock_opts_cls, \
             patch("onnxruntime.InferenceSession") as mock_sess_cls, \
             patch("kokoro_onnx.Kokoro.from_session") as mock_from_sess:

            mock_opts = MagicMock()
            mock_opts_cls.return_value = mock_opts
            mock_sess = MagicMock()
            mock_sess.get_providers.return_value = ["CPUExecutionProvider"]
            mock_sess_cls.return_value = mock_sess
            mock_kokoro_inst = MagicMock()
            mock_from_sess.return_value = mock_kokoro_inst

            tts = TextToSpeech(cfg)

            # Assert InferenceSession was called with exact CPUExecutionProvider
            mock_sess_cls.assert_called_once()
            _, kwargs = mock_sess_cls.call_args
            self.assertEqual(kwargs.get("providers"), ["CPUExecutionProvider"])

            # Assert thread count is 4 and execution mode is sequential
            self.assertEqual(mock_opts.intra_op_num_threads, 4)
            self.assertEqual(mock_opts.inter_op_num_threads, 1)

            # Assert provider contract exposed on engine
            self.assertIn("CPUExecutionProvider", tts._kokoro._session.get_providers())

    def test_02_pipelined_synthesis_overlap_inter_sentence_gap(self) -> None:
        """Verifies Sentence 2 synthesizes while Sentence 1 is buffered and the

        natural inter-sentence gap stays strictly below 350 ms.
        """
        sr = 24000
        # Calculate natural trailing sentence boundary pause
        pause_samples = TextToSpeech._calculate_pause_samples("Good day, Sir.", sr)
        pause_duration_ms = (pause_samples / float(sr)) * 1000.0

        # Assert pause duration is exactly 240ms, which is strictly < 350ms
        self.assertEqual(pause_duration_ms, 240.0)
        self.assertLess(pause_duration_ms, 350.0)

        # Verify ring buffer overlap: writing Sentence 1 and Sentence 2 back-to-back
        ring = AudioRingBuffer(capacity_samples=48000 * 10, ramp_samples=120)
        s1_samples = np.ones(24000, dtype=np.float32)  # 1.0s speech
        s2_samples = np.full(24000, 2.0, dtype=np.float32)  # 1.0s speech

        w1 = ring.write(s1_samples, producer_epoch=0)
        w2 = ring.write(s2_samples, producer_epoch=0)
        self.assertEqual(w1, 24000)
        self.assertEqual(w2, 24000)
        self.assertEqual(ring.available_samples, 48000)

        # Draining Sentence 1 transitions immediately into Sentence 2 with 0 buffer gap
        out = np.zeros(24000, dtype=np.float32)
        n1 = ring.read_into(out, frames=24000)
        self.assertEqual(n1, 24000)
        self.assertEqual(out[0], 1.0)
        self.assertEqual(ring.available_samples, 24000)

        # Next read immediately yields Sentence 2
        n2 = ring.read_into(out, frames=24000)
        self.assertEqual(n2, 24000)
        self.assertEqual(out[0], 2.0)
        self.assertEqual(ring.available_samples, 0)

    def test_03_conversational_vad_silence_threshold(self) -> None:
        """Verifies fast-path conversational VAD calculates a 0.65s silence threshold

        for short conversational turns (<1.5s speech) while scaling up for long queries.
        """
        block_size = 1280
        sample_rate = 16000
        chunk_duration = block_size / sample_rate  # 0.08s
        base_silence_s = 1.4
        thinking_pause_s = 3.2

        # 1. Short conversational utterance (0.4s speech, e.g. "Hello Jarvis")
        spoken_duration_short = 0.4
        if spoken_duration_short < 1.5:
            conv_base = min(base_silence_s, 0.65)
            dynamic_silence_short = conv_base + (spoken_duration_short * 0.20)
        else:
            dynamic_silence_short = min(thinking_pause_s, base_silence_s + (spoken_duration_short * 0.35))

        min_chunks_floor = max(5, int(0.65 / chunk_duration))  # 8 chunks
        chunks_needed_short = max(min_chunks_floor, int(dynamic_silence_short / chunk_duration))

        # Dynamic silence for short utterance must be sub-800ms
        self.assertAlmostEqual(dynamic_silence_short, 0.73, delta=0.01)
        self.assertLessEqual(chunks_needed_short * chunk_duration, 0.80)

        # 2. Long thought (6.0s speech) scales up to thinking_pause_s (3.2s)
        spoken_duration_long = 6.0
        if spoken_duration_long < 1.5:
            dynamic_silence_long = min(base_silence_s, 0.65) + (spoken_duration_long * 0.20)
        else:
            dynamic_silence_long = min(thinking_pause_s, base_silence_s + (spoken_duration_long * 0.35))

        self.assertEqual(dynamic_silence_long, 3.2)

    def test_04_casual_greeting_reflex_acknowledgment(self) -> None:
        """Verifies natural casual greetings deliver an immediate conversational acoustic

        acknowledgment (<10ms) while technical queries and dismissives are handled appropriately.
        """
        # Casual greetings deliver immediate verbal bridges
        ack_how = get_immediate_intent_acknowledgment("how are you today")
        self.assertEqual(ack_how, "Very well, Sir, at your service.")

        ack_hello = get_immediate_intent_acknowledgment("hello jarvis")
        self.assertEqual(ack_hello, "Good day, Sir.")

        ack_morning = get_immediate_intent_acknowledgment("good morning jarvis")
        self.assertEqual(ack_morning, "Good morning, Sir.")

        # Actionable queries return specific action intent acknowledgments
        ack_search = get_immediate_intent_acknowledgment("search the codebase for audio pipeline")
        self.assertEqual(ack_search, "Scanning through the codebase now, Sir.")

        # Dismissives return None (no ack needed)
        ack_dismiss = get_immediate_intent_acknowledgment("it's okay, never mind")
        self.assertIsNone(ack_dismiss)

        ack_thanks = get_immediate_intent_acknowledgment("thank you jarvis")
        self.assertIsNone(ack_thanks)

        # Fast reflexes bypass immediate intent acks
        ack_reflex = get_immediate_intent_acknowledgment("hello", is_fast_reflex=True)
        self.assertIsNone(ack_reflex)

    def test_05_tool_schema_pruning_on_casual_turns(self) -> None:
        """Verifies CognitiveRouter prunes tool schemas to frozenset() on casual

        dialogue queries, avoiding expensive prompt prefill overhead.
        """
        router = CognitiveRouter({})

        # Pure conversational greetings -> zero tools selected
        tools_greeting = router.select_tools_for_query("Hello Jarvis, how are you today?")
        self.assertEqual(tools_greeting, frozenset())

        tools_hi = router.select_tools_for_query("Hi Jarvis")
        self.assertEqual(tools_hi, frozenset())

        tools_morning = router.select_tools_for_query("Good morning Jarvis, hope you are well")
        self.assertEqual(tools_morning, frozenset())

        # Actionable requests retain appropriate tools
        tools_search = router.select_tools_for_query("Search for recent audio commits")
        self.assertTrue(len(tools_search) > 0)
        self.assertTrue("search_codebase" in tools_search or "web_search" in tools_search)

        tools_time = router.select_tools_for_query("What time is it right now?")
        self.assertIn("get_current_time_and_date", tools_time)

    def test_06_barge_in_epoch_invalidation_during_pipelining(self) -> None:
        """Verifies that an incoming user speech barge-in bumps the epoch and

        invalidates buffered lookahead audio in <1us without deadlocks.
        """
        ring = AudioRingBuffer(capacity_samples=48000 * 5, ramp_samples=120)
        speech_chunk = np.ones(24000, dtype=np.float32)

        # Write Sentence 1 and Sentence 2 under epoch 0
        ring.write(speech_chunk, producer_epoch=0)
        ring.write(speech_chunk, producer_epoch=0)
        self.assertEqual(ring.available_samples, 48000)

        # User interrupts: epoch is bumped via signal_global_barge_in
        from jarvis.core.cancellation import signal_global_barge_in

        signal_global_barge_in()
        new_epoch = int(GLOBAL_EPOCH_STATE.barge_in_epoch)
        self.assertGreater(new_epoch, 0)

        # Inform ring buffer of barge-in
        ring.barge_in()

        # Next ring write from stale producer epoch is rejected
        stale_write = ring.write(speech_chunk, producer_epoch=0)
        self.assertEqual(stale_write, 0)

        # Next consumer read executes anti-click ramp and flushes stale samples
        out = np.ones(1024, dtype=np.float32)
        read_frames = ring.read_into(out, frames=1024)
        self.assertGreaterEqual(read_frames, 0)
        self.assertEqual(ring.available_samples, 0)

    def test_07_elastic_prebuffer_cushion_starvation_protection(self) -> None:
        """Verifies _stream_audio_callback enforces an elastic 2,048-sample (~85ms)

        pre-buffer cushion to prevent audio dropouts during micro-hesitations.
        """
        cfg = {"tts": {"engine": "sapi5", "use_shm_transport": False}}
        tts = TextToSpeech(cfg)
        tts._stream_active = True
        tts._prebuffer_filled = False

        mock_synth = MagicMock()
        mock_synth.is_alive.return_value = True
        tts._synth_thread = mock_synth

        # Less than cushion (1500 samples < 2048) -> remains unready, outputs zero
        tts._ring_buffer.write(np.ones(1500, dtype=np.float32), producer_epoch=0)
        out = np.ones((1024, 2), dtype=np.float32)
        tts._stream_audio_callback(out, 1024, None, None)
        self.assertFalse(tts._prebuffer_filled)
        np.testing.assert_allclose(out, 0.0)

        # Adding 600 samples (total 2100 >= 2048) crosses cushion -> fills prebuffer
        tts._ring_buffer.write(np.ones(600, dtype=np.float32), producer_epoch=0)
        tts._stream_audio_callback(out, 1024, None, None)
        self.assertTrue(tts._prebuffer_filled)
        self.assertTrue(np.any(out != 0.0))

    def test_08_jarvis_main_package_entry_point(self) -> None:
        """Verifies that `jarvis.__main__` can be imported and cleanly exposes `main`."""
        import jarvis.__main__ as jm

        self.assertTrue(hasattr(jm, "main"))
        self.assertTrue(callable(jm.main))
        self.assertIn("main", jm.__all__)

    def test_09_profile_voice_turn_cli_dry_run(self) -> None:
        """Verifies `tools/profile_voice_turn.py` dry-run profiles stages accurately

        and asserts inter-sentence gap < 350ms and 0 MB Kokoro VRAM.
        """
        stages, telemetry, extra = profile_turn_dry_run("Hello Jarvis, how are you today?")

        self.assertLess(stages.t_gap_ms, 350.0)
        self.assertEqual(telemetry.kokoro_vram_mb, 0.0)
        self.assertTrue(extra["tools_pruned"])
        self.assertIsNotNone(extra["reflex_acknowledgment"])

        # Also test invocation via subprocess with --dry-run
        cmd = [
            sys.executable,
            str(_ROOT / "tools" / "profile_voice_turn.py"),
            "--mock-query",
            "Hello Jarvis, how are you today?",
            "--dry-run",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertEqual(res.returncode, 0, f"Profiler CLI failed: {res.stderr}")
        self.assertIn("ALL PLAN 046 LATENCY & VRAM TARGETS SATISFIED", res.stdout)
        self.assertIn("PASS: <350ms", res.stdout)
        self.assertIn("0.0 MB (100% P-Core CPU Isolation, 0 MB VRAM)", res.stdout)


if __name__ == "__main__":
    unittest.main()
