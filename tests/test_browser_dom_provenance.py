"""Unit and integration tests for Browser DOM Provenance Tracking in Jarvis 2.0.

Verifies:
1. click_web_element_semantic taints a clean turn when returning externally controlled DOM accessible text.
2. type_into_web_element_semantic taints a clean turn when returning externally controlled DOM accessible text.
3. Taint is established before the tool result is delivered to the LLM.
4. Subsequent Level 1 privileged mutations encounter Dual-Key authorization gates.
5. Exceptions and unsuccessful DOM lookups do not expose unverified DOM text and leave the turn untainted.
6. Opaque canvas element inspections establish taint before returning canvas element names.
"""
import json
import unittest
from unittest.mock import MagicMock, patch

from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills.hybrid_actuator import (
    click_web_element_semantic,
    type_into_web_element_semantic,
)


def _make_mock_ws(element_name: str = "Test Button", role: str = "button", is_canvas: bool = False):
    """Creates a deterministic mock websocket for CDP interaction."""
    fake_ax_tree = {
        "nodes": [
            {
                "nodeId": "1",
                "ignored": False,
                "role": {"value": role},
                "name": {"value": element_name},
                "backendDOMNodeId": 42,
            }
        ]
    }
    fake_box_model = {
        "model": {
            "border": [100.0, 100.0, 200.0, 100.0, 200.0, 150.0, 100.0, 150.0]
        }
    }
    mock_ws = MagicMock()
    responses = [
        # 1. locate_semantic_target: Accessibility.getFullAXTree (cmd_id=101)
        json.dumps({"id": 101, "result": fake_ax_tree}),
        # 2. locate_semantic_target: DOM.getBoxModel (cmd_id=102)
        json.dumps({"id": 102, "result": fake_box_model}),
    ]
    if not is_canvas:
        # 3. actuate_with_verification: Pre-eval (cmd_id=201)
        responses.append(json.dumps({"id": 201, "result": {"result": {"value": "<body active>"}}}))
        # 4. Input.dispatchMouseEvent (cmd_id=202)
        responses.append(json.dumps({"id": 202, "result": {}}))
        # 5. Input.dispatchMouseEvent (cmd_id=203)
        responses.append(json.dumps({"id": 203, "result": {}}))
        # 6. Input.insertText (cmd_id=204 - if typing)
        responses.append(json.dumps({"id": 204, "result": {}}))
        # 7. Post-eval (cmd_id=210)
        responses.append(json.dumps({"id": 210, "result": {"result": {"value": "<focused active>"}}}))

    mock_ws.recv.side_effect = responses
    return mock_ws


