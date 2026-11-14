"""Jarvis 2.0 — Phase 2 Stage 6C: Strict Email Schema & Approval Transparency.

Deterministic adversarial RED specification test suite probing:
1. Strict Email Schema & Parameter Fidelity:
   - Rejection of permissive **kwargs and *args in send_email signature.
   - Exact fidelity between function signature and generated OpenAI tool schema.
   - Direct invocation rejects unexpected/extraneous kwargs (TypeError / SecurityBoundaryViolation).
   - SkillRegistry dispatch rejects unexpected parameters without execution.
   - Strict validation of recipient address, attachments argument structure, and account type.
2. Meaningful Attachment Approval Transparency:
   - Structured preview generation for supported text attachments in human approval payload.
   - Bounded preview length with truncation indicators to prevent terminal/memory flood.
   - Suppression of raw binary bytes in favor of safe binary indicators and metadata.
   - Static inert preview of active content files (.py, .sh) without execution or evaluation.
   - Exact immutable snapshot byte-for-byte transmission to SMTP regardless of preview formatting.
3. Positive Workflows:
   - Legitimate approved dispatch with text and binary attachments succeeds once and consumes grant.
"""
from __future__ import annotations

import contextlib
import inspect
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
    compute_attachments_digest,
)
from jarvis.skills.base import SkillRegistry
from jarvis.skills.email_manager import send_email


