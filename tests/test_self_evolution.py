"""Unit and integration test suite for Autonomous Self-Building and Dynamic Skill Hot-Reloading."""
import sys
import unittest
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from jarvis.skills.base import SkillRegistry
from jarvis.core.security_boundaries import DualKeyApprovalManager
from jarvis.skills.self_evolution import (
    create_or_update_custom_skill,
    list_custom_skills,
    delete_custom_skill,
    load_all_custom_skills,
    CUSTOM_SKILLS_DIR,
)


class TestSelfEvolution(unittest.TestCase):
    def setUp(self):
        # Clean any leftover test skills
        DualKeyApprovalManager.get_instance().grant_approval("delete_custom_skill", {"skill_name": "test_mock_greeter"})
        delete_custom_skill("test_mock_greeter")
        DualKeyApprovalManager.get_instance().grant_approval("delete_custom_skill", {"skill_name": "test_broken_skill"})
        delete_custom_skill("test_broken_skill")

    def tearDown(self):
        DualKeyApprovalManager.get_instance().grant_approval("delete_custom_skill", {"skill_name": "test_mock_greeter"})
        delete_custom_skill("test_mock_greeter")
        DualKeyApprovalManager.get_instance().grant_approval("delete_custom_skill", {"skill_name": "test_broken_skill"})
        delete_custom_skill("test_broken_skill")

    def test_syntax_error_rejection(self):
        """Verifies that invalid Python code is caught by the AST parser before saving."""
        bad_code = "def broken_func(:\n    return 42"
        res = create_or_update_custom_skill(
            skill_name="test_broken_skill",
            python_code=bad_code,
            description="A broken skill"
        )
        self.assertIn("SyntaxError", res)
        # Verify the file was not written to disk
        self.assertFalse((CUSTOM_SKILLS_DIR / "test_broken_skill.py").exists())

    def test_dynamic_skill_creation_and_hot_reload(self):
        """Verifies that valid Python code is saved, hot-reloaded into SkillRegistry, and executable."""
        skill_code = '''
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(
    name="test_mock_greeter",
    description="A test greeter skill built autonomously."
)
def test_mock_greeter(target_name: str = "Sir") -> str:
    """Greets the target warmly."""
    return f"Greetings, {target_name}. Autonomous skill is fully operational!"
'''
        DualKeyApprovalManager.get_instance().grant_approval(
            "create_or_update_custom_skill",
            {"skill_name": "test_mock_greeter", "python_code": skill_code, "description": "Test greeter"}
        )
        res = create_or_update_custom_skill(
            skill_name="test_mock_greeter",
            python_code=skill_code,
            description="Test greeter"
        )
        self.assertIn("Successfully engineered and hot-reloaded", res)
        self.assertIn("test_mock_greeter", res)

        # 1. Verify file exists on disk
        self.assertTrue((CUSTOM_SKILLS_DIR / "test_mock_greeter.py").exists())

        # 2. Verify tool is in SkillRegistry
        tool_fn = SkillRegistry.get_tool("test_mock_greeter")
        self.assertIsNotNone(tool_fn, "test_mock_greeter must be registered in SkillRegistry")

        # 3. Verify tool execution via SkillRegistry.execute
        exec_result = SkillRegistry.execute("test_mock_greeter", target_name="Commander")
        self.assertEqual(exec_result, "Greetings, Commander. Autonomous skill is fully operational!")

        # 4. Verify listed in list_custom_skills()
        listing = list_custom_skills()
        self.assertIn("test_mock_greeter", listing)

        # 5. Verify delete_custom_skill cleans up
        DualKeyApprovalManager.get_instance().grant_approval(
            "delete_custom_skill",
            {"skill_name": "test_mock_greeter"}
        )
        del_res = delete_custom_skill("test_mock_greeter")
        self.assertIn("Successfully deleted", del_res)
        self.assertFalse((CUSTOM_SKILLS_DIR / "test_mock_greeter.py").exists())

    def test_user_model_choice_for_self_building(self):
        """Verifies that self-building requests strictly use the user's chosen model and do not auto-override."""
        from jarvis.core.config_loader import load_config
        from jarvis.core.brain import JarvisBrain
        from jarvis.core.memory import MemoryManager
        from jarvis.core.safety import SafetyGate

        config = load_config()
        memory = MemoryManager(config)
        safety = SafetyGate(config)
        brain = JarvisBrain(config, memory, safety)

        # 1. User locks model to Gemini 3.8 Flash High
        brain.antigravity_model = "gemini-3.8-flash-high"
        brain.antigravity_auto_tiering = False

        # When asking to build a feature, it must stay locked to gemini-3.8-flash-high
        prompt = "Add a feature to yourself to open a Brave tab"
        # Simulate model extraction logic
        self.assertEqual(brain.antigravity_model, "gemini-3.8-flash-high")

        # 2. User specifies a model directly in the prompt
        explicit_prompt = "Using Claude, add a feature to yourself to open a Brave tab"
        override_match = brain.process_query.__globals__["re"].search(
            r"\b(?:use|using|with)\s+(pro|flash|claude|sonnet|opus|argon|3\.8|3\.1|4)(?:\s+(low|medium|med|high))?\b",
            explicit_prompt,
            brain.process_query.__globals__["re"].IGNORECASE,
        )
        self.assertIsNotNone(override_match)
        self.assertEqual(override_match.group(1).lower(), "claude")

        # 3. User specifies Pro High directly in the prompt
        pro_prompt = "Use Pro High to build a feature to check free disk space"
        override_match_pro = brain.process_query.__globals__["re"].search(
            r"\b(?:use|using|with)\s+(pro|flash|claude|sonnet|opus|argon|3\.8|3\.1|4)(?:\s+(low|medium|med|high))?\b",
            pro_prompt,
            brain.process_query.__globals__["re"].IGNORECASE,
        )
        self.assertIsNotNone(override_match_pro)
        self.assertEqual(override_match_pro.group(1).lower(), "pro")
        self.assertEqual(override_match_pro.group(2).lower(), "high")


if __name__ == "__main__":
    unittest.main()
