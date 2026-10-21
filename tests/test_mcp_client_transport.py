"""Unit tests for Native Stateless HTTP and Job-Sandboxed Stdio MCP Client in Jarvis 2.0."""
import json
import sys
import time
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch

from jarvis.core.mcp_manager import (
    MCPClientSession,
    MCPManager,
    register_discovered_mcp_tools,
    MCP_PROTOCOL_VERSION,
)
from jarvis.core.security_boundaries import TurnTaintTracker, SecurityBoundaryViolation
from jarvis.skills.base import SkillRegistry


class TestMCPClientHTTPTransport(unittest.TestCase):
    """Tests Stateless HTTP transport: headers, caching, SSRF gating, and tool execution."""

    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()

    @patch("jarvis.core.mcp_manager.validate_egress_url")
    @patch("urllib.request.urlopen")
    def test_http_handshake_and_caching(self, mock_urlopen, mock_validate_url):
        mock_validate_url.return_value = "http://127.0.0.1:8000/mcp"

        # Mock responses for initialize, tools/list, and tools/call
        mock_resp = MagicMock()
        mock_resp.read.side_effect = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "test-srv"}}}).encode("utf-8"),
            json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"tools": [{"name": "echo", "description": "Echoes input", "inputSchema": {}}]},
            }).encode("utf-8"),
        ]
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        session = MCPClientSession(
            name="test_http",
            transport="http",
            endpoint_url="http://127.0.0.1:8000/mcp",
            tool_cache_ttl_sec=2.0,
        )

        init_res = session.initialize()
        self.assertIn("serverInfo", init_res.get("result", {}))

        # First list_tools call performs network request
        tools1 = session.list_tools()
        self.assertEqual(len(tools1), 1)
        self.assertEqual(tools1[0]["name"], "echo")

        # Second list_tools call within TTL uses cached tools without network request
        tools2 = session.list_tools()
        self.assertEqual(len(tools2), 1)
        self.assertEqual(mock_urlopen.call_count, 2)  # 1 for init, 1 for tools/list

        # Verify outgoing headers contained protocol version
        last_req = mock_urlopen.call_args[0][0]
        self.assertEqual(last_req.headers.get("Mcp-protocol-version"), MCP_PROTOCOL_VERSION)

    @patch("jarvis.core.mcp_manager.validate_egress_url")
    def test_http_egress_ssrf_blocked(self, mock_validate_url):
        mock_validate_url.side_effect = SecurityBoundaryViolation("SSRF blocked")
        session = MCPClientSession(
            name="malicious",
            transport="http",
            endpoint_url="http://169.254.169.254/latest/meta-data/",
        )
        with self.assertRaises(SecurityBoundaryViolation) as ctx:
            session.initialize()
        self.assertIn("SSRF blocked", str(ctx.exception))


class TestMCPClientStdioTransport(unittest.TestCase):
    """Tests Job-Sandboxed Stdio subprocess transport: lifecycle, E-core pinning, and stderr drain."""

    def test_stdio_roundtrip_and_cleanup(self):
        # A simple Python echo server script speaking JSON-RPC 2.0 lines
        helper_code = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    data = json.loads(line)\n"
            "    method = data.get('method')\n"
            "    req_id = data.get('id')\n"
            "    sys.stderr.write(f'Log: received {method}\\n')\n"
            "    sys.stderr.flush()\n"
            "    if method == 'initialize':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'status': 'ready'}}\n"
            "    elif method == 'tools/list':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': [{'name': 'compute', 'description': 'Computes math'}]}}\n"
            "    elif method == 'tools/call':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'output': 42}}\n"
            "    else:\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'error': 'unknown'}\n"
            "    sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "    sys.stdout.flush()\n"
        )

        session = MCPClientSession(
            name="mock_stdio",
            transport="stdio",
            command=[sys.executable, "-u", "-c", helper_code],
        )

        try:
            init_res = session.initialize(timeout=3.0)
            self.assertEqual(init_res.get("result", {}).get("status"), "ready")

            tools = session.list_tools(timeout=3.0)
            self.assertEqual(len(tools), 1)
            self.assertEqual(tools[0]["name"], "compute")

            call_res = session.call_tool("compute", {"expr": "6*7"}, timeout=3.0)
            self.assertEqual(call_res.get("output"), 42)

            # Wait briefly for stderr drain thread to log
            time.sleep(0.05)
            self.assertTrue(len(session._stderr_buffer) > 0)
        finally:
            session.close()

        # Confirm process is terminated cleanly
        self.assertIsNone(session._proc)

    def test_stdio_timeout_raises(self):
        # Helper script that never responds
        helper_code = (
            "import sys, time\n"
            "for line in sys.stdin:\n"
            "    time.sleep(2.0)\n"
        )

        session = MCPClientSession(
            name="slow_stdio",
            transport="stdio",
            command=[sys.executable, "-u", "-c", helper_code],
        )

        try:
            with self.assertRaises(TimeoutError):
                session.initialize(timeout=0.25)
        finally:
            session.close()


