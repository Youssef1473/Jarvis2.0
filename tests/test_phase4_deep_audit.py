"""Phase 4: Deep Authorization-Path & Sandbox Audit Verification Suite.

Comprehensive adversarial regression suite covering:
- Finding 1: MCP subprocess environment scrubbing (host secret stripping).
- Finding 2: MCP tool risk classification, trusted read-only policy, and clean-turn write gating.
- Finding 3: Tool cancellation actuation, Job Object drain, and confirmed stopped semantics.
- Finding 4: Tool alias canonicalization consistency and immutable registry proxy.
- Finding 5: Strict metadata read-only in DualTrackCoordinator (no prefix override).
- Finding 6: E-Core affinity fallback warning and degraded affinity tracking.
- Finding 7: AST loop-scoped break analysis and reflection aliasing prevention.
- Finding 8: Isolated child runner early nonce and kwargs type validation (code 4).
- Finding 9: Speculative timeout background future cancellation and stale metrics.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, patch

from jarvis.core.cancellation import ToolCancellationToken
from jarvis.core.evolution import isolated_runner
from jarvis.core.evolution.sandbox import SkillSandbox
from jarvis.core.mcp_manager import (
    MCP_BASE_ENV_ALLOWLIST,
    MCPClientSession,
    register_discovered_mcp_tools,
)
from jarvis.core.pipeline.dual_track import DualTrackCoordinator
from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
from jarvis.core.security_boundaries import (
    DualKeyApprovalManager,
    SecurityBoundaryViolation,
    TurnTaintTracker,
)
from jarvis.skills.base import SkillRegistry
from jarvis.skills.job_object_sandbox import (
    is_affinity_degraded,
    run_job_sandboxed_process,
)
from jarvis.speculative.engine import (
    SpeculativeEntry,
    SpeculativeExecutionEngine,
    ValidationSnapshot,
)


class TestPhase4MCPEnvironmentScrubbing(unittest.TestCase):
    """Finding 1: MCP subprocesses must not inherit parent host secrets or credentials."""

    def test_mcp_base_env_allowlist_excludes_credentials(self):
        """Allowlist must strictly omit host secrets and user API keys."""
        forbidden_keys = {
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "DATABASE_URL",
            "SECRET_KEY",
            "PRIVATE_KEY",
        }
        for key in forbidden_keys:
            self.assertNotIn(key, MCP_BASE_ENV_ALLOWLIST)

    def test_mcp_stdio_launcher_strips_host_secrets(self):
        """MCP subprocess launch environment retains only allowlisted keys plus declared server overlay."""
        session = MCPClientSession(
            name="test_stdio_server",
            transport="stdio",
            command=["python", "-c", "import sys; sys.exit(0)"],
            env={"CUSTOM_SERVER_TOKEN": "declared_token_123"},
        )

        with patch.dict(os.environ, {
            "SYSTEMROOT": r"C:\Windows",
            "PATH": r"C:\Windows\System32",
            "OPENAI_API_KEY": "sk-secret-do-not-leak",
            "GITHUB_TOKEN": "ghp_super_secret_token",
            "DATABASE_URL": "postgres://user:pass@localhost:5432/db",
        }, clear=True), patch("subprocess.Popen") as mock_popen, \
             patch("jarvis.core.mcp_manager._setup_win32_job_object", return_value=12345), \
             patch("jarvis.skills.job_object_sandbox.resume_process", return_value=True), \
             patch("shutil.which", side_effect=lambda x: x):

            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc._handle = 999
            mock_popen.return_value = mock_proc

            session._ensure_stdio_process()

            self.assertTrue(mock_popen.called)
            _, kwargs = mock_popen.call_args
            child_env = kwargs.get("env", {})

            # Prohibited parent secrets must NOT be present
            self.assertNotIn("OPENAI_API_KEY", child_env)
            self.assertNotIn("GITHUB_TOKEN", child_env)
            self.assertNotIn("DATABASE_URL", child_env)

            # Safe OS primitives must be present
            self.assertIn("SYSTEMROOT", child_env)
            self.assertIn("PATH", child_env)

            # Declared overlay must be present
            self.assertEqual(child_env.get("CUSTOM_SERVER_TOKEN"), "declared_token_123")


class TestPhase4MCPToolRiskAndGate(unittest.TestCase):
    """Finding 2: MCP tool risk classification and first-call write gating on clean turns."""

    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()
        self.approval_mgr = DualKeyApprovalManager.get_instance()

    def tearDown(self):
        self.tracker.reset_turn()

    def test_consequential_mcp_tool_classified_risk_level_2(self):
        """Consequential / destructive MCP tools default to risk_level=2 (Critical)."""
        session = MagicMock(spec=MCPClientSession)
        session.name = "cloud_ops"
        session.list_tools.return_value = [
            {"name": "delete_remote_resource", "description": "Deletes a remote cloud resource"},
            {"name": "drop_database_table", "description": "Drops a SQL table"},
            {"name": "purge_cdn_cache", "description": "Purges cached assets"},
        ]

        registered = register_discovered_mcp_tools(session)
        self.addCleanup(lambda: [SkillRegistry.unregister(r) for r in registered])

        for reg_name in registered:
            meta = SkillRegistry.get_metadata(reg_name)
            self.assertIsNotNone(meta)
            self.assertEqual(meta.risk_level, 2, f"Tool {reg_name} should have risk_level=2")
            self.assertFalse(meta.read_only)

    def test_first_call_remote_write_blocked_on_clean_turn_without_approval(self):
        """First-call remote write on a clean turn is blocked fail-closed without human approval."""
        session = MagicMock(spec=MCPClientSession)
        session.name = "cloud_ops"
        session.list_tools.return_value = [
            {"name": "delete_remote_resource", "description": "Deletes a remote cloud resource"}
        ]
        registered = register_discovered_mcp_tools(session)
        reg_tool = registered[0]
        self.addCleanup(lambda: SkillRegistry.unregister(reg_tool))

        # Turn is clean / untainted
        self.assertFalse(self.tracker.is_tainted)

        # Direct execution must be blocked fail-closed by Critical Authorization Gate
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            SkillRegistry.execute(reg_tool, resource_id="res-999")
        self.assertIn("Critical Authorization Gate", str(ctx.exception))
        self.assertFalse(session.call_tool.called)

    def test_first_call_remote_write_permitted_with_human_approval(self):
        """First-call remote write executes when out-of-band user approval is granted."""
        session = MagicMock(spec=MCPClientSession)
        session.name = "cloud_ops"
        session.list_tools.return_value = [
            {"name": "delete_remote_resource", "description": "Deletes a remote cloud resource"}
        ]
        session.call_tool.return_value = {"status": "deleted", "id": "res-999"}
        registered = register_discovered_mcp_tools(session)
        reg_tool = registered[0]
        self.addCleanup(lambda: SkillRegistry.unregister(reg_tool))

        self.assertFalse(self.tracker.is_tainted)

        # Grant human approval
        self.approval_mgr.grant_approval(reg_tool, {"resource_id": "res-999"})
        res = SkillRegistry.execute(reg_tool, resource_id="res-999")
        self.assertEqual(res, {"status": "deleted", "id": "res-999"})
        self.assertTrue(session.call_tool.called)

    def test_trusted_read_only_policy_marks_read_only(self):
        """Explicitly policy-certified read-only tools are marked read_only=True with risk_level=1."""
        session = MagicMock(spec=MCPClientSession)
        session.name = "weather_srv"
        session.trusted_read_only_tools = {"get_temperature", "read_forecast"}
        session.list_tools.return_value = [
            {"name": "get_temperature", "description": "Fetches current temperature"},
            {"name": "set_alert_threshold", "description": "Mutates alert threshold"},
        ]

        registered = register_discovered_mcp_tools(session)
        self.addCleanup(lambda: [SkillRegistry.unregister(r) for r in registered])

        ro_meta = SkillRegistry.get_metadata("mcp_weather_srv_get_temperature")
        self.assertIsNotNone(ro_meta)
        self.assertTrue(ro_meta.read_only)
        self.assertEqual(ro_meta.risk_level, 0)

        mut_meta = SkillRegistry.get_metadata("mcp_weather_srv_set_alert_threshold")
        self.assertIsNotNone(mut_meta)
        self.assertFalse(mut_meta.read_only)

    def test_consequential_mcp_mutating_tools_classified_risk_level_2(self):
        """Mutating operations (create_issue, update_record, send_message, publish_release, unknown writes) designated as consequential default to risk_level=2."""
        session = MagicMock(spec=MCPClientSession)
        session.name = "ops_srv"
        session.consequential_tools = {
            "create_issue", "update_record", "send_message", "publish_release", "unknown_custom_mutation"
        }
        session.list_tools.return_value = [
            {"name": "create_issue", "description": "Creates an issue"},
            {"name": "update_record", "description": "Updates a record"},
            {"name": "send_message", "description": "Sends a message"},
            {"name": "publish_release", "description": "Publishes a release"},
            {"name": "unknown_custom_mutation", "description": "Arbitrary external mutation"},
        ]

        registered = register_discovered_mcp_tools(session)
        self.addCleanup(lambda: [SkillRegistry.unregister(r) for r in registered])

        for reg_name in registered:
            meta = SkillRegistry.get_metadata(reg_name)
            self.assertIsNotNone(meta)
            self.assertEqual(meta.risk_level, 2, f"Tool {reg_name} should have risk_level=2")
            self.assertFalse(meta.read_only)

    def test_untrusted_mcp_writes_fail_closed_on_clean_turn_without_approval(self):
        """Untrusted MCP writes (create_issue, update_record, send_message, publish_release, unknown) designated consequential fail closed on clean turns."""
        session = MagicMock(spec=MCPClientSession)
        session.name = "github_srv"
        session.consequential_tools = {
            "create_issue", "update_record", "send_message", "publish_release", "arbitrary_custom_write"
        }
        session.list_tools.return_value = [
            {"name": "create_issue", "description": "Creates an issue"},
            {"name": "update_record", "description": "Updates a record"},
            {"name": "send_message", "description": "Sends a message"},
            {"name": "publish_release", "description": "Publishes a release"},
            {"name": "arbitrary_custom_write", "description": "Unknown mutation"},
        ]
        registered = register_discovered_mcp_tools(session)
        self.addCleanup(lambda: [SkillRegistry.unregister(r) for r in registered])

        self.assertFalse(self.tracker.is_tainted)

        for reg_tool in registered:
            with self.assertRaises(SecurityBoundaryViolation) as ctx:
                SkillRegistry.execute(reg_tool, payload="test_data")
            self.assertIn("Critical Authorization Gate", str(ctx.exception))
            self.assertFalse(session.call_tool.called)

    def test_remote_declaration_alone_cannot_bypass_read_only_certification(self):
        """Remote MCP server declaring read_only=True is rejected unless certified by trusted configuration policy."""
        session = MagicMock(spec=MCPClientSession)
        session.name = "rogue_srv"
        # Remote tool attempts to self-declare read_only
        session.list_tools.return_value = [
            {"name": "send_message", "description": "Sends message", "read_only": True},
            {"name": "arbitrary_action", "description": "Custom action", "read_only": True},
        ]
        registered = register_discovered_mcp_tools(session, trusted_read_only_tools=set())
        self.addCleanup(lambda: [SkillRegistry.unregister(r) for r in registered])

        for reg_tool in registered:
            meta = SkillRegistry.get_metadata(reg_tool)
            self.assertIsNotNone(meta)
            self.assertFalse(meta.read_only, f"Tool {reg_tool} was untrusted but accepted as read-only!")
            self.assertEqual(meta.risk_level, 1)

    def test_mcp_manager_configuration_path_propagates_consequential_tools_end_to_end(self):
        """Server configuration declaring consequential_tools propagates to registration, sets risk 2, and rejects clean-turn write."""
        from jarvis.core.mcp_manager import MCPManager

        mock_session = MagicMock(spec=MCPClientSession)
        mock_session.name = "prod_ops"
        mock_session.list_tools.return_value = [
            {"name": "create_issue", "description": "Creates an issue"},
            {"name": "read_metrics", "description": "Inspects metrics"},
        ]

        # Config declares create_issue as consequential and read_metrics as read-only
        server_config = {
            "mcpServers": {
                "prod_ops": {
                    "transport": "http",
                    "endpoint_url": "http://127.0.0.1:9090",
                    "consequential_tools": ["create_issue"],
                    "trusted_read_only_tools": ["read_metrics"],
                }
            }
        }

        mgr = MCPManager()
        with (
            patch.object(mgr, "load_servers_config", return_value=server_config["mcpServers"]),
            patch.object(mgr, "connect_http", return_value=mock_session) as mock_connect,
        ):
            registered_by_server = mgr.start_configured_servers(auto_register_skills=True)

        self.assertIn("prod_ops", registered_by_server)
        registered_tools = registered_by_server["prod_ops"]
        self.addCleanup(lambda: [SkillRegistry.unregister(t) for t in registered_tools])

        # Verify connect_http received consequential_tools
        mock_connect.assert_called_once()
        self.assertEqual(mock_connect.call_args.kwargs.get("consequential_tools"), ["create_issue"])

        # Verify create_issue registered with risk_level=2
        ci_meta = SkillRegistry.get_metadata("mcp_prod_ops_create_issue")
        self.assertIsNotNone(ci_meta)
        self.assertEqual(ci_meta.risk_level, 2)
        self.assertFalse(ci_meta.read_only)

        # Verify read_metrics registered with risk_level=0 (read-only)
        rm_meta = SkillRegistry.get_metadata("mcp_prod_ops_read_metrics")
        self.assertIsNotNone(rm_meta)
        self.assertEqual(rm_meta.risk_level, 0)
        self.assertTrue(rm_meta.read_only)

        # Verify execution of consequential tool is rejected on a clean turn without approval
        self.assertFalse(self.tracker.is_tainted)
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            SkillRegistry.execute("mcp_prod_ops_create_issue", title="Unapproved Incident")
        self.assertIn("Critical Authorization Gate", str(ctx.exception))
        self.assertFalse(mock_session.call_tool.called)


class TestPhase4TruthfulCancellation(unittest.TestCase):
    """Finding 3: Cancellation semantics and Job Object child process tree cessation."""

    def test_cancel_before_launch_returns_cancelled_confirmed(self):
        """Cancelling before launch reports cancelled_confirmed=True and never spawns process."""
        token = ToolCancellationToken()
        token.cancel()
        self.assertTrue(token.is_cancelled)

        res = run_job_sandboxed_process(
            command=["powershell", "-NoProfile", "-Command", "Start-Sleep -Seconds 5"],
            cancel_token=token,
        )

        self.assertEqual(res.get("status"), "cancelled")
        self.assertTrue(res.get("cancelled_confirmed"))
        self.assertTrue(token.confirmed_stopped)

    def test_orchestrator_returns_cancelled_before_execution(self):
        """ToolOrchestrator rejects pre-cancelled token with failure_category='cancelled'."""
        token = ToolCancellationToken()
        token.cancel()

        orchestrator = ToolOrchestrator()
        orchestrator.cancel_token = token

        res = orchestrator.execute_step_with_status("get_battery_status", {})
        self.assertFalse(res.success)
        self.assertEqual(res.failure_category, "cancelled")
        self.assertIn("Action cancelled", res.result)

    def test_tool_cancellation_token_drain_callbacks(self):
        """ToolCancellationToken triggers registered drain callbacks upon cancel()."""
        token = ToolCancellationToken()
        drained = False

        def _drain_hook():
            nonlocal drained
            drained = True

        token.register_drain_callback(_drain_hook)
        self.assertFalse(drained)

        token.cancel()
        self.assertTrue(drained)
        self.assertTrue(token.cancellation_requested)


class TestPhase4ToolAliasesAndImmutableRegistry(unittest.TestCase):
    """Finding 4: Canonical name resolver across all methods and immutable get_all_tools proxy."""

    def test_aliases_share_exact_canonical_metadata_and_risk(self):
        """Every alias resolves identically to canonical metadata, risk level, and tool callable."""
        alias_pairs = [
            ("remember", "remember_fact"),
            ("recall", "recall_fact"),
            ("store_fact", "remember_fact"),
            ("save_fact", "remember_fact"),
            ("get_fact", "recall_fact"),
            ("find_fact", "recall_fact"),
        ]

        for alias, canonical in alias_pairs:
            canonical_tool = SkillRegistry.get_tool(canonical)
            alias_tool = SkillRegistry.get_tool(alias)
            self.assertEqual(alias_tool, canonical_tool, f"Tool mismatch for alias '{alias}' -> '{canonical}'")

            canonical_meta = SkillRegistry.get_metadata(canonical)
            alias_meta = SkillRegistry.get_metadata(alias)
            self.assertEqual(alias_meta, canonical_meta, f"Metadata mismatch for alias '{alias}' -> '{canonical}'")

            canonical_risk = SkillRegistry.get_risk_level(canonical)
            alias_risk = SkillRegistry.get_risk_level(alias)
            self.assertEqual(alias_risk, canonical_risk, f"Risk mismatch for alias '{alias}' -> '{canonical}'")

            canonical_ingestion = SkillRegistry.is_ingestion_tool(canonical)
            alias_ingestion = SkillRegistry.is_ingestion_tool(alias)
            self.assertEqual(alias_ingestion, canonical_ingestion, f"Ingestion mismatch for alias '{alias}' -> '{canonical}'")

    def test_get_all_tools_returns_immutable_mapping_proxy(self):
        """SkillRegistry.get_all_tools() returns a MappingProxyType that prohibits mutation."""
        tools = SkillRegistry.get_all_tools()
        self.assertIsInstance(tools, MappingProxyType)

        # Attempting dictionary modification must raise TypeError
        with self.assertRaises(TypeError):
            tools["malicious_tool"] = lambda: "evil"

        with self.assertRaises(TypeError):
            del tools["remember_fact"]

        self.assertFalse(hasattr(tools, "pop"))
        self.assertFalse(hasattr(tools, "clear"))


class TestPhase4StrictMetadataReadOnly(unittest.TestCase):
    """Finding 5: DualTrackCoordinator relies strictly on SkillRegistry metadata without prefix bypass."""

    def test_mutating_tool_with_safe_prefix_not_treated_as_read_only(self):
        """A tool named get_and_delete_resource with read_only=False must NOT be considered read_only."""
        tool_name = "get_and_delete_resource"

        @SkillRegistry.register(name=tool_name, description="Mutating resource cleaner", read_only=False, risk_level=1)
        def _mutating_cleaner(resource_id: str = "default"):
            return f"deleted_{resource_id}"

        self.addCleanup(lambda: SkillRegistry.unregister(tool_name))

        # Prefix 'get_' must NOT override explicit read_only=False
        is_ro = DualTrackCoordinator.is_read_only_tool(tool_name)
        self.assertFalse(is_ro, "DualTrackCoordinator incorrectly classified mutating tool as read-only due to prefix!")

    def test_certified_read_only_tool_without_prefix_is_read_only(self):
        """A tool registered with read_only=True without standard prefixes is recognized as read-only."""
        tool_name = "inspect_telemetry_snapshot"

        SkillRegistry.certify_test_read_only_tool(tool_name)

        @SkillRegistry.register(name=tool_name, description="Telemetry snapshot reader", read_only=True)
        def _ro_reader():
            return "telemetry_ok"

        self.addCleanup(lambda: (
            SkillRegistry.unregister(tool_name),
            SkillRegistry.decertify_test_read_only_tool(tool_name),
        ))

        is_ro = DualTrackCoordinator.is_read_only_tool(tool_name)
        self.assertTrue(is_ro)


class TestPhase4AffinityFallbackAndTracking(unittest.TestCase):
    """Finding 6: E-Core affinity fallback detection and degraded state tracking."""

    def test_affinity_degraded_flag_when_no_overlap(self):
        """is_affinity_degraded returns True when requested mask has zero overlap with resolved mask."""
        self.assertTrue(is_affinity_degraded(0xFF00, 0x000F))
        self.assertFalse(is_affinity_degraded(0xFF00, 0xFF00))
        self.assertFalse(is_affinity_degraded(0xFF00, 0xF000))
        self.assertFalse(is_affinity_degraded(0, 0x000F))

    def test_sandboxed_process_reports_affinity_degraded_field(self):
        """run_job_sandboxed_process returns explicit affinity_degraded boolean."""
        res = run_job_sandboxed_process(
            command=["cmd.exe", "/c", "echo test"],
            requested_affinity=0,
            timeout_sec=5.0,
        )
        self.assertIn("affinity_degraded", res)
        self.assertIsInstance(res["affinity_degraded"], bool)


class TestPhase4ASTSecurityAnalysis(unittest.TestCase):
    """Finding 7: Loop-scoped break analysis and reflection aliasing prevention in AST checks."""

    def test_nested_loop_break_does_not_satisfy_outer_while_true(self):
        """A break inside an inner loop does NOT break the outer while True loop and is rejected."""
        malicious_code = """
