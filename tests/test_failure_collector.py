"""Unit Test Suite for Deterministic Failure-Event Collector (ENG-OS-02 Phase 3B2).

Comprehensive, un-merged test suite covering all Phase 3B2 specifications:
1. Incomplete identity policy & missing-identifier behavior:
   - Distinct PostToolUse step numbers produce distinct IDs (RELIABLE).
   - Duplicate PostToolUse step numbers produce identical IDs (RELIABLE, idempotent).
   - Distinct Stop execution numbers produce distinct IDs (RELIABLE).
   - Duplicate Stop execution numbers produce identical IDs (RELIABLE, idempotent).
   - Missing identifiers with different payloads produce distinct IDs (INCOMPLETE_DISCRIMINATED).
   - Missing identifier collisions preserve secondary records as UNRESOLVABLE_DEDUPLICATION
     without silently discarding potentially independent events.
   - Different conversations produce distinct IDs.
2. Error-field semantics:
   - Explicit empty string ("error": "") -> error_status="EMPTY_STRING", tool_outcome="SUCCESS", exit_code=None.
   - Explicit null ("error": null) -> error_status="EXPLICIT_NULL", tool_outcome="SUCCESS", exit_code=None.
   - Omitted error field -> error_status="OMITTED", tool_outcome="SUCCESS", exit_code=None.
   - Non-empty error ("error": "exit status 1") -> error_status="NON_EMPTY", tool_outcome="FAILURE", exit_code=1.
   - Ambiguous non-empty error -> error_status="NON_EMPTY", tool_outcome="FAILURE", exit_code=None.
3. Strict exit-code extraction:
   - Outer host-reported exit status parsed with HIGH confidence.
   - Outer process exit banner parsed with HIGH confidence.
   - Nested output text rejected as UNAVAILABLE without fabricating exit codes.
4. Success filtering:
   - Routine successful commands are NOT persisted to disk.
   - Failures ARE persisted.
5. Windows evidence path confinement:
   - Path traversal outside allowed root rejected.
   - Symlinks rejected.
   - Junctions rejected.
   - 64 KB byte cap enforced with truncation.
6. Storage destination auditing:
   - System roots (C:\\, C:\\Windows) rejected.
   - Safe custom directory accepted.
   - Missing artifact dir safely skips persistence rather than polluting workspace.
7. Privacy and redaction:
   - Conversation ID masked to conv-<hash>.
   - Bearer tokens, API keys (sk-, ghp-, AIza-), passwords, and Windows usernames sanitized.
   - Clean text preserves CLEAN status.
8. Envelope model and Stop parsing:
   - EventEnvelope fields and serialization verified.
   - Stop normal completion -> SUCCESS.
   - Stop error termination -> OBSERVED_FAILURE.
   - Stop user cancellation -> OBSERVED_CANCELLATION.
   - Stop background not idle -> OBSERVED_INCOMPLETE.
9. Fail-open CLI protocol:
   - Valid payload returns {} and exits 0.
   - Garbage input returns {} and exits 0, logging to stderr.
   - Empty input returns {} and exits 0.
   - Malformed JSON string returns empty dict.
   - Non-dict payload returns empty dict.
10. Architectural boundary invariants:
   - Zero root-cause taxonomy invention.
   - Zero permanent lesson promotion.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.failure_collector import (
    MAX_EVIDENCE_READ_BYTES,
    SCHEMA_VERSION,
    EventEnvelope,
    compute_event_id,
    extract_exit_code,
    is_safe_storage_path,
    main,
    mask_conversation_id,
    parse_post_tool_use,
    parse_stop,
    persist_event_envelope,
    process_hook_payload,
    read_step_evidence,
    redact_sensitive_text,
    resolve_storage_directory,
    validate_path_confinement,
)

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


class TestEventIdentityAndDeduplication(unittest.TestCase):
    """Verifies event-type specific identity formulas, collision prevention, and incomplete identity policy."""

    def test_distinct_post_tool_use_steps(self) -> None:
        conv = "conv-abc123456789"
        id1, stat1 = compute_event_id(conv, "POST_TOOL_USE", step_idx=1)
        id2, stat2 = compute_event_id(conv, "POST_TOOL_USE", step_idx=2)
        self.assertNotEqual(id1, id2)
        self.assertEqual(stat1, "RELIABLE")
        self.assertEqual(stat2, "RELIABLE")

    def test_duplicate_post_tool_use_idempotency(self) -> None:
        conv = "conv-abc123456789"
        id1, stat1 = compute_event_id(conv, "POST_TOOL_USE", step_idx=5)
        id2, stat2 = compute_event_id(conv, "POST_TOOL_USE", step_idx=5)
        self.assertEqual(id1, id2)
        self.assertEqual(stat1, "RELIABLE")
        self.assertEqual(stat2, "RELIABLE")

    def test_distinct_stop_executions(self) -> None:
        conv = "conv-abc123456789"
        id1, stat1 = compute_event_id(conv, "STOP", exec_num=1)
        id2, stat2 = compute_event_id(conv, "STOP", exec_num=2)
        self.assertNotEqual(id1, id2)
        self.assertEqual(stat1, "RELIABLE")
        self.assertEqual(stat2, "RELIABLE")

    def test_duplicate_stop_idempotency(self) -> None:
        conv = "conv-abc123456789"
        id1, stat1 = compute_event_id(conv, "STOP", exec_num=3)
        id2, stat2 = compute_event_id(conv, "STOP", exec_num=3)
        self.assertEqual(id1, id2)
        self.assertEqual(stat1, "RELIABLE")
        self.assertEqual(stat2, "RELIABLE")

    def test_missing_identifiers_do_not_silently_collapse(self) -> None:
        conv = "conv-abc123456789"
        id_stop_err, stat_err = compute_event_id(conv, "STOP", exec_num=None, discriminator="disc_error")
        id_stop_can, stat_can = compute_event_id(conv, "STOP", exec_num=None, discriminator="disc_cancel")
        self.assertNotEqual(id_stop_err, id_stop_can)
        self.assertEqual(stat_err, "INCOMPLETE_DISCRIMINATED")
        self.assertEqual(stat_can, "INCOMPLETE_DISCRIMINATED")

        id_tool_cmd1, stat_cmd1 = compute_event_id(conv, "POST_TOOL_USE", step_idx=None, discriminator="disc_cmd1")
        id_tool_cmd2, stat_cmd2 = compute_event_id(conv, "POST_TOOL_USE", step_idx=None, discriminator="disc_cmd2")
        self.assertNotEqual(id_tool_cmd1, id_tool_cmd2)
        self.assertEqual(stat_cmd1, "INCOMPLETE_DISCRIMINATED")
        self.assertEqual(stat_cmd2, "INCOMPLETE_DISCRIMINATED")

    def test_incomplete_identity_collision_preserves_records(self) -> None:
        """Incomplete identity policy: Collisions on incomplete identities must NOT be discarded."""
        with tempfile.TemporaryDirectory() as tmp:
            storage_dir = Path(tmp)
            env1 = EventEnvelope(
                event_id="incomplete_event_001",
                event_type="POST_TOOL_USE",
                identity_status="INCOMPLETE_DISCRIMINATED",
                collection_status="OBSERVED_FAILURE",
                failure_signal="exit status 1",
            )
            # First persistence
            ok1, msg1 = persist_event_envelope(env1, storage_dir)
            self.assertTrue(ok1)
            self.assertEqual(msg1, "RECORDED")

            # Second event with identical incomplete identity arrives (e.g. repeated failure without step_idx)
            env2 = EventEnvelope(
                event_id="incomplete_event_001",
                event_type="POST_TOOL_USE",
                identity_status="INCOMPLETE_DISCRIMINATED",
                collection_status="OBSERVED_FAILURE",
                failure_signal="exit status 1",
            )
            ok2, msg2 = persist_event_envelope(env2, storage_dir)
            self.assertTrue(ok2)
            self.assertEqual(msg2, "RECORDED")
            # Invariant: Must preserve secondary file rather than silently dropping!
            self.assertEqual(env2.identity_status, "UNRESOLVABLE_DEDUPLICATION")
            files = sorted(storage_dir.glob("*.json"))
            self.assertEqual(len(files), 2)
            self.assertEqual(files[0].name, "incomplete_event_001.json")
            self.assertEqual(files[1].name, "incomplete_event_001_unresolvable_1.json")

    def test_different_conversations_produce_different_ids(self) -> None:
        id_conv1, _ = compute_event_id("conv-111111111111", "POST_TOOL_USE", step_idx=10)
        id_conv2, _ = compute_event_id("conv-222222222222", "POST_TOOL_USE", step_idx=10)
        self.assertNotEqual(id_conv1, id_conv2)


class TestErrorFieldSemantics(unittest.TestCase):
    """Verifies absent, empty, null, and non-empty error field semantics."""

    def test_error_empty_string(self) -> None:
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "dir"}},
            "stepIdx": 1,
            "error": "",
            "conversationId": "test-err-empty",
        }
        envelope = parse_post_tool_use(payload)
        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.error_status, "EMPTY_STRING")
        self.assertEqual(envelope.tool_outcome, "SUCCESS")
        self.assertEqual(envelope.collection_status, "SUCCESS")
        self.assertIsNone(envelope.exit_code)
        self.assertEqual(envelope.exit_code_confidence, "UNAVAILABLE")

    def test_error_explicit_null(self) -> None:
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "dir"}},
            "stepIdx": 2,
            "error": None,
            "conversationId": "test-err-null",
        }
        envelope = parse_post_tool_use(payload)
        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.error_status, "EXPLICIT_NULL")
        self.assertEqual(envelope.tool_outcome, "SUCCESS")
        self.assertEqual(envelope.collection_status, "SUCCESS")
        self.assertIsNone(envelope.exit_code)
        self.assertEqual(envelope.exit_code_confidence, "UNAVAILABLE")

    def test_error_omitted(self) -> None:
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "dir"}},
            "stepIdx": 3,
            "conversationId": "test-err-omitted",
        }
        envelope = parse_post_tool_use(payload)
        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.error_status, "OMITTED")
        self.assertEqual(envelope.tool_outcome, "SUCCESS")
        self.assertEqual(envelope.collection_status, "SUCCESS")
        self.assertIsNone(envelope.exit_code)
        self.assertEqual(envelope.exit_code_confidence, "UNAVAILABLE")

    def test_error_non_empty_failure(self) -> None:
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "fail_cmd"}},
            "stepIdx": 4,
            "error": "exit status 1",
            "conversationId": "test-err-fail",
        }
        envelope = parse_post_tool_use(payload)
        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.error_status, "NON_EMPTY")
        self.assertEqual(envelope.tool_outcome, "FAILURE")
        self.assertEqual(envelope.collection_status, "OBSERVED_FAILURE")
        self.assertEqual(envelope.exit_code, 1)
        self.assertEqual(envelope.exit_code_confidence, "HIGH")

    def test_error_non_empty_ambiguous(self) -> None:
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "fail_cmd"}},
            "stepIdx": 5,
            "error": "unspecified host error occurred",
            "conversationId": "test-err-ambig",
        }
        envelope = parse_post_tool_use(payload)
        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.error_status, "NON_EMPTY")
        self.assertEqual(envelope.tool_outcome, "FAILURE")
        self.assertEqual(envelope.collection_status, "OBSERVED_FAILURE")
        self.assertIsNone(envelope.exit_code)
        self.assertEqual(envelope.exit_code_confidence, "UNAVAILABLE")


class TestStrictExitCodeExtraction(unittest.TestCase):
    """Verifies that numeric exit codes are extracted strictly from outer process sources."""

    def test_outer_host_exit_status(self) -> None:
        code, conf = extract_exit_code("exit status 2")
        self.assertEqual(code, 2)
        self.assertEqual(conf, "HIGH")

    def test_outer_runner_footer(self) -> None:
        snippet = "Some process output...\nCommand exited with code 127."
        code, conf = extract_exit_code(None, output_snippet=snippet)
        self.assertEqual(code, 127)
        self.assertEqual(conf, "HIGH")

    def test_nested_text_in_output_body_rejected(self) -> None:
        # User output containing arbitrary strings like "exit code: 0" in text body
        snippet = "Test assertion: expected exit code: 0 but process crashed.\nDetailed traceback here."
        code, conf = extract_exit_code(None, output_snippet=snippet)
        # Invariant: Must not extract 0 from arbitrary body prose!
        self.assertIsNone(code)
        self.assertEqual(conf, "UNAVAILABLE")


class TestSuccessFiltering(unittest.TestCase):
    """Verifies that routine successes do NOT create persistent records."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_dir = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_routine_success_not_persisted(self) -> None:
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo hello"}},
            "stepIdx": 10,
            "error": "",
            "conversationId": "session-success-filter",
        }
        resp, envelope = process_hook_payload(payload, custom_storage_dir=self.storage_dir)
        self.assertEqual(resp, {})
        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.collection_status, "SUCCESS")
        saved_files = list(self.storage_dir.glob("*.json"))
        self.assertEqual(len(saved_files), 0)

    def test_failure_is_persisted(self) -> None:
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "bad_command"}},
            "stepIdx": 11,
            "error": "exit status 1",
            "conversationId": "session-success-filter",
        }
        resp, envelope = process_hook_payload(payload, custom_storage_dir=self.storage_dir)
        self.assertEqual(resp, {})
        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.collection_status, "OBSERVED_FAILURE")
        saved_files = list(self.storage_dir.glob("*.json"))
        self.assertEqual(len(saved_files), 1)


