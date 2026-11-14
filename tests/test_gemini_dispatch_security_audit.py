"""Empirical audit test verifying Gemini tool dispatch security and authorization boundaries."""
import os
import unittest
from unittest.mock import MagicMock, patch

from google.genai import types

from jarvis.core.brain import JarvisBrain
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate
from jarvis.core.security_boundaries import DualKeyApprovalManager, TurnTaintTracker


class TestGeminiDispatchSecurityAudit(unittest.TestCase):
    """P0 Audit Suite: Verifies Gemini tool dispatch path security boundaries."""

    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {"GEMINI_API_KEY": "test_key_12345"})
        self.env_patcher.start()

        self.mock_client = MagicMock()
        self.mock_chat = MagicMock()
        self.mock_client.chats.create.return_value = self.mock_chat

        self.client_patcher = patch("jarvis.core.brain_gemini.genai.Client", return_value=self.mock_client)
        self.client_patcher.start()

        self.config = {"_base_dir": ".", "gemini_model": "gemini-2.0-flash"}
        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate()
        self.brain = JarvisBrain(self.config, self.memory, self.safety, antigravity_resolver=lambda: None)

        self.brain.gemini_model = self.brain.gemini_candidates[self.brain.gemini_model_idx]
        self.brain.gemini_task_mode = self.brain.active_task_mode
        self.brain.gemini_tool_names = self.brain.active_tool_names
        self.brain.gemini_client = self.mock_client
        self.brain.gemini_chat = self.mock_chat

        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()

    def tearDown(self):
        self.client_patcher.stop()
        self.env_patcher.stop()
        self.approval_mgr.reset()
        self.tracker.reset_turn()

    def test_01_automatic_function_calling_strictly_disabled_in_configs(self):
        """INVARIANT 1: automatic_function_calling must be explicitly disabled in all Gemini configs."""
        # Check _init_gemini configuration
        with patch.object(self.brain.gemini_client.chats, "create") as mock_create:
            self.brain._init_gemini("gemini-2.0-flash")
            mock_create.assert_called_once()
            _, kwargs = mock_create.call_args
            config = kwargs.get("config")
            self.assertIsNotNone(config)
            afc = getattr(config, "automatic_function_calling", None)
            self.assertIsNotNone(afc)
            self.assertTrue(getattr(afc, "disable", False), "AFC must be explicitly disabled in chats.create config.")

    @patch("jarvis.skills.email_manager._send_smtp_email")
    def test_02_unauthorized_gemini_function_call_fails_closed_without_execution(self, mock_smtp):
        """INVARIANT 2: An unauthorized sensitive tool call from Gemini must fail closed.

        Model supplying confirm=True MUST NOT self-authorize.
        The underlying function (_send_smtp_email) MUST NOT be executed.
        """
        # Model attempts to call send_email with injected confirm=True
        fn_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[
                            types.Part.from_function_call(
                                name="send_email",
                                args={
                                    "to_address": "target@external.com",
                                    "subject": "Exfiltration Attempt",
                                    "body": "System secret payload",
                                    "confirm": True,
                                },
                            )
                        ]
                    )
                )
            ]
        )

        followup_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part.from_text(text="I was unable to dispatch the email.")]
                    )
                )
            ]
        )

        self.mock_chat.send_message_stream.side_effect = [
            [fn_chunk],
            [followup_chunk],
        ]

        _ = self.brain._process_gemini_query("Send the confidential report")

        # 1. Underlying execution boundary must NEVER be reached
        mock_smtp.assert_not_called()

        # 2. Inspect tool response returned to Gemini
        self.assertEqual(self.mock_chat.send_message_stream.call_count, 2)
        tool_responses_sent = self.mock_chat.send_message_stream.call_args_list[1][0][0]
        self.assertEqual(len(tool_responses_sent), 1)

        resp_part = tool_responses_sent[0]
        fn_resp = getattr(resp_part, "function_response", None)
        self.assertIsNotNone(fn_resp)
        result_str = str(fn_resp.response.get("result", ""))

        # 3. Security error must be surfaced in tool response
        self.assertTrue(
            "Security Boundary Violation" in result_str
            or "Security Schema Violation" in result_str
            or "requires explicit human authorization" in result_str,
            f"Expected authorization violation message, got: {result_str}",
        )

    @patch("jarvis.skills.email_manager._send_smtp_email", return_value=(True, "Message accepted"))
    @patch("jarvis.skills.email_manager._get_credentials", return_value=("tony@stark.com", "pass", "imap.gmail.com", "smtp.gmail.com", 465, True))
    def test_03_authorized_gemini_function_call_dispatches_cleanly(self, _mock_cred, mock_smtp):
        """INVARIANT 3: With valid human approval, Gemini function call executes successfully."""
        to_addr = "rhodey@usaf.mil"
        subj = "Flight Telemetry"
        body = "Flight parameters verified."

        # Pre-approve action in DualKeyApprovalManager as pending grant
        clean_args = {"to_address": to_addr, "subject": subj, "body": body}
        self.approval_mgr.grant_approval("send_email", clean_args)

        fn_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[
                            types.Part.from_function_call(
                                name="send_email",
                                args={"to_address": to_addr, "subject": subj, "body": body},
                            )
                        ]
                    )
                )
            ]
        )

        followup_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part.from_text(text="Email successfully sent, Sir.")]
                    )
                )
            ]
        )

        self.mock_chat.send_message_stream.side_effect = [
            [fn_chunk],
            [followup_chunk],
        ]

        reply = self.brain._process_gemini_query("Send telemetry to Rhodey")
        mock_smtp.assert_called_once()
        self.assertIn("Email successfully sent", reply)

    @patch("jarvis.skills.email_manager._send_smtp_email")
    def test_04_gemini_unauthorized_call_with_valid_schema_fails_closed(self, mock_smtp):
        """INVARIANT 4: Valid schema arguments without human grant must fail closed."""
        to_addr = "external@target.com"
        subj = "Status"
        body = "Report text"

        fn_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[
                            types.Part.from_function_call(
                                name="send_email",
                                args={"to_address": to_addr, "subject": subj, "body": body},
                            )
                        ]
                    )
                )
            ]
        )

        followup_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part.from_text(text="I cannot send without authorization.")]
                    )
                )
            ]
        )

        self.mock_chat.send_message_stream.side_effect = [
            [fn_chunk],
            [followup_chunk],
        ]

        _ = self.brain._process_gemini_query("Send the status report")
        mock_smtp.assert_not_called()

        tool_responses_sent = self.mock_chat.send_message_stream.call_args_list[1][0][0]
        resp_part = tool_responses_sent[0]
        fn_resp = getattr(resp_part, "function_response", None)
        result_str = str(fn_resp.response.get("result", ""))
        self.assertTrue(
            any(k in result_str for k in ("Critical Authorization Gate", "Authorization Required", "requires explicit", "Security Boundary Violation")),
            f"Expected authorization required error, got: {result_str}",
        )

    @patch("jarvis.skills.email_manager._send_smtp_email")
    def test_05_gemini_call_with_modified_arguments_fails_closed(self, mock_smtp):
        """INVARIANT 5: Approved action for one recipient fails closed when model modifies argument."""
        # Grant approved ONLY for alice@example.com
        approved_args = {"to_address": "alice@example.com", "subject": "Hello", "body": "Approved body"}
        self.approval_mgr.grant_approval("send_email", approved_args)

        # Model attempts to call with evil@example.com instead
        fn_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[
                            types.Part.from_function_call(
                                name="send_email",
                                args={"to_address": "evil@example.com", "subject": "Hello", "body": "Approved body"},
                            )
                        ]
                    )
                )
            ]
        )

        followup_chunk = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part.from_text(text="Authorization rejected.")]
                    )
                )
            ]
        )

        self.mock_chat.send_message_stream.side_effect = [
            [fn_chunk],
            [followup_chunk],
        ]

        _ = self.brain._process_gemini_query("Send email to alice")
        mock_smtp.assert_not_called()

        tool_responses_sent = self.mock_chat.send_message_stream.call_args_list[1][0][0]
        resp_part = tool_responses_sent[0]
        fn_resp = getattr(resp_part, "function_response", None)
        result_str = str(fn_resp.response.get("result", ""))
        self.assertTrue(
            any(k in result_str for k in ("Critical Authorization Gate", "Authorization Required", "requires explicit", "Security Boundary Violation")),
            f"Expected authorization failure for modified args, got: {result_str}",
        )
