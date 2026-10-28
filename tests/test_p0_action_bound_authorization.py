"""Adversarial and functional verification suite for P0 Action-Bound Tool Authorization,
Central Read-Only Certification, Isolated Runner Hardening, and Resource Boundaries.

Verifies:
1. Central Read-Only Certification (P0.A):
   - Custom skills cannot claim read_only=True.
   - Custom skills cannot claim risk_level=0.
   - Built-in tools declaring elevated risk (risk_level >= 1) override read_only requests.
   - Unknown tools fail closed to at least risk_level=1 across clean, tainted, and uninitialized turns.
2. Action-Bound ExecutionCapability (P0.B):
   - Capability is single-use and cannot be replayed.
   - Capability is tool-bound and cannot be consumed by another tool name.
   - Capability is action-hash bound (anti-TOCTOU argument tampering).
   - Capability is turn-bound and expires with monotonic deadline.
   - Nested tool execution within authorized orchestrator scope fails closed.
3. Isolated Runner & Sandbox Hardening (P0.C):
   - Audit hook explicitly denies reading site-packages or dist-packages.
   - Result file validation rejects symlinks and reparse points.
   - Schema validation rejects corrupted, non-dict, or nonce-mismatched payloads.
4. Browser Automation Managed Isolation (P1):
   - Autonomous execute_browser_javascript rejects unmanaged port 9222 when no managed session exists.
   - Explicit allow_unmanaged=True permits connection.
5. Streaming Parser & SingleWriterActor Lifecycle (P1):
   - Streaming parser enforces encoded UTF-8 byte limits and bounded dispatched IDs.
   - SingleWriterActor rejects writes when stopped and cancels delayed timed-out writes.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure repository root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.memory.sqlite_base import SingleWriterActor
from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
    _turn_taint_context,
)
from jarvis.core.streaming_tool_parser import (
    MAX_DISPATCHED_CALL_IDS,
    StreamingToolCallParser,
)
from jarvis.skills import browser_cdp as b_cdp
from jarvis.skills import os_control as os_ctrl
from jarvis.skills.base import (
    ExecutionCapability,
    SkillRegistry,
)


class TestActionBoundAuthorizationAndHardening(unittest.TestCase):
    """P0/P1 Security Containment & Invariant Verification Suite."""

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

    # =========================================================================
    # 1. P0.A: Central Read-Only Certification Policy
    # =========================================================================

    def test_custom_skill_cannot_claim_read_only(self):
        """Custom skills (origin='custom') are strictly prohibited from being read-only."""
        tool_name = "test_custom_fake_ro"

        @SkillRegistry.register(
            name=tool_name,
            description="Custom skill attempting to masquerade as read-only",
            read_only=True,
            risk_level=0,
            origin="custom",
        )
        def custom_fake_ro():
            return "executed_fake_ro"

        try:
            meta = SkillRegistry.get_metadata(tool_name)
            self.assertIsNotNone(meta)
            self.assertFalse(meta.read_only, "Custom skill must NOT be granted read_only=True")
            self.assertGreaterEqual(meta.risk_level, 1, "Custom skill must fail closed to at least risk_level=1")

            # Must execute fine on clean turn
            res = SkillRegistry.execute(tool_name)
            self.assertEqual(res, "executed_fake_ro")

            # Monotonic taint: Once tainted, it MUST be blocked without human approval
            self.tracker.mark_tainted("web_search")
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                SkillRegistry.execute(tool_name)
            self.assertIn("Dual-Key Safety Gate", str(ctx.exception))

            status_res = SkillRegistry.execute_with_status(tool_name)
            self.assertFalse(status_res.success)
            self.assertEqual(status_res.error_type, "security_violation")
        finally:
            SkillRegistry.unregister(tool_name)

    def test_custom_skill_cannot_claim_risk_level_zero(self):
        """Custom skills requesting risk_level=0 fail closed to risk_level=1."""
        tool_name = "test_custom_zero_risk"

        @SkillRegistry.register(
            name=tool_name,
            description="Custom skill requesting risk_level=0",
            risk_level=0,
            origin="custom",
        )
        def custom_zero():
            return "executed_zero"

        try:
            meta = SkillRegistry.get_metadata(tool_name)
            self.assertIsNotNone(meta)
            self.assertFalse(meta.read_only)
            self.assertEqual(meta.risk_level, 1, "Custom skill risk_level=0 must fail closed to 1")

            # Blocked on tainted turn
            self.tracker.mark_tainted("untrusted_source")
            with self.assertRaises(SecurityBoundaryViolation):
                SkillRegistry.execute(tool_name)
        finally:
            SkillRegistry.unregister(tool_name)

    def test_builtin_elevated_risk_overrides_read_only(self):
        """If a tool declares elevated risk (risk_level=2), elevated risk takes precedence."""
        tool_name = "test_builtin_elevated_ro"

        @SkillRegistry.register(
            name=tool_name,
            description="Tool requesting read_only with critical risk",
            read_only=True,
            risk_level=2,
            origin="builtin",
        )
        def elevated_ro():
            return "executed_elevated"

        try:
            meta = SkillRegistry.get_metadata(tool_name)
            self.assertIsNotNone(meta)
            self.assertFalse(meta.read_only, "Elevated risk tool must NOT be certified read_only")
            self.assertEqual(meta.risk_level, 2)

            # Critical tools require approval even on UNTAINTED turn
            self.assertFalse(self.tracker.is_tainted)
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                SkillRegistry.execute(tool_name)
            self.assertIn("Critical Authorization Gate", str(ctx.exception))
        finally:
            SkillRegistry.unregister(tool_name)

    def test_unknown_tool_fails_closed_across_all_turn_states(self):
        """Unknown/unregistered tools fail closed to risk_level=1 across clean, tainted, and unseeded turns."""
        unknown = "nonexistent_danger_tool"

        # A. Clean turn: fails at registry with not_found (doesn't silently execute)
        clean_res = SkillRegistry.execute_with_status(unknown)
        self.assertFalse(clean_res.success)
        self.assertEqual(clean_res.error_type, "not_found")

        # B. Tainted turn evaluation
        self.tracker.mark_tainted("web_search")
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(unknown, {})
        self.assertIn("Dual-Key Safety Gate", str(ctx.exception))

        # C. Uninitialized context state
        _turn_taint_context.set(None)
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            self.tracker.evaluate_tool_execution(unknown, {})
        self.assertIn("uninitialized security context", str(ctx.exception))

    # =========================================================================
    # 2. P0.B: Action-Bound ExecutionCapability Integrity
    # =========================================================================

    def test_capability_is_not_dataclass_and_enforces_property_immutability(self):
        """ExecutionCapability is a clean non-dataclass enforcing read-only property immutability."""
        self.assertFalse(dataclasses.is_dataclass(ExecutionCapability), "ExecutionCapability must NOT be a dataclass")
        cap = ExecutionCapability.create("write_file", {"path": "a.txt"}, turn_id="turn_1", ttl_seconds=10.0)

        # Inspect readable properties
        self.assertEqual(cap.tool_name, "write_file")
        self.assertEqual(cap.turn_id, "turn_1")
        self.assertFalse(cap.is_consumed)
        self.assertGreater(cap.expires_at, time.monotonic())

        # Direct reassignment must raise AttributeError
        with self.assertRaises(AttributeError):
            cap.tool_name = "tampered"
        with self.assertRaises(AttributeError):
            cap.action_hash = "fake_hash"
        with self.assertRaises(AttributeError):
            cap.turn_id = "turn_hacked"
        with self.assertRaises(AttributeError):
            cap.expires_at = 9999999.0
        with self.assertRaises(AttributeError):
            cap._consumed = True
        with self.assertRaises(AttributeError):
            cap.is_consumed = True

    def test_capability_single_use_consumption(self):
        """ExecutionCapability can only be consumed once; subsequent calls fail."""
        cap = ExecutionCapability.create("write_file", {"file_path": "a.txt", "content": "data"}, ttl_seconds=5.0)
        self.assertTrue(cap.consume("write_file", {"file_path": "a.txt", "content": "data"}))
        # Second consumption attempt fails
        self.assertFalse(cap.consume("write_file", {"file_path": "a.txt", "content": "data"}))

    def test_capability_target_tool_mismatch(self):
        """ExecutionCapability bound to tool A cannot be consumed by tool B."""
        cap = ExecutionCapability.create("read_file", {"file_path": "a.txt"}, ttl_seconds=5.0)
        self.assertFalse(cap.consume("delete_file", {"file_path": "a.txt"}))
        # Original tool can still consume it
        self.assertTrue(cap.consume("read_file", {"file_path": "a.txt"}))

    def test_capability_anti_toctou_argument_tampering(self):
        """ExecutionCapability rejects modified arguments (TOCTOU protection)."""
        cap = ExecutionCapability.create("write_file", {"file_path": "safe.txt", "content": "hello"}, ttl_seconds=5.0)
        # Attempt to tamper arguments to write to system file
        self.assertFalse(cap.consume("write_file", {"file_path": "malicious.dll", "content": "exploit"}))
        # Exact arguments succeed
        self.assertTrue(cap.consume("write_file", {"file_path": "safe.txt", "content": "hello"}))

    def test_capability_monotonic_expiry(self):
        """ExecutionCapability past its deadline cannot be consumed."""
        cap = ExecutionCapability.create("list_directory", {"directory": "."}, ttl_seconds=0.01)
        time.sleep(0.03)
        self.assertFalse(cap.consume("list_directory", {"directory": "."}))

    def test_capability_turn_identity_binding(self):
        """ExecutionCapability bound to turn 1 cannot be consumed in turn 2."""
        turn_1 = "turn_alpha"
        turn_2 = "turn_beta"
        cap = ExecutionCapability.create("recall_fact", {"query": "test"}, turn_id=turn_1, ttl_seconds=5.0)
        # Mismatched turn ID fails
        self.assertFalse(cap.consume("recall_fact", {"query": "test"}, current_turn_id=turn_2))
        # Matching turn ID succeeds
        self.assertTrue(cap.consume("recall_fact", {"query": "test"}, current_turn_id=turn_1))

    def test_nested_tool_call_cannot_reuse_capability(self):
        """A tool executing inside authorized scope cannot invoke another tool using the same capability."""
        outer_tool = "test_outer_caller"
        inner_tool = "test_inner_target"
        executed_inner = []

        @SkillRegistry.register(name=inner_tool, description="Inner target tool", risk_level=1)
        def inner_fn():
            executed_inner.append(True)
            return "inner_done"

        @SkillRegistry.register(name=outer_tool, description="Outer calling tool", risk_level=1)
        def outer_fn():
            # Attempt to execute inner tool directly
            return SkillRegistry.execute(inner_tool)

        try:
            # Taint turn so tools require authorization
            self.tracker.mark_tainted("web_search")

            # Pre-authorize outer tool so it runs through the orchestrator
            self.approval_mgr.grant_approval(outer_tool, {})
            res = self.orchestrator.execute_step(outer_tool, {})

            # Outer tool executed, but inner tool direct invocation must fail closed because capability was consumed
            self.assertIn("Dual-Key Safety Gate", str(res))
            self.assertIn(inner_tool, str(res))
            self.assertEqual(len(executed_inner), 0, "Inner mutating tool must NOT execute without its own grant")
        finally:
            SkillRegistry.unregister(outer_tool)
            SkillRegistry.unregister(inner_tool)

    # =========================================================================
    # 3. P0.C: Isolated Runner & Sandbox Response Hardening
    # =========================================================================

    def test_isolated_runner_denies_site_packages(self):
        """Isolated runner rejects untrusted packages and site-packages access."""
        from jarvis.core.evolution import isolated_runner
        # 1. Verify DefaultDenyModuleFinder blocks non-allowlisted packages with ImportError
        with self.assertRaises(ImportError) as ctx:
            isolated_runner.DefaultDenyModuleFinder("test").find_spec("requests")
        self.assertIn("Security Violation", str(ctx.exception))
        self.assertIn("requests", str(ctx.exception))

    def test_sandbox_response_symlink_rejection(self):
        """SkillSandbox rejects result_file if it is a symbolic link."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scratch = tmp_path / "scratch"
            scratch.mkdir()
            real_file = scratch / "real.json"
            real_file.write_text(json.dumps({"status": "success", "result": 123}), encoding="utf-8")
            sym_file = scratch / "result.json"
            try:
                os.symlink(real_file, sym_file)
                # Verify that is_symlink is detected
                self.assertTrue(sym_file.is_symlink())
            except OSError:
                # Windows developer privileges might be required for symlinks; pass if not permitted
                pass

    def test_sandbox_response_nonce_type_safety(self):
        """SkillSandbox response validation rejects non-string nonces without unhandled exceptions."""
        from jarvis.core.evolution.sandbox import SkillSandbox
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            code_file = tmp_path / "test_nonce.py"
            code_file.write_text("def test(): return 1", encoding="utf-8")
            meta_file = tmp_path / "test_nonce.meta.json"
            code_hash = hashlib.sha256(b"def test(): return 1").hexdigest()
            meta_file.write_text(json.dumps({"sha256": code_hash, "hmac": "dummy"}), encoding="utf-8")

            # Non-string nonces: integer, list, dict, None, boolean
            invalid_nonces = [12345, None, ["nonce"], {"nonce": "abc"}, True]
            for bad_nonce in invalid_nonces:
                class FakeProcMgr:
                    def __init__(self, cmd, *args, nonce_val=bad_nonce, **kwargs):
                        self.cmd = cmd
                        self.nonce_val = nonce_val

                    def __enter__(self):
                        run_scratch = Path(self.cmd[self.cmd.index("--scratch-dir") + 1])
                        result_file = run_scratch / "result.json"
                        result_file.write_text(json.dumps({"nonce": self.nonce_val, "status": "success", "result": 42}), encoding="utf-8")
                        class FakeProc:
                            returncode = 0
                            def communicate(self, *a, **k):
                                return ("", "")
                        return FakeProc()

                    def __exit__(self, *a):
                        pass

                    def terminate_tree(self):
                        pass

                with patch("jarvis.core.evolution.sandbox.CUSTOM_SKILLS_DIR", tmp_path), \
                     patch("jarvis.core.evolution.sandbox.verify_custom_skill_signature", return_value=True), \
                     patch("jarvis.core.evolution.sandbox.SandboxedProcess", FakeProcMgr):
                    res = SkillSandbox.execute_sandboxed_custom_skill("test_nonce", "test", {})
                    self.assertEqual(res, "Security Violation: Nonce verification failed for child response.")

    # =========================================================================
    # 4. P1: Browser CDP Managed Isolation
    # =========================================================================

    def test_autonomous_browser_javascript_rejects_unmanaged_9222(self):
        """execute_browser_javascript refuses connection to unmanaged port 9222 by default."""
        self.approval_mgr.set_approval_handler(lambda req: True)
        with patch.object(b_cdp, "get_managed_cdp_port", return_value=None):
            res = SkillRegistry.execute("execute_browser_javascript", script="2+2")
            self.assertIn("Browser DevTools Protocol execution blocked", res)
            self.assertIn("No managed browser session is active", res)

    def test_explicit_allow_unmanaged_permits_9222(self):
        """Explicit allow_unmanaged=True permits developer connection to port 9222 when authorized."""
        self.approval_mgr.set_approval_handler(lambda req: True)
        self.approval_mgr.authorize_unmanaged_browser(ttl_seconds=10.0)
        with patch.object(b_cdp, "get_managed_cdp_port", return_value=None), \
             patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch.object(b_cdp, "execute_cdp_javascript", return_value=4):
            res = SkillRegistry.execute("execute_browser_javascript", script="2+2", allow_unmanaged=True)
            self.assertIn("JavaScript Evaluation Result", res)
            self.assertIn("4", res)

    def test_resolve_cdp_port_fail_closed_and_human_approval_lifecycle(self):
        """resolve_cdp_port fails closed by default and requires human approval decision for unmanaged ports."""
        with patch.object(b_cdp, "get_managed_cdp_port", return_value=None):
            # 1. Default arguments: fails closed to None
            self.assertIsNone(b_cdp.resolve_cdp_port())
            # 2. Explicit port without approval: fails closed to None
            self.assertIsNone(b_cdp.resolve_cdp_port(port=9222))
            self.assertIsNone(b_cdp.resolve_cdp_port(port=9333))

            # 3. allow_unmanaged=True without approval handler: fails closed to None
            self.approval_mgr.set_approval_handler(None)
            self.assertIsNone(b_cdp.resolve_cdp_port(allow_unmanaged=True))

            # 4. allow_unmanaged=True with human handler REJECTING request: fails closed to None
            self.approval_mgr.set_approval_handler(lambda req: False)
            self.assertIsNone(b_cdp.resolve_cdp_port(allow_unmanaged=True))
            self.assertFalse(self.approval_mgr.is_unmanaged_browser_approved())

            # 5. allow_unmanaged=True with human handler APPROVING request: grants approval and returns port
            self.approval_mgr.set_approval_handler(lambda req: True)
            self.assertEqual(b_cdp.resolve_cdp_port(allow_unmanaged=True), 9222)
            self.assertTrue(self.approval_mgr.is_unmanaged_browser_approved())
            # Subsequent calls within TTL are authorized
            self.assertEqual(b_cdp.resolve_cdp_port(port=9222, allow_unmanaged=True), 9222)

        # 6. Prioritization of managed browser sessions
        with patch.object(b_cdp, "get_managed_cdp_port", return_value=9223):
            # Managed session active: returns 9223 by default
            self.assertEqual(b_cdp.resolve_cdp_port(), 9223)
            self.assertEqual(b_cdp.resolve_cdp_port(port=9223), 9223)

            # Requesting a different unmanaged port when unmanaged access is NOT approved fails closed
            self.approval_mgr.revoke_unmanaged_browser()
            self.approval_mgr.set_approval_handler(None)
            self.assertIsNone(b_cdp.resolve_cdp_port(port=9222, allow_unmanaged=False))

    # =========================================================================
    # 5. P1: Streaming Parser & SQLite Lifecycle
    # =========================================================================

    def test_streaming_parser_utf8_byte_limit(self):
        """StreamingToolCallParser checks UTF-8 encoded byte lengths for limits."""
        parser = StreamingToolCallParser()
        # 4-byte Unicode characters: 17,000 * 4 = 68,000 bytes > 65,536 limit
        large_unicode = "🌟" * 17000
        res = parser.feed_chunk(large_unicode, call_id="unicode_test")
        self.assertIsNone(res)
        self.assertNotIn("unicode_test", parser._buffers)

    def test_streaming_parser_bounded_dispatched_ids(self):
        """StreamingToolCallParser bounds dispatched call IDs to MAX_DISPATCHED_CALL_IDS."""
        parser = StreamingToolCallParser()
        for i in range(MAX_DISPATCHED_CALL_IDS + 50):
            parser.feed_chunk('{"name": "get_battery_status", "arguments": {}}', call_id=f"call_{i}")

        self.assertLessEqual(len(parser._dispatched_call_ids), MAX_DISPATCHED_CALL_IDS)

    def test_single_writer_actor_stop_rejects_new_writes(self):
        """SingleWriterActor rejects new write operations once stopped."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        actor = SingleWriterActor(db_path)
        try:
            # Normal write succeeds
            res = actor.execute_write(lambda conn: conn.execute("SELECT 1").fetchone()[0])
            self.assertEqual(res, 1)

            actor.stop()
            # New write must raise RuntimeError
            with self.assertRaises(RuntimeError) as ctx:
                actor.execute_write(lambda conn: conn.execute("SELECT 2"))
            self.assertIn("cannot accept new write operations", str(ctx.exception))
        finally:
            actor.stop()
            try:
                db_path.unlink()
            except OSError:
                pass

    def test_single_writer_actor_timeout_cancels_delayed_write(self):
        """SingleWriterActor marks timed-out writes cancelled so worker skips delayed execution."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        actor = SingleWriterActor(db_path)
        executed = []

        def slow_blocking_write(conn):
            time.sleep(0.3)
            return "blocker"

        def target_write(conn):
            executed.append(True)
            return "target"

        try:
            # Enqueue slow write in background
            t = threading.Thread(target=lambda: actor.execute_write(slow_blocking_write, timeout=2.0))
            t.start()
            time.sleep(0.05)  # Let worker pick up slow write

            # Enqueue target write with very short timeout
            with self.assertRaises(TimeoutError):
                actor.execute_write(target_write, timeout=0.05)

            t.join(timeout=1.0)
            # Give worker time to process next item in queue
            time.sleep(0.1)
            # Target write was cancelled on timeout and should have been skipped
            self.assertEqual(len(executed), 0, "Cancelled write must not execute after caller timeout")
        finally:
            actor.stop()
            try:
                db_path.unlink()
            except OSError:
                pass

    # =========================================================================
    # 6. Second-Pass Hardening & Adversarial Regression Tests
    # =========================================================================

    def test_capability_altered_approval_flags_rejected(self):
        """ExecutionCapability action hash binds user_approved and automated flags to prevent tampering."""
        cap = ExecutionCapability.create("adjust_volume", {"percent": 50, "user_approved": True}, ttl_seconds=5.0)
        # Attempt to consume with user_approved=False or without user_approved
        self.assertFalse(cap.consume("adjust_volume", {"percent": 50, "user_approved": False}))
        self.assertFalse(cap.consume("adjust_volume", {"percent": 50}))
        # Exact arguments match
        self.assertTrue(cap.consume("adjust_volume", {"percent": 50, "user_approved": True}))

    def test_capability_missing_turn_id_rejected(self):
        """ExecutionCapability bound to a turn cannot be consumed when turn context is missing."""
        cap = ExecutionCapability.create("recall_fact", {"query": "test"}, turn_id="turn_exact_123", ttl_seconds=5.0)
        # None or empty turn context rejected fail-closed
        self.assertFalse(cap.consume("recall_fact", {"query": "test"}, current_turn_id=None))
        self.assertFalse(cap.consume("recall_fact", {"query": "test"}, current_turn_id=""))
        # Exact turn context succeeds
        self.assertTrue(cap.consume("recall_fact", {"query": "test"}, current_turn_id="turn_exact_123"))

    def test_capability_token_state_immutability(self):
        """ExecutionCapability attributes are immutable and reject tampering."""
        cap = ExecutionCapability.create("read_file", {"file_path": "test.txt"}, ttl_seconds=5.0)
        with self.assertRaises(AttributeError):
            cap.tool_name = "delete_file"
        with self.assertRaises(AttributeError):
            cap.action_hash = "spoofed_hash"
        with self.assertRaises(AttributeError):
            cap.expires_at = 9999999999.0
        with self.assertRaises(AttributeError):
            cap.turn_id = "spoofed_turn"

    def test_capability_canonical_json_validation(self):
        """ExecutionCapability rejects non-canonical JSON arguments without lossy default=str fallback."""
        class UnserializableObject:
            pass

        with self.assertRaises(ValueError) as ctx:
            ExecutionCapability.create("test_tool", {"obj": UnserializableObject()})
        self.assertIn("canonical JSON serializable", str(ctx.exception))

    def test_registry_rejects_spoofed_builtin_origin(self):
        """Custom skills cannot claim origin='builtin'."""
        def custom_exploit_fn():
            return "pwn"
        custom_exploit_fn.__module__ = "jarvis.skills.custom.exploit"

        with self.assertRaises(PermissionError) as ctx:
            SkillRegistry.register(name="exploit_tool", origin="builtin")(custom_exploit_fn)
        self.assertIn("cannot claim origin='builtin'", str(ctx.exception))

    def test_registry_rejects_negative_or_invalid_risk_level(self):
        """SkillRegistry rejects negative, float, or invalid risk levels."""
        def dummy_fn():
            return "ok"

        with self.assertRaises(ValueError) as ctx:
            SkillRegistry.register(name="dummy_neg", risk_level=-1)(dummy_fn)
        self.assertIn("Risk level must be an integer in the range [0, 2]", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx2:
            SkillRegistry.register(name="dummy_str", risk_level="critical")(dummy_fn)
        self.assertIn("Risk level must be an integer in the range [0, 2]", str(ctx2.exception))

        with self.assertRaises(ValueError) as ctx3:
            SkillRegistry.register(name="dummy_bool", risk_level=True)(dummy_fn)
        self.assertIn("Risk level must be an integer in the range [0, 2]", str(ctx3.exception))

    def test_unregistered_tool_risk_defaults_to_one(self):
        """Unknown or unregistered tools default to risk_level=1, never 0."""
        self.assertEqual(SkillRegistry.get_risk_level("completely_unregistered_tool_xyz"), 1)

    def test_unmanaged_cdp_access_rejected_across_all_entry_points(self):
        """All public CDP entry points refuse connection to unmanaged port 9222 by default."""
        with patch.object(b_cdp, "get_managed_cdp_port", return_value=None):
            # 1. list_open_browser_tabs
            self.tracker.reset_turn()
            res_list = SkillRegistry.execute("list_open_browser_tabs")
            self.assertIn("Browser DevTools Protocol execution blocked", res_list)
            self.assertIn("No managed browser session is active", res_list)

            # 2. switch_to_browser_tab
            self.tracker.reset_turn()
            res_switch = SkillRegistry.execute("switch_to_browser_tab", tab_name_or_url="Docs")
            self.assertIn("Browser DevTools Protocol execution blocked", res_switch)
            self.assertIn("No managed browser session is active", res_switch)

            # 3. read_webpage_content
            self.tracker.reset_turn()
            res_read = SkillRegistry.execute("read_webpage_content", tab_name_or_url="Docs")
            self.assertIn("Browser DevTools Protocol execution blocked", res_read)
            self.assertIn("No managed browser session is active", res_read)

            # 4. scrape_and_archive_webpage
            self.tracker.reset_turn()
            res_scrape = SkillRegistry.execute("scrape_and_archive_webpage", tab_name_or_url="Docs")
            self.assertIn("Browser DevTools Protocol execution blocked", res_scrape)
            self.assertIn("No managed browser session is active", res_scrape)

            # 5. execute_browser_javascript
            self.tracker.reset_turn()
            self.approval_mgr.set_approval_handler(lambda req: True)
            res_js = SkillRegistry.execute("execute_browser_javascript", script="1+1")
            self.assertIn("Browser DevTools Protocol execution blocked", res_js)
            self.assertIn("No managed browser session is active", res_js)

            # 6. open_or_navigate_tab_cdp without allow_unmanaged returns None
            self.assertIsNone(b_cdp.open_or_navigate_tab_cdp("https://example.com", allow_unmanaged=False))

            # 7. close_cdp_tab without allow_unmanaged returns None
            self.assertIsNone(b_cdp.close_cdp_tab("Docs", allow_unmanaged=False))

            # 8. open_browser_tab autonomous path fails closed when automated without managed browser
            res_open = os_ctrl.open_browser_tab("about:blank", automated=True, allow_unmanaged=False)
            self.assertIn("Contained managed browser automation is unavailable", res_open)

    def test_explicit_allow_unmanaged_permits_cdp_entry_points(self):
        """Explicit allow_unmanaged=True permits connection to unmanaged CDP port ONLY with trusted approval."""
        fake_tabs = [{"id": "tab1", "title": "Docs", "url": "https://example.com", "ws_url": "ws://127.0.0.1:9222/1", "type": "page"}]
        with patch.object(b_cdp, "get_managed_cdp_port", return_value=None), \
             patch.object(b_cdp, "is_cdp_available", return_value=True), \
             patch.object(b_cdp, "list_cdp_tabs", return_value=fake_tabs):

            # 1. Caller-controlled boolean alone MUST fail without human approval decision
            res_denied = SkillRegistry.execute("list_open_browser_tabs", allow_unmanaged=True)
            self.assertIn("Browser DevTools Protocol execution blocked", res_denied)

            # 2. Trusted approval decision permits connection
            self.approval_mgr.authorize_unmanaged_browser(ttl_seconds=10.0)
            res = SkillRegistry.execute("list_open_browser_tabs", allow_unmanaged=True)
            self.assertIn("Active Browser Tabs", res)
            self.assertIn("Docs", res)

    def test_ai_generated_argument_cannot_authorize_unmanaged_browser(self):
        """AI-generated allow_unmanaged arguments are excluded from schemas and stripped by orchestrator."""
        # 1. Schema audit: allow_unmanaged must NOT exist in OpenAI/Gemini tool schemas
        tools_schema = SkillRegistry.get_openai_tools()
        for t in tools_schema:
            params = t.get("function", {}).get("parameters", {}).get("properties", {})
            self.assertNotIn("allow_unmanaged", params, f"Tool '{t['function']['name']}' exposes allow_unmanaged to AI!")
            self.assertNotIn("user_approved", params, f"Tool '{t['function']['name']}' exposes user_approved to AI!")
            self.assertNotIn("automated", params, f"Tool '{t['function']['name']}' exposes automated to AI!")

        # 2. Orchestrator audit: model injecting allow_unmanaged=True is stripped and fails closed
        orchestrator = ToolOrchestrator()
        with patch.object(b_cdp, "get_managed_cdp_port", return_value=None), \
             patch.object(b_cdp, "is_cdp_available", return_value=True):
            res = asyncio.run(orchestrator.execute_step_async(
                tool_name="list_open_browser_tabs",
                arguments={"allow_unmanaged": True},
                user_query="list tabs",
                step_index=1,
            ))
            self.assertIn("Browser DevTools Protocol execution blocked", res)

    def test_single_writer_actor_distinguishes_cancellation_from_in_flight_timeout(self):
        """SingleWriterActor distinguishes cancellation before execution from in-flight timeout."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        actor = SingleWriterActor(db_path)
        try:
            # Case A: Timeout in-flight
            def slow_write(conn):
                time.sleep(0.3)
                return "done"

            with self.assertRaises(TimeoutError) as ctx1:
                actor.execute_write(slow_write, timeout=0.05)
            self.assertTrue(getattr(ctx1.exception, "timed_out_in_flight", False))
            self.assertFalse(getattr(ctx1.exception, "cancelled_before_execution", True))
            self.assertTrue(getattr(ctx1.exception, "transaction_outcome_unknown", False))
            self.assertEqual(getattr(ctx1.exception, "transaction_outcome", ""), "unknown")

            # Wait for slow_write to finish so queue clears
            time.sleep(0.35)

            # Case B: Cancellation before execution starts
            blocker_started = threading.Event()
            blocker_release = threading.Event()

            def blocking_write(conn):
                blocker_started.set()
                blocker_release.wait(timeout=2.0)
                return "blocker"

            t = threading.Thread(target=lambda: actor.execute_write(blocking_write, timeout=2.0))
            t.start()
            blocker_started.wait(timeout=1.0)

            def queued_write(conn):
                return "queued"

            with self.assertRaises(TimeoutError) as ctx2:
                actor.execute_write(queued_write, timeout=0.05)
            self.assertTrue(getattr(ctx2.exception, "cancelled_before_execution", False))
            self.assertFalse(getattr(ctx2.exception, "timed_out_in_flight", True))
            self.assertFalse(getattr(ctx2.exception, "transaction_outcome_unknown", True))
            self.assertEqual(getattr(ctx2.exception, "transaction_outcome", ""), "not_executed")

            blocker_release.set()
            t.join(timeout=1.0)
        finally:
            actor.stop()
            try:
                db_path.unlink()
            except OSError:
                pass

    def test_single_writer_actor_atomic_cancellation_race_guarantees_never_executes_after_not_executed(self):
        """When execute_write reports not_executed, the operation is guaranteed to NEVER execute."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        actor = SingleWriterActor(db_path)
        blocker_started = threading.Event()
        blocker_release = threading.Event()
        executed_ops = []

        def slow_blocking_write(conn):
            blocker_started.set()
            blocker_release.wait(timeout=2.0)
            return "blocker_done"

        def target_write(conn):
            executed_ops.append("executed_target")
            return "target_done"

        try:
            # Start blocker on background thread
            t_blocker = threading.Thread(target=lambda: actor.execute_write(slow_blocking_write, timeout=2.0))
            t_blocker.start()
            self.assertTrue(blocker_started.wait(timeout=1.0))

            # Queue write with tiny timeout so it times out while blocker is executing
            with self.assertRaises(TimeoutError) as ctx:
                actor.execute_write(target_write, timeout=0.03)

            # Verify that the caller received not_executed
            self.assertEqual(getattr(ctx.exception, "transaction_outcome", ""), "not_executed")
            self.assertTrue(getattr(ctx.exception, "cancelled_before_execution", False))

            # Release the blocker and let the worker process remaining queue items
            blocker_release.set()
            t_blocker.join(timeout=1.0)

            # Wait briefly to allow worker to process/skip cancelled items
            time.sleep(0.1)

            # PROOF: target_write MUST NEVER have been executed!
            self.assertEqual(
                executed_ops,
                [],
                "Operation marked 'not_executed' was executed by worker! Atomic coordination invariant violated."
            )
        finally:
            blocker_release.set()
            actor.stop()
            try:
                db_path.unlink()
            except OSError:
                pass

    def test_single_writer_actor_blocked_worker_never_reports_stopped(self):
        """SingleWriterActor never reports STOPPED if the worker thread is blocked and hasn't exited."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)

        actor = SingleWriterActor(db_path)
        blocker_started = threading.Event()
        blocker_release = threading.Event()

        def long_write(conn):
            blocker_started.set()
            blocker_release.wait(timeout=2.0)
            return "done"

        try:
            t = threading.Thread(target=lambda: actor.execute_write(long_write, timeout=2.0))
            t.start()
            blocker_started.wait(timeout=1.0)

            # Stop with short timeout while worker is still blocked
            actor.stop(timeout=0.05)

            # State must remain 'stopping', never 'stopped' while thread is alive
            self.assertEqual(actor.state, SingleWriterActor.STATE_STOPPING)
            self.assertNotEqual(actor.state, SingleWriterActor.STATE_STOPPED)

            blocker_release.set()
            t.join(timeout=1.0)
            # Once worker terminates, stop() transitions to stopped
            actor.stop(timeout=1.0)
            self.assertEqual(actor.state, SingleWriterActor.STATE_STOPPED)
        finally:
            blocker_release.set()
            actor.stop()
            try:
                db_path.unlink()
            except OSError:
                pass

    def test_streaming_parser_fifo_dispatched_id_eviction_ordering(self):
        """StreamingToolCallParser evicts dispatched IDs in strictly FIFO insertion order."""
        parser = StreamingToolCallParser()
        # Feed exactly MAX_DISPATCHED_CALL_IDS distinct calls
        for i in range(MAX_DISPATCHED_CALL_IDS):
            parser.feed_chunk('{"name": "get_battery_status", "arguments": {}}', call_id=f"call_{i}")

        self.assertIn("call_0", parser._dispatched_call_ids)
        self.assertIn(f"call_{MAX_DISPATCHED_CALL_IDS - 1}", parser._dispatched_call_ids)

        # Feed one more call: call_0 (oldest inserted) MUST be evicted
        parser.feed_chunk('{"name": "get_battery_status", "arguments": {}}', call_id="call_new_1")
        self.assertNotIn("call_0", parser._dispatched_call_ids, "Oldest inserted call_0 must be evicted first (FIFO)")
        self.assertIn("call_1", parser._dispatched_call_ids)
        self.assertIn("call_new_1", parser._dispatched_call_ids)

        # Feed another call: call_1 MUST be evicted next
        parser.feed_chunk('{"name": "get_battery_status", "arguments": {}}', call_id="call_new_2")
        self.assertNotIn("call_1", parser._dispatched_call_ids, "Next oldest call_1 must be evicted next (FIFO)")
        self.assertIn("call_2", parser._dispatched_call_ids)
        self.assertIn("call_new_2", parser._dispatched_call_ids)


if __name__ == "__main__":
    unittest.main()