def run_loop():
    while True:
        for x in range(10):
            if x > 5:
                break
"""
        ok, msg = SkillSandbox.check_ast_security(malicious_code)
        self.assertFalse(ok)
        self.assertIn("Unbounded 'while True' loop detected", msg)

    def test_outer_loop_break_is_accepted(self):
        """A break directly targeting the outer while True loop is accepted."""
        valid_code = """
def run_loop():
    while True:
        for x in range(10):
            pass
        if True:
            break
"""
        ok, msg = SkillSandbox.check_ast_security(valid_code)
        self.assertTrue(ok, f"Expected AST check to pass, but failed with: {msg}")

    def test_reflection_function_aliasing_is_blocked(self):
        """Assigning blocked reflection primitives to local aliases is rejected."""
        aliased_getattr = """
def exploit():
    my_get = getattr
    return my_get
"""
        ok, msg = SkillSandbox.check_ast_security(aliased_getattr)
        self.assertFalse(ok)
        self.assertIn("Aliasing blocked function/module 'getattr'", msg)

    def test_globals_aliasing_is_blocked(self):
        """Assigning globals to a local alias is rejected."""
        aliased_globals = """
def exploit():
    g = globals
    return g
"""
        ok, msg = SkillSandbox.check_ast_security(aliased_globals)
        self.assertFalse(ok)
        self.assertIn("Aliasing blocked function/module 'globals'", msg)


class TestPhase4IsolatedRunnerEarlyValidation(unittest.TestCase):
    """Finding 8: Child runner validates nonce and kwargs types before compiling or touching code."""

    def test_malformed_nonce_rejected_with_exit_code_4(self):
        """Isolated runner exits with code 4 when nonce is non-string or empty without loading skill."""
        runner_path = Path(isolated_runner.__file__).resolve()
        with tempfile.TemporaryDirectory() as tmp_dir:
            res_file = Path(tmp_dir) / "result.json"
            fake_skill = Path(tmp_dir) / "skill.py"
            fake_skill.write_text("raise RuntimeError('Should not execute!')", encoding="utf-8")

            # Nonce is an integer instead of string
            stdin_data = json.dumps({"nonce": 12345, "kwargs": {}})
            cli_args = [
                sys.executable,
                "-I",
                str(runner_path),
                "--module-path", str(fake_skill),
                "--tool-name", "sample_tool",
                "--result-file", str(res_file),
                "--expected-hash", "fakehash",
                "--scratch-dir", str(tmp_dir),
            ]

            proc = subprocess.run(
                cli_args,
                input=stdin_data,
                text=True,
                capture_output=True,
                timeout=5.0,
                check=False,
            )

            self.assertEqual(proc.returncode, 4)
            result_data = json.loads(res_file.read_text(encoding="utf-8"))
            self.assertEqual(result_data.get("status"), "error")
            self.assertIn("'nonce' must be a non-empty string", result_data.get("error", ""))

    def test_malformed_kwargs_rejected_with_exit_code_4(self):
        """Isolated runner exits with code 4 when kwargs is not a dict."""
        runner_path = Path(isolated_runner.__file__).resolve()
        with tempfile.TemporaryDirectory() as tmp_dir:
            res_file = Path(tmp_dir) / "result.json"
            fake_skill = Path(tmp_dir) / "skill.py"
            fake_skill.write_text("raise RuntimeError('Should not execute!')", encoding="utf-8")

            # kwargs is a list instead of dict
            stdin_data = json.dumps({"nonce": "valid_nonce_abc", "kwargs": ["not", "a", "dict"]})
            cli_args = [
                sys.executable,
                "-I",
                str(runner_path),
                "--module-path", str(fake_skill),
                "--tool-name", "sample_tool",
                "--result-file", str(res_file),
                "--expected-hash", "fakehash",
                "--scratch-dir", str(tmp_dir),
            ]

            proc = subprocess.run(
                cli_args,
                input=stdin_data,
                text=True,
                capture_output=True,
                timeout=5.0,
                check=False,
            )

            self.assertEqual(proc.returncode, 4)
            result_data = json.loads(res_file.read_text(encoding="utf-8"))
            self.assertEqual(result_data.get("status"), "error")
            self.assertIn("'kwargs' must be a JSON dictionary", result_data.get("error", ""))


class TestPhase4SpeculativeTimeoutFutureCancellation(unittest.TestCase):
    """Finding 9: Speculative timeout future cancellation, duplicate execution prevention, and stale metric tracking."""

    def test_speculative_timeout_cancels_future_and_increments_stale_metric(self):
        """Speculative execution timeout cancels the background future and increments stale_invalidations."""
        tool_name = "spec_timeout_test_tool"
        SkillRegistry.certify_test_read_only_tool(tool_name)

        @SkillRegistry.register(name=tool_name, read_only=True)
        def _dummy_tool():
            return "speculative_value"

        self.addCleanup(lambda: (
            SkillRegistry.unregister(tool_name),
            SkillRegistry.decertify_test_read_only_tool(tool_name),
        ))

        engine = SpeculativeExecutionEngine(max_workers=4)
        meta = SkillRegistry.get_metadata(tool_name)
        self.assertIsNotNone(meta)

        # Create a mock future that raises TimeoutError on result(timeout=...)
        mock_future = MagicMock()
        mock_future.done.return_value = False
        mock_future.result.side_effect = TimeoutError("Timed out waiting for speculative result")

        entry = SpeculativeEntry(
            cache_key="mock_cache_key",
            future=mock_future,
            snapshot=ValidationSnapshot(scope_epochs={}, file_stamps=()),
            meta=meta,
            bound_args={},
        )

        with engine._entries_lock:
            engine._active_entries["mock_cache_key"] = entry

        initial_stale = engine.metrics.stale_invalidations

        with patch("jarvis.speculative.engine.compute_deterministic_cache_key", return_value="mock_cache_key"):
            with self.assertRaises(TimeoutError) as ctx:
                engine.execute_or_claim(tool_name)
            self.assertIn("duplicate fallback execution blocked", str(ctx.exception))

        # Verify background future was cancelled
        self.assertTrue(mock_future.cancel.called)
        # Verify stale metrics incremented
        self.assertEqual(engine.metrics.stale_invalidations, initial_stale + 1)

    def test_speculative_timeout_blocking_tool_avoids_duplicate_concurrent_execution(self):
        """Verifies timeout handling on a blocking tool uses cooperative cancellation
        and strictly prevents duplicate concurrent executions."""
        tool_name = "cooperative_blocking_spec_tool"
        SkillRegistry.certify_test_read_only_tool(tool_name)

        tool_started = threading.Event()
        tool_cancelled = threading.Event()
        allow_tool_finish = threading.Event()
        execution_count = 0

        @SkillRegistry.register(name=tool_name, read_only=True, default_timeout_ms=50)
        def _blocking_tool():
            nonlocal execution_count
            execution_count += 1
            tool_started.set()
            from jarvis.core.cancellation import get_current_tool_cancellation_token
            tok = get_current_tool_cancellation_token()
            while not allow_tool_finish.is_set():
                if tok and tok.is_cancelled:
                    tool_cancelled.set()
                    return "cancelled"
                time.sleep(0.005)
            return "finished"

        self.addCleanup(lambda: (
            SkillRegistry.unregister(tool_name),
            SkillRegistry.decertify_test_read_only_tool(tool_name),
        ))

        engine = SpeculativeExecutionEngine(max_workers=2)

        try:
            # 1. Speculate ahead in worker thread
            engine.speculate(tool_name)
            self.assertTrue(tool_started.wait(timeout=1.0), "Speculative task did not start in background worker.")
            self.assertEqual(execution_count, 1)

            # 2. Claim with execute_or_claim; times out after 50ms
            timed_out = False
            try:
                engine.execute_or_claim(tool_name)
            except TimeoutError:
                timed_out = True

            self.assertTrue(timed_out, "Expected execute_or_claim to raise TimeoutError on slow speculative task.")

            # Verify cooperative cancellation was observed by the background worker
            self.assertTrue(tool_cancelled.wait(timeout=1.0), "Background worker did not receive cooperative cancellation.")
            # Verify duplicate concurrent execution was strictly avoided (exactly 1 execution)
            self.assertEqual(execution_count, 1)
        finally:
            allow_tool_finish.set()
            engine.shutdown(wait=False)

    def test_speculative_timeout_uncooperative_task_blocks_duplicate_execution(self):
        """Verifies timeout on an uncooperative task raises TimeoutError and blocks duplicate execution."""
        tool_name = "uncooperative_blocking_spec_tool"
        SkillRegistry.certify_test_read_only_tool(tool_name)

        tool_started = threading.Event()
        allow_tool_finish = threading.Event()
        execution_count = 0

        @SkillRegistry.register(name=tool_name, read_only=True, default_timeout_ms=50)
        def _uncooperative_tool():
            nonlocal execution_count
            execution_count += 1
            tool_started.set()
            while not allow_tool_finish.is_set():
                time.sleep(0.005)
            return "finished"

        self.addCleanup(lambda: (
            SkillRegistry.unregister(tool_name),
            SkillRegistry.decertify_test_read_only_tool(tool_name),
        ))

        engine = SpeculativeExecutionEngine(max_workers=2)

        try:
            engine.speculate(tool_name)
            self.assertTrue(tool_started.wait(timeout=1.0))
            self.assertEqual(execution_count, 1)

            # Claiming timed-out uncooperative tool must raise TimeoutError and refuse duplicate launch
            with self.assertRaises(TimeoutError) as ctx:
                engine.execute_or_claim(tool_name)
            self.assertIn("duplicate fallback execution blocked", str(ctx.exception))
            self.assertEqual(execution_count, 1)
        finally:
            allow_tool_finish.set()
            engine.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
