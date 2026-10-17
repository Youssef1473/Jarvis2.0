"""Unit Tests for J.A.R.V.I.S. Authentic Persona, Epistemic Humility & Socratic Co-Engineering."""
import unittest
from unittest.mock import MagicMock

from jarvis.core.brain import JarvisBrain
from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate


class TestInteractivePersona(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        cls.memory = MagicMock(spec=MemoryManager)
        cls.safety = MagicMock(spec=SafetyGate)

    def setUp(self):
        self.brain = JarvisBrain(self.config, self.memory, self.safety)

    def test_system_prompt_contains_authentic_bettany_demeanor(self):
        """Verifies BASE_SYSTEM_PROMPT embeds Paul Bettany cadence, dry irony, and anti-corporate directives."""
        prompt = JarvisBrain.BASE_SYSTEM_PROMPT
        self.assertIn("Paul Bettany", prompt)
        self.assertIn("Authentic J.A.R.V.I.S. Demeanor, Cadence & Dry Irony", prompt)
        self.assertIn("Anti-Corporate Directive", prompt)
        self.assertIn("Never sound like a scripted corporate helpdesk", prompt)

    def test_system_prompt_contains_epistemic_humility_protocol(self):
        """Verifies BASE_SYSTEM_PROMPT mandates radical honesty on knowledge boundaries and active memory inscription."""
        prompt = JarvisBrain.BASE_SYSTEM_PROMPT
        self.assertIn("II. Epistemic Humility & Active Memory Inscription", prompt)
        self.assertIn("Radical Honesty on Knowledge Boundaries", prompt)
        self.assertIn("commit it to memory", prompt)
        self.assertIn("NEVER hallucinate, guess, or invent ungrounded facts", prompt)

    def test_system_prompt_contains_socratic_co_engineering_protocol(self):
        """Verifies BASE_SYSTEM_PROMPT mandates task simplification and A/B co-design instead of lecture dumping."""
        prompt = JarvisBrain.BASE_SYSTEM_PROMPT
        self.assertIn("III. Socratic Co-Engineering & Interactive Collaboration", prompt)
        self.assertIn("Simplify Before Acting on Complex Work", prompt)
        self.assertIn("Do NOT output a monolithic 10-step lecture", prompt)
        self.assertIn("Option A", prompt)
        self.assertIn("Option B", prompt)

    def test_intent_groups_include_active_learning_and_commit_keywords(self):
        """Verifies TOOL_INTENT_GROUPS captures commit and remember variations for active learning."""
        selected_tools = self.brain._select_tools_for_query("remember that for me")
        self.assertIn("remember_fact", selected_tools)

        selected_commit = self.brain._select_tools_for_query("commit this to memory")
        self.assertIn("remember_fact", selected_commit)

    def test_multi_turn_affirmative_follow_up_active_memory(self):
        """Verifies saying 'yes' following a memory inquiry dynamically activates memory tools."""
        self.brain.openai_messages = [
            {"role": "user", "content": "We are migrating to Project Janus next week."},
            {
                "role": "assistant",
                "content": "I don't seem to have any record of Project Janus in my archives, Sir. Would you like me to commit it to memory so I'm properly calibrated?"
            }
        ]
        # Sir simply says "Yes, please"
        tools = self.brain._select_tools_for_query("yes, please")
        self.assertIn("remember_fact", tools)
        self.assertIn("update_user_preference", tools)

    def test_multi_turn_affirmative_follow_up_co_engineering(self):
        """Verifies saying 'yes' or selecting Option A following a co-engineering prompt activates code tools."""
        self.brain.openai_messages = [
            {"role": "user", "content": "Let's build a new audio telemetry monitor."},
            {
                "role": "assistant",
                "content": "We have two viable approaches here, Sir: Option A (fast reflex) or Option B (deep reasoning). Shall we build Option A first?"
            }
        ]
        # Sir answers "Option A"
        tools = self.brain._select_tools_for_query("Option A, please")
        self.assertTrue(any(t in tools for t in ("patch_file", "write_file", "execute_powershell")))


if __name__ == "__main__":
    unittest.main()
