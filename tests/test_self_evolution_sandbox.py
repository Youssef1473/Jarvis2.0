"""Comprehensive test suite for SkillSandbox deep AST security, canary test, and snapshot rollback."""
import sys
import unittest
from unittest.mock import MagicMock
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from jarvis.core.evolution.sandbox import SkillSandbox, Win32JobObject, CUSTOM_SKILLS_DIR, SNAPSHOTS_DIR
from jarvis.skills.base import SkillRegistry


class TestSkillSandboxAdvancedSecurity(unittest.TestCase):
    """Verifies AST security gate blocks reflection, dunders, and dangerous imports."""

    def test_blocks_dynamic_reflection_getattr(self):
        code = """
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="hack_getattr")
def hack_getattr():
    fn = getattr(int, "__doc__")
    return fn
"""
        is_safe, msg = SkillSandbox.check_ast_security(code)
        self.assertFalse(is_safe)
        self.assertIn("Dynamic reflection call 'getattr' is blocked", msg)

    def test_blocks_dynamic_reflection_globals(self):
        code = """
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="hack_globals")
def hack_globals():
    return globals().keys()
"""
        is_safe, msg = SkillSandbox.check_ast_security(code)
        self.assertFalse(is_safe)
        self.assertIn("Dynamic reflection call 'globals' is blocked", msg)

    def test_blocks_dunder_subclasses_traversal(self):
        code = """
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="hack_subclasses")
def hack_subclasses():
    return ().__class__.__bases__[0].__subclasses__()
"""
        is_safe, msg = SkillSandbox.check_ast_security(code)
        self.assertFalse(is_safe)
        self.assertIn("dunder attribute '__subclasses__'", msg)

    def test_blocks_dunder_globals_traversal(self):
        code = """
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="hack_dunder_globals")
def hack_dunder_globals():
    return hack_dunder_globals.__globals__
"""
        is_safe, msg = SkillSandbox.check_ast_security(code)
        self.assertFalse(is_safe)
        self.assertIn("dunder attribute '__globals__'", msg)

    def test_blocks_dangerous_module_import_ctypes(self):
        code = """
import ctypes
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="hack_ctypes")
def hack_ctypes():
    return "ctypes"
"""
        is_safe, msg = SkillSandbox.check_ast_security(code)
        self.assertFalse(is_safe)
        self.assertIn("Importing module 'ctypes' is blocked", msg)

    def test_blocks_dangerous_module_import_from_pickle(self):
        code = """
from pickle import loads
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="hack_pickle")
def hack_pickle():
    return loads(b"")
"""
        is_safe, msg = SkillSandbox.check_ast_security(code)
        self.assertFalse(is_safe)
        self.assertIn("Importing from module 'pickle' is blocked", msg)

    def test_blocks_unbounded_while_true_loop(self):
        code = """
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="infinite_loop")
def infinite_loop():
    while True:
        pass
    return "never"
"""
        is_safe, msg = SkillSandbox.check_ast_security(code)
        self.assertFalse(is_safe)
        self.assertIn("Operational Guardrail Violation: Unbounded 'while True' loop", msg)

    def test_blocks_massive_allocation_multiplier(self):
        code = """
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="memory_bomb")
def memory_bomb():
    return [0] * 60000000
"""
        is_safe, msg = SkillSandbox.check_ast_security(code)
        self.assertFalse(is_safe)
        self.assertIn("Operational Guardrail Violation: Massive allocation multiplier", msg)


class TestSkillSandboxCanaryAndRollback(unittest.TestCase):
    """Verifies automated canary contract testing, snapshotting, and transactional rollback."""

    def setUp(self):
        self.test_skill = f"skill_{self._testMethodName}"
        self.target_path = CUSTOM_SKILLS_DIR / f"{self.test_skill}.py"
        if self.target_path.exists():
            try:
                self.target_path.unlink()
            except Exception:
                pass
        SkillRegistry.unregister(self.test_skill)
        sys.modules.pop(f"jarvis.skills.custom.{self.test_skill}", None)

    def tearDown(self):
        if self.target_path.exists():
            try:
                self.target_path.unlink()
            except Exception:
                pass
        SkillRegistry.unregister(self.test_skill)
        sys.modules.pop(f"jarvis.skills.custom.{self.test_skill}", None)
        # Clean test snapshots
        for s in SNAPSHOTS_DIR.glob(f"{self.test_skill}_*.py"):
            try:
                s.unlink()
            except Exception:
                pass

    def test_canary_rejects_module_without_skill_registry_registration(self):
        """Code that does not decorate a function with @SkillRegistry.register fails canary verification."""
        code_without_decorator = """
def standalone_helper(name: str = "Sir") -> str:
    return f"Hello, {name}"
"""
        res = SkillSandbox.validate_and_promote(self.test_skill, code_without_decorator)
        self.assertFalse(res.success)
        self.assertIn("Subprocess verification failed", res.message)
        self.assertFalse(self.target_path.exists())

    def test_canary_verifies_and_promotes_valid_skill(self):
        """Valid skill with @SkillRegistry.register passes canary verification and is promoted."""
        valid_code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(
    name="{self.test_skill}",
    description="Valid canary skill."
)
def {self.test_skill}(arg: str = "test") -> str:
    return f"Canary success: {{arg}}"
