"""Unit tests for ContextOrchestrator in Jarvis 2.0."""
import unittest
from unittest.mock import MagicMock

from jarvis.context.context_orchestrator import ContextOrchestrator, SOVEREIGN_IDENTITY, BASE_SYSTEM_PROMPT


class TestContextOrchestrator(unittest.TestCase):
    def setUp(self):
        self.orchestrator = ContextOrchestrator(config={"_base_dir": "C:/mock_workspace"})

    def test_load_curated_prompt_valid_and_invalid(self):
        """Verifies curated engineering prompts can be loaded from disk."""
        content = self.orchestrator.load_curated_prompt("epistemic_anti_hallucination")
        self.assertIsInstance(content, str)
        if content:
            self.assertIn("anti-hallucination", content.lower())

        missing = self.orchestrator.load_curated_prompt("non_existent_curated_prompt_12345")
        self.assertEqual(missing, "")

    def test_build_ephemeral_user_envelope_full_assembly(self):
        """Verifies envelope orders telemetry, JIT memory, episodic lessons, and user utterance."""
        mock_twin = MagicMock()
        mock_twin.build_persona_telemetry_envelope.return_value = "<live_telemetry_envelope>focus=deep</live_telemetry_envelope>"

        mock_memory = MagicMock()
        mock_memory.get_context_summary.return_value = "- User prefers Python 3.12"
        mock_memory.recall_episodic_lessons.return_value = [
            {"tool_name": "patch_file", "lesson": "Always read before patching."}
        ]

        envelope = self.orchestrator.build_ephemeral_user_envelope(
            user_text="refactor the parser",
            memory=mock_memory,
            cognitive_twin=mock_twin,
        )

        self.assertIn("<live_telemetry_envelope>", envelope)
        self.assertIn("<jit_recalled_memory>", envelope)
        self.assertIn("User prefers Python 3.12", envelope)
        self.assertIn("<episodic_lessons>", envelope)
        self.assertIn("Always read before patching", envelope)
        self.assertTrue(envelope.endswith("refactor the parser"))

    def test_build_ephemeral_user_envelope_minimal(self):
        """Verifies envelope graceful degradation when memory and twin are None."""
        envelope = self.orchestrator.build_ephemeral_user_envelope(
            user_text="hello jarvis",
            memory=None,
            cognitive_twin=None,
        )
        self.assertEqual(envelope, "hello jarvis")

    def test_assemble_system_prompt_structure(self):
        """Verifies complete system prompt structure and tool formatting."""
        mock_memory = MagicMock()
        mock_memory.format_core_preferences_block.return_value = "<core_preferences>Theme: Dark</core_preferences>"
        mock_memory.get_recent_interactions.return_value = [
            {"user_input": "hello", "jarvis_reply": "Good day, Sir."}
        ]
        mock_memory.get_context_summary.return_value = "System online."
        mock_memory.recall_episodic_lessons.return_value = []

        tools = [
            {"function": {"name": "adjust_volume", "description": "Adjusts master audio volume."}},
            {"function": {"name": "launch_application", "description": "Launches an application."}},
        ]

        prompt = self.orchestrator.assemble_system_prompt(
            memory=mock_memory,
            available_tools=tools,
            provider="antigravity",
            active_model="gemini-3.8-flash-high",
            workspace="C:/mock_workspace",
            active_task_mode="general",
            user_text="adjust volume to 50",
            tier1_profile_xml="<tier1>user_profile</tier1>",
            flow_state_xml="<flow_state>idle</flow_state>",
            bookmark_context="<bookmark>resumed</bookmark>",
        )

        self.assertIn("J.A.R.V.I.S.", prompt)
        self.assertIn("Current runtime: provider=antigravity; model=gemini-3.8-flash-high.", prompt)
        self.assertIn("Workspace: C:/mock_workspace.", prompt)
        self.assertIn("- adjust_volume: Adjusts master audio volume.", prompt)
        self.assertIn("- launch_application: Launches an application.", prompt)
        self.assertIn("<core_preferences>Theme: Dark</core_preferences>", prompt)
        self.assertIn("Sir: hello", prompt)
        self.assertIn("Jarvis: Good day, Sir.", prompt)
        self.assertIn("<tier1>user_profile</tier1>", prompt)
        self.assertIn("<flow_state>idle</flow_state>", prompt)
        self.assertIn("<bookmark>resumed</bookmark>", prompt)

    def test_assemble_system_prompt_code_mode(self):
        """Verifies software engineering directives and epistemic gate are injected in code mode."""
        mock_memory = MagicMock()
        mock_memory.format_core_preferences_block.return_value = ""
        mock_memory.get_recent_interactions.return_value = []
        mock_memory.get_context_summary.return_value = ""
        mock_memory.recall_episodic_lessons.return_value = []

        prompt = self.orchestrator.assemble_system_prompt(
            memory=mock_memory,
            available_tools=[],
            provider="antigravity",
            active_model="claude-sonnet-4-6",
            active_task_mode="code",
            user_text="write a unit test",
        )

        self.assertIn("Current task mode: software engineering.", prompt)
        self.assertIn("Inspect before editing", prompt)

    def test_radix_node_tree_invariance_across_turns(self):
        """Verifies H_root and H_user remain 100% byte-for-byte and digest-invariant across changing turns."""
        from jarvis.core.prompt_layout import RadixNode, format_compact_typescript_tools

        preamble = SOVEREIGN_IDENTITY + "\n\n" + BASE_SYSTEM_PROMPT
        tools = [
            {"function": {"name": "adjust_volume", "description": "Adjusts audio volume."}},
            {"function": {"name": "take_screenshot", "description": "Captures desktop screen."}},
        ]
        tool_specs = format_compact_typescript_tools(tools)

        # 1. Root node materialized at boot
        root = RadixNode.create_root(preamble, tool_specs)
        self.assertEqual(len(root.digest), 8)
        initial_root_digest = root.hex_digest

        # 2. User preference node
        user_node = root.append_child("User prefers concise British diction.", "ZONE 2: PREFERENCES")
        self.assertEqual(len(user_node.digest), 8)
        initial_user_digest = user_node.hex_digest

        # 3. Simulate 10 turns with fluctuating volatile state
        current_node = user_node
        for turn_idx in range(1, 11):
            # Verify root and user digests have not mutated
            self.assertEqual(root.hex_digest, initial_root_digest)
            self.assertEqual(user_node.hex_digest, initial_user_digest)

            # Append turn pair
            turn_content = f"User: Command {turn_idx}\nJarvis: Executed action {turn_idx}, Sir."
            current_node = current_node.append_child(turn_content, "ZONE 3: HISTORY")

            # Compile full prompt with volatile tail
            volatile_leaf = f"<flow_state>battery={100 - turn_idx}% window=Code.exe timestamp=170000{turn_idx}</flow_state>\nUser: Do step {turn_idx + 1}"
            full_prompt = current_node.compile_full_prompt(volatile_leaf)

            # Assert volatile leaf is placed strictly at the tail
            self.assertTrue(full_prompt.endswith(volatile_leaf))
            # Assert root preamble is preserved at index 0
            self.assertTrue(full_prompt.startswith(preamble.strip()))

        # Current node depth should be 1 (root) + 1 (user) + 10 (turns) = 12
        self.assertEqual(current_node.depth, 12)


if __name__ == "__main__":
    unittest.main()
