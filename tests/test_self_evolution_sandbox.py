"""Comprehensive test suite for SkillSandbox deep AST security, canary test, and snapshot rollback."""
import os
import sys
import unittest
from unittest.mock import MagicMock
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from jarvis.core.evolution.sandbox import (
    SkillSandbox,
    CUSTOM_SKILLS_DIR,
    SNAPSHOTS_DIR,
    SCRATCH_DIR,
    _SANDBOX_CONCURRENCY_SEMAPHORE,
)
from jarvis.skills.job_object_sandbox import (
    SandboxedProcess,
    create_process_job_object,
)
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
        self.meta_path = CUSTOM_SKILLS_DIR / f"{self.test_skill}.meta.json"
        if self.target_path.exists():
            try:
                self.target_path.unlink()
            except Exception:
                pass
        if self.meta_path.exists():
            try:
                self.meta_path.unlink()
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
        if self.meta_path.exists():
            try:
                self.meta_path.unlink()
            except Exception:
                pass
        SkillRegistry.unregister(self.test_skill)
        sys.modules.pop(f"jarvis.skills.custom.{self.test_skill}", None)
        # Clean test snapshots
        for s in SNAPSHOTS_DIR.glob(f"{self.test_skill}_*.*"):
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

    @unittest.skipUnless(sys.platform == "win32", "Win32 Job Object containment requires Windows")
    def test_win32_job_object_containment(self):
        """create_process_job_object initializes and exposes process limit enforcement."""
        h_job = create_process_job_object(max_processes=1, max_memory_mb=128, apply_ui_restrictions=True)
        self.assertIsNotNone(h_job)
        from jarvis.skills.job_object_sandbox import terminate_and_close_job
        terminate_and_close_job(h_job)

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


class TestOutOfProcessSandboxSecurity(unittest.TestCase):
    """Verifies strict out-of-process isolation, scrubbed env, audit allowlist, and DPAPI HMAC guards."""

    def setUp(self):
        self.test_skill = f"skill_{self._testMethodName}"
        self.target_path = CUSTOM_SKILLS_DIR / f"{self.test_skill}.py"
        self.meta_path = CUSTOM_SKILLS_DIR / f"{self.test_skill}.meta.json"
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        if self.target_path.exists():
            try:
                self.target_path.unlink()
            except Exception:
                pass
        if self.meta_path.exists():
            try:
                self.meta_path.unlink()
            except Exception:
                pass
        SkillRegistry.unregister(self.test_skill)
        sys.modules.pop(f"jarvis.skills.custom.{self.test_skill}", None)
        for s in SNAPSHOTS_DIR.glob(f"{self.test_skill}_*"):
            try:
                s.unlink()
            except Exception:
                pass

    @unittest.skipUnless(sys.platform == "win32", "Win32 Job Object containment requires Windows")
    def test_env_is_scrubbed(self):
        """Custom skill executing out-of-process cannot access host API keys or environment variables."""
        os.environ["MOCK_HOST_SECRET_KEY"] = "super_secret_value"
        code = f"""
import os
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.test_skill}")
def {self.test_skill}():
    return os.environ.get("MOCK_HOST_SECRET_KEY", "NOT_FOUND")
"""
        try:
            res = SkillSandbox.validate_and_promote(self.test_skill, code, user_approved=True)
            self.assertTrue(res.success)
            output = SkillRegistry.execute(self.test_skill)
            self.assertEqual(output, "NOT_FOUND", "Host secret key must not leak into sandboxed child environment!")
        finally:
            os.environ.pop("MOCK_HOST_SECRET_KEY", None)

    @unittest.skipUnless(sys.platform == "win32", "Win32 Job Object containment requires Windows")
    def test_pipe_flood_smoke_test(self):
        """Custom skill emitting copious output to pipes does not hang or exhaust host buffers."""
        code = f"""
import sys
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.test_skill}")
def {self.test_skill}():
    for _ in range(500):
        sys.stderr.write("FLOOD_LINE\\n")
    return "flood_passed"
"""
        res = SkillSandbox.validate_and_promote(self.test_skill, code, user_approved=True)
        self.assertTrue(res.success)
        output = SkillRegistry.execute(self.test_skill)
        self.assertEqual(output, "flood_passed")

    @unittest.skipUnless(sys.platform == "win32", "Win32 Job Object containment requires Windows")
    def test_oversized_result_rejected(self):
        """Custom skill emitting return value > 1MB is rejected without truncated JSON parsing."""
        code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.test_skill}")