"""
        res = SkillSandbox.validate_and_promote(self.test_skill, valid_code, user_approved=True)
        self.assertTrue(res.success)
        self.assertTrue(self.target_path.exists())
        self.assertIn(self.test_skill, SkillRegistry.get_all_tools())
        self.assertEqual(SkillRegistry.execute(self.test_skill, arg="hello"), "Canary success: hello")

    def test_transactional_rollback_preserves_previous_working_version(self):
        """When an update fails canary verification, the previous working version is restored from snapshot."""
        # 1. Deploy version 1
        v1_code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.test_skill}", description="V1")
def {self.test_skill}(num: int = 1) -> str:
    return f"V1: {{num}}"
"""
        res1 = SkillSandbox.validate_and_promote(self.test_skill, v1_code, user_approved=True)
        self.assertTrue(res1.success)
        self.assertEqual(SkillRegistry.execute(self.test_skill, num=42), "V1: 42")

        # Verify snapshot was recorded when updating
        # 2. Deploy bad version 2 that fails canary (missing registration)
        bad_v2_code = f"""
def {self.test_skill}_broken():
    return "BROKEN"
"""
        res2 = SkillSandbox.validate_and_promote(self.test_skill, bad_v2_code)
        self.assertFalse(res2.success)

        # 3. Verify V1 was preserved/restored on disk and still active
        self.assertTrue(self.target_path.exists())
        with open(self.target_path, "r", encoding="utf-8") as f:
            restored_content = f.read()
        self.assertIn("V1", restored_content)

        # 4. Verify snapshot file was created in SNAPSHOTS_DIR
        snapshots = SkillSandbox.list_snapshots(self.test_skill)
        self.assertTrue(len(snapshots) >= 1)

    def test_canary_rejects_runtime_timeout_hang(self):
        """A skill that hangs or sleeps beyond 2.0s during smoke execution must fail canary."""
        hanging_code = f"""
import time
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.test_skill}", description="Hanging skill")
def {self.test_skill}():
    time.sleep(3.0)
    return "done"
"""
        res = SkillSandbox.validate_and_promote(self.test_skill, hanging_code, timeout_seconds=8.0)
        self.assertFalse(res.success)
        self.assertIn("exceeded 2.0s execution limit", res.message)
        self.assertFalse(self.target_path.exists())

    def test_canary_rejects_file_write_attempts(self):
        """A skill that attempts to perform file write operations must fail canary with PermissionError."""
        file_write_code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.test_skill}", description="Write skill")
def {self.test_skill}():
    with open("forbidden_sandbox_write.txt", "w") as f:
        f.write("blocked")
    return "done"
"""
        res = SkillSandbox.validate_and_promote(self.test_skill, file_write_code)
        self.assertFalse(res.success)
        self.assertIn("File write operations are prohibited", res.message)
        self.assertFalse(self.target_path.exists())

    def test_canary_rejects_mocked_dangerous_modules(self):
        """A skill that attempts to invoke blocked modules like raw socket creation must fail canary."""
        code = f"""
import socket
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.test_skill}", description="Socket skill")
def {self.test_skill}():
    s = socket.socket()
    return "done"
"""
        res = SkillSandbox.validate_and_promote(self.test_skill, code)
        self.assertFalse(res.success)
        self.assertIn("Raw socket creation is disabled", res.message)
        self.assertFalse(self.target_path.exists())

    def test_win32_job_object_containment(self):
        """Win32JobObject initializes and exposes process assignment and limit enforcement."""
        with Win32JobObject(max_processes=1, max_memory_bytes=128 * 1024 * 1024) as job:
            if sys.platform == "win32":
                self.assertIsNotNone(job._handle)
                # Assign dummy object without crashing
                dummy = MagicMock()
                dummy._handle = None
                self.assertFalse(job.assign_process(dummy))

    def test_episodic_lesson_recorded_on_validation_failure(self):
        """SkillSandbox synthesizes a Reflexion lesson and records it into MemoryManager on failure."""
        mock_mem = MagicMock()
        mock_mem.record_episodic_lesson.return_value = 42

        # 1. Trigger AST security failure with memory_manager
        bad_ast_code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.test_skill}")
def {self.test_skill}():
    return getattr(int, "__doc__")
"""
        res = SkillSandbox.validate_and_promote(self.test_skill, bad_ast_code, memory_manager=mock_mem)
        self.assertFalse(res.success)
        mock_mem.record_episodic_lesson.assert_called_once()
        call_kwargs = mock_mem.record_episodic_lesson.call_args.kwargs
        self.assertEqual(call_kwargs["tool_name"], "create_or_update_custom_skill")
        self.assertIn(f"self_evolution:{self.test_skill}", call_kwargs["trigger_context"])
        self.assertIn("prohibited", call_kwargs["lesson"].lower())


if __name__ == "__main__":
    unittest.main()

