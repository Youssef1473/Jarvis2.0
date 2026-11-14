"""Comprehensive Provider Routing & Distinct Error Policy Audit Suite for Jarvis 2.0.

Audits and verifies:
1. Exact provider selection order under Hybrid and sovereign modes.
2. Distinct error class policies:
   - Quota exhaustion (HTTP 429 / RESOURCE_EXHAUSTED) -> cascade to next tier.
   - Authentication failure (HTTP 401 / 403 / unauthenticated) -> fail closed, no unauthorized cascade.
   - Timeout & cancellation -> bounded waiting, zero duplicate tool executions.
   - Malformed provider responses -> rejected without arbitrary execution.
3. 4-Tier Routing Contract:
   - Tier 1: Antigravity / Gemini 3.8 Flash (Primary)
   - Tier 2: Direct Gemini API (Secondary reasoning failover)
   - Tier 3: GitHub Copilot CLI (Tertiary text-only fallback)
   - Tier 4: Claude Sonnet / Opus (Dynamic quota cascade within Antigravity)
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from jarvis.core.antigravity_bridge import AntigravityBridge
from jarvis.core.brain import JarvisBrain
from jarvis.core.config_loader import load_config
from jarvis.core.github_bridge import GitHubCopilotBridge
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


class TestProviderRoutingAudit(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="jarvis_audit_mem_")
        self.config = load_config()
        self.config["_base_dir"] = self.temp_dir
        self.config.setdefault("memory", {})["db_path"] = os.path.join(self.temp_dir, "test_memory.db")
        self.config.setdefault("llm", {})["antigravity_resolver"] = lambda: None
        self.memory = MemoryManager(self.config)
        self.safety = SafetyGate(self.config)
        self.brain = JarvisBrain(
            self.config,
            self.memory,
            self.safety,
            antigravity_resolver=lambda: None,
        )
        self.brain.active_tool_names = frozenset()
        self.brain.antigravity_auto_tiering = False
        self.brain.antigravity_model = "gemini-3.8-flash-low"

        # Mock bridges for controlled hermetic isolation
        self.mock_agy_bridge = MagicMock()
        self.mock_agy_bridge.is_available.return_value = True
        self.mock_agy_bridge.is_claude_exhausted.return_value = False
        self.mock_agy_bridge.is_gemini_flash_exhausted.return_value = False
        self.mock_agy_bridge._is_token_exhaustion_error.side_effect = (
            lambda err: any(k in (err or "").lower() for k in ("429", "quota", "resource_exhausted", "rate limit", "code 3"))
        )
        self.mock_agy_bridge._is_auth_error.side_effect = AntigravityBridge._is_auth_error
        self.brain.antigravity_bridge = self.mock_agy_bridge

        self.mock_gh_bridge = MagicMock()
        self.mock_gh_bridge.is_available.return_value = True
        self.brain.github_bridge = self.mock_gh_bridge

        # Mock genai.Client for Direct Gemini hermetic isolation
        self.mock_chat = MagicMock()
        self.mock_genai_client = MagicMock()
        self.mock_genai_client.chats.create.return_value = self.mock_chat
        self.client_patcher = patch("jarvis.core.brain_gemini.genai.Client", return_value=self.mock_genai_client)
        self.client_patcher.start()

    def tearDown(self):
        self.client_patcher.stop()
        if hasattr(self, "brain") and self.brain:
            self.brain.shutdown()
        if hasattr(self, "memory") and self.memory:
            self.memory.close()
        if hasattr(self, "temp_dir") and os.path.isdir(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Primary Path & Normal Routing
    # -------------------------------------------------------------------------
    def test_01_primary_antigravity_selected_when_available(self):
        """P0: Verifies primary Antigravity reasoning is selected on normal turns."""
        self.mock_agy_bridge.generate_response.return_value = {
            "status": "SUCCESS",
            "response": "All systems operating within normal parameters, Sir.",
            "duration_seconds": 0.25,
        }
        with patch.object(self.brain, "_get_system_instruction", return_value="System"):
            reply = self.brain._process_antigravity_query("System status check")

        self.assertIn("normal parameters", reply)
        self.mock_agy_bridge.generate_response.assert_called_once()
        self.mock_gh_bridge.generate_response.assert_not_called()

    # -------------------------------------------------------------------------
    # 2. Quota Exhaustion (429 / RESOURCE_EXHAUSTED) Cascades
    # -------------------------------------------------------------------------
    def test_02_antigravity_quota_exhaustion_cascades_to_direct_gemini_when_configured(self):
        """P0: Quota exhaustion in Antigravity seamlessly cascades to Direct Gemini API."""
        self.mock_agy_bridge.generate_response.return_value = {
            "status": "ERROR",
            "response": "429 RESOURCE_EXHAUSTED: Rate limit reached for model gemini-3.8-flash.",
            "duration_seconds": 0.1,
        }
        self.brain._is_gemini_configured = lambda: True

        with patch.object(self.brain, "_process_gemini_query", return_value="Gemini API fallback reply, Sir.") as mock_gem:
            reply = self.brain._process_antigravity_query("Compute the prime factorization of 1024")

        self.assertEqual(reply, "Gemini API fallback reply, Sir.")
        mock_gem.assert_called_once()
        self.mock_gh_bridge.generate_response.assert_not_called()

    def test_03_antigravity_quota_exhaustion_cascades_to_copilot_when_gemini_unconfigured(self):
        """P0: Quota exhaustion in Antigravity falls back to Copilot CLI when Direct Gemini is not configured."""
        self.mock_agy_bridge.generate_response.return_value = {
            "status": "ERROR",
            "response": "429 RESOURCE_EXHAUSTED: Quota exceeded.",
            "duration_seconds": 0.1,
        }
        self.brain._is_gemini_configured = lambda: False
        self.mock_gh_bridge.generate_response.return_value = {
            "status": "SUCCESS",
            "response": "Copilot CLI fallback reply, Sir.",
            "duration_seconds": 0.3,
        }

        with patch.object(self.brain, "_get_system_instruction", return_value="System"):
            reply = self.brain._process_antigravity_query("Summarize this document")

        self.assertEqual(reply, "Copilot CLI fallback reply, Sir.")
        self.mock_gh_bridge.generate_response.assert_called_once()

    def test_04_antigravity_and_gemini_both_quota_exhausted_falls_back_to_copilot(self):
        """P0: When Antigravity quota is exhausted AND Direct Gemini quota is exhausted, cascades to Copilot CLI."""
        self.mock_agy_bridge.generate_response.return_value = {
            "status": "ERROR",
            "response": "429 RESOURCE_EXHAUSTED: Antigravity quota exceeded.",
            "duration_seconds": 0.1,
        }
        self.brain._is_gemini_configured = lambda: True

        # Gemini fails with high network congestion / quota error
        gemini_congestion_reply = "My apologies, Sir. My neural uplink is currently experiencing high network congestion. Please give me a brief moment."
        self.mock_gh_bridge.generate_response.return_value = {
            "status": "SUCCESS",
            "response": "Copilot tertiary fallback reply after Gemini quota exhaustion, Sir.",
            "duration_seconds": 0.4,
        }

        with patch.object(self.brain, "_process_gemini_query", return_value=gemini_congestion_reply) as mock_gem, \
             patch.object(self.brain, "_get_system_instruction", return_value="System"):
            reply = self.brain._process_antigravity_query("Explain quantum entanglement")

        mock_gem.assert_called_once()
        self.assertEqual(reply, "Copilot tertiary fallback reply after Gemini quota exhaustion, Sir.")
        self.mock_gh_bridge.generate_response.assert_called_once()

    # -------------------------------------------------------------------------
    # 3. Authentication Failure (401/403) — Fast Fail-Closed Policy
    # -------------------------------------------------------------------------
    def test_05_antigravity_auth_failure_fails_closed_without_unauthorized_cascade(self):
        """P0: Authentication failure in Antigravity must fail closed and NOT cascade to other providers."""
        self.mock_agy_bridge.generate_response.return_value = {
            "status": "ERROR",
            "response": "401 Unauthorized: Invalid session token or API credentials expired.",
            "duration_seconds": 0.1,
        }
        self.brain._is_gemini_configured = lambda: True

        with patch.object(self.brain, "_process_gemini_query") as mock_gem:
            reply = self.brain._process_antigravity_query("Run security audit")

        # Must NOT silently cascade to Gemini or Copilot on authentication failure
        mock_gem.assert_not_called()
        self.mock_gh_bridge.generate_response.assert_not_called()
        self.assertIn("authentication", reply.lower())

    def test_06_antigravity_candidate_pool_aborts_on_auth_failure_without_wasting_retries(self):
        """P0: AntigravityBridge candidate cascade must abort immediately on 401/403 without trying other models."""
        bridge = AntigravityBridge(self.config, executable_resolver=lambda: "mock_agy.exe")
        with patch.object(bridge, "is_available", return_value=True), \
             patch.object(bridge, "use_daemon", False), \
             patch.object(bridge, "_generate_batch") as mock_batch:
            mock_batch.return_value = {
                "status": "ERROR",
                "response": "403 Forbidden: Account suspended or credentials invalid.",
                "duration_seconds": 0.05,
            }
            res = bridge.generate_response("Test prompt", model="gemini-3.8-flash-high")

            # Must have aborted after 1 attempt, not tried all 3 cascade candidates
            self.assertEqual(mock_batch.call_count, 1)
            self.assertEqual(res["status"], "ERROR")

    def test_07_gemini_auth_failure_fails_closed_without_candidate_rotation(self):
        """P0: Direct Gemini API 401/403 fails fast without retrying candidate models."""
        self.mock_chat.send_message_stream.side_effect = Exception("401 Unauthorized: API_KEY_INVALID")
        self.brain.gemini_candidates = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"]
        self.brain.gemini_model_idx = 0
        self.brain._is_gemini_configured = lambda: True

        with patch.dict("os.environ", {"GEMINI_API_KEY": "test_key"}):
            reply = self.brain._process_gemini_query("Hello")

        # Must fail closed immediately with credentials notice, not rotate to next candidate
        self.assertEqual(self.brain.gemini_model_idx, 0)
        self.assertIn("failed authentication", reply)
        self.assertEqual(self.mock_chat.send_message_stream.call_count, 1)

    def test_08_copilot_auth_failure_reports_credentials_error_without_retry(self):
        """P0: Copilot CLI authentication failure reports login requirement."""
        bridge = GitHubCopilotBridge(self.config)
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Authentication required. Please run `copilot login`."

        with patch.object(bridge, "is_available", return_value=True), \
             patch("subprocess.run", return_value=mock_proc):
            res = bridge.generate_response("Hello")

        self.assertEqual(res["status"], "ERROR")
        self.assertIn("Authentication failed", res["response"])

    # -------------------------------------------------------------------------
    # 4. Tool Deduplication & Safety Boundaries
    # -------------------------------------------------------------------------
    def test_09_copilot_output_never_executed_as_tool_call(self):
        """P0: Copilot CLI output is strictly inert text; JSON tool calls are never executed."""
        tool_shaped_json = json.dumps({"tool": "delete_custom_skill", "arguments": {"skill_name": "important"}})
        self.mock_agy_bridge.generate_response.return_value = {
            "status": "ERROR",
            "response": "429 RESOURCE_EXHAUSTED",
            "duration_seconds": 0.1,
        }
        self.brain._is_gemini_configured = lambda: False
        self.mock_gh_bridge.generate_response.return_value = {
            "status": "SUCCESS",
            "response": tool_shaped_json,
            "duration_seconds": 0.2,
        }

        with patch.object(self.brain, "_get_system_instruction", return_value="System"), \
             patch("jarvis.core.brain.SkillRegistry.execute") as mock_exec:
            reply = self.brain._process_antigravity_query("Clean up skills")

        self.assertEqual(reply, tool_shaped_json)
        mock_exec.assert_not_called()

    def test_10_tool_executed_mid_turn_suppresses_provider_failover(self):
        """P0: If a tool was already executed in a turn, Antigravity errors must NOT trigger provider failover."""
        # Turn 1: Antigravity returns tool call
        tool_call_json = '{"tool": "get_current_time_and_date", "arguments": {}}'
        self.mock_agy_bridge.generate_response.side_effect = [
            {"status": "SUCCESS", "response": tool_call_json, "duration_seconds": 0.2},
            {"status": "ERROR", "response": "429 RESOURCE_EXHAUSTED", "duration_seconds": 0.1},
        ]
        self.brain._is_gemini_configured = lambda: True

        with patch.object(self.brain, "_dispatch_tool_pipeline", return_value=("18:00:00", True)), \
             patch.object(self.brain, "_process_gemini_query") as mock_gem:
            reply = self.brain._process_antigravity_query("What time is it?")

        # Must NOT fail over to Gemini because tool was already dispatched!
        mock_gem.assert_not_called()
        self.assertIn("Action completed", reply)
        self.assertIn("18:00:00", reply)

    def test_10b_tool_failed_mid_turn_reports_failure_without_failover(self):
        """P0: If tool execution failed mid-turn, returns truthful failure status without provider failover."""
        tool_call_json = '{"tool": "read_registry_key", "arguments": {"key": "bad_key"}}'
        self.mock_agy_bridge.generate_response.side_effect = [
            {"status": "SUCCESS", "response": tool_call_json, "duration_seconds": 0.2},
            {"status": "ERROR", "response": "429 RESOURCE_EXHAUSTED", "duration_seconds": 0.1},
        ]
        self.brain._is_gemini_configured = lambda: True

        with patch.object(self.brain, "_dispatch_tool_pipeline", return_value=("Failed to read registry key: Key not found", False)), \
             patch.object(self.brain, "_process_gemini_query") as mock_gem:
            reply = self.brain._process_antigravity_query("Read registry")

        mock_gem.assert_not_called()
        self.assertIn("Action halted or failed", reply)
        self.assertIn("Failed to read registry key", reply)

    def test_10c_tool_rejected_by_auth_reports_rejection_without_failover(self):
        """P0: If tool was rejected by authorization gate, returns rejection status without provider failover."""
        tool_call_json = '{"tool": "format_disk", "arguments": {"drive": "C:"}}'
        self.mock_agy_bridge.generate_response.side_effect = [
            {"status": "SUCCESS", "response": tool_call_json, "duration_seconds": 0.2},
            {"status": "ERROR", "response": "429 RESOURCE_EXHAUSTED", "duration_seconds": 0.1},
        ]
        self.brain._is_gemini_configured = lambda: True

        rejection_msg = "Critical Authorization Gate: Operation rejected because user approval was denied"
        with patch.object(self.brain, "_dispatch_tool_pipeline", return_value=(rejection_msg, False)), \
             patch.object(self.brain, "_process_gemini_query") as mock_gem:
            reply = self.brain._process_antigravity_query("Format drive")

        mock_gem.assert_not_called()
        self.assertIn("Action authorization required", reply)
        self.assertIn("Critical Authorization Gate", reply)

    # -------------------------------------------------------------------------
    # 5. User Cancellation Boundaries
    # -------------------------------------------------------------------------
    def test_11_user_cancellation_aborts_immediately_without_secondary_failover(self):
        """P0: User cancellation mid-turn aborts execution immediately without triggering fallback provider."""
        cancel_token = MagicMock()
        cancel_token.is_cancelled = True
        self.brain._active_cancel_token = cancel_token

        with patch.object(self.brain, "_process_gemini_query") as mock_gem:
            reply = self.brain._process_antigravity_query("Analyze architecture")

        self.assertEqual(reply, "Command cancelled, Sir.")
        mock_gem.assert_not_called()
        self.mock_agy_bridge.generate_response.assert_not_called()

    # -------------------------------------------------------------------------
    # 6. Additional Distinct Error Policies (Network, Timeout, 503, Missing Keys)
    # -------------------------------------------------------------------------
    def test_12_gemini_missing_credentials_fails_fast_with_guidance(self):
        """P0: Missing or placeholder GEMINI_API_KEY fails immediately with configuration guidance."""
        self.brain.gemini_client = None
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            reply = self.brain._process_gemini_query("Hello")

        self.assertIn("requires a valid GEMINI_API_KEY", reply)
        self.mock_genai_client.chats.create.assert_not_called()

    def test_13_gemini_connection_failure_reports_network_error(self):
        """P0: Network connectivity failure (ConnectError/getaddrinfo) reports connection failure."""
        self.brain._is_gemini_configured = lambda: True
        self.mock_chat.send_message_stream.side_effect = Exception("ConnectError: getaddrinfo failed - name resolution error")

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}):
            reply = self.brain._process_gemini_query("Run query")

        self.assertIn("unable to connect to Google Gemini due to a network connection failure", reply)

    def test_14_gemini_model_unavailable_rotates_candidates(self):
        """P0: HTTP 503 / Model Unavailable error triggers rotation to secondary candidate."""
        self.brain._is_gemini_configured = lambda: True
        self.brain.gemini_candidates = ["gemini-3.8-flash", "gemini-3.6-flash"]
        self.brain.gemini_model_idx = 0

        # Attempt 1 raises 503 Unavailable; Attempt 2 succeeds
        mock_chunk = MagicMock()
        mock_part = MagicMock()
        mock_part.text = "Response from rotated candidate, Sir."
        mock_part.thought = False
        mock_cand = MagicMock()
        mock_cand.content.parts = [mock_part]
        mock_chunk.candidates = [mock_cand]
        mock_chunk.function_calls = []

        self.mock_chat.send_message_stream.side_effect = [
            Exception("503 Service Unavailable: Model temporarily overloaded"),
            [mock_chunk],
        ]

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}), \
             patch("time.sleep"):  # skip the 0.5s backoff in unit test
            reply = self.brain._process_gemini_query("Check status")

        self.assertEqual(reply, "Response from rotated candidate, Sir.")
        self.assertEqual(self.brain.gemini_model_idx, 1)

    def test_15_antigravity_timeout_returns_bounded_error(self):
        """P0: Antigravity timeout returns bounded error without hanging."""
        self.mock_agy_bridge.generate_response.return_value = {
            "status": "ERROR",
            "error": "Timeout of 90.0s exceeded while waiting for Antigravity CLI",
            "response": "",
            "duration_seconds": 90.0,
        }
        self.brain._is_gemini_configured = lambda: False
        self.mock_gh_bridge.is_available.return_value = False

        reply = self.brain._process_antigravity_query("Long computation")

        self.assertIn("Antigravity generation failed", reply)
        self.assertIn("Timeout", reply)

    def test_16_antigravity_malformed_empty_response_returns_error_notice(self):
        """P0: Antigravity abnormal exit with malformed/empty response reports truthful error."""
        self.mock_agy_bridge.generate_response.return_value = {
            "status": "ERROR",
            "error": "Subprocess crashed with exit code 3221225477",
            "response": "",
            "duration_seconds": 0.05,
        }
        self.brain._is_gemini_configured = lambda: False
        self.mock_gh_bridge.is_available.return_value = False

        reply = self.brain._process_antigravity_query("Test crash")

        self.assertIn("Antigravity generation failed", reply)
        self.assertIn("Subprocess crashed", reply)

    def test_17_gemini_turn_deadline_aborts_retries_when_budget_insufficient(self):
        """P0: Direct Gemini stops retrying candidate models when remaining turn budget is <10.0s."""
        self.brain._is_gemini_configured = lambda: True
        self.brain.gemini_candidates = ["gemini-3.8-flash", "gemini-3.6-flash"]
        self.mock_chat.send_message_stream.side_effect = Exception("504 DEADLINE_EXCEEDED: Gateway timeout")

        # Simulate turn starting with only 2s remaining in total turn budget (budget exhausted)
        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}), \
             patch("time.perf_counter", side_effect=[0.0, 23.5, 23.6, 23.7]):
            reply = self.brain._process_gemini_query("Process with tight deadline")

        self.assertIn("timed out before receiving a response from Gemini", reply)

    def test_18_gemini_empty_response_stream_reports_provider_failure(self):
        """P0: Direct Gemini completely empty chunk response stream reports explicit provider failure."""
        self.brain._is_gemini_configured = lambda: True
        self.mock_chat.send_message_stream.return_value = []

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}):
            reply = self.brain._process_gemini_query("Ping empty stream")

        self.assertNotEqual(reply, "At your service, Sir.")
        self.assertIn("empty response stream", reply.lower())
        self.assertIsNone(self.brain.gemini_chat)

    def test_19_gemini_excessive_tool_steps_capped_safely(self):
        """P0: Direct Gemini function calling loop halts and invalidates session when exceeding tool step limit."""
        self.brain._is_gemini_configured = lambda: True
        self.brain.active_task_mode = "general"

        mock_call = MagicMock()
        mock_call.name = "get_current_time_and_date"
        mock_call.args = {}

        mock_chunk = MagicMock()
        mock_chunk.candidates = []
        mock_chunk.function_calls = [mock_call]

        # Return continuous function calls on every stream attempt
        self.mock_chat.send_message_stream.return_value = [mock_chunk]

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}), \
             patch.object(self.brain, "_dispatch_tool_pipeline", return_value=("12:00", True)):
            reply = self.brain._process_gemini_query("Looping tools")

        self.assertIn("reached the limit for tool steps", reply)
        # Session must be invalidated to prevent corrupt history state
        self.assertIsNone(self.brain.gemini_chat)

    # -------------------------------------------------------------------------
    # 5. Direct Gemini Empty, Malformed, Safety & Cancellation Behavior
    # -------------------------------------------------------------------------
    def test_20_gemini_response_with_no_candidates_reports_provider_failure(self):
        """P0: Direct Gemini response with empty candidate list reports explicit provider failure."""
        self.brain._is_gemini_configured = lambda: True
        empty_chunk = MagicMock()
        empty_chunk.candidates = []
        empty_chunk.function_calls = []
        self.mock_chat.send_message_stream.return_value = [empty_chunk]

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}):
            reply = self.brain._process_gemini_query("Ping empty candidates")

        self.assertNotEqual(reply, "At your service, Sir.")
        self.assertIn("no response candidates", reply.lower())
        self.assertIsNone(self.brain.gemini_chat)

    def test_21_gemini_candidate_with_no_usable_text_reports_provider_failure(self):
        """P0: Direct Gemini candidate with only thought tokens or empty parts reports explicit failure."""
        self.brain._is_gemini_configured = lambda: True
        thought_part = MagicMock(text="internal reasoning", thought=True)
        cand = MagicMock(content=MagicMock(parts=[thought_part]), finish_reason="STOP")
        chunk = MagicMock(candidates=[cand], function_calls=[])
        self.mock_chat.send_message_stream.return_value = [chunk]

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}):
            reply = self.brain._process_gemini_query("Ping thought only")

        self.assertNotEqual(reply, "At your service, Sir.")
        self.assertIn("without usable content", reply.lower())
        self.assertIsNone(self.brain.gemini_chat)

    def test_22_gemini_malformed_response_rejected_without_fabricated_success(self):
        """P0: Direct Gemini malformed chunk payload is rejected without fabricating success."""
        self.brain._is_gemini_configured = lambda: True

        class CorruptChunk:
            @property
            def candidates(self):
                raise ValueError("Corrupt chunk structure")

        self.mock_chat.send_message_stream.return_value = [CorruptChunk()]

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}):
            reply = self.brain._process_gemini_query("Ping corrupt chunk")

        self.assertNotEqual(reply, "At your service, Sir.")
        self.assertIn("anomaly in my cognitive circuits", reply.lower())
        self.assertIsNone(self.brain.gemini_chat)

    def test_23_gemini_safety_blocked_response_returns_appropriate_refusal(self):
        """P0: Direct Gemini response blocked by safety filters returns refusal without candidate rotation."""
        self.brain._is_gemini_configured = lambda: True
        self.brain.gemini_candidates = ["gemini-3.8-flash", "gemini-3.6-flash"]
        self.brain.gemini_model_idx = 0

        cand = MagicMock(content=None, finish_reason="SAFETY")
        chunk = MagicMock(candidates=[cand], function_calls=[])
        self.mock_chat.send_message_stream.return_value = [chunk]

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}):
            reply = self.brain._process_gemini_query("Unsafe query")

        self.assertNotEqual(reply, "At your service, Sir.")
        self.assertIn("content safety policies", reply.lower())
        self.assertIsNone(self.brain.gemini_chat)
        # Crucial: safety refusal must NOT rotate candidates to bypass safety filters
        self.assertEqual(self.brain.gemini_model_idx, 0)

    def test_24_gemini_valid_text_response_returns_response(self):
        """P0: Direct Gemini valid text response returns text cleanly."""
        self.brain._is_gemini_configured = lambda: True
        part = MagicMock(text="Quantum computing optimizes factorizations, Sir.", thought=False)
        cand = MagicMock(content=MagicMock(parts=[part]), finish_reason="STOP")
        chunk = MagicMock(candidates=[cand], function_calls=[])
        self.mock_chat.send_message_stream.return_value = [chunk]

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}):
            reply = self.brain._process_gemini_query("Explain quantum computing")

        self.assertEqual(reply, "Quantum computing optimizes factorizations, Sir.")

    def test_25_gemini_valid_tool_call_routes_through_authorized_dispatcher(self):
        """P0: Direct Gemini valid function call routes through authorized dispatcher."""
        self.brain._is_gemini_configured = lambda: True
        self.brain.active_tool_names = frozenset(["get_system_vitals"])

        mock_call = MagicMock()
        mock_call.name = "get_system_vitals"
        mock_call.args = {"detailed": True}
        mock_call.id = "call_vitals_1"

        tool_chunk = MagicMock(candidates=[], function_calls=[mock_call])
        followup_part = MagicMock(text="CPU utilization is 12%, Sir.", thought=False)
        followup_cand = MagicMock(content=MagicMock(parts=[followup_part]), finish_reason="STOP")
        followup_chunk = MagicMock(candidates=[followup_cand], function_calls=[])

        self.mock_chat.send_message_stream.side_effect = [
            [tool_chunk],
            [followup_chunk],
        ]

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}), \
             patch.object(self.brain, "_dispatch_tool_pipeline", return_value=("CPU: 12%", True)) as mock_dispatch:
            reply = self.brain._process_gemini_query("What are system vitals?")

        mock_dispatch.assert_called_once_with("get_system_vitals", {"detailed": True}, step_index=1, sentence_callback=None)
        self.assertEqual(reply, "CPU utilization is 12%, Sir.")

    def test_26_gemini_cancellation_aborts_without_further_provider_fallback(self):
        """P0: User cancellation during Gemini tool execution aborts cleanly without fallback."""
        self.brain._is_gemini_configured = lambda: True
        self.brain.active_tool_names = frozenset(["reboot_system"])

        mock_call = MagicMock()
        mock_call.name = "reboot_system"
        mock_call.args = {}
        mock_call.id = "call_reboot_1"

        tool_chunk = MagicMock(candidates=[], function_calls=[mock_call])
        self.mock_chat.send_message_stream.return_value = [tool_chunk]

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}), \
             patch.object(self.brain, "_dispatch_tool_pipeline", side_effect=RuntimeError("Action cancelled by user interrupt")):
            reply = self.brain._process_gemini_query("Reboot the machine")

        self.assertEqual(reply, "Command cancelled, Sir.")
        self.assertIsNone(self.brain.gemini_chat)

        with patch.dict("os.environ", {"GEMINI_API_KEY": "valid_key"}), \
             patch.object(self.brain, "_dispatch_tool_pipeline", return_value=("12:00", True)):
            reply = self.brain._process_gemini_query("Looping tools")

        self.assertIn("reached the limit for tool steps", reply)
        # Session must be invalidated to prevent corrupt history state
        self.assertIsNone(self.brain.gemini_chat)


if __name__ == "__main__":
    unittest.main()