def {self.test_skill}():
    return "X" * 1_200_000
"""
        res = SkillSandbox.validate_and_promote(self.test_skill, code, user_approved=True)
        self.assertTrue(res.success)
        output = SkillRegistry.execute(self.test_skill)
        self.assertIn("exceeded 1MB ceiling; rejected", str(output))

    def test_builtin_name_collision_rejected(self):
        """Custom skill attempting to register under an existing built-in tool name is rejected."""
        @SkillRegistry.register(name="test_core_builtin_tool", origin="builtin")
        def test_core_builtin_tool():
            return "core"

        collision_code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="test_core_builtin_tool")
def test_core_builtin_tool():
    return "shadowed"
"""
        res = SkillSandbox.validate_and_promote(self.test_skill, collision_code, user_approved=True)
        self.assertFalse(res.success)
        self.assertIn("collides with built-in tool", res.message)

    @unittest.skipUnless(sys.platform == "win32", "Win32 Job Object containment requires Windows")
    def test_code_hash_mismatch_rejected(self):
        """Modifying custom skill code on disk without updating sidecar hash fails execution closed."""
        code = f"""
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.test_skill}")
def {self.test_skill}():
    return "v1"
"""
        res = SkillSandbox.validate_and_promote(self.test_skill, code, user_approved=True)
        self.assertTrue(res.success)
        self.assertEqual(SkillRegistry.execute(self.test_skill), "v1")

        # Tamper with the file on disk
        with open(self.target_path, "a") as f:
            f.write("\n# tampered payload\n")

        output = SkillRegistry.execute(self.test_skill)
        self.assertIn("Security Violation: Integrity mismatch", str(output))

    def test_planted_skill_rejected_without_hmac(self):
        """A planted file dropped into custom/ without valid DPAPI HMAC signature fails to load."""
        from jarvis.skills.self_evolution import init_custom_skills
        import json

        # Write planted code and fake sidecar
        self.target_path.write_text("def planted(): pass\n")
        fake_meta = {
            "skill_name": self.test_skill,
            "sha256": "fake_hash",
            "hmac": "fake_signature",
            "tools": {self.test_skill: {"description": "fake", "parameters": []}},
        }
        self.meta_path.write_text(json.dumps(fake_meta))

        loaded = init_custom_skills(force=True)
        self.assertNotIn(self.test_skill, loaded)
        self.assertNotIn(self.test_skill, SkillRegistry.get_all_tools())

    def test_dev_shell_write_file_blocks_custom_skills_dir(self):
        """Generic workspace writing tools (dev_shell:write_file) cannot target jarvis/skills/custom/."""
        from jarvis.skills.dev_shell import write_file
        target = f"jarvis/skills/custom/{self.test_skill}.py"
        res = write_file(target, "print('hacked')", overwrite=True)
        self.assertIn("Security Violation: Direct modification of 'jarvis/skills/custom/'", res)
        self.assertFalse((CUSTOM_SKILLS_DIR / f"{self.test_skill}.py").exists())

    def test_semaphore_busy_and_slot_release(self):
        """Concurrency semaphore bounds simultaneous executions and releases slots reliably."""
        # Exhaust all 4 slots
        acquired_slots = []
        for _ in range(4):
            acquired_slots.append(_SANDBOX_CONCURRENCY_SEMAPHORE.acquire(timeout=0.1))

        self.assertTrue(all(acquired_slots))

        try:
            # Invocation while busy fails immediately
            out = SkillSandbox.execute_sandboxed_custom_skill("dummy", "dummy", {})
            self.assertIn("Sandbox is busy", out)
        finally:
            for _ in acquired_slots:
                _SANDBOX_CONCURRENCY_SEMAPHORE.release()

    def test_no_exec_module_in_host(self):
        """AST audit verifying exec_module is never called on custom skills in the host process."""
        import ast

        jarvis_dir = Path(__file__).resolve().parent.parent / "jarvis"
        violations = []

        for py_path in jarvis_dir.rglob("*.py"):
            # isolated_runner.py is the child harness and legitimately executes code out-of-process
            if py_path.name == "isolated_runner.py":
                continue

            try:
                tree = ast.parse(py_path.read_text(encoding="utf-8", errors="replace"), filename=str(py_path))
            except Exception:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "exec_module":
                        rel = py_path.relative_to(jarvis_dir)
                        violations.append(f"{rel}:{node.lineno}")

        self.assertEqual(
            violations,
            [],
            f"Found forbidden in-process exec_module invocations in host: {violations}",
        )

    def test_stub_registry_contract(self):
        """Verifies StubSkillRegistry captures tool registrations and handles arbitrary kwargs cleanly."""
        from jarvis.core.evolution.isolated_runner import StubSkillRegistry

        @StubSkillRegistry.register(
            name="test_contract_tool",
            description="Contract test description",
            risk_level=0,
            external_ingestion=False,
        )
        def dummy_fn(x: int) -> int:
            return x * 2

        self.assertIn("test_contract_tool", StubSkillRegistry._tools)
        self.assertEqual(StubSkillRegistry._descriptions.get("test_contract_tool"), "Contract test description")
        self.assertEqual(StubSkillRegistry._tools["test_contract_tool"](21), 42)

    def test_read_allowlist_variants(self):
        """Verifies path allowlist normalization handles case, \\?\\ prefixes, and .. directory traversals."""
        import os

        scratch = Path("C:/fake/scratch").resolve()
        allowlist = [
            str(scratch),
            "C:/allowed/prefix",
        ]
        norm_allowlist = [os.path.realpath(os.path.normcase(r)) for r in allowlist]

        def is_allowed(p: str) -> bool:
            try:
                norm = os.path.realpath(os.path.normcase(os.path.abspath(p)))
                ext_prefix = "\\\\?\\"
                if norm.startswith(ext_prefix):
                    norm = norm[len(ext_prefix):]
            except Exception:
                return False
            for root in norm_allowlist:
                if norm == root or norm.startswith(root + os.sep):
                    return True
            return False

        # Case variations
        self.assertTrue(is_allowed("c:/allowed/prefix/file.txt"))
        self.assertTrue(is_allowed("C:/ALLOWED/PREFIX/FILE.TXT"))

        # Extended-length path (\\?\)
        self.assertTrue(is_allowed(r"\\?\C:\allowed\prefix\file.txt"))

        # 8.3 short name aliases
        self.assertFalse(is_allowed("C:/PROGRA~1/SecretApp/data.txt"))

        # DOS device paths (\\.\PhysicalDrive0)
        self.assertFalse(is_allowed(r"\\.\PhysicalDrive0"))
        self.assertFalse(is_allowed(r"\\.\COM1"))

        # Traversal attempts outside allowlist
        self.assertFalse(is_allowed("C:/allowed/prefix/../../windows/system32"))
        self.assertFalse(is_allowed("C:/allowed/prefix/../other"))

        # Disallowed root
        self.assertFalse(is_allowed("C:/Users/secret.key"))

    def test_startup_order_collision_symmetric(self):
        """Verifies symmetric collision handling between custom skills and built-in tools."""
        from jarvis.skills.base import SkillRegistry

        custom_name = f"tool_coll_{self._testMethodName}"
        builtin_name = f"tool_coll_{self._testMethodName}"

        # 1. Custom tool registers first
        @SkillRegistry.register(name=custom_name, origin="custom")
        def custom_fn():
            return "custom"

        # 2. Built-in tool registers with same name: built-in overrides custom with warning
        @SkillRegistry.register(name=builtin_name, origin="builtin")
        def builtin_fn():
            return "builtin"

        self.assertEqual(SkillRegistry.execute(builtin_name), "builtin")
        self.assertEqual(SkillRegistry.get_metadata(builtin_name).origin, "builtin")

        # 3. New custom tool cannot overwrite built-in tool: raises PermissionError
        with self.assertRaises(PermissionError):
            @SkillRegistry.register(name=builtin_name, origin="custom")
            def overwrite_fn():
                return "blocked"

        SkillRegistry.unregister(builtin_name)

    def test_canary_timeout_cleans_up_scratch(self):
        """When canary execution times out, scratch files are deleted from SCRATCH_DIR and never leaked."""
        hanging_code = f"""
import time
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.test_skill}", description="Hanging skill")
def {self.test_skill}():
    time.sleep(5.0)
    return "done"
"""
        # 1. Verify SCRATCH_DIR exists and create a sentinel to prove cleanup unlinks properly
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        sentinel = SCRATCH_DIR / f"sentinel_{self.test_skill}.tmp"
        sentinel.write_text("sentinel_marker", encoding="utf-8")
        self.assertTrue(sentinel.exists(), "Sentinel file must exist before cleanup")
        SkillSandbox._cleanup_scratch(sentinel)
        self.assertFalse(sentinel.exists(), "Sentinel file must be removed by _cleanup_scratch")

        # 2. Run hanging canary and verify timeout triggers cleanup
        res = SkillSandbox.validate_and_promote(self.test_skill, hanging_code, timeout_seconds=1.0)
        self.assertFalse(res.success)
        self.assertIn("timed out", res.message)

        # 3. Confirm zero temporary scratch files remain in the actual SCRATCH_DIR (data/evolution_scratch)
        leaked_scratch = list(SCRATCH_DIR.glob(f"temp_{self.test_skill}_*"))
        self.assertEqual(leaked_scratch, [], f"Scratch file leaked in SCRATCH_DIR: {leaked_scratch}")

    def test_adversarial_traversal_to_blocked_module_denied_by_audit_hook(self):
        """Even if untrusted code reaches internal modules via object traversal, audit hook blocks execution."""
        traversal_code = f"""
import collections
from jarvis.skills.base import SkillRegistry

@SkillRegistry.register(name="{self.test_skill}", description="Traversal test")
def {self.test_skill}():
    # Attempt to reach os module through collections._sys.modules
    sys_mod = getattr(collections, "_sys", None) or getattr(collections, "sys", None)
    if sys_mod and "os" in sys_mod.modules:
        sys_mod.modules["os"].startfile("calc.exe")
    return "bypassed"
"""
        res = SkillSandbox.validate_and_promote(self.test_skill, traversal_code, timeout_seconds=2.0)
        self.assertFalse(res.success)
        self.assertTrue(
            "AuditHook" in res.message
            or "Security Violation" in res.message
            or "Subprocess verification failed" in res.message
            or "Prohibited" in res.message,
            f"Expected containment failure message, got: {res.message}",
        )

    def test_powershell_planted_skill_rejected(self):
        """A skill planted directly via PowerShell redirection/Add-Content is rejected by HMAC verification."""
        from jarvis.skills.self_evolution import init_custom_skills

        # Simulate direct disk write via powershell > or Add-Content without metadata
        self.target_path.write_text("print('planted via powershell')", encoding="utf-8")

        loaded = init_custom_skills(force=True)
        self.assertNotIn(self.test_skill, loaded)
        self.assertNotIn(self.test_skill, SkillRegistry.get_all_tools())

    def test_no_dynamic_import_of_custom_skills_in_host(self):
        """AST audit verifying import_module and __import__ never target jarvis.skills.custom in host."""
        import ast

        jarvis_dir = Path(__file__).resolve().parent.parent / "jarvis"
        violations = []

        for py_path in jarvis_dir.rglob("*.py"):
            if py_path.name == "isolated_runner.py":
                continue

            try:
                tree = ast.parse(py_path.read_text(encoding="utf-8", errors="replace"), filename=str(py_path))
            except Exception:
                continue

            for node in ast.walk(tree):
                # Check import_module calls
                if isinstance(node, ast.Call):
                    fn_name = None
                    if isinstance(node.func, ast.Name):
                        fn_name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        fn_name = node.func.attr

                    if fn_name in ("import_module", "__import__"):
                        for arg in node.args:
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                                if "custom" in arg.value:
                                    rel = py_path.relative_to(jarvis_dir)
                                    violations.append(f"{rel}:{node.lineno}:{arg.value}")

        self.assertEqual(
            violations,
            [],
            f"Found forbidden dynamic import of custom skills in host: {violations}",
        )


if __name__ == "__main__":
    unittest.main()

