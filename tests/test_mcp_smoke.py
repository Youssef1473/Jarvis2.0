"""Opt-in integration smoke tests for configured MCP servers in Jarvis 2.0.

These tests invoke live external MCP processes and services. They are guarded by the
JARVIS_RUN_MCP_SMOKE=1 environment variable to keep default local CI deterministic,
offline, and isolated.
"""
import os
import sys
import time
import unittest

from jarvis.core.mcp_manager import MCPClientSession


@unittest.skipUnless(
    os.environ.get("JARVIS_RUN_MCP_SMOKE") == "1",
    "Opt-in integration smoke test; set JARVIS_RUN_MCP_SMOKE=1 to run live external MCP processes",
)
class TestMCPLiveSmokeSuite(unittest.TestCase):
    """Live smoke tests with explicit timeouts and reliable teardown."""

    def test_context7_harmless_invocation(self):
        """Context7: Verify live initialization, tool discovery, and harmless library resolution."""
        session = MCPClientSession(
            name="context7_smoke",
            transport="stdio",
            command="npx",
            args=["-y", "@upstash/context7-mcp@4.4.4"],
        )
        proc = None
        try:
            init_res = session.initialize(timeout=15.0)
            self.assertIn("serverInfo", init_res.get("result", {}))

            tools = session.list_tools(timeout=15.0)
            self.assertGreater(len(tools), 0)
            tool_names = [t.get("name") for t in tools]
            self.assertIn("resolve-library-id", tool_names)

            proc = session._proc

            # Harmless read-only documentation resolution call
            res = session.call_tool(
                "resolve-library-id",
                arguments={"libraryName": "fastapi", "query": "routing"},
                timeout=15.0,
            )
            self.assertIsInstance(res, dict)
            self.assertIn("content", res)
        finally:
            session.close()
            if proc:
                self.assertIsNotNone(proc.poll(), "Context7 subprocess was not cleanly reaped.")

    def test_github_missing_token_bounded_failure(self):
        """GitHub: Verify initialization, tool discovery, and that unauthenticated invocation fails within bounded timeout."""
        session = MCPClientSession(
            name="github_smoke",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
        )
        proc = None
        try:
            # 1. Verify initialization succeeds without credentials
            init_res = session.initialize(timeout=15.0)
            server_info = init_res.get("result", {}).get("serverInfo", {})
            self.assertIn("name", server_info)
            self.assertEqual(server_info.get("name"), "github-mcp-server")

            # 2. Verify tool discovery succeeds without credentials
            tools = session.list_tools(timeout=15.0)
            self.assertGreaterEqual(len(tools), 20)
            tool_names = [t.get("name") for t in tools]
            self.assertIn("search_repositories", tool_names)

            proc = session._proc

            # 3. Unauthenticated invocation: octokit fails to authenticate and times out deterministically
            t_start = time.monotonic()
            with self.assertRaises(TimeoutError) as ctx:
                session.call_tool(
                    "search_repositories",
                    arguments={"query": "jarvis"},
                    timeout=5.0,
                )
            elapsed = time.monotonic() - t_start
            self.assertLessEqual(elapsed, 8.0, f"GitHub timeout took too long: {elapsed:.2f}s")
            self.assertIn("timed out after 5.0s", str(ctx.exception))
        finally:
            session.close()
            if proc:
                self.assertIsNotNone(proc.poll(), "GitHub subprocess was not cleanly reaped.")

    def test_sequential_thinking_live_invocation(self):
        """Sequential Thinking: Verify live initialization, tool discovery, and thought step execution."""
        session = MCPClientSession(
            name="sequential_thinking_smoke",
            transport="stdio",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-sequential-thinking@2.0.0"],
        )
        proc = None
        try:
            init_res = session.initialize(timeout=15.0)
            self.assertIn("serverInfo", init_res.get("result", {}))

            tools = session.list_tools(timeout=15.0)
            tool_names = [t.get("name") for t in tools]
            self.assertIn("sequentialthinking", tool_names)

            proc = session._proc

            res = session.call_tool(
                "sequentialthinking",
                arguments={
                    "thought": "Smoke test verifying MCP pipeline integrity.",
                    "thoughtNumber": 1,
                    "totalThoughts": 1,
                    "nextThoughtNeeded": False,
                },
                timeout=15.0,
            )
            self.assertIsInstance(res, dict)
            self.assertIn("content", res)
        finally:
            session.close()
            if proc:
                self.assertIsNotNone(proc.poll(), "Sequential thinking subprocess was not cleanly reaped.")

    def test_everything_search_status_and_dependency(self):
        """Everything Search: Verify initialization, discovery, and harmless check_status query."""
        session = MCPClientSession(
            name="everything_smoke",
            transport="stdio",
            command="npx",
            args=["-y", "everything-search-mcp@1.1.0"],
        )
        proc = None
        try:
            init_res = session.initialize(timeout=15.0)
            self.assertIn("serverInfo", init_res.get("result", {}))

            tools = session.list_tools(timeout=15.0)
            tool_names = [t.get("name") for t in tools]
            self.assertIn("check_status", tool_names)
            self.assertIn("search_files", tool_names)

            proc = session._proc

            # Harmless check_status tool call
            res = session.call_tool("check_status", {}, timeout=5.0)
            self.assertIsInstance(res, dict)
            self.assertIn("content", res)
        finally:
            session.close()
            if proc:
                self.assertIsNotNone(proc.poll(), "Everything subprocess was not cleanly reaped.")

    def test_gemini_docs_live_invocation(self):
        """Gemini Docs: Verify HTTP initialization, tool discovery, and public documentation search."""
        session = MCPClientSession(
            name="gemini_docs_smoke",
            transport="http",
            endpoint_url="https://gemini-api-docs-mcp.dev",
            headers={"MCP-Protocol-Version": "2024-11-05"},
        )
        try:
            init_res = session.initialize(timeout=10.0)
            server_info = init_res.get("result", {}).get("serverInfo", {})
            self.assertEqual(server_info.get("name"), "gemini-docs")

            tools = session.list_tools(timeout=10.0)
            tool_names = [t.get("name") for t in tools]
            self.assertIn("gemini_search_docs", tool_names)
            self.assertIn("gemini_get_doc", tool_names)

            # Public search tool invocation
            res = session.call_tool(
                "gemini_search_docs",
                arguments={"query": "generateContent", "limit": 2},
                timeout=10.0,
            )
            self.assertIsInstance(res, dict)
            self.assertFalse(res.get("isError", False))
            self.assertIn("content", res)
        finally:
            session.close()

    def test_chrome_devtools_discovery_and_containment(self):
        """Chrome DevTools: Verify live initialization, tool discovery, and Job Object containment."""
        session = MCPClientSession(
            name="chrome_devtools_smoke",
            transport="stdio",
            command="npx",
            args=[
                "-y",
                "chrome-devtools-mcp@1.12.0",
                "--headless",
                "--no-usage-statistics",
                "-e",
                os.path.expandvars("%LOCALAPPDATA%\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
            ],
        )
        proc = None
        try:
            init_res = session.initialize(timeout=20.0)
            self.assertIn("serverInfo", init_res.get("result", {}))

            tools = session.list_tools(timeout=15.0)
            self.assertGreater(len(tools), 0)
            tool_names = [t.get("name") for t in tools]
            self.assertIn("list_pages", tool_names)
            self.assertIn("evaluate_script", tool_names)

            if sys.platform == "win32":
                self.assertIsNotNone(session._job_handle, "Job Object handle was not assigned to Chrome DevTools.")

            proc = session._proc
        finally:
            session.close()
            if proc:
                self.assertIsNotNone(proc.poll(), "Chrome DevTools subprocess was not cleanly reaped.")


if __name__ == "__main__":
    unittest.main()