class TestSafeWindowsEvidenceConfinement(unittest.TestCase):
    """Verifies Windows path confinement, symlink, junction, and reparse-point rejection."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_relative_path_traversal_rejected(self) -> None:
        outside_dir = tempfile.TemporaryDirectory()
        try:
            outside_file = Path(outside_dir.name) / "secret.txt"
            outside_file.write_text("secret outside data", encoding="utf-8")

            is_safe, reason = validate_path_confinement(outside_file, self.root)
            self.assertFalse(is_safe)
            self.assertEqual(reason, "PATH_TRAVERSAL_REJECTED")

            content, _red, meta = read_step_evidence(outside_file, self.root)
            self.assertIsNone(content)
            self.assertEqual(meta["status"], "PATH_TRAVERSAL_REJECTED")
        finally:
            outside_dir.cleanup()

    def test_symlink_rejection(self) -> None:
        target_file = self.root / "real_file.txt"
        target_file.write_text("data", encoding="utf-8")
        with patch("pathlib.Path.is_symlink", return_value=True):
            is_safe, reason = validate_path_confinement(target_file, self.root)
            self.assertFalse(is_safe)
            self.assertEqual(reason, "REPARSE_POINT_REJECTED")

    def test_junction_rejection(self) -> None:
        test_file = self.root / "file.txt"
        test_file.write_text("sample content", encoding="utf-8")
        with patch("os.path.isjunction", return_value=True):
            is_safe, reason = validate_path_confinement(test_file, self.root)
            self.assertFalse(is_safe)
            self.assertEqual(reason, "REPARSE_POINT_REJECTED")

    def test_oversized_evidence_truncation(self) -> None:
        step_file = self.root / "output.txt"
        large_text = "A" * (MAX_EVIDENCE_READ_BYTES + 4096)
        step_file.write_text(large_text, encoding="utf-8")

        content, _red, meta = read_step_evidence(step_file, self.root)
        self.assertIsNotNone(content)
        assert content is not None
        self.assertEqual(len(content), MAX_EVIDENCE_READ_BYTES)
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["bytes_read"], MAX_EVIDENCE_READ_BYTES)


class TestStorageDestinationAudit(unittest.TestCase):
    """Verifies storage destination validation and rejection of unsafe/system roots."""

    def test_system_root_rejected(self) -> None:
        self.assertFalse(is_safe_storage_path(Path("C:\\")))
        self.assertFalse(is_safe_storage_path(Path("C:\\Windows")))
        self.assertFalse(is_safe_storage_path(Path("C:\\Windows\\System32")))

    def test_safe_custom_storage_dir_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "safe_events"
            self.assertTrue(is_safe_storage_path(p))

    def test_missing_artifact_dir_skips_persistence(self) -> None:
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "fail"}},
            "stepIdx": 1,
            "error": "exit status 1",
        }
        with patch.dict(os.environ, {}, clear=True):
            resolved = resolve_storage_directory(payload)
            self.assertIsNone(resolved)


class TestPrivacyAndRedaction(unittest.TestCase):
    """Verifies secret sanitization, masking, and privacy boundaries."""

    def test_conversation_id_masking(self) -> None:
        raw_guid = "94dad805-1298-4972-b6f0-a4aa04cd8d8f"
        masked = mask_conversation_id(raw_guid)
        self.assertIsNotNone(masked)
        assert masked is not None
        self.assertTrue(masked.startswith("conv-"))
        self.assertNotIn(raw_guid, masked)
        self.assertEqual(masked, mask_conversation_id(raw_guid))

    def test_sensitive_credential_redaction(self) -> None:
        samples = [
            ("Bearer dummy_jwt_token_1234567890", "Bearer [REDACTED_TOKEN]", "REDACTED"),
            ("Authorization: Bearer dummy-auth-token-xyz123", "Authorization: Bearer [REDACTED_TOKEN]", "REDACTED"),
            ("api_key='sk-dummytestkey1234567890abcdef'", "api_key='[REDACTED_API_KEY]'", "REDACTED"),
            ("token = ghp_dummytoken12345678901234567890", "token = [REDACTED_GITHUB_TOKEN]", "REDACTED"),
            ("key: AIzaSyDummyGoogleApiKey123456789012345", "key: [REDACTED_GOOGLE_API_KEY]", "REDACTED"),
            ("password='my_dummy_password'", "password='[REDACTED]'", "REDACTED"),
            (r"C:\Users\DummyUser\Projects\Jarvis_2.0", r"C:\Users\[USERNAME]\Projects\Jarvis_2.0", "REDACTED"),
        ]
        for original, expected, status in samples:
            sanitized, actual_status = redact_sensitive_text(original)
            self.assertEqual(sanitized, expected, f"Failed redacting: {original}")
            self.assertEqual(actual_status, status)

    def test_clean_text_preserves_clean_status(self) -> None:
        clean = "pytest tests/test_memory.py -v"
        sanitized, status = redact_sensitive_text(clean)
        self.assertEqual(sanitized, clean)
        self.assertEqual(status, "CLEAN")


class TestEnvelopeModelAndStopParsing(unittest.TestCase):
    """Verifies EventEnvelope model fields and Stop lifecycle parsing."""

    def test_envelope_schema_and_fields(self) -> None:
        envelope = EventEnvelope(
            schema_version=SCHEMA_VERSION,
            event_id="test_env_001",
            event_type="POST_TOOL_USE",
            identity_status="RELIABLE",
            conversation_reference="conv-1234567890ab",
            step_index=5,
            execution_number=None,
            tool_name="run_command",
            tool_outcome="FAILURE",
            error_status="NON_EMPTY",
            failure_signal="exit status 1",
            exit_code=1,
            exit_code_confidence="HIGH",
            termination_reason=None,
            fully_idle=None,
            timestamp="2026-11-04T12:00:00Z",
            evidence_references={"status": "FILE_NOT_FOUND"},
            safe_summary="Tool 'run_command' failed: exit status 1",
            redaction_status="CLEAN",
            collection_status="OBSERVED_FAILURE",
        )
        data = envelope.to_dict()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["identity_status"], "RELIABLE")
        self.assertEqual(data["tool_outcome"], "FAILURE")
        self.assertEqual(data["error_status"], "NON_EMPTY")
        self.assertEqual(data["exit_code"], 1)

    def test_stop_normal_completion(self) -> None:
        p = {"executionNum": 1, "terminationReason": "NO_TOOL_CALL", "error": "", "fullyIdle": True}
        env = parse_stop(p)
        self.assertIsNotNone(env)
        assert env is not None
        self.assertEqual(env.collection_status, "SUCCESS")
        self.assertEqual(env.tool_outcome, "NOT_APPLICABLE")
        self.assertEqual(env.identity_status, "RELIABLE")

    def test_stop_error_termination(self) -> None:
        p = {"executionNum": 2, "terminationReason": "ERROR", "error": "Daemon timeout", "fullyIdle": True}
        env = parse_stop(p)
        self.assertIsNotNone(env)
        assert env is not None
        self.assertEqual(env.collection_status, "OBSERVED_FAILURE")

    def test_stop_user_cancellation(self) -> None:
        p = {"executionNum": 3, "terminationReason": "USER_CANCELED", "error": "", "fullyIdle": True}
        env = parse_stop(p)
        self.assertIsNotNone(env)
        assert env is not None
        self.assertEqual(env.collection_status, "OBSERVED_CANCELLATION")

    def test_stop_background_incomplete(self) -> None:
        p = {"executionNum": 4, "terminationReason": "NO_TOOL_CALL", "error": "", "fullyIdle": False}
        env = parse_stop(p)
        self.assertIsNotNone(env)
        assert env is not None
        self.assertEqual(env.collection_status, "OBSERVED_INCOMPLETE")


class TestFailOpenCLIProtocol(unittest.TestCase):
    """Verifies fail-open CLI execution and envelope serialization."""

    def test_cli_main_returns_empty_json(self) -> None:
        stdin = io.StringIO(json.dumps({
            "toolCall": {"name": "run_command", "args": {"CommandLine": "dir"}},
            "stepIdx": 1,
            "error": "exit status 1",
        }))
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(stdin_stream=stdin, stdout_stream=stdout, stderr_stream=stderr)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "{}")

    def test_cli_main_fail_open_on_garbage(self) -> None:
        stdin = io.StringIO("<<<GARBAGE>>>")
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(stdin_stream=stdin, stdout_stream=stdout, stderr_stream=stderr)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "{}")
        self.assertIn("Malformed JSON", stderr.getvalue())

    def test_cli_main_empty_input(self) -> None:
        stdin = io.StringIO("")
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(stdin_stream=stdin, stdout_stream=stdout, stderr_stream=stderr)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "{}")

    def test_malformed_json_returns_empty_dict(self) -> None:
        resp, envelope = process_hook_payload("{invalid json!")
        self.assertEqual(resp, {})
        self.assertIsNone(envelope)

    def test_non_dict_payload_returns_empty_dict(self) -> None:
        for non_dict in ["[1, 2, 3]", "123", "true"]:
            resp, envelope = process_hook_payload(non_dict)
            self.assertEqual(resp, {})
            self.assertIsNone(envelope)


class TestArchitecturalInvariants(unittest.TestCase):
    """Enforces critical architectural and epistemic boundaries."""

    def test_no_root_cause_invention(self) -> None:
        forbidden = [
            "DEFECT_IMPLEMENTATION",
            "DEFECT_REGRESSION",
            "LIMITATION_ENVIRONMENT",
            "FLAKY_NON_DETERMINISTIC",
            "PROMPT_SCOPE_AMBIGUITY",
            "INCONCLUSIVE",
        ]

        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "python fail.py"}},
            "stepIdx": 5,
            "error": "AssertionError: Expected true but got false",
            "conversationId": "invariant-check",
        }
        with tempfile.TemporaryDirectory() as tmp:
            _, envelope = process_hook_payload(payload, custom_storage_dir=Path(tmp))
            self.assertIsNotNone(envelope)
            assert envelope is not None

            data = envelope.to_dict()
            self.assertNotIn("root_cause", data)
            self.assertNotIn("category", data)
            self.assertNotIn("taxonomy", data)
            for cat in forbidden:
                self.assertNotEqual(envelope.collection_status, cat)

    def test_no_lesson_promotion(self) -> None:
        lessons_path = WORKSPACE_ROOT / "docs" / "agent" / "LESSONS.md"
        if lessons_path.exists():
            original_content = lessons_path.read_text(encoding="utf-8")
            original_mtime = lessons_path.stat().st_mtime
        else:
            original_content = None
            original_mtime = None

        with tempfile.TemporaryDirectory() as tmp:
            for i in range(5):
                payload = {
                    "toolCall": {"name": "run_command", "args": {"CommandLine": f"cmd {i}"}},
                    "stepIdx": i,
                    "error": f"exit status {i + 1}",
                    "conversationId": "promote-check",
                }
                process_hook_payload(payload, custom_storage_dir=Path(tmp))

        if original_content is not None:
            self.assertEqual(lessons_path.read_text(encoding="utf-8"), original_content)
            self.assertEqual(lessons_path.stat().st_mtime, original_mtime)


class TestStopHookContractCompliance(unittest.TestCase):
    """Verifies that failure_collector adheres strictly to the documented Antigravity Stop hook contract."""

    def test_stop_hook_all_eight_scenarios_contract(self) -> None:
        """Verifies that all 8 Stop execution scenarios return {"decision": "stop"}."""
        scenarios = [
            # 1. Normal completion
            {"executionNum": 1, "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "conversationId": "sc-1"},
            # 2. Error termination
            {"executionNum": 2, "terminationReason": "ERROR", "error": "fatal execution defect", "fullyIdle": False, "conversationId": "sc-2"},
            # 3. User cancellation
            {"executionNum": 3, "terminationReason": "USER_CANCELED", "fullyIdle": True, "conversationId": "sc-3"},
            # 4. Timeout termination
            {"executionNum": 4, "terminationReason": "TIMEOUT", "error": "exceeded 30s", "fullyIdle": False, "conversationId": "sc-4"},
            # 5. Max invocations termination
            {"executionNum": 5, "terminationReason": "MAX_INVOCATIONS", "fullyIdle": True, "conversationId": "sc-5"},
            # 6. Empty termination
            {"executionNum": 6, "terminationReason": "", "fullyIdle": True, "conversationId": "sc-6"},
            # 7. Custom/unknown termination reason
            {"executionNum": 7, "terminationReason": "CUSTOM_STOP_SIGNAL", "fullyIdle": None, "conversationId": "sc-7"},
            # 8. Termination with missing terminationReason but fullyIdle=True
            {"executionNum": 8, "fullyIdle": True, "conversationId": "sc-8"},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for sc in scenarios:
                resp, _ = process_hook_payload(sc, custom_storage_dir=tmp_path)
                self.assertIsInstance(resp, dict)
                self.assertEqual(resp, {"decision": "stop"}, f"Failed for scenario: {sc}")

                # Test via main() stdin/stdout
                stdin = io.StringIO(json.dumps(sc))
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = main(stdin_stream=stdin, stdout_stream=stdout, stderr_stream=stderr)
                self.assertEqual(code, 0)
                parsed_out = json.loads(stdout.getvalue().strip())
                self.assertEqual(parsed_out, {"decision": "stop"}, f"CLI stdout failed for: {sc}")

    def test_post_tool_use_maintains_empty_dict(self) -> None:
        """Verifies that PostToolUse continues emitting {} as required by its contract."""
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "dir"}},
            "stepIdx": 42,
            "error": "",
            "conversationId": "ptu-check",
        }
        with tempfile.TemporaryDirectory() as tmp:
            resp, _ = process_hook_payload(payload, custom_storage_dir=Path(tmp))
            self.assertEqual(resp, {})

            stdin = io.StringIO(json.dumps(payload))
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = main(stdin_stream=stdin, stdout_stream=stdout, stderr_stream=stderr)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue().strip()), {})


if __name__ == "__main__":
    unittest.main()
