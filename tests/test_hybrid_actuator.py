"""Unit tests for Hybrid CDP AXTree + BoxModel Actuator skill."""
import json
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.skills.base import SkillRegistry
from jarvis.skills.hybrid_actuator import (
    SemanticTarget,
    send_cdp_command,
    locate_semantic_target,
    actuate_with_verification,
    click_web_element_semantic,
    type_into_web_element_semantic,
)


class TestHybridActuatorSuite(unittest.TestCase):
    """Verifies AXTree element resolution, box-model centroid math, and closed-loop actuation."""

    def test_skills_registered(self):
        """Verifies semantic web interaction skills are registered in SkillRegistry."""
        tools = SkillRegistry.get_all_tools()
        self.assertIn("click_web_element_semantic", tools)
        self.assertIn("type_into_web_element_semantic", tools)

    def test_cdp_unavailable_returns_courteous_notice(self):
        """Verifies courteous error notice when port 9222 is unavailable."""
        with patch("jarvis.skills.hybrid_actuator.is_cdp_available", return_value=False):
            res_click = click_web_element_semantic("Submit Button")
            self.assertIn("DevTools Protocol is not available", res_click)

            res_type = type_into_web_element_semantic("Search", "Jarvis")
            self.assertIn("DevTools Protocol is not available", res_type)

    def test_locate_semantic_target_centroid_calculation(self):
        """Verifies resolving element from Accessibility Tree and calculating exact centroid from box model."""
        fake_ax_tree = {
            "nodes": [
                {
                    "nodeId": "1",
                    "ignored": False,
                    "role": {"value": "button"},
                    "name": {"value": "Save Document"},
                    "backendDOMNodeId": 42,
                },
                {
                    "nodeId": "2",
                    "ignored": False,
                    "role": {"value": "canvas"},
                    "name": {"value": "Figma Canvas Viewport"},
                    "backendDOMNodeId": 99,
                },
            ]
        }
        # Box model border quad: [x1, y1, x2, y2, x3, y3, x4, y4]
        # (100, 200) to (300, 250) -> centroid (200.0, 225.0)
        fake_box_model = {
            "model": {
                "border": [100.0, 200.0, 300.0, 200.0, 300.0, 250.0, 100.0, 250.0]
            }
        }

        mock_ws = MagicMock()
        mock_ws.recv.side_effect = [
            json.dumps({"id": 101, "result": fake_ax_tree}),
            json.dumps({"id": 102, "result": fake_box_model}),
        ]

        target = locate_semantic_target(mock_ws, "Save Document")
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "Save Document")
        self.assertEqual(target.role, "button")
        self.assertEqual(target.center_x, 200.0)
        self.assertEqual(target.center_y, 225.0)
        self.assertFalse(target.is_canvas_opaque)

    def test_locate_semantic_target_opaque_canvas_detection(self):
        """Verifies opaque <canvas> surfaces are identified for visual anchor fallback."""
        fake_ax_tree = {
            "nodes": [
                {
                    "nodeId": "2",
                    "ignored": False,
                    "role": {"value": "canvas"},
                    "name": {"value": "Figma Canvas Viewport"},
                    "backendDOMNodeId": 99,
                }
            ]
        }
        fake_box_model = {
            "model": {
                "border": [0.0, 0.0, 800.0, 0.0, 800.0, 600.0, 0.0, 600.0]
            }
        }

        mock_ws = MagicMock()
        mock_ws.recv.side_effect = [
            json.dumps({"id": 101, "result": fake_ax_tree}),
            json.dumps({"id": 102, "result": fake_box_model}),
        ]

        target = locate_semantic_target(mock_ws, "Figma Canvas")
        self.assertIsNotNone(target)
        self.assertTrue(target.is_canvas_opaque)

    def test_actuate_with_verification_click_and_type(self):
        """Verifies closed-loop execution: pre-snapshot, mouse click, text insertion, and post-check."""
        target = SemanticTarget(
            node_id=1,
            backend_node_id=42,
            role="textbox",
            name="Username Input",
            center_x=150.0,
            center_y=80.0,
        )

        mock_ws = MagicMock()
        # Mock responses: 1) Pre-eval, 2) Mouse press, 3) Mouse release, 4) Insert text, 5) Post-eval
        mock_ws.recv.side_effect = [
            json.dumps({"id": 201, "result": {"result": {"value": "<body active>"}}}),
            json.dumps({"id": 202, "result": {}}),
            json.dumps({"id": 203, "result": {}}),
            json.dumps({"id": 204, "result": {}}),
            json.dumps({"id": 210, "result": {"result": {"value": "<input id='username' active>"}}}),
        ]

        report = actuate_with_verification(mock_ws, target, text_to_type="admin", click=True)
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["target"], "Username Input")
        self.assertIn("username", report["active_element"])


if __name__ == "__main__":
    unittest.main()