class TestP1Stage6CApprovalTransparency(unittest.TestCase):
    """RED specification and adversarial suite for Stage 6C Email Transparency."""

    def setUp(self) -> None:
        """Establishes clean, deterministic test fixture state."""
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)
        self.test_dir = tempfile.mkdtemp(prefix="jarvis_stage6c_")
        self._ctx_tokens = []

    def tearDown(self) -> None:
        """Guarantees tear-down of sandbox and security contexts."""
        for tok in self._ctx_tokens:
            with contextlib.suppress(Exception):
                self.approval_mgr.clear_active_authorized(tok)
        self._ctx_tokens.clear()
        self.approval_mgr.set_approval_handler(None)
        self.approval_mgr.reset()
        self.tracker.reset_turn()
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # PART 1: STRICT EMAIL SCHEMA & PARAMETER FIDELITY
    # =========================================================================

    def test_01_send_email_signature_strictly_defined_without_var_kwargs(self) -> None:
        """INVARIANT: send_email must have a strictly defined signature.

        Permissive **kwargs and *args must be removed to prevent model argument injection.
        Allowed parameters must be strictly: to_address, subject, body, account, attachments.
        """
        sig = inspect.signature(send_email)
        param_kinds = {p.name: p.kind for p in sig.parameters.values()}

        # 1. No **kwargs or *args allowed
        has_var_keyword = any(k == inspect.Parameter.VAR_KEYWORD for k in param_kinds.values())
        has_var_positional = any(k == inspect.Parameter.VAR_POSITIONAL for k in param_kinds.values())

        self.assertFalse(
            has_var_keyword,
            "Security Schema Violation: send_email must NOT accept **kwargs; all parameters must be explicit.",
        )
        self.assertFalse(
            has_var_positional,
            "Security Schema Violation: send_email must NOT accept *args; all parameters must be explicit.",
        )

        # 2. Strict exact parameter set
        expected_params = {"to_address", "subject", "body", "account", "attachments"}
        actual_params = set(sig.parameters.keys())
        self.assertEqual(
            actual_params,
            expected_params,
            f"send_email parameters mismatch. Expected {expected_params}, found {actual_params}",
        )

        # 3. Verify no synthetic __signature__ override exists
        self.assertIsNone(
            getattr(send_email, "__signature__", None),
            "Custom __signature__ override must be removed; Python must introspect the actual def.",
        )

        # 4. Strict exception inheritance invariant: TypeError is NOT a subclass of SecurityBoundaryViolation
        self.assertFalse(
            issubclass(TypeError, SecurityBoundaryViolation),
            "Exception Hierarchy Invariant: TypeError must NOT be a subclass of SecurityBoundaryViolation.",
        )
        self.assertFalse(
            isinstance(TypeError("test"), SecurityBoundaryViolation),
            "Exception Hierarchy Invariant: TypeError instance must NOT be an instance of SecurityBoundaryViolation.",
        )

    def test_02_send_email_schema_fidelity_matches_exact_signature(self) -> None:
        """INVARIANT: Generated tool schema in SkillRegistry must match strict-mode specification.

        OpenAI strict-mode tool schema must satisfy:
        - strict: true on the function object
        - additionalProperties: false on parameters
        - required: all five declared parameters (to_address, subject, body, account, attachments)
        - nullable types expressed as type unions: ['string', 'null'], ['array', 'null']
        - items explicitly typed for array properties
        - no OpenAPI-style nullable: true properties
        """
        schemas = SkillRegistry.get_openai_tools(["send_email"])
        self.assertEqual(len(schemas), 1)

        tool_schema = schemas[0]
        self.assertEqual(tool_schema["type"], "function")
        fn = tool_schema["function"]
        self.assertEqual(fn["name"], "send_email")
        self.assertIs(fn.get("strict"), True, "Strict Mode: function must declare strict=True.")

        params = fn["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertIs(
            params.get("additionalProperties"),
            False,
            "Strict Mode: parameters must declare additionalProperties=False.",
        )
        props = params["properties"]

        # Expected properties must be present
        self.assertEqual(
            set(props.keys()),
            {"to_address", "subject", "body", "account", "attachments"},
        )

        # Under strict mode, all declared properties must be required
        required = set(params.get("required", []))
        self.assertEqual(required, {"to_address", "subject", "body", "account", "attachments"})

        # Primitive non-null types
        self.assertEqual(props["to_address"]["type"], "string")
        self.assertEqual(props["subject"]["type"], "string")
        self.assertEqual(props["body"]["type"], "string")

        # Nullable union types (OpenAI Strict Mode format)
        self.assertEqual(props["account"]["type"], ["string", "null"])
        self.assertEqual(props["attachments"]["type"], ["array", "null"])
        self.assertEqual(props["attachments"]["items"], {"type": "string"})

        # Verify no OpenAPI-style 'nullable' keys exist
        for p_name, p_schema in props.items():
            self.assertNotIn("nullable", p_schema, f"Strict Mode: {p_name} must NOT use OpenAPI nullable: true.")

    @patch("jarvis.skills.email_manager._get_credentials", return_value=("tony@stark.com", "pass", "imap.gmail.com", "smtp.gmail.com", 465, True))
    def test_03_direct_send_email_rejects_extraneous_parameters(self, _mock_cred: MagicMock) -> None:
        """INVARIANT: Direct invocation of send_email with extraneous parameters must fail closed.

        Model-injected flags like confirm=True, cc=..., or unexpected kwargs must raise TypeError
        directly from Python's parameter binding rather than being silently ignored.
        """
        to_addr = "rhodey@usaf.mil"
        subj = "Telemetry Report"
        body = "Flight test telemetry staged."

        clean_args = {"to_address": to_addr, "subject": subj, "body": body}
        grant = self.approval_mgr.grant_approval("send_email", clean_args)
        token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(token)

        # Direct Python call with unexpected kwargs must raise TypeError
        with self.assertRaises(TypeError) as ctx1:
            send_email(
                to_address=to_addr,
                subject=subj,
                body=body,
                confirm=True,  # Extraneous parameter
            )
        self.assertIn("unexpected keyword argument", str(ctx1.exception))

        with self.assertRaises(TypeError) as ctx2:
            send_email(
                to_address=to_addr,
                subject=subj,
                body=body,
                cc="attacker@evil.com",  # Extraneous parameter
            )
        self.assertIn("unexpected keyword argument", str(ctx2.exception))

    def test_04_skill_registry_dispatch_rejects_extraneous_parameters(self) -> None:
        """INVARIANT: SkillRegistry.execute with unexpected arguments must fail closed.

        Any execution call with arguments outside the strict schema must report failure
        with error_type='security_violation'.
        """
        res = SkillRegistry.execute_with_status(
            "send_email",
            to_address="rhodey@usaf.mil",
            subject="Telemetry",
            body="Flight data.",
            unexpected_override=True,
        )
        self.assertFalse(res.success, "SkillRegistry must fail closed on unexpected parameters.")
        self.assertEqual(res.error_type, "security_violation")
        self.assertIn("unexpected", str(res.error_message).lower() + str(res.result).lower())

    @patch("jarvis.skills.email_manager._get_credentials", return_value=("tony@stark.com", "pass", "imap.gmail.com", "smtp.gmail.com", 465, True))
    def test_05_send_email_rejects_blank_or_invalid_recipient_address(self, _mock_cred: MagicMock) -> None:
        """INVARIANT: send_email must fail closed when recipient address is empty or whitespace."""
        clean_args = {"to_address": "   ", "subject": "Test", "body": "Body"}
        grant = self.approval_mgr.grant_approval("send_email", clean_args)
        token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(token)

        with self.assertRaises((ValueError, SecurityBoundaryViolation)):
            send_email(to_address="   ", subject="Test", body="Body")

    @patch("jarvis.skills.email_manager._get_credentials", return_value=("tony@stark.com", "pass", "imap.gmail.com", "smtp.gmail.com", 465, True))
    def test_06_send_email_rejects_malformed_attachments_argument(self, _mock_cred: MagicMock) -> None:
        """INVARIANT: send_email must fail closed when attachments is not a list of path strings."""
        clean_args = {"to_address": "rhodey@usaf.mil", "subject": "Test", "body": "Body"}
        grant = self.approval_mgr.grant_approval("send_email", clean_args)
        token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(token)

        # Non-list attachments
        with self.assertRaises((TypeError, ValueError, SecurityBoundaryViolation)):
            send_email(
                to_address="rhodey@usaf.mil",
                subject="Test",
                body="Body",
                attachments="not_a_list.pdf",  # type: ignore[arg-type]
            )

        # List with non-string elements
        with self.assertRaises((TypeError, ValueError, SecurityBoundaryViolation)):
            send_email(
                to_address="rhodey@usaf.mil",
                subject="Test",
                body="Body",
                attachments=[12345],  # type: ignore[list-item]
            )

    @patch("jarvis.skills.email_manager._get_credentials", return_value=("tony@stark.com", "pass", "imap.gmail.com", "smtp.gmail.com", 465, True))
    def test_07_send_email_rejects_unsupported_account_provider(self, _mock_cred: MagicMock) -> None:
        """INVARIANT: send_email must fail closed when account is not a supported provider."""
        clean_args = {"to_address": "rhodey@usaf.mil", "subject": "Test", "body": "Body", "account": "malicious_relay"}
        grant = self.approval_mgr.grant_approval("send_email", clean_args)
        token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(token)

        with self.assertRaises((ValueError, SecurityBoundaryViolation)):
            send_email(
                to_address="rhodey@usaf.mil",
                subject="Test",
                body="Body",
                account="malicious_relay",
            )

    # =========================================================================
    # PART 2: MEANINGFUL ATTACHMENT APPROVAL TRANSPARENCY
    # =========================================================================

    def test_08_text_attachment_approval_includes_bounded_safe_preview(self) -> None:
        """INVARIANT: Approval request for text attachments must contain a structured text preview.

        The human operator must see what is inside the text file before granting approval.
        The preview must indicate is_text=True and include bounded safe preview content.
        """
        text_file = os.path.join(self.test_dir, "briefing.txt")
        file_content = "CONFIDENTIAL DIRECTIVE:\nProject Pegasus Phase 2 flight tests staged."
        with open(text_file, "w", encoding="utf-8") as f:
            f.write(file_content)

        captured_requests: list[dict] = []

        def capture_handler(req: dict) -> bool:
            captured_requests.append(req)
            return True

        self.approval_mgr.set_approval_handler(capture_handler)

        auth = self.approval_mgr.request_human_approval(
            tool_name="send_email",
            arguments={
                "to_address": "rhodey@usaf.mil",
                "subject": "Pegasus Briefing",
                "body": "Briefing attached.",
                "attachments": [text_file],
            },
        )
        self.assertIsNotNone(auth)
        self.assertEqual(len(captured_requests), 1)

        req = captured_requests[0]
        self.assertIn("attachment_details", req)
        details = req["attachment_details"]
        self.assertEqual(len(details), 1)

        att = details[0]
        self.assertEqual(att["filename"], "briefing.txt")
        self.assertTrue(att.get("is_text"), "Text attachment must be marked as is_text=True")
        self.assertIn("preview", att, "Text attachment details must include a 'preview' field")
        self.assertIn("Project Pegasus Phase 2", att["preview"])

    def test_09_large_text_attachment_preview_is_strictly_bounded_and_truncated(self) -> None:
        """INVARIANT: Large text attachment previews must be bounded to prevent buffer flood.

        Previews must be capped (e.g. <= 2048 chars) with a clear truncation indicator,
        while size_bytes and sha256 reflect the complete file.
        """
        large_file = os.path.join(self.test_dir, "large_report.log")
        # 50 KB text file
        repeated_line = "LOG ENTRY [2026-11-09]: System diagnostics nominal.\n"
        full_content = repeated_line * 1000
        with open(large_file, "w", encoding="utf-8") as f:
            f.write(full_content)

        captured_requests: list[dict] = []

        def capture_handler(req: dict) -> bool:
            captured_requests.append(req)
            return True

        self.approval_mgr.set_approval_handler(capture_handler)

        self.approval_mgr.request_human_approval(
            tool_name="send_email",
            arguments={
                "to_address": "rhodey@usaf.mil",
                "subject": "Diagnostic Logs",
                "body": "Logs attached.",
                "attachments": [large_file],
            },
        )
        self.assertEqual(len(captured_requests), 1)
        att = captured_requests[0]["attachment_details"][0]

        self.assertTrue(att.get("is_text"))
        self.assertIn("preview", att)
        preview = att["preview"]

        # Preview must be capped and indicate truncation
        self.assertLessEqual(len(preview), 2048, "Text preview length must be bounded to <= 2048 characters.")
        self.assertTrue(
            "truncated" in preview.lower() or "..." in preview,
            "Large text preview must contain a truncation indicator.",
        )
        # Full size must be accurately reported
        self.assertGreater(att["size_bytes"], 40000)

    def test_10_binary_attachment_approval_suppresses_raw_bytes_and_marks_binary(self) -> None:
        """INVARIANT: Binary attachments must NOT dump raw bytes into preview.

        Binary files must be classified as is_text=False with preview showing a safe placeholder,
        never corrupting the console or UI with binary control codes or null bytes.
        """
        bin_file = os.path.join(self.test_dir, "firmware.bin")
        raw_binary = b"\x00\x01\x02\x03\xff\xfe\x00\x80\x90\xaa\xbb\xcc" * 100
        with open(bin_file, "wb") as f:
            f.write(raw_binary)

        captured_requests: list[dict] = []

        def capture_handler(req: dict) -> bool:
            captured_requests.append(req)
            return True

        self.approval_mgr.set_approval_handler(capture_handler)

        self.approval_mgr.request_human_approval(
            tool_name="send_email",
            arguments={
                "to_address": "rhodey@usaf.mil",
                "subject": "Firmware Binary",
                "body": "Firmware update attached.",
                "attachments": [bin_file],
            },
        )
        self.assertEqual(len(captured_requests), 1)
        att = captured_requests[0]["attachment_details"][0]

        self.assertFalse(att.get("is_text", True), "Binary attachment must have is_text=False.")
        preview = att.get("preview", "")

        # Must not contain null bytes or control codes
        self.assertNotIn("\x00", preview, "Binary preview must not contain null bytes.")
        self.assertTrue(
            "binary" in preview.lower(),
            "Binary attachment preview must safely indicate binary content.",
        )

    def test_11_active_document_preview_does_not_execute_or_evaluate(self) -> None:
        """INVARIANT: Document preview generation must be strictly passive and inert.

        Active code files (.py, .sh, .js) and markup (.html) must be treated as inert text strings
        and must NEVER be imported, executed, evaluated, or rendered as active DOM/script elements.
        """
        # 1. Python script with active trap
        trap_py = os.path.join(self.test_dir, "script_trap.py")
        trap_code = "import sys; raise RuntimeError('ACTIVE_PYTHON_EXECUTED_DURING_PREVIEW_TRAP')\n"
        with open(trap_py, "w", encoding="utf-8") as f:
            f.write(trap_code)

        # 2. HTML document with XSS payload and markup
        trap_html = os.path.join(self.test_dir, "exploit.html")
        html_content = "<!DOCTYPE html><html><body><script>throw new Error('ACTIVE_HTML_SCRIPT_TRAP');</script><img src='x' onerror='alert(1)'><h1>Report</h1></body></html>"
        with open(trap_html, "w", encoding="utf-8") as f:
            f.write(html_content)

        # 3. JavaScript file with trap
        trap_js = os.path.join(self.test_dir, "node_trap.js")
        js_content = "throw new Error('ACTIVE_JS_EXECUTED_TRAP');\n"
        with open(trap_js, "w", encoding="utf-8") as f:
            f.write(js_content)

        captured_requests: list[dict] = []

        def capture_handler(req: dict) -> bool:
            captured_requests.append(req)
            return True

        self.approval_mgr.set_approval_handler(capture_handler)

        # Must not raise the trap exception during approval generation
        auth = self.approval_mgr.request_human_approval(
            tool_name="send_email",
            arguments={
                "to_address": "rhodey@usaf.mil",
                "subject": "Active Documents",
                "body": "Active payloads attached.",
                "attachments": [trap_py, trap_html, trap_js],
            },
        )
        self.assertIsNotNone(auth)
        self.assertEqual(len(captured_requests), 1)
        details = captured_requests[0]["attachment_details"]
        self.assertEqual(len(details), 3)

        # All 3 attachments must be classified as inert text with safe previews
        for att in details:
            self.assertTrue(att.get("is_text"), f"Attachment {att['filename']} must be classified as text.")
            self.assertFalse(att.get("is_truncated"))
            self.assertIn("preview", att)

        # Verify scripts and markup are preserved as inert plain text, not executed
        py_att = details[0]
        html_att = details[1]
        js_att = details[2]
        self.assertIn("ACTIVE_PYTHON_EXECUTED_DURING_PREVIEW_TRAP", py_att["preview"])
        self.assertIn("ACTIVE_HTML_SCRIPT_TRAP", html_att["preview"])
        self.assertIn("ACTIVE_JS_EXECUTED_TRAP", js_att["preview"])

    @patch("jarvis.skills.email_manager._send_smtp_email", return_value=(True, "Message accepted"))
    @patch("jarvis.skills.email_manager._get_credentials", return_value=("tony@stark.com", "pass", "imap.gmail.com", "smtp.gmail.com", 465, True))
    def test_12_immutable_snapshot_bytes_transmitted_to_smtp_unmodified(
        self, _mock_cred: MagicMock, mock_smtp_send: MagicMock
    ) -> None:
        """INVARIANT: Exact immutable snapshot bytes must be transmitted to SMTP.

        Preview generation must NOT mutate, strip, or alter the binary bytes transmitted.
        Even if preview text replaces line endings or decodes UTF-8, the sent attachment
        must be the exact byte-for-byte snapshot.
        """
        text_file = os.path.join(self.test_dir, "exact_bytes.txt")
        exact_bytes = b"Special Characters: \xc3\xa9\xc3\xa0\xc3\xb4\r\nLine2\r\n"
        with open(text_file, "wb") as f:
            f.write(exact_bytes)

        clean_args = {
            "to_address": "rhodey@usaf.mil",
            "subject": "Exact Snapshot Test",
            "body": "Body text.",
            "attachments": [text_file],
            "attachments_digest": compute_attachments_digest([text_file]),
        }
        grant = self.approval_mgr.grant_approval("send_email", clean_args)
        token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(token)

        res = send_email(
            to_address="rhodey@usaf.mil",
            subject="Exact Snapshot Test",
            body="Body text.",
            attachments=[text_file],
        )
        self.assertIn("Email dispatched", res)
        mock_smtp_send.assert_called_once()
        _, kwargs = mock_smtp_send.call_args
        sent_snapshots = kwargs.get("attachment_snapshots", [])
        self.assertEqual(len(sent_snapshots), 1)

        filename, transmitted_bytes = sent_snapshots[0]
        self.assertEqual(filename, "exact_bytes.txt")
        self.assertEqual(
            transmitted_bytes,
            exact_bytes,
            "Attachment byte transmission mismatch: Transmitted bytes differ from snapshot bytes.",
        )

    # =========================================================================
    # PART 3: POSITIVE WORKFLOWS
    # =========================================================================

    @patch("jarvis.skills.email_manager._send_smtp_email", return_value=(True, "Message accepted for delivery"))
    @patch("jarvis.skills.email_manager._get_credentials", return_value=("tony@stark.com", "pass", "imap.gmail.com", "smtp.gmail.com", 465, True))
    def test_13_positive_approved_send_email_with_attachments_dispatches_once(
        self, _mock_cred: MagicMock, mock_smtp_send: MagicMock
    ) -> None:
        """POSITIVE WORKFLOW: Legitimate approved email with attachments dispatches once.

        Single-use capability is consumed; replay attempt fails closed.
        """
        text_att = os.path.join(self.test_dir, "summary.txt")
        with open(text_att, "w", encoding="utf-8") as f:
            f.write("Executive Summary content.")

        to_addr = "pepper@stark.com"
        subj = "Quarterly Financials"
        body = "Attached is the summary, Pepper."

        clean_args = {
            "to_address": to_addr,
            "subject": subj,
            "body": body,
            "attachments": [text_att],
            "attachments_digest": compute_attachments_digest([text_att]),
        }
        grant = self.approval_mgr.grant_approval("send_email", clean_args)
        token = self.approval_mgr.bind_active_grant(grant)
        self._ctx_tokens.append(token)

        # Actuation 1: Must succeed
        res = send_email(
            to_address=to_addr,
            subject=subj,
            body=body,
            attachments=[text_att],
        )
        self.assertIn("Email dispatched, Sir", res)
        self.assertEqual(mock_smtp_send.call_count, 1)

        # Actuation 2: Replay must fail closed
        with self.assertRaises(SecurityBoundaryViolation):
            send_email(
                to_address=to_addr,
                subject=subj,
                body=body,
                attachments=[text_att],
            )
        self.assertEqual(mock_smtp_send.call_count, 1, "Replay must NOT trigger a second SMTP dispatch.")

    def test_14_schema_converter_handles_optional_unions_and_generics(self) -> None:
        """INVARIANT: SkillRegistry schema converter must support optional unions and generic types with strict-mode nullable array unions."""
        import typing

        from jarvis.skills.base import _resolve_json_schema_property

        # Optional list of strings (testing backward compatibility with typing.Optional)
        s_opt_list = _resolve_json_schema_property(typing.Optional[list[str]])  # noqa: UP045
        self.assertEqual(s_opt_list, {"type": ["array", "null"], "items": {"type": "string"}})

        # Union type list[int] | None
        s_union_list = _resolve_json_schema_property(list[int] | None)
        self.assertEqual(s_union_list, {"type": ["array", "null"], "items": {"type": "integer"}})

        # Primitive optional
        s_opt_int = _resolve_json_schema_property(int | None)
        self.assertEqual(s_opt_int, {"type": ["integer", "null"]})

        s_opt_bool = _resolve_json_schema_property(bool | None)
        self.assertEqual(s_opt_bool, {"type": ["boolean", "null"]})

        # Dict optional
        s_opt_dict = _resolve_json_schema_property(dict | None)
        self.assertEqual(s_opt_dict, {"type": ["object", "null"]})

        # Non-optional types must not have null in type
        s_raw_list = _resolve_json_schema_property(list[str])
        self.assertEqual(s_raw_list, {"type": "array", "items": {"type": "string"}})

        s_raw_str = _resolve_json_schema_property(str)
        self.assertEqual(s_raw_str, {"type": "string"})

    def test_15_attachment_preview_strips_ansi_escapes_and_control_codes(self) -> None:
        """INVARIANT: Terminal escape codes and dangerous control characters must be neutralized in preview."""
        ansi_file = os.path.join(self.test_dir, "colored_log.ansi")
        raw_text = "\x1b[31;1mCRITICAL ERROR:\x1b[0m Core reactor temp: \x1b[33m450K\x1b[0m\n"
        with open(ansi_file, "w", encoding="utf-8") as f:
            f.write(raw_text)

        captured: list[dict] = []
        self.approval_mgr.set_approval_handler(lambda req: (captured.append(req) or True))

        self.approval_mgr.request_human_approval(
            tool_name="send_email",
            arguments={
                "to_address": "rhodey@usaf.mil",
                "subject": "Reactor Alert",
                "body": "Report attached.",
                "attachments": [ansi_file],
            },
        )
        self.assertEqual(len(captured), 1)
        preview = captured[0]["attachment_details"][0]["preview"]

        # ANSI escapes must be stripped from presentation preview
        self.assertNotIn("\x1b[31;1m", preview)
        self.assertNotIn("\x1b[0m", preview)
        self.assertIn("CRITICAL ERROR: Core reactor temp: 450K", preview)

    def test_16_attachment_preview_does_not_mutate_underlying_bytes(self) -> None:
        """INVARIANT: Preview sanitization must never modify the transmitted raw bytes."""
        mixed_file = os.path.join(self.test_dir, "mixed.txt")
        raw_bytes = b"\x1b[32mGREEN_STATUS\x1b[0m\r\nSpecial: \xc3\xa9\xc3\xa8\r\n"
        with open(mixed_file, "wb") as f:
            f.write(raw_bytes)

        captured: list[dict] = []
        self.approval_mgr.set_approval_handler(lambda req: (captured.append(req) or True))

        auth = self.approval_mgr.request_human_approval(
            tool_name="send_email",
            arguments={
                "to_address": "rhodey@usaf.mil",
                "subject": "Mixed",
                "body": "Body",
                "attachments": [mixed_file],
            },
        )
        self.assertIsNotNone(auth)
        self.assertEqual(len(captured), 1)

        # Transmitted snapshot bytes stored in grant must match raw disk bytes exactly
        stored_bytes = auth.attachment_snapshots[mixed_file]
        self.assertEqual(stored_bytes, raw_bytes, "Snapshot bytes must remain identical to original file bytes.")


if __name__ == "__main__":
    unittest.main()
