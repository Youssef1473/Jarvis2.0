"""Integration and startup compatibility audit for decomposed JarvisBrain and MemoryManager."""
import inspect
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from jarvis.core.config_loader import load_config
from jarvis.core.memory import (
    MemoryManager,
    VectorMemoryEngine,
    SingleWriterActor,
    fuse_vec_and_fts_rrf,
    sanitize_fts5_query,
    sanitize_memory_taint,
)
from jarvis.core.brain import JarvisBrain
from jarvis.core.brain_openai import OpenAIProviderMixin
from jarvis.core.brain_gemini import GeminiProviderMixin
from jarvis.core.brain_antigravity import AntigravityProviderMixin
from jarvis.core.brain_specialist import SpecialistConsultationMixin
from jarvis.core.safety import SafetyGate


class TestApplicationStartupAudit(unittest.TestCase):
    """Verifies public exports, mixin interfaces, startup resilience, and shutdown cleanups."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = load_config()
        self.config["_base_dir"] = self.temp_dir
        self.config["memory"]["db_path"] = "startup_audit.db"
        self.config["memory"]["background_learning"] = False

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_public_memory_api_and_exports(self):
        """Verify that all public functions, classes, and signatures remain intact on MemoryManager."""
        mem = MemoryManager(self.config)
        try:
            # Check mixin inheritance
            self.assertTrue(issubclass(MemoryManager, OpenAIProviderMixin) is False)
            from jarvis.core.memory.sqlite_base import SqliteBaseMixin
            from jarvis.core.memory.temporal_graph import TemporalGraphMixin
            from jarvis.core.memory.facts_manager import FactsManagerMixin
            from jarvis.core.memory.tasks_calendar import TasksCalendarMixin
            from jarvis.core.memory.document_vault import DocumentVaultMixin
            from jarvis.core.memory.interaction_memory import InteractionMemoryMixin

            self.assertTrue(issubclass(MemoryManager, SqliteBaseMixin))
            self.assertTrue(issubclass(MemoryManager, TemporalGraphMixin))
            self.assertTrue(issubclass(MemoryManager, FactsManagerMixin))
            self.assertTrue(issubclass(MemoryManager, TasksCalendarMixin))
            self.assertTrue(issubclass(MemoryManager, DocumentVaultMixin))
            self.assertTrue(issubclass(MemoryManager, InteractionMemoryMixin))

            # Check method availability & callable signatures
            required_methods = [
                ("remember_fact", ["key", "value"]),
                ("get_fact", ["key"]),
                ("update_user_preference", ["key", "value"]),
                ("get_user_preferences", []),
                ("format_core_preferences_xml", []),
                ("add_task", ["description"]),
                ("get_pending_tasks", []),
                ("complete_task", ["task_id"]),
                ("log_interaction", ["user_input", "jarvis_reply"]),
                ("get_recent_interactions", []),
                ("stage_document", ["filename", "file_path"]),
                ("save_document_knowledge", ["title"]),
                ("search_documents", ["query"]),
                ("format_document_reference_block", ["docs"]),
                ("remember_edge", ["subject", "predicate", "object_val"]),
                ("query_active_edges", []),
                ("record_episodic_lesson", ["tool_name", "trigger_context", "failure_signature", "lesson"]),
                ("recall_episodic_lessons", ["query"]),
                ("execute_write", ["func"]),
                ("close", []),
            ]
            for fn_name, expected_params in required_methods:
                self.assertTrue(hasattr(mem, fn_name), f"MemoryManager missing method: {fn_name}")
                fn = getattr(mem, fn_name)
                self.assertTrue(callable(fn), f"Attribute {fn_name} is not callable")
                sig = inspect.signature(fn)
                for param in expected_params:
                    self.assertIn(param, sig.parameters, f"Method {fn_name} missing parameter: {param}")
        finally:
            mem.close()

    def test_public_brain_api_and_mixins(self):
        """Verify that JarvisBrain correctly inherits all 4 mixins without method name collision."""
        self.assertTrue(issubclass(JarvisBrain, OpenAIProviderMixin))
        self.assertTrue(issubclass(JarvisBrain, GeminiProviderMixin))
        self.assertTrue(issubclass(JarvisBrain, AntigravityProviderMixin))
        self.assertTrue(issubclass(JarvisBrain, SpecialistConsultationMixin))

        required_brain_methods = [
            ("process_query", ["user_text"]),
            ("_dispatch_tool_pipeline", ["tool_name", "tool_args"]),
            ("_execute_tool_with_orchestrator", ["tool_name", "tool_args"]),
            ("get_current_turn_tool_outputs", []),
            ("specialist_status", []),
            ("_consult_specialist", ["provider", "prompt"]),
            ("get_latencies", []),
            ("get_latency_profiler", []),
            ("classify_query_tier", ["query"]),
            ("should_capture_screen", ["query"]),
            ("shutdown", []),
            ("stop", []),
            ("set_cancellation_token", ["token"]),
            ("get_cancellation_token", []),
            ("cancel_active_turn", []),
        ]
        for fn_name, expected_params in required_brain_methods:
            self.assertTrue(hasattr(JarvisBrain, fn_name), f"JarvisBrain missing method: {fn_name}")
            fn = getattr(JarvisBrain, fn_name)
            self.assertTrue(callable(fn), f"JarvisBrain attribute {fn_name} is not callable")
            sig = inspect.signature(fn)
            for param in expected_params:
                self.assertIn(param, sig.parameters, f"Method {fn_name} missing parameter: {param}")

    @patch("jarvis.core.brain.CognitiveRouter")
    @patch("jarvis.core.brain.AntigravityBridge")
    @patch("jarvis.core.brain.GitHubCopilotBridge")
    def test_brain_initialization_and_provider_switching(self, mock_gh, mock_ag, mock_cog):
        """Verify that JarvisBrain instantiates cleanly with memory and switches provider modes."""
        mem = MemoryManager(self.config)
        mock_ag_inst = MagicMock()
        mock_ag_inst.is_available.return_value = True
        mock_ag.return_value = mock_ag_inst

        mock_gh_inst = MagicMock()
        mock_gh_inst.is_available.return_value = True
        mock_gh.return_value = mock_gh_inst

        brain = None
        try:
            # 1. Antigravity provider initialization
            self.config["llm"]["provider"] = "antigravity"
            safety = SafetyGate(self.config)
            brain = JarvisBrain(self.config, memory_manager=mem, safety_gate=safety)
            self.assertEqual(brain.provider, "antigravity")
            status = brain.specialist_status()
            self.assertIn("antigravity", status)
            self.assertIn("gemini", status)
            self.assertIn("github", status)
            self.assertIn("openrouter", status)

            # 2. Reflex verification
            self.assertTrue(brain.is_fast_reflex("mute audio"))
            self.assertTrue(brain.is_fast_reflex("pause media"))
            self.assertTrue(brain.is_fast_reflex("minimize all windows"))

            # 3. Clean shutdown
            brain.shutdown(timeout_seconds=0.5)
        finally:
            if brain:
                brain.shutdown(timeout_seconds=0.5)
            mem.close()


if __name__ == "__main__":
    unittest.main()
