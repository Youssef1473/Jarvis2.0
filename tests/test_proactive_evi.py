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
)


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


if __name__ == "__main__":
    unittest.main()
