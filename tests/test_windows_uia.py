import sys
import unittest
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

warnings.filterwarnings("ignore", message=".*Revert to STA COM threading mode.*")

import jarvis.skills.os_control as os_ctrl
import jarvis.skills.windows_uia as w_uia
from jarvis.skills.base import SkillRegistry


class TestWindowsUIASuite(unittest.TestCase):
    """Verifies pywinauto UIA support, element inspection, semantic focus, and pattern retrieval."""

    def test_uia_supported_on_windows(self):
        """Verifies UIA availability detection."""
        self.assertTrue(w_uia.is_uia_supported())

    def test_inspect_window_controls_empty(self):
        """Verifies empty window title is politely rejected."""
        res = SkillRegistry.execute("inspect_window_controls", window_title="")
        self.assertIn("cannot be empty", res.lower())

    def test_inspect_window_controls_nonexistent(self):
        """Verifies non-existent window handling."""
        res = SkillRegistry.execute("inspect_window_controls", window_title="nonexistent_xyz_998877")
        self.assertIn("could not find", res.lower())

    def test_focus_window_uia_empty(self):
        """Verifies empty title returns None cleanly without error."""
        res = w_uia.focus_window_uia("")
        self.assertIsNone(res)

    def test_os_control_focus_window_with_uia_fallback(self):
        """Verifies focus_window seamlessly handles non-existent windows via UIA/Win32."""
        res = os_ctrl.focus_window("nonexistent_app_window_9988")
        self.assertTrue(
            "no open window matching" in res.lower() or "failed to focus window" in res.lower()
        )

    def test_is_hwnd_responsive_zero_or_invalid(self):
        """Verifies invalid or zero HWND returns True without crashing."""
        self.assertTrue(w_uia.is_hwnd_responsive(0))

    def test_is_hwnd_responsive_hung_detection(self):
        """Verifies that an unresponsive window (hung message pump) is detected."""
        with patch("ctypes.windll.user32.IsHungAppWindow", return_value=1):
            self.assertFalse(w_uia.is_hwnd_responsive(12345, timeout_ms=10))

    def test_inspect_window_controls_hung_window_aborts_safely(self):
        """Verifies inspect_window_controls detects hung window and aborts without deadlock."""
        mock_win = MagicMock()
        mock_win.window_text.return_value = "Frozen Document - App"
        mock_win.handle = 99999

        mock_desktop = MagicMock()
        mock_desktop.windows.return_value = [mock_win]

        with patch("pywinauto.Desktop", return_value=mock_desktop), \
             patch("jarvis.skills.windows_uia.is_hwnd_responsive", return_value=False):
            res = w_uia.inspect_window_controls("Frozen Document")
            self.assertTrue(
                "unresponsive" in res.lower()
                or "hungappwindow" in res.lower()
                or "could not find" in res.lower()
            )

    def test_get_element_patterns_none_and_invalid(self):
        """Verifies invalid or None element yields empty patterns list safely."""
        self.assertEqual(w_uia.get_element_patterns(None), [])
        self.assertEqual(w_uia.get_element_patterns("invalid_string"), [])

    def test_get_element_patterns_root_desktop(self):
        """Verifies root Desktop UIA element exposes standard accessibility patterns."""
        from pywinauto.uia_defines import IUIA
        root = IUIA().root
        patterns = w_uia.get_element_patterns(root)
        self.assertIsInstance(patterns, list)
        self.assertIn("LegacyIAccessible", patterns)

    def test_get_element_patterns_mock(self):
        """Verifies get_element_patterns accurately maps pattern IDs from COM interfaces."""
        from pywinauto.uia_defines import pattern_ids

        val_id, _ = pattern_ids["Value"]
        inv_id, _ = pattern_ids["Invoke"]

        mock_elem = MagicMock()

        def _get_cur_pattern(pid):
            if pid in (val_id, inv_id):
                return MagicMock()
            return None

        mock_elem.GetCurrentPattern.side_effect = _get_cur_pattern

        patterns = w_uia.get_element_patterns(mock_elem)
        self.assertEqual(sorted(patterns), ["Invoke", "Value"])

    def test_get_pattern_details_none(self):
        """Verifies None element returns empty pattern dictionary safely."""
        self.assertEqual(w_uia.get_pattern_details(None), {})

    def test_get_pattern_details_mock_patterns(self):
        """Verifies extraction and state normalization for Value, Toggle, and ExpandCollapse patterns."""
        from pywinauto.uia_defines import pattern_ids

        val_id, _ = pattern_ids["Value"]
        tog_id, _ = pattern_ids["Toggle"]
        exp_id, _ = pattern_ids["ExpandCollapse"]

        mock_val_iface = MagicMock()
        mock_val_iface.CurrentValue = "https://example.com"
        mock_val_iface.CurrentIsReadOnly = False
        mock_val_ptr = MagicMock()
        mock_val_ptr.QueryInterface.return_value = mock_val_iface

        mock_tog_iface = MagicMock()
        mock_tog_iface.CurrentToggleState = 1  # "on"
        mock_tog_ptr = MagicMock()
        mock_tog_ptr.QueryInterface.return_value = mock_tog_iface

        mock_exp_iface = MagicMock()
        mock_exp_iface.CurrentExpandCollapseState = 0  # "collapsed"
        mock_exp_ptr = MagicMock()
        mock_exp_ptr.QueryInterface.return_value = mock_exp_iface

        mock_elem = MagicMock()

        def _get_cur_pattern(pid):
            if pid == val_id:
                return mock_val_ptr
            if pid == tog_id:
                return mock_tog_ptr
            if pid == exp_id:
                return mock_exp_ptr
            return None

        mock_elem.GetCurrentPattern.side_effect = _get_cur_pattern

        details = w_uia.get_pattern_details(mock_elem)
        self.assertIn("Value", details)
        self.assertEqual(details["Value"]["value"], "https://example.com")
        self.assertFalse(details["Value"]["is_read_only"])

        self.assertIn("Toggle", details)
        self.assertEqual(details["Toggle"]["toggle_state"], "on")

        self.assertIn("ExpandCollapse", details)
        self.assertEqual(details["ExpandCollapse"]["expand_collapse_state"], "collapsed")

    def test_get_focused_element_uia_mock(self):
        """Verifies get_focused_element_uia wraps and inspects the active focused element."""
        mock_iuia_inst = MagicMock()
        mock_elem = MagicMock()
        mock_iuia_inst.get_focused_element.return_value = mock_elem

        mock_info = MagicMock()
        mock_info.name = "Omnibox"
        mock_info.control_type = "Edit"
        mock_info.automation_id = "addressEditBox"
        mock_info.class_name = "Chrome_RenderWidgetHostHWND"
        mock_info.handle = 12345

        mock_wrap = MagicMock()
        mock_wrap.rectangle.return_value = MagicMock(left=10, top=20, right=100, bottom=50)
        mock_wrap.is_keyboard_focusable.return_value = True
        mock_wrap.has_keyboard_focus.return_value = True

        with patch("pywinauto.uia_defines.IUIA", return_value=mock_iuia_inst), \
             patch("pywinauto.uia_element_info.UIAElementInfo", return_value=mock_info), \
             patch("pywinauto.controls.uiawrapper.UIAWrapper", return_value=mock_wrap), \
             patch("jarvis.skills.windows_uia.get_element_patterns", return_value=["Text", "Value"]), \
             patch("jarvis.skills.windows_uia.get_pattern_details", return_value={"Value": {"value": "jarvis.ai"}}):

            result = w_uia.get_focused_element_uia()
            self.assertIsNotNone(result)
            self.assertEqual(result["name"], "Omnibox")
            self.assertEqual(result["control_type"], "Edit")
            self.assertEqual(result["automation_id"], "addressEditBox")
            self.assertTrue(result["has_keyboard_focus"])
            self.assertEqual(result["supported_patterns"], ["Text", "Value"])
            self.assertEqual(result["bounding_rectangle"], (10, 20, 100, 50))

    def test_focus_element_uia_empty_criteria(self):
        """Verifies focus_element_uia rejects calls without any target criterion."""
        res = w_uia.focus_element_uia(window_title="SomeApp")
        self.assertFalse(res["success"])
        self.assertIn("at least one target criterion", res["error"].lower())

    def test_focus_element_uia_window_not_found(self):
        """Verifies focus_element_uia reports failure if the specified host window is missing."""
        mock_desktop = MagicMock()
        mock_desktop.windows.return_value = []
        with patch("pywinauto.Desktop", return_value=mock_desktop):
            res = w_uia.focus_element_uia(window_title="UnknownAppXYZ", control_type="Button")
            self.assertFalse(res["success"])
            self.assertIn("could not find", res["error"].lower())

    def test_focus_element_uia_hung_window(self):
        """Verifies focus_element_uia aborts safely when host window is unresponsive."""
        mock_win = MagicMock()
        mock_win.window_text.return_value = "Hung Window"
        mock_win.handle = 54321
        mock_desktop = MagicMock()
        mock_desktop.windows.return_value = [mock_win]

        with patch("pywinauto.Desktop", return_value=mock_desktop), \
             patch("jarvis.skills.windows_uia.is_hwnd_responsive", return_value=False):
            res = w_uia.focus_element_uia(window_title="Hung", control_type="Button")
            self.assertFalse(res["success"])
            self.assertIn("unresponsive", res["error"].lower())

    def test_focus_element_uia_mock_success(self):
        """Verifies semantic focus execution, child element matching, and closed-loop verification."""
        mock_child = MagicMock()
        mock_child.element_info.control_type = "Edit"
        mock_child.element_info.name = "Address Bar"
        mock_child.element_info.automation_id = "urlBar"
        mock_child.element_info.class_name = "EditControl"
        mock_child.element_info.handle = 1001
        mock_child.has_keyboard_focus.return_value = True

        mock_win = MagicMock()
        mock_win.window_text.return_value = "Brave Browser"
        mock_win.handle = 2002
        mock_win.is_minimized.return_value = False
        mock_win.descendants.return_value = [mock_child]

        mock_desktop = MagicMock()
        mock_desktop.windows.return_value = [mock_win]

        with patch("pywinauto.Desktop", return_value=mock_desktop), \
             patch("jarvis.skills.windows_uia.is_hwnd_responsive", return_value=True), \
             patch("jarvis.skills.windows_uia.get_element_patterns", return_value=["Text", "Value"]), \
             patch("jarvis.skills.windows_uia.get_pattern_details", return_value={"Value": {"value": "https://brave.com"}}):

            res = w_uia.focus_element_uia(window_title="Brave", control_type="Edit", timeout_sec=0.1)
            self.assertTrue(res["success"])
            self.assertTrue(res["has_keyboard_focus"])
            self.assertEqual(res["name"], "Address Bar")
            self.assertEqual(res["control_type"], "Edit")
            self.assertEqual(res["automation_id"], "urlBar")
            mock_child.set_focus.assert_called_once()

    def test_skill_registry_tools(self):
        """Verifies SkillRegistry integration for focus_element, get_focused_element, and retrieve_element_patterns."""
        # 1. focus_element empty criteria rejection
        res_focus = SkillRegistry.execute(
            "focus_element",
            window_title="",
            control_type="",
            element_name="",
            automation_id="",
        )
        self.assertIn("unable to focus element", res_focus.lower())

        # 2. get_focused_element returns a string description
        res_get_focused = SkillRegistry.execute("get_focused_element")
        self.assertIsInstance(res_get_focused, str)

        # 3. retrieve_element_patterns with non-existent window
        res_patterns = SkillRegistry.execute(
            "retrieve_element_patterns",
            window_title="nonexistent_window_12345",
            control_type="Edit",
        )
        self.assertIn("could not inspect patterns", res_patterns.lower())


if __name__ == "__main__":
    unittest.main()