class TestBrowserDOMProvenanceSuite(unittest.TestCase):
    """Test suite verifying DOM provenance tracking and security boundaries for hybrid browser actuation."""

    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()

    def tearDown(self):
        self.tracker.reset_turn()
        self.approval_mgr.reset()

    def test_click_web_element_semantic_taints_clean_turn(self):
        """Verifies click_web_element_semantic marks the active turn tainted with browser_dom provenance."""
        self.assertFalse(self.tracker.is_tainted)
        mock_ws = _make_mock_ws(element_name="Malicious External Button", role="button")

        with patch("jarvis.skills.hybrid_actuator.is_cdp_available", return_value=True), \
             patch("jarvis.skills.hybrid_actuator._get_active_or_matching_tab", return_value={"ws_url": "ws://127.0.0.1:9222/devtools/page/1"}), \
             patch("websockets.sync.client.connect", return_value=MagicMock(__enter__=MagicMock(return_value=mock_ws))):

            result = click_web_element_semantic("Malicious External Button")

        self.assertIn("Malicious External Button", result)
        self.assertTrue(self.tracker.is_tainted, "Active turn MUST be tainted after returning DOM element text")
        self.assertIn("browser_dom:click_web_element_semantic", self.tracker.tainting_sources)

        # Verify provenance record metadata
        records = [r for r in self.tracker.provenance_records if r.source == "browser_dom:click_web_element_semantic"]
        self.assertTrue(len(records) >= 1)
        self.assertEqual(records[0].metadata.get("element_name"), "Malicious External Button")
        self.assertEqual(records[0].metadata.get("role"), "button")

    def test_type_into_web_element_semantic_taints_clean_turn(self):
        """Verifies type_into_web_element_semantic marks the active turn tainted with browser_dom provenance."""
        self.assertFalse(self.tracker.is_tainted)
        mock_ws = _make_mock_ws(element_name="Prompt Injection Input Field", role="textbox")

        with patch("jarvis.skills.hybrid_actuator.is_cdp_available", return_value=True), \
             patch("jarvis.skills.hybrid_actuator._get_active_or_matching_tab", return_value={"ws_url": "ws://127.0.0.1:9222/devtools/page/1"}), \
             patch("websockets.sync.client.connect", return_value=MagicMock(__enter__=MagicMock(return_value=mock_ws))):

            result = type_into_web_element_semantic("Prompt Injection", "safe content to type")

        self.assertIn("Prompt Injection Input Field", result)
        self.assertTrue(self.tracker.is_tainted, "Active turn MUST be tainted after returning DOM field name")
        self.assertIn("browser_dom:type_into_web_element_semantic", self.tracker.tainting_sources)

        # Verify provenance record metadata
        records = [r for r in self.tracker.provenance_records if r.source == "browser_dom:type_into_web_element_semantic"]
        self.assertTrue(len(records) >= 1)
        self.assertEqual(records[0].metadata.get("element_name"), "Prompt Injection Input Field")
        self.assertEqual(records[0].metadata.get("role"), "textbox")

    def test_taint_is_established_before_result_delivery(self):
        """Verifies taint is recorded synchronously before the tool function returns its payload."""
        self.assertFalse(self.tracker.is_tainted)
        mock_ws = _make_mock_ws(element_name="Login Submit", role="button")
        taint_observed_during_call = False

        original_mark_tainted = self.tracker.mark_tainted

        def spy_mark_tainted(source, metadata=None):
            nonlocal taint_observed_during_call
            original_mark_tainted(source, metadata=metadata)
            # Inspect tracker at the exact moment mark_tainted completes
            if self.tracker.is_tainted and "browser_dom:click_web_element_semantic" in self.tracker.tainting_sources:
                taint_observed_during_call = True

        with patch("jarvis.skills.hybrid_actuator.is_cdp_available", return_value=True), \
             patch("jarvis.skills.hybrid_actuator._get_active_or_matching_tab", return_value={"ws_url": "ws://127.0.0.1:9222/devtools/page/1"}), \
             patch("websockets.sync.client.connect", return_value=MagicMock(__enter__=MagicMock(return_value=mock_ws))), \
             patch.object(self.tracker, "mark_tainted", side_effect=spy_mark_tainted):

            res = click_web_element_semantic("Login")

        self.assertTrue(taint_observed_during_call, "Taint must be established during tool execution before result delivery")
        self.assertIn("Login Submit", res)
        self.assertTrue(self.tracker.is_tainted)

    def test_subsequent_level1_privileged_operation_blocked_after_dom_taint(self):
        """Verifies a subsequent Level 1 mutation encounters the Dual-Key Safety Gate after DOM interaction."""
        self.assertFalse(self.tracker.is_tainted)
        mock_ws = _make_mock_ws(element_name="Untrusted Forum Button", role="button")

        with patch("jarvis.skills.hybrid_actuator.is_cdp_available", return_value=True), \
             patch("jarvis.skills.hybrid_actuator._get_active_or_matching_tab", return_value={"ws_url": "ws://127.0.0.1:9222/devtools/page/1"}), \
             patch("websockets.sync.client.connect", return_value=MagicMock(__enter__=MagicMock(return_value=mock_ws))):

            click_web_element_semantic("Untrusted Forum Button")

        self.assertTrue(self.tracker.is_tainted)

        # Attempt high-privilege Level 1 mutation (close_application) without grant
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(
                "close_application",
                {"app_name": "notepad"},
                risk_level=1,
            )

        self.assertIn("Critical Authorization Gate: Tool 'close_application'", str(ctx.exception))

    def test_unsuccessful_dom_lookup_does_not_expose_unverified_dom_text_and_leaves_clean_turn(self):
        """Verifies failed element lookup does not leak untrusted tab titles or DOM text, and turn remains untainted."""
        self.assertFalse(self.tracker.is_tainted)
        empty_ax_tree = {"nodes": []}
        mock_ws = MagicMock()
        mock_ws.recv.return_value = json.dumps({"id": 101, "result": empty_ax_tree})

        with patch("jarvis.skills.hybrid_actuator.is_cdp_available", return_value=True), \
             patch("jarvis.skills.hybrid_actuator._get_active_or_matching_tab", return_value={
                 "ws_url": "ws://127.0.0.1:9222/devtools/page/1",
                 "title": "MALICIOUS <script>alert(1)</script> TAB TITLE"
             }), \
             patch("websockets.sync.client.connect", return_value=MagicMock(__enter__=MagicMock(return_value=mock_ws))):

            res_click = click_web_element_semantic("NonExistentButton")
            res_type = type_into_web_element_semantic("NonExistentInput", "text")

        # Must not expose the untrusted tab title
        self.assertNotIn("MALICIOUS", res_click)
        self.assertNotIn("<script>", res_click)
        self.assertNotIn("MALICIOUS", res_type)
        self.assertNotIn("<script>", res_type)

        # Turn must remain untainted because no DOM content was returned
        self.assertFalse(self.tracker.is_tainted)
        self.assertEqual(len(self.tracker.tainting_sources), 0)

    def test_exceptions_do_not_expose_unverified_dom_text_and_leave_clean_turn(self):
        """Verifies exceptions during connection or communication do not leak DOM text, leaving turn untainted."""
        self.assertFalse(self.tracker.is_tainted)

        with patch("jarvis.skills.hybrid_actuator.is_cdp_available", return_value=True), \
             patch("jarvis.skills.hybrid_actuator._get_active_or_matching_tab", return_value={"ws_url": "ws://127.0.0.1:9222/devtools/page/1"}), \
             patch("websockets.sync.client.connect", side_effect=ConnectionRefusedError("Connection refused by CDP endpoint")):

            res_click = click_web_element_semantic("AnyButton")
            res_type = type_into_web_element_semantic("AnyInput", "text")

        self.assertIn("CDP semantic actuation encountered an error:", res_click)
        self.assertIn("CDP semantic typing encountered an error:", res_type)

        # Turn remains untainted
        self.assertFalse(self.tracker.is_tainted)
        self.assertEqual(len(self.tracker.tainting_sources), 0)

    def test_opaque_canvas_element_taints_turn_before_recommending_som(self):
        """Verifies opaque canvas elements taint the turn before returning the accessible canvas name."""
        self.assertFalse(self.tracker.is_tainted)
        mock_ws = _make_mock_ws(element_name="Interactive WebGL Graph", role="canvas", is_canvas=True)

        with patch("jarvis.skills.hybrid_actuator.is_cdp_available", return_value=True), \
             patch("jarvis.skills.hybrid_actuator._get_active_or_matching_tab", return_value={"ws_url": "ws://127.0.0.1:9222/devtools/page/1"}), \
             patch("websockets.sync.client.connect", return_value=MagicMock(__enter__=MagicMock(return_value=mock_ws))):

            res = click_web_element_semantic("WebGL Graph")

        self.assertIn("Interactive WebGL Graph", res)
        self.assertIn("opaque <canvas>", res)
        self.assertTrue(self.tracker.is_tainted)
        self.assertIn("browser_dom:click_web_element_semantic", self.tracker.tainting_sources)


if __name__ == "__main__":
    unittest.main()