class TestMCPToolRegistrationAndTaint(unittest.TestCase):
    """Tests dynamic SkillRegistry integration and TurnTaintTracker tainting."""

    def setUp(self):
        self.tracker = TurnTaintTracker.get_instance()
        self.tracker.reset_turn()

    def test_tool_registration_and_tainting(self):
        session = MagicMock(spec=MCPClientSession)
        session.name = "github"
        session.list_tools.return_value = [
            {"name": "create-issue", "description": "Creates a GitHub issue"}
        ]
        session.call_tool.return_value = {"issue_id": 101}

        registered = register_discovered_mcp_tools(session)
        self.assertEqual(registered, ["mcp_github_create_issue"])
        self.addCleanup(lambda: SkillRegistry.unregister("mcp_github_create_issue"))

        self.assertIn("mcp_github_create_issue", SkillRegistry.get_all_tools())
        self.assertEqual(SkillRegistry.get_risk_level("mcp_github_create_issue"), 1)

        # Before calling: turn is not tainted
        self.assertFalse(self.tracker.is_tainted)

        # Execute registered MCP tool
        res = SkillRegistry.execute("mcp_github_create_issue", title="Bug report")
        self.assertEqual(res, {"issue_id": 101})

        # After calling: turn is marked as tainted!
        self.assertTrue(self.tracker.is_tainted)


class TestMCPManager(unittest.TestCase):
    """Verifies MCPManager lifecycle and session shutdown."""

    def test_manager_lifecycle(self):
        mgr = MCPManager()
        s1 = mgr.connect_http("test1", "http://127.0.0.1:8080/mcp")
        self.assertEqual(mgr.get_session("test1"), s1)

        mgr.shutdown_all()
        self.assertEqual(mgr.active_sessions, {})
        self.assertTrue(s1._is_closed)

    def test_load_servers_config(self):
        mgr = MCPManager()
        servers = mgr.load_servers_config()
        self.assertIsInstance(servers, dict)
        self.assertIn("everything", servers)
        self.assertIn("github", servers)
        self.assertIn("context7", servers)
        self.assertIn("sequential_thinking", servers)
        self.assertTrue(servers["everything"]["enabled"])
        self.assertTrue(servers["github"]["enabled"])
        self.assertTrue(servers["context7"]["enabled"])
        self.assertTrue(servers["sequential_thinking"]["enabled"])

    @patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_mocktoken123"}, clear=False)
    @patch("jarvis.core.mcp_manager.register_discovered_mcp_tools")
    def test_start_configured_servers_mocked(self, mock_reg):
        mock_reg.return_value = ["mcp_everything_search", "mcp_github_issues"]

        mgr = MCPManager()
        with patch.object(mgr, "connect_stdio") as mock_connect:
            mock_session = MagicMock()
            mock_connect.return_value = mock_session

            res = mgr.start_configured_servers(auto_register_skills=True)

            self.assertIn("everything", res)
            self.assertIn("github", res)
            self.assertIn("context7", res)
            self.assertIn("sequential_thinking", res)
            self.assertIn("chrome_devtools", res)
            self.assertIn("gemini_docs", res)
            self.assertEqual(mock_connect.call_count, 5)

            # Check that GITHUB_TOKEN environment variable was expanded
            calls = mock_connect.call_args_list
            github_call = [c for c in calls if c[0][0] == "github"][0]
            passed_env = github_call[1].get("env", {})
            self.assertEqual(passed_env.get("GITHUB_PERSONAL_ACCESS_TOKEN"), "ghp_mocktoken123")

    @patch("subprocess.Popen")
    @patch("jarvis.core.mcp_manager._setup_win32_job_object")
    def test_stdio_executable_resolution_and_env_merging(self, mock_job, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        mock_job.return_value = 9999

        session = MCPClientSession(
            name="test_cmd",
            transport="stdio",
            command="python",
            args=["-m", "mock_module"],
            env={"CUSTOM_KEY": "custom_val_${TEST_MCP_VAR}"},
        )

        with patch.dict("os.environ", {"TEST_MCP_VAR": "resolved_val"}, clear=False):
            session._ensure_stdio_process()

        self.assertTrue(mock_popen.called)
        called_args = mock_popen.call_args[0][0]
        called_env = mock_popen.call_args[1].get("env", {})

        # Ensure executable was resolved or present
        self.assertTrue(len(called_args) >= 3)
        # Ensure custom key was expanded
        self.assertEqual(called_env.get("CUSTOM_KEY"), "custom_val_resolved_val")
        # Ensure system env (e.g. PATH) was preserved
        self.assertIn("PATH", called_env)

        session.close()


if __name__ == "__main__":
    unittest.main()
