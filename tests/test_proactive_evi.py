"""Automated tests for Cognitive Twin Engine and Expected Value of Interruption (EVI) routing."""
import time
import unittest
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.cognition.proactive_evi import (
    CognitiveState,
    DeliveryModality,
    TelemetrySlice,
    CognitiveTwinEngine,
    PrewarmedContextEntry,
)
from jarvis.speculative.engine import EpochVector


class TestProactiveEVISuite(unittest.TestCase):
    """Verifies cognitive state inference, fatigue decay, EVI modality routing, and telemetry envelope."""

    def setUp(self):
        self.engine = CognitiveTwinEngine()

    def test_infer_deep_flow_state(self):
        """Verifies high IDE focus and typing cadence with low window switching infers DEEP_FLOW."""
        t = TelemetrySlice(
            kpm=185.0,
            backspace_ratio=0.05,
            window_switches_per_min=1.2,
            ide_focus_ratio=0.92,
            active_debug_signals=0,
            continuous_session_mins=45.0,
        )
        state = self.engine.infer_state(t)
        self.assertEqual(state, CognitiveState.DEEP_FLOW)

    def test_infer_active_debugging_state(self):
        """Verifies active debug signals and moderate IDE focus infers ACTIVE_DEBUGGING."""
        t = TelemetrySlice(
            kpm=95.0,
            backspace_ratio=0.14,
            window_switches_per_min=6.5,
            ide_focus_ratio=0.65,
            active_debug_signals=2,
            continuous_session_mins=30.0,
        )
        state = self.engine.infer_state(t)
        self.assertEqual(state, CognitiveState.ACTIVE_DEBUGGING)

    def test_infer_fatigued_drift_state(self):
        """Verifies long session uptime and high backspace ratio infers FATIGUED_DRIFT."""
        t = TelemetrySlice(
            kpm=45.0,
            backspace_ratio=0.22,
            window_switches_per_min=14.0,
            ide_focus_ratio=0.30,
            active_debug_signals=0,
            continuous_session_mins=150.0,
        )
        state = self.engine.infer_state(t)
        self.assertEqual(state, CognitiveState.FATIGUED_DRIFT)

    def test_evi_suppresses_low_utility_during_deep_flow(self):
        """Verifies low-utility alerts are suppressed or routed to HUD rather than speaking in DEEP_FLOW."""
        t = TelemetrySlice(
            kpm=190.0,
            backspace_ratio=0.04,
            window_switches_per_min=1.0,
            ide_focus_ratio=0.95,
        )
        self.engine.infer_state(t)

        # Low utility alert (e.g. routine tab cleanup suggestion)
        modality, score = self.engine.evaluate_proactive_action(
            intrinsic_utility=0.20,
            context_relevance=0.30,
            urgency=0.10,
        )
        self.assertEqual(modality, DeliveryModality.SUPPRESS)

    def test_evi_allows_voice_for_critical_alerts(self):
        """Verifies high-utility urgent alerts (e.g. battery dying or server crash) trigger VOICE."""
        t = TelemetrySlice(kpm=100.0, backspace_ratio=0.08, window_switches_per_min=2.0, ide_focus_ratio=0.5)
        self.engine.infer_state(t)

        # Critical alert: utility=0.95, relevance=1.0, urgency=0.95
        modality, score = self.engine.evaluate_proactive_action(
            intrinsic_utility=0.95,
            context_relevance=1.0,
            urgency=0.95,
        )
        self.assertEqual(modality, DeliveryModality.VOICE)
        self.assertGreaterEqual(score, 0.80)

    def test_persona_telemetry_envelope_generation(self):
        """Verifies build_persona_telemetry_envelope outputs valid XML structure with directive."""
        t = TelemetrySlice(
            kpm=180.0,
            backspace_ratio=0.04,
            window_switches_per_min=1.5,
            ide_focus_ratio=0.90,
            continuous_session_mins=40.0,
        )
        self.engine.infer_state(t)
        envelope = self.engine.build_persona_telemetry_envelope(t)

        self.assertIn('<live_telemetry_envelope state="DEEP_FLOW"', envelope)
        self.assertIn("Directive: Sir is in deep engineering flow.", envelope)

    def test_speculative_prewarm_option_a_gating(self):
        """Verifies evaluate_speculative_prewarm approves silent pre-warming when utility exceeds threshold."""
        # Standard routine state
        self.engine.state = CognitiveState.ROUTINE_ADMIN
        v = EpochVector(fs_usn_epoch=1, git_head_crc=100, terminal_seq=2, active_hwnd=300)

        # High utility & relevance -> approved for silent pre-warming
        approved, score = self.engine.evaluate_speculative_prewarm(
            utility=0.8,
            relevance=0.8,
            epoch_vector=v,
            current_vector=v,
        )
        self.assertTrue(approved)
        self.assertGreaterEqual(score, 0.25)
        self.assertTrue(self.engine.should_prewarm_context(utility=0.8, relevance=0.8, epoch_vector=v, current_vector=v))

    def test_speculative_prewarm_invalidation_on_epoch_mismatch(self):
        """Verifies evaluate_speculative_prewarm disapproves when EpochVector is stale/incompatible."""
        v_cached = EpochVector(fs_usn_epoch=1, git_head_crc=100, terminal_seq=2, active_hwnd=300)
        v_current = EpochVector(fs_usn_epoch=2, git_head_crc=100, terminal_seq=2, active_hwnd=300)  # FS modified!

        approved, score = self.engine.evaluate_speculative_prewarm(
            utility=0.9,
            relevance=0.9,
            epoch_vector=v_cached,
            current_vector=v_current,
        )
        self.assertFalse(approved)
        self.assertEqual(score, 0.0)
        self.assertFalse(self.engine.should_prewarm_context(epoch_vector=v_cached, current_vector=v_current))

    def test_prewarmed_context_entry_validation(self):
        """Verifies PrewarmedContextEntry.is_valid detects TOCTOU epoch drift."""
        v1 = EpochVector(fs_usn_epoch=5, git_head_crc=99, terminal_seq=1, active_hwnd=500)
        entry = PrewarmedContextEntry(
            context_id="ast_summary:main.py",
            data={"symbols": ["run", "shutdown"]},
            epoch_vector=v1,
        )

        v_same = EpochVector(fs_usn_epoch=5, git_head_crc=99, terminal_seq=1, active_hwnd=500)
        v_changed = EpochVector(fs_usn_epoch=5, git_head_crc=99, terminal_seq=2, active_hwnd=500)

        self.assertTrue(entry.is_valid(v_same))
        self.assertFalse(entry.is_valid(v_changed))


if __name__ == "__main__":
    unittest.main()
