"""Unit Tests for Curated Prompts Arsenal and Epistemic Anti-Hallucination Gate."""
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from jarvis.core.brain import JarvisBrain
from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


class TestCuratedPromptsAndEpistemicGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        cls.memory = MagicMock(spec=MemoryManager)
        cls.memory.get_context_summary.return_value = ""
        cls.memory.format_core_preferences_block.return_value = ""
        cls.memory.recall_episodic_lessons.return_value = []
        cls.memory.get_recent_interactions.return_value = []
        cls.safety = MagicMock(spec=SafetyGate)

    def setUp(self):
        self.brain = JarvisBrain(self.config, self.memory, self.safety)

    def test_curated_prompts_exist_on_disk(self):
        """Verifies all five curated prompt arsenal files exist and are populated."""
        expected_prompts = [
            "epistemic_anti_hallucination",
            "whitebox_security_auditor",
            "phased_ast_code_reviewer",
            "pctce_meta_prompt_architect",
            "sqlite_vector_performance_architect",
        ]
        prompts_dir = Path("data/prompts")
        if not prompts_dir.is_dir():
            prompts_dir = Path(__file__).resolve().parent.parent / "data" / "prompts"
        self.assertTrue(prompts_dir.is_dir(), "data/prompts directory must exist")
        for name in expected_prompts:
            prompt_file = prompts_dir / f"{name}.md"
            self.assertTrue(prompt_file.is_file(), f"Prompt file {name}.md must exist")
            content = prompt_file.read_text(encoding="utf-8").strip()
            self.assertGreater(len(content), 100, f"Prompt {name}.md must have meaningful content")

    def test_load_curated_prompt_helper(self):
        """Verifies load_curated_prompt successfully reads valid prompts and handles missing ones."""
        content = JarvisBrain.load_curated_prompt("epistemic_anti_hallucination")
        self.assertIn("Epistemic Grounding & Anti-Hallucination Gate", content)
        self.assertIn("FACT: Explicitly verified in the repository AST", content)
        self.assertIn("Radical Honesty on Unknowns", content)

        missing = JarvisBrain.load_curated_prompt("non_existent_prompt_xyz")
        self.assertEqual(missing, "")

    def test_code_mode_system_instruction_contains_epistemic_gate(self):
        """Verifies active_task_mode == 'code' injects the epistemic anti-hallucination gate."""
        self.brain.active_task_mode = "code"
        sys_inst = self.brain._get_system_instruction(user_text="Refactor the audio worker")
        self.assertIn("Current task mode: software engineering", sys_inst)
        self.assertIn("Epistemic Grounding & Anti-Hallucination Gate", sys_inst)
        self.assertIn("FACT: Explicitly verified in the repository AST", sys_inst)
        self.assertIn("Never compress uncertainty into a confident tone", sys_inst)

    def test_voice_mode_system_instruction_remains_lean(self):
        """Verifies default conversation turns do not carry the epistemic code gate (preserving <1.5KB latency budget)."""
        self.brain.active_task_mode = ""
        sys_inst = self.brain._get_system_instruction(user_text="What's the weather?")
        self.assertNotIn("Current task mode: software engineering", sys_inst)
        self.assertNotIn("Epistemic Grounding & Anti-Hallucination Gate", sys_inst)

    def test_consult_specialist_injects_security_curated_prompt(self):
        """Verifies _consult_specialist('security') passes whitebox_security_auditor to the bridge."""
        mock_bridge = MagicMock()
        mock_bridge.is_available.return_value = True
        mock_bridge.generate_response.return_value = {"response": "Security audit complete: zero critical risks found."}
        self.brain.antigravity_bridge = mock_bridge

        result = self.brain._consult_specialist("security", "Review dev_shell.py execution safety")
        self.assertIn("Security audit complete", result)

        call_args = mock_bridge.generate_response.call_args
        self.assertIsNotNone(call_args)
        kwargs = call_args[1] if len(call_args) > 1 and call_args[1] else call_args.kwargs
        sys_instruction = kwargs.get("system_instruction", "")
        self.assertIn("White-Box AST Security & Blast-Radius Auditor", sys_instruction)
        self.assertIn("Command & Process Sandboxing", sys_instruction)

    def test_consult_specialist_injects_architect_curated_prompt(self):
        """Verifies _consult_specialist('architect') passes phased_ast_code_reviewer to the bridge."""
        mock_bridge = MagicMock()
        mock_bridge.is_available.return_value = True
        mock_bridge.generate_response.return_value = {"response": "Architect review: event loop integrity verified."}
        self.brain.antigravity_bridge = mock_bridge

        result = self.brain._consult_specialist("architect", "Review event loop and thread safety")
        self.assertIn("Architect review", result)

        call_args = mock_bridge.generate_response.call_args
        kwargs = call_args[1] if len(call_args) > 1 and call_args[1] else call_args.kwargs
        sys_instruction = kwargs.get("system_instruction", "")
        self.assertIn("Phased AST Code Reviewer & Performance Profiler", sys_instruction)
        self.assertIn("Concurrency & Event Loop Integrity", sys_instruction)


if __name__ == "__main__":
    unittest.main()
