"""Unit tests for jarvis/skills/reflex_macro_compiler.py."""
import json
import os
import tempfile
import unittest

from jarvis.skills.base import SkillRegistry
from jarvis.skills.reflex_macro_compiler import (
    compile_workflow_reflex,
    execute_workflow_reflex,
    list_compiled_reflex_macros,
    delete_compiled_reflex_macro,
)


class TestReflexMacroCompiler(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        # Register a mock test tool
        @SkillRegistry.register(name="_mock_reflex_step", description="Mock step for macro test")
        def _mock_reflex_step(val: str = "ok"):
            return {"echo": val, "status": "done"}

    def tearDown(self):
        SkillRegistry.unregister("_mock_reflex_step")
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)

    def test_compile_and_execute_macro(self):
        steps = [
            {
                "skill_name": "_mock_reflex_step",
                "static_args": {"val": "alpha"},
                "expected_post_condition": {"status": "done"},
            },
            {
                "skill_name": "_mock_reflex_step",
                "static_args": {"val": "beta"},
                "expected_post_condition": {"status": "done"},
            },
        ]
        res = compile_workflow_reflex(
            trigger_phrase_pattern=r"run test sequence",
            macro_name="test_macro_seq",
            steps_json=json.dumps(steps),
            db_path=self.tmp_db,
        )
        self.assertEqual(res.get("status"), "compiled")
        self.assertEqual(res.get("step_count"), 2)

        # Execute
        exec_res = execute_workflow_reflex(
            macro_name="test_macro_seq",
            db_path=self.tmp_db,
        )
        self.assertEqual(exec_res.get("status"), "success")
        self.assertEqual(exec_res.get("steps_executed"), 2)

    def test_forbidden_destructive_skill_rejection(self):
        steps = [
            {"skill_name": "execute_powershell", "static_args": {"command": "dir"}},
        ]
        res = compile_workflow_reflex(
            trigger_phrase_pattern=r"run danger",
            macro_name="danger_macro",
            steps_json=json.dumps(steps),
            db_path=self.tmp_db,
        )
        self.assertEqual(res.get("status"), "rejected")

    def test_post_condition_failure_handling(self):
        steps = [
            {
                "skill_name": "_mock_reflex_step",
                "static_args": {"val": "mismatch"},
                "expected_post_condition": {"echo": "expected_different"},
            },
        ]
        compile_workflow_reflex(
            trigger_phrase_pattern=r"run mismatch",
            macro_name="mismatch_macro",
            steps_json=json.dumps(steps),
            db_path=self.tmp_db,
        )
        res = execute_workflow_reflex(
            macro_name="mismatch_macro",
            db_path=self.tmp_db,
        )
        self.assertEqual(res.get("status"), "post_condition_failed")

    def test_list_and_delete_macro(self):
        steps = [{"skill_name": "_mock_reflex_step", "static_args": {}}]
        compile_workflow_reflex(
            trigger_phrase_pattern=r"run temp",
            macro_name="temp_macro",
            steps_json=json.dumps(steps),
            db_path=self.tmp_db,
        )
        listing = list_compiled_reflex_macros(db_path=self.tmp_db)
        self.assertEqual(listing.get("count"), 1)

        del_res = delete_compiled_reflex_macro(macro_name="temp_macro", db_path=self.tmp_db)
        self.assertEqual(del_res.get("status"), "deleted")

    def test_dynamic_reflex_engine_macro_integration(self):
        from jarvis.core.reflex_engine import DynamicReflexEngine
        steps = [{"skill_name": "_mock_reflex_step", "static_args": {"val": "hello"}}]
        compile_workflow_reflex(
            trigger_phrase_pattern=r"^launch test pipeline$",
            macro_name="test_pipeline_macro",
            steps_json=json.dumps(steps),
            db_path=self.tmp_db,
        )
        engine = DynamicReflexEngine(db_path=self.tmp_db)
        match = engine.check_habit("launch test pipeline")
        self.assertIsNotNone(match)
        self.assertEqual(match["skill_name"], "execute_workflow_reflex")
        self.assertEqual(match["skill_kwargs"]["macro_name"], "test_pipeline_macro")
        self.assertTrue(match.get("is_macro"))


if __name__ == "__main__":
    unittest.main()
