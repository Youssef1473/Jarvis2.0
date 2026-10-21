"""Unit tests for newly integrated plugin skills: Modern Web Guidance, Gemini Docs MCP, and Chrome DevTools."""
import json
import unittest
from unittest.mock import MagicMock, patch

from jarvis.core.mcp_manager import MCPClientSession, MCPManager, register_discovered_mcp_tools
from jarvis.skills.base import SkillRegistry
import jarvis.skills.modern_web as modern_web
import jarvis.skills.gemini_intelligence as gemini_intel
import jarvis.skills.chrome_devtools as chrome_devtools


class TestPluginIntegrations(unittest.TestCase):
    """Tests the integration of plugins into Jarvis 2.0 SkillRegistry and MCP architecture."""

    def test_skill_registry_registration(self):
        """Verifies that all new plugin tools are properly registered in SkillRegistry."""
        registered_tools = SkillRegistry._tools
        self.assertIn("search_modern_web_guidance", registered_tools)
        self.assertIn("retrieve_modern_web_guide", registered_tools)
        self.assertIn("search_gemini_api_documentation", registered_tools)
        self.assertIn("get_gemini_documentation_chunk", registered_tools)
        self.assertIn("get_gemini_live_architecture_overview", registered_tools)
        self.assertIn("audit_webpage_accessibility", registered_tools)
        self.assertIn("audit_webpage_performance", registered_tools)
        self.assertIn("capture_devtools_snapshot", registered_tools)

    @patch("subprocess.run")
    def test_modern_web_guidance_search(self, mock_subproc):
        """Tests search_modern_web_guidance formats CLI results properly."""
        mock_output = json.dumps([
            {
                "id": "declarative-dialog-popover-control",
                "description": "Toggle dialog or popover with buttons.",
                "category": "ui-behaviors",
                "featuresUsed": ["Popover", "<dialog>"]
            }
        ])
        mock_subproc.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        res = modern_web.search_modern_web_guidance("dialog modal", limit=1)
        self.assertIn("declarative-dialog-popover-control", res)
        self.assertIn("Toggle dialog or popover with buttons.", res)

    @patch("subprocess.run")
    def test_modern_web_guidance_retrieve(self, mock_subproc):
        """Tests retrieve_modern_web_guide returns guide content."""
        guide_text = "Use invoker commands to toggle dialogs."
        mock_subproc.return_value = MagicMock(returncode=0, stdout=guide_text, stderr="")

        res = modern_web.retrieve_modern_web_guide("declarative-dialog-popover-control")
        self.assertIn("Modern Web Guidance: declarative-dialog-popover-control", res)
        self.assertIn("Use invoker commands to toggle dialogs.", res)

    @patch("urllib.request.urlopen")
    def test_gemini_docs_mcp_sse_parsing(self, mock_urlopen):
        """Tests that MCPClientSession correctly parses SSE streams from gemini-api-docs-mcp.dev."""
        sse_body = (
            b"event: message\n"
            b"data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"tools\":[{\"name\":\"gemini_search_docs\",\"description\":\"Search docs\"}]}}\n"
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = sse_body
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        sess = MCPClientSession(
            name="test_gemini",
            transport="http",
            endpoint_url="https://gemini-api-docs-mcp.dev",
            headers={"MCP-Protocol-Version": "2024-11-05"},
        )
        tools = sess.list_tools()
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "gemini_search_docs")

    @patch("urllib.request.urlopen")
    def test_gemini_search_docs_skill(self, mock_urlopen):
        """Tests search_gemini_api_documentation skill with mocked MCP response."""
        data_payload = {
            "hits": [
                {
                    "chunk_id": "python-sdk/README.md#create-client",
                    "heading": "Create Client",
                    "breadcrumb": "python-sdk > README.md",
                    "snippet": "client = genai.Client()"
                }
            ]
        }
        sse_body = (
            b"event: message\n"
            b"data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"content\":[{\"type\":\"text\",\"text\":"
            + json.dumps(json.dumps(data_payload)).encode("utf-8")
            + b"}]}}\n"
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = sse_body
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = gemini_intel.search_gemini_api_documentation("create client")
        self.assertIn("Create Client", res)
        self.assertIn("python-sdk/README.md#create-client", res)

    def test_gemini_live_spec_skill(self):
        """Tests that get_gemini_live_architecture_overview returns structured specs."""
        overview = gemini_intel.get_gemini_live_architecture_overview()
        self.assertIn("Gemini 3.8 Live API Architecture Reference", overview)
        self.assertIn("WebSocket", overview)
        self.assertIn("Barge-in", overview)


if __name__ == "__main__":
    unittest.main()
