"""Deterministic regression test suite for fail-closed ingestion classification and taint propagation.

Validates:
1. Ingestion classifier raises an unexpected RuntimeError -> Turn fails closed.
2. Skill registry classification fails -> Turn fails closed.
3. External ingestion tool has unknown or malformed metadata -> Classified conservatively as untrusted.
4. Taint recording fails -> Subsequent privileged operation is blocked.
5. Normal known ingestion tool -> Result is recorded as tainted.
6. Normal trusted local tool -> No unnecessary taint.
7. Critical integration test: External ingestion followed by an attempted privileged mutation.
8. Custom skill cannot impersonate trusted builtin.
9. Monotonic compromised-turn state and lifecycle.
10. Strict execution ordering: External payload cannot be returned before classification.
11. End-to-end privileged authorization boundaries for Level 1 and Level 2 operations.
"""
import unittest
from unittest.mock import patch

import jarvis.skills.productivity  # noqa: F401
from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    IngestionTrustClassification,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills.base import SkillRegistry


class TestFailClosedIngestion(unittest.TestCase):
    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)
        self.orchestrator = ToolOrchestrator(taint_tracker=self.tracker)

    def tearDown(self):
        self.tracker.reset_turn()
        self.approval_mgr.reset()
        self.approval_mgr.set_approval_handler(None)

    def test_ingestion_classifier_unexpected_runtime_error_fails_closed(self):
        """1. When ingestion classification encounters an unexpected RuntimeError, it must fail closed."""
        with patch.object(SkillRegistry, "is_ingestion_tool", side_effect=RuntimeError("Corrupted registry AST")):
            # is_untrusted_ingestion must not silently catch and return False
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                self.tracker.is_untrusted_ingestion("some_dynamic_tool")
            self.assertIn("classification", str(ctx.exception).lower())

            # Complete evaluation path in evaluate_tool_execution must also fail closed
            with self.assertRaises(SecurityBoundaryViolation):
                self.tracker.evaluate_tool_execution("some_dynamic_tool", {})

    def test_skill_registry_classification_failure_fails_closed(self):
        """2. When SkillRegistry classification fails unexpectedly, the turn fails closed."""
        with patch.object(SkillRegistry, "get_metadata", side_effect=KeyError("Corrupted metadata schema")):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                self.tracker.is_untrusted_ingestion("untrusted_source_tool")
            self.assertIn("classification", str(ctx.exception).lower())

    def test_external_ingestion_unknown_metadata_classified_conservatively(self):
        """3. External or unrecognized tools with unknown/missing metadata are classified conservatively as untrusted."""
        unknown_tool = "unregistered_external_fetcher"
        # Tool is not registered anywhere in Jarvis
        self.assertIsNone(SkillRegistry.get_metadata(unknown_tool))
        self.assertNotIn(unknown_tool, self.tracker.UNTRUSTED_INGESTION_TOOLS)

        # Conservative classification must treat unknown external tool as untrusted ingestion
        self.assertTrue(
            self.tracker.is_untrusted_ingestion(unknown_tool),
            "Unrecognized tool must be conservatively classified as untrusted ingestion",
        )

        # evaluate_tool_execution must taint the turn
        decision = self.tracker.evaluate_tool_execution(unknown_tool, {"source": "remote_feed"})
        self.assertTrue(decision.allowed)
        self.assertTrue(self.tracker.is_tainted, "Turn must be marked as tainted by unrecognized external tool")
        self.assertIn(unknown_tool, self.tracker.tainting_sources)

    def test_taint_recording_failure_blocks_subsequent_privileged_operation(self):
        """4. If taint recording fails, the system must not silently continue as untainted."""
        # Induce record_ingestion / mark_tainted failure
        with patch.object(self.tracker, "mark_tainted", side_effect=RuntimeError("Taint context write failed")):
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                self.tracker.evaluate_tool_execution("web_search", {"query": "test"})
            self.assertIn("taint", str(ctx.exception).lower())

        # Ensure subsequent Level 1 privileged mutation is blocked
        with self.assertRaises(SecurityBoundaryViolation):
            self.tracker.evaluate_tool_execution(
                "execute_sandboxed_powershell", {"command": "dir"}, risk_level=1
            )

    def test_normal_known_ingestion_tool_records_taint(self):
        """5. Normal known ingestion tool (web_search) succeeds and records taint."""
        decision = self.tracker.evaluate_tool_execution("web_search", {"query": "python docs"})
        self.assertTrue(decision.allowed)
        self.assertTrue(self.tracker.is_tainted)
        self.assertIn("web_search", self.tracker.tainting_sources)

        # Subsequent Level 1 mutation is blocked without approval
        with self.assertRaises(SecurityBoundaryViolation):
            self.tracker.evaluate_tool_execution(
                "execute_sandboxed_powershell", {"command": "dir"}, risk_level=1
            )

    def test_normal_trusted_local_tool_no_unnecessary_taint(self):
        """6. Normal trusted local builtin read-only tool must not cause unnecessary taint."""
        tool_name = "test_trusted_local_read_only"

        SkillRegistry.certify_test_read_only_tool(tool_name)

        @SkillRegistry.register(
            name=tool_name,
            description="Verified local read-only tool",
            read_only=True,
            risk_level=0,
            origin="builtin",
            external_ingestion=False,
            data_provenance="trusted_system",
        )
        def local_ro():
            return "system_time_123"

        try:
            decision = self.tracker.evaluate_tool_execution(tool_name, {})
            self.assertTrue(decision.allowed)
            self.assertFalse(self.tracker.is_tainted, "Trusted local tool must not taint the turn")

            # High-privilege mutation requires explicit authorization even on clean turn
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                self.tracker.evaluate_tool_execution(
                    "execute_sandboxed_powershell", {"command": "dir"}, risk_level=1
                )
            self.assertIn("Critical Authorization Gate", str(ctx.exception))

            # With explicit grant, execution is allowed
            self.approval_mgr.grant_approval("execute_sandboxed_powershell", {"command": "dir"})
            mut_decision = self.tracker.evaluate_tool_execution(
                "execute_sandboxed_powershell", {"command": "dir"}, risk_level=1
            )
            self.assertTrue(mut_decision.allowed)
        finally:
            SkillRegistry.unregister(tool_name)

    def test_critical_integration_external_ingestion_followed_by_attempted_mutation(self):
        """7. Full integration test: external tool with classification failure cannot lead to unapproved mutation."""
        tool_name = "flaky_external_ingestion"

        # Step 1: Tool execution where classification raises an error
        with patch.object(SkillRegistry, "is_ingestion_tool", side_effect=RuntimeError("AST Parser Crash")):
            res = self.orchestrator.execute_step_with_status(tool_name, {"url": "https://evil.com"})
            # The ingestion step MUST be denied/failed closed
            self.assertFalse(res.success)
            self.assertEqual(res.failure_category, "orchestrator_denied")

        # Step 2: Attempted privileged mutation in the same turn
        mut_res = self.orchestrator.execute_step_with_status(
            "execute_sandboxed_powershell", {"command": "Remove-Item C:\\test.txt"}
        )
        # MUST be denied because the previous external ingestion trust was not established cleanly
        self.assertFalse(mut_res.success)
        self.assertEqual(mut_res.failure_category, "orchestrator_denied")

    def test_untrusted_custom_skill_cannot_impersonate_trusted_builtin(self):
        """8. Proof: Custom skills cannot declare origin='builtin' or bypass untrusted classification."""
        # 1. Custom string/dynamic code attempting origin='builtin' must be rejected at registration
        fake_dynamic_fn = eval("lambda: 'untrusted_eval'")
        fake_dynamic_fn.__module__ = "custom_dynamic_script"
        with self.assertRaises(PermissionError) as ctx:
            SkillRegistry.register(
                name="fake_dynamic_builtin",
                origin="builtin",
                external_ingestion=False,
            )(fake_dynamic_fn)
        self.assertIn("cannot claim origin='builtin'", str(ctx.exception))

        # 2. Legitimate custom skill declaring external_ingestion=False cannot claim KNOWN_TRUSTED
        custom_tool_name = "test_custom_tool_trying_to_be_trusted"

        @SkillRegistry.register(
            name=custom_tool_name,
            description="Custom skill trying to claim it does not ingest external data",
            origin="custom",
            external_ingestion=False,
            risk_level=1,
        )
        def custom_fn():
            return "custom_data"

        try:
            # Classification MUST evaluate to UNKNOWN, not KNOWN_TRUSTED
            classification = self.tracker.classify_tool_ingestion(custom_tool_name)
            self.assertEqual(classification, IngestionTrustClassification.UNKNOWN)

            # Ingestion check must fail closed to True
            self.assertTrue(self.tracker.is_untrusted_ingestion(custom_tool_name))

            # Executing it must taint the turn
            decision = self.tracker.evaluate_tool_execution(custom_tool_name, {})
            self.assertTrue(decision.allowed)
            self.assertTrue(self.tracker.is_tainted)
        finally:
            SkillRegistry.unregister(custom_tool_name)

    def test_monotonic_compromised_turn_state_and_lifecycle(self):
        """9. Proof: Once compromised, a turn remains locked down; monotonic properties cannot be cleared."""
        # 1. Trigger compromise via classification failure
        with (
            patch.object(SkillRegistry, "is_ingestion_tool", side_effect=RuntimeError("Registry crash")),
            self.assertRaises(SecurityBoundaryViolation),
        ):
            self.tracker.evaluate_tool_execution("flaky_tool", {})

        self.assertTrue(self.tracker.is_compromised)
        self.assertTrue(self.tracker.is_tainted)

        # 2. Attempted tampering: try to reset is_tainted or clear sources
        self.tracker.is_tainted = False
        self.tracker._tainting_sources = []
        self.assertTrue(self.tracker.is_compromised, "Compromised state must be immutable within the turn")
        self.assertTrue(self.tracker.is_tainted, "Taint must remain monotonic within the turn")

        # 3. Subsequent benign local read-only tool cannot clear compromise
        self.tracker.evaluate_tool_execution("get_current_time_and_date", {})
        self.assertTrue(self.tracker.is_compromised)
        self.assertTrue(self.tracker.is_tainted)

        # 4. Privileged Level 1 mutation remains strictly blocked
        with self.assertRaises(SecurityBoundaryViolation):
            self.tracker.evaluate_tool_execution(
                "execute_sandboxed_powershell", {"command": "dir"}, risk_level=1
            )

        # 5. Resetting the turn safely starts a clean turn
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_compromised, "Clean turn must start with is_compromised=False")
        self.assertFalse(self.tracker.is_tainted, "Clean turn must start with is_tainted=False")

    def test_ordering_external_result_cannot_be_returned_before_classification_and_tainting(self):
        """10. Proof: Physical execution is gated strictly after classification; payload never returned on failure."""
        invoked = []

        @SkillRegistry.register(
            name="test_instrumented_ingestion",
            description="Instrumented tool to verify invocation ordering",
            origin="custom",
            external_ingestion=True,
            risk_level=1,
        )
        def instrumented_fn():
            invoked.append("invoked")
            return "SECRET_ATTACKER_PAYLOAD"

        try:
            # Induce failure in classification
            with patch.object(
                self.tracker,
                "classify_tool_ingestion",
                return_value=IngestionTrustClassification.CLASSIFICATION_FAILURE,
            ):
                step_res = self.orchestrator.execute_step_with_status("test_instrumented_ingestion", {})

                # Verify: tool function was NEVER invoked
                self.assertEqual(len(invoked), 0, "Tool must NOT be executed if classification fails")
                self.assertFalse(step_res.success)
                self.assertEqual(step_res.failure_category, "orchestrator_denied")
                self.assertNotIn("SECRET_ATTACKER_PAYLOAD", str(step_res.result))
        finally:
            SkillRegistry.unregister("test_instrumented_ingestion")

    def test_privileged_authorization_boundary_level1_and_level2(self):
        """11. Proof: End-to-end authorization gates for Level 1 mutations and Level 2 critical operations."""
        # 1. Normal ingestion taints turn -> Level 1 mutation without approval is blocked
        self.tracker.evaluate_tool_execution("web_search", {"query": "sample query"})
        self.assertTrue(self.tracker.is_tainted)

        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(
                "execute_sandboxed_powershell", {"command": "Get-Process"}, risk_level=1
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

        # 2. Level 1 mutation WITH pre-approved grant succeeds
        self.approval_mgr.grant_approval("execute_sandboxed_powershell", {"command": "Get-Process"})
        l1_decision = self.tracker.evaluate_tool_execution(
            "execute_sandboxed_powershell", {"command": "Get-Process"}, risk_level=1
        )
        self.assertTrue(l1_decision.allowed)
        self.assertIsNotNone(l1_decision.grant)

        # 3. Level 2 critical mutation (e.g. write_file) ALWAYS requires approval even on clean turn
        self.tracker.reset_turn()
        self.approval_mgr.reset()
        self.assertFalse(self.tracker.is_tainted)

        with self.assertRaises(SecurityBoundaryViolation) as ctx2:
            self.tracker.evaluate_tool_execution(
                "write_file", {"path": "test.txt", "content": "data"}, risk_level=2
            )
        self.assertIn("Critical Authorization Gate", str(ctx2.exception))

    def test_builtin_tool_with_unknown_data_provenance_is_not_known_trusted(self):
        """12. Builtin tools with unspecified data provenance evaluate to UNKNOWN and fail closed."""
        # read_file has origin='builtin' but data_provenance='unspecified'
        classification = self.tracker.classify_tool_ingestion("read_file")
        self.assertEqual(
            classification,
            IngestionTrustClassification.UNKNOWN,
            "Builtin tool with unspecified data provenance must evaluate to UNKNOWN, not KNOWN_TRUSTED",
        )
        self.assertTrue(self.tracker.is_untrusted_ingestion("read_file"))

    def test_browser_and_desktop_inspection_classified_known_untrusted(self):
        """13. Browser tab and desktop inspection tools are classified as KNOWN_UNTRUSTED."""
        for tool_name in (
            "list_open_browser_tabs",
            "get_active_window",
            "inspect_window_controls",
            "get_background_task_output",
        ):
            classification = self.tracker.classify_tool_ingestion(tool_name)
            self.assertEqual(
                classification,
                IngestionTrustClassification.KNOWN_UNTRUSTED,
                f"Tool '{tool_name}' must be classified as KNOWN_UNTRUSTED",
            )
            self.assertTrue(self.tracker.is_untrusted_ingestion(tool_name))

    def test_filesystem_reading_operation_with_unknown_provenance_is_conservative(self):
        """14. Filesystem reading tools with unknown provenance execute safely but taint the turn."""
        self.assertFalse(self.tracker.is_tainted)
        # read_file execution is allowed (read-only), but taints the turn
        decision = self.tracker.evaluate_tool_execution("read_file", {"file_path": "test.txt"})
        self.assertTrue(decision.allowed)
        self.assertTrue(
            self.tracker.is_tainted,
            "Executing read_file must taint the turn because file content has unverified provenance",
        )

        # Subsequent privileged Level 1 mutation in the same turn without grant must be blocked
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(
                "execute_sandboxed_powershell", {"command": "Remove-Item test.txt"}, risk_level=1
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_explicitly_trusted_system_telemetry_tools_remain_untainted(self):
        """15. Pure system and hardware telemetry tools are KNOWN_TRUSTED and do not taint the turn."""
        for tool_name in (
            "get_current_time_and_date",
            "get_battery_status",
            "get_system_vitals",
            "get_pipeline_latency_stats",
            "get_voice_settings",
        ):
            self.tracker.reset_turn()
            classification = self.tracker.classify_tool_ingestion(tool_name)
            self.assertEqual(
                classification,
                IngestionTrustClassification.KNOWN_TRUSTED,
                f"Certified telemetry tool '{tool_name}' must be classified as KNOWN_TRUSTED",
            )
            decision = self.tracker.evaluate_tool_execution(tool_name, {})
            self.assertTrue(decision.allowed)
            self.assertFalse(self.tracker.is_tainted, f"Executing '{tool_name}' must NOT taint the turn")

    def test_custom_skill_cannot_declare_trusted_data_provenance(self):
        """16. Custom tools cannot declare trusted data provenance at registration or runtime."""
        # 1. Custom skill declaring trusted_system raises PermissionError at registration
        with self.assertRaises(PermissionError) as ctx:
            @SkillRegistry.register(
                name="malicious_custom_skill_claiming_trusted_data",
                origin="custom",
                data_provenance="trusted_system",
            )
            def evil_custom():
                return "attacker_data"
        self.assertIn("cannot declare trusted data provenance", str(ctx.exception))

        # 2. Even if metadata was fabricated with data_provenance='trusted_system', non-builtin origin fails closed
        fake_tool_name = "test_fabricated_custom_tool"

        @SkillRegistry.register(
            name=fake_tool_name,
            origin="custom",
            external_ingestion=False,
        )
        def fake_fn():
            return "fake"

        try:
            # Overwrite metadata object's data_provenance attribute to simulate tampering
            meta = SkillRegistry.get_metadata(fake_tool_name)
            if meta:
                object.__setattr__(meta, "data_provenance", "trusted_system")
            # Classification MUST still return UNKNOWN because origin is 'custom'
            classification = self.tracker.classify_tool_ingestion(fake_tool_name)
            self.assertEqual(
                classification,
                IngestionTrustClassification.UNKNOWN,
                "Non-builtin tool cannot receive KNOWN_TRUSTED regardless of data_provenance value",
            )
        finally:
            SkillRegistry.unregister(fake_tool_name)

    def test_document_level_provenance_dynamic_taint(self):
        """17. Document tools are KNOWN_TRUSTED at boundary but dynamically taint on untrusted document."""
        # Boundary classification is KNOWN_TRUSTED (delegates to runtime doc provenance)
        self.assertEqual(
            self.tracker.classify_tool_ingestion("read_archived_document"),
            IngestionTrustClassification.KNOWN_TRUSTED,
        )
        self.assertEqual(
            self.tracker.classify_tool_ingestion("search_archived_documents"),
            IngestionTrustClassification.KNOWN_TRUSTED,
        )

        # Dynamic tainting: when document has untrusted provenance, turn is tainted
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)
        self.tracker.mark_tainted(
            source="archived_document:42",
            metadata={"title": "web_scrape_doc", "source_provenance": "web_scrape"},
        )
        self.assertTrue(self.tracker.is_tainted)
        self.assertIn("archived_document:42", self.tracker.tainting_sources)

    def test_speculative_taint_failure_locks_down_turn_and_blocks_mutation(self):
        """18. When speculative taint propagation fails, turn is compromised and subsequent mutations blocked."""
        from jarvis.speculative.engine import SpeculativeExecutionEngine

        spec_engine = SpeculativeExecutionEngine.get_instance()

        # Step 1: Establish clean turn
        self.tracker.reset_turn()
        self.assertFalse(self.tracker.is_tainted)
        self.assertFalse(self.tracker.is_compromised)

        # Step 2: Speculate a tool call
        key = spec_engine.speculate("get_current_time_and_date")
        self.assertIsNotNone(key)

        # Step 3: Induce failure in record_ingestion during claim
        with (
            patch.object(self.tracker, "is_untrusted_ingestion", return_value=True),
            patch.object(self.tracker, "record_ingestion", side_effect=RuntimeError("Disk write failure")),
            self.assertRaises(SecurityBoundaryViolation),
        ):
            spec_engine.execute_or_claim("get_current_time_and_date")

        # Step 4: Verify turn is now COMPROMISED and TAINTED
        self.assertTrue(self.tracker.is_compromised, "Turn must be marked compromised after speculative claim failure")
        self.assertTrue(self.tracker.is_tainted, "Turn must be marked tainted after speculative claim failure")

        # Step 5: Verify subsequent Level 1 mutation in same turn is DENIED
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(
                "execute_sandboxed_powershell", {"command": "dir"}, risk_level=1
            )
        self.assertIn("Critical Authorization Gate", str(ctx.exception))

    def test_grant_scoped_to_logical_turn_and_revoked_on_reset(self):
        """19. Dual-Key grants are bound to the logical turn; unconsumed grants are revoked on reset_turn()."""
        # Step 1: Turn 1 issues an approval grant
        turn1_id = self.tracker.get_current_turn_id()
        self.assertIsNotNone(turn1_id)

        grant = self.approval_mgr.grant_approval(
            "execute_sandboxed_powershell", {"command": "Get-Service"}
        )
        self.assertEqual(grant.turn_id, turn1_id)
        self.assertTrue(
            self.approval_mgr.has_valid_grant("execute_sandboxed_powershell", {"command": "Get-Service"})
        )

        # Step 2: Turn 2 begins via reset_turn() without consuming Turn 1's grant
        self.tracker.reset_turn()
        turn2_id = self.tracker.get_current_turn_id()
        self.assertNotEqual(turn1_id, turn2_id)

        # Step 3: In Turn 2, the unconsumed grant from Turn 1 MUST be invalid / revoked
        self.assertFalse(
            self.approval_mgr.has_valid_grant("execute_sandboxed_powershell", {"command": "Get-Service"}),
            "Grant from Turn 1 must not be valid in Turn 2",
        )
        consumed = self.approval_mgr.consume_grant_object(
            "execute_sandboxed_powershell", {"command": "Get-Service"}
        )
        self.assertIsNone(consumed, "Grant from Turn 1 must not be consumable in Turn 2")

    def test_grant_turn_id_mismatch_and_argument_tampering_rejected(self):
        """20. Grants with mismatched turn_id or modified arguments cannot be consumed."""
        # 1. Grant bound to explicit turn_A cannot be consumed in turn_B
        grant = self.approval_mgr.grant_approval(
            "execute_sandboxed_powershell", {"command": "dir"}, turn_id="turn_A"
        )
        self.assertEqual(grant.turn_id, "turn_A")

        # Attempt consumption with mismatched turn_id
        consumed = self.approval_mgr.consume_grant_object(
            "execute_sandboxed_powershell", {"command": "dir"}, turn_id="turn_B"
        )
        self.assertIsNone(consumed, "Mismatched turn_id must be rejected")

        # 2. Consumption with modified arguments cannot consume the grant
        consumed_diff_args = self.approval_mgr.consume_grant_object(
            "execute_sandboxed_powershell", {"command": "rmdir /s /q C:\\"}, turn_id="turn_A"
        )
        self.assertIsNone(consumed_diff_args, "Modified arguments must be rejected")

        # 3. Consumption with different tool name cannot consume the grant
        consumed_diff_tool = self.approval_mgr.consume_grant_object(
            "write_file", {"command": "dir"}, turn_id="turn_A"
        )
        self.assertIsNone(consumed_diff_tool, "Different tool name must be rejected")

        # 4. Legitimate consumption with exact tool, arguments, and turn_A succeeds
        valid_consumed = self.approval_mgr.consume_grant_object(
            "execute_sandboxed_powershell", {"command": "dir"}, turn_id="turn_A"
        )
        self.assertIsNotNone(valid_consumed)
        self.assertEqual(valid_consumed.grant_id, grant.grant_id)

        # 5. Subsequent re-use fails (single-use)
        reused = self.approval_mgr.consume_grant_object(
            "execute_sandboxed_powershell", {"command": "dir"}, turn_id="turn_A"
        )
        self.assertIsNone(reused, "Single-use grant cannot be consumed twice")
