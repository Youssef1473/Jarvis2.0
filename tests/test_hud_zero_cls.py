"""Unit tests for HUD Zero-CLS Invariants and Compositor-Accelerated Telemetry.

Validates that jarvis/ui/templates/hud.html adheres to the Council's Frontend & UX
Performance Directive:
1. Zero Cumulative Layout Shift (CLS = 0)
2. GPU compositor-accelerated indicator transforms (scaleX)
3. Zero runtime mutations of width/height/margin on telemetry gauges
4. Tabular figures and invariant container heights
"""
import re
import unittest
from pathlib import Path


class TestHudZeroClsInvariants(unittest.TestCase):
    """Verifies that hud.html strictly satisfies Zero-CLS and hardware acceleration rules."""

    @classmethod
    def setUpClass(cls):
        hud_path = Path(__file__).resolve().parent.parent / "jarvis" / "ui" / "templates" / "hud.html"
        cls.assertTrue(cls, hud_path.exists(), f"hud.html must exist at {hud_path}")
        cls.hud_content = hud_path.read_text(encoding="utf-8")

    def test_meter_bar_css_invariants(self):
        """Meter bars must have transform: scaleX(0), origin-left, and will-change: transform."""
        self.assertIn("transform: scaleX(0);", self.hud_content)
        self.assertIn("transform-origin: left;", self.hud_content)
        self.assertIn("will-change: transform;", self.hud_content)

    def test_gauge_elements_have_reflow_guards(self):
        """Gauge meter DOM elements must declare origin-left and will-change-transform classes."""
        for meter_id in ["cpuMeter", "ramMeter", "batteryMeter"]:
            pattern = rf'id="{meter_id}"[^>]*class="[^"]*origin-left[^"]*will-change-transform[^"]*"|class="[^"]*origin-left[^"]*will-change-transform[^"]*"[^>]*id="{meter_id}"'
            self.assertRegex(
                self.hud_content,
                pattern,
                f"Element #{meter_id} must include origin-left and will-change-transform classes"
            )

    def test_telemetry_values_use_tabular_nums(self):
        """Telemetry figures must use tabular-nums and inline-block for layout shift prevention."""
        for val_id in ["cpuVal", "batteryVal"]:
            pattern = rf'id="{val_id}"[^>]*class="[^"]*tabular-nums[^"]*inline-block[^"]*"|class="[^"]*tabular-nums[^"]*inline-block[^"]*"[^>]*id="{val_id}"'
            self.assertRegex(
                self.hud_content,
                pattern,
                f"Element #{val_id} must have tabular-nums and inline-block classes"
            )

    def test_no_meter_style_width_mutation_in_js(self):
        """JavaScript must not mutate meter.style.width directly (banned for CLS prevention)."""
        # Find script block
        script_matches = re.findall(r"<script>(.*?)</script>", self.hud_content, re.DOTALL)
        self.assertTrue(len(script_matches) > 0, "Script block must be present in hud.html")
        script_body = script_matches[-1]

        # Ensure no meter style.width assignments exist
        bad_patterns = [
            r"cpuMeter\.style\.width\s*=",
            r"ramMeter\.style\.width\s*=",
            r"batteryMeter\.style\.width\s*=",
        ]
        for pat in bad_patterns:
            self.assertNotRegex(
                script_body,
                pat,
                f"Banned layout-shifting mutation detected: {pat}"
            )

    def test_hardware_accelerated_scalex_transforms_in_js(self):
        """Meters must be updated using GPU transform scaleX in renderTelemetryFrame."""
        self.assertIn("domRefs.cpuMeter.style.transform = `scaleX(${ratio})`", self.hud_content)
        self.assertIn("domRefs.ramMeter.style.transform = `scaleX(${ratio})`", self.hud_content)
        self.assertIn("domRefs.batteryMeter.style.transform = `scaleX(${ratio})`", self.hud_content)

    def test_pre_cached_dom_references_and_raf_batching(self):
        """DOM references must be pre-cached and frames batched with requestAnimationFrame."""
        self.assertIn("function initDomReferences()", self.hud_content)
        self.assertIn("requestAnimationFrame(renderTelemetryFrame)", self.hud_content)


if __name__ == "__main__":
    unittest.main()
