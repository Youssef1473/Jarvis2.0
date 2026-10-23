"""Unit and regression tests for the PreToolUse safety watchdog (tools/watchdog_hook.py)."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.watchdog_hook import (
    evaluate_command_payload,
    evaluate_file_payload,
    process_hook_payload,
)


class TestSafetyWatchdogCommandPolicy(unittest.TestCase):
    """Tests evaluating command operations against the safety policy."""

    def test_broad_git_staging_is_denied(self) -> None:
        """Unambiguous broad git add commands must be hard-denied."""
        prohibited_commands = [
            "git add .",
            "git add ./",
            "git add .\\",
            "git add -A",
            "git add --all",
            "git add *",
            "git.exe add .",
            'git add "."',
            "git -C /repo add .",
        ]
        for cmd in prohibited_commands:
            with self.subTest(cmd=cmd):
                payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
                decision, reason = evaluate_command_payload(payload)
                self.assertEqual(decision, "deny", f"Expected deny for: {cmd}")
                self.assertIsNotNone(reason)
                self.assertTrue(any(term in reason.lower() for term in ("broad", "wildcard")))

    def test_explicit_git_staging_is_allowed(self) -> None:
        """Explicit file-targeted git add commands must be allowed."""
        allowed_commands = [
            "git add tools/watchdog_hook.py",
            "git add tests/test_safety_watchdog.py",
            "git add .agents/rules/ast-grep/prohibited-os-system.yml",
            "git add file1.py file2.py",
            "git add tools\\lint.py",
        ]
        for cmd in allowed_commands:
            with self.subTest(cmd=cmd):
                payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
                decision, reason = evaluate_command_payload(payload)
                self.assertEqual(decision, "allow", f"Expected allow for: {cmd}")
                self.assertIsNone(reason)

    def test_destructive_git_operations_are_denied(self) -> None:
        """Destructive git operations that discard uncommitted state must be denied."""
        destructive_commands = [
            "git reset --hard",
            "git reset --hard HEAD~1",
            "git clean -f",
            "git clean -fd",
            "git clean -fx",
            "git clean --force",
            "git checkout .",
            "git checkout -- .",
            "git restore .",
            "git restore --staged --worktree .",
            "git branch -D feature-branch",
        ]
        for cmd in destructive_commands:
            with self.subTest(cmd=cmd):
                payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
                decision, reason = evaluate_command_payload(payload)
                self.assertEqual(decision, "deny", f"Expected deny for: {cmd}")
                self.assertIsNotNone(reason)

    def test_safe_git_operations_are_allowed(self) -> None:
        """Ordinary non-destructive git commands must be allowed."""
        safe_commands = [
            "git status",
            "git status --short",
            "git diff",
            "git diff --cached",
            "git log -n 5 --oneline",
            "git checkout main",
            "git checkout -b feature-test",
            "git restore file.py",
            "git restore --staged file.py",
            "git reset --soft HEAD~1",
            "git branch -d merged-feature",
            "git branch --show-current",
        ]
        for cmd in safe_commands:
            with self.subTest(cmd=cmd):
                payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
                decision, _ = evaluate_command_payload(payload)
                self.assertEqual(decision, "allow", f"Expected allow for: {cmd}")

    def test_git_force_push_variants_are_denied(self) -> None:
        """Any force-push command must be hard-denied."""
        force_push_commands = [
            "git push --force",
            "git push -f",
            "git push origin main --force",
            "git push origin main -f",
            "git push --force-with-lease",
            "git push --force-if-includes",
        ]
        for cmd in force_push_commands:
            with self.subTest(cmd=cmd):
                payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
                decision, reason = evaluate_command_payload(payload)
                self.assertEqual(decision, "deny", f"Expected deny for: {cmd}")
                self.assertIsNotNone(reason)
                self.assertIn("force-push", reason.lower())

    def test_direct_git_commit_requires_confirmation(self) -> None:
        """Direct git commit commands require explicit confirmation (force_ask)."""
        commit_commands = [
            'git commit -m "feat: new feature"',
            "git commit --amend",
            'git.exe commit -m "update"',
        ]
        for cmd in commit_commands:
            with self.subTest(cmd=cmd):
                payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
                decision, reason = evaluate_command_payload(payload)
                self.assertEqual(decision, "force_ask", f"Expected force_ask for: {cmd}")
                self.assertIsNotNone(reason)
                self.assertIn("commit", reason.lower())

    def test_destructive_shell_commands_are_denied(self) -> None:
        """Destructive shell commands must be hard-denied."""
        destructive_commands = [
            "rmdir /s /q temp",
            "rd /s /q temp",
            "rm -rf ./build",
            "rm -r -f ./dist",
            "del /s /q *",
            "format D: /FS:NTFS",
            "diskpart /s script.txt",
        ]
        for cmd in destructive_commands:
            with self.subTest(cmd=cmd):
                payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
                decision, reason = evaluate_command_payload(payload)
                self.assertEqual(decision, "deny", f"Expected deny for: {cmd}")
                self.assertIsNotNone(reason)

    def test_ordinary_safe_developer_commands_are_allowed(self) -> None:
        """Ordinary safe development commands must execute without friction."""
        safe_commands = [
            "pytest tests/test_lint_tool.py",
            "python tools/lint.py check tools tests",
            "python tools/diagnostics.py scan jarvis tools tests",
            "python -m unittest tests/test_diagnostics_tool.py",
            "dir /b",
            "ls -la",
            "Get-ChildItem -Path .",
            "python -c 'print(1)'",
        ]
        for cmd in safe_commands:
            with self.subTest(cmd=cmd):
                payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
                decision, _ = evaluate_command_payload(payload)
                self.assertEqual(decision, "allow", f"Expected allow for: {cmd}")

    def test_chained_commands_fail_if_any_statement_violates_policy(self) -> None:
        """If any chained statement is prohibited, the entire command line is denied."""
        chained_commands = [
            "echo 'building...' && git add .",
            "git status; git reset --hard",
            "dir & rmdir /s /q data",
            "python -m pytest || git checkout .",
        ]
        for cmd in chained_commands:
            with self.subTest(cmd=cmd):
                payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
                decision, _ = evaluate_command_payload(payload)
                self.assertEqual(decision, "deny", f"Expected deny for chained command: {cmd}")

    def test_wrapped_commands_are_unpacked_and_evaluated(self) -> None:
        """Commands wrapped inside cmd /c or powershell -Command are analyzed recursively."""
        wrapped_commands = [
            'cmd /c "git add ."',
            'cmd.exe /c "git reset --hard"',
            'powershell -Command "git add -A"',
            'pwsh -c "git push --force"',
        ]
        for cmd in wrapped_commands:
            with self.subTest(cmd=cmd):
                payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
                decision, _ = evaluate_command_payload(payload)
                self.assertEqual(decision, "deny", f"Expected deny for wrapped command: {cmd}")

    def test_encoded_powershell_requires_confirmation(self) -> None:
        """Encoded PowerShell commands must require explicit confirmation (force_ask)."""
        encoded_commands = [
            "powershell -EncodedCommand JABhACAAPQAgADEA",
            "pwsh -enc VwByAGkAdABlAC0ASABvAHMAdA==",
            "powershell.exe -c [Convert]::FromBase64String('abc')",
            "powershell -c IEX (New-Object Net.WebClient).DownloadString('http://...')",
        ]
        for cmd in encoded_commands:
            with self.subTest(cmd=cmd):
                payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
                decision, _ = evaluate_command_payload(payload)
                self.assertEqual(decision, "force_ask", f"Expected force_ask for: {cmd}")


class TestSafetyWatchdogFilePolicy(unittest.TestCase):
    """Tests evaluating file editing operations against the safety policy."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temp_dir.name).resolve()
        # Create standard layout
        (self.workspace_root / ".git").mkdir(parents=True, exist_ok=True)
        (self.workspace_root / "tools").mkdir(parents=True, exist_ok=True)
        (self.workspace_root / ".agents" / "rules").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_writes_to_git_internal_state_are_denied(self) -> None:
        """Direct edits to .git directory or files are unconditionally denied."""
        prohibited_paths = [
            str(self.workspace_root / ".git" / "config"),
            str(self.workspace_root / ".git" / "HEAD"),
            str(self.workspace_root / ".git" / "hooks" / "pre-commit"),
            ".git/config",
            ".git\\config",
            ".git",
        ]
        for path in prohibited_paths:
            with self.subTest(path=path):
                payload = {
                    "toolCall": {"name": "write_file", "args": {"Path": path}},
                    "workspacePaths": [str(self.workspace_root)],
                }
                decision, reason = evaluate_file_payload(payload, self.workspace_root)
                self.assertEqual(decision, "deny", f"Expected deny for: {path}")
                self.assertIsNotNone(reason)
                self.assertIn(".git", reason)

    def test_writes_outside_workspace_are_denied(self) -> None:
        """Path traversal outside permitted workspace roots must be denied."""
        outside_paths = [
            "../../external.py",
            str(self.workspace_root.parent / "unrelated.py"),
            r"C:\Windows\System32\cmd.exe" if os.name == "nt" else "/etc/passwd",
        ]
        for path in outside_paths:
            with self.subTest(path=path):
                payload = {
                    "toolCall": {"name": "edit_file", "args": {"Path": path}},
                    "workspacePaths": [str(self.workspace_root)],
                }
                decision, reason = evaluate_file_payload(payload, self.workspace_root)
                self.assertEqual(decision, "deny", f"Expected deny for outside path: {path}")
                self.assertIsNotNone(reason)

    def test_unc_network_paths_are_denied(self) -> None:
        """UNC network paths must be denied."""
        unc_paths = [
            r"\\attacker-server\share\exploit.py",
            "//192.168.1.1/share/test.py",
        ]
        for path in unc_paths:
            with self.subTest(path=path):
                payload = {
                    "toolCall": {"name": "write_file", "args": {"Path": path}},
                    "workspacePaths": [str(self.workspace_root)],
                }
                decision, reason = evaluate_file_payload(payload, self.workspace_root)
                self.assertEqual(decision, "deny", f"Expected deny for UNC: {path}")
                self.assertIn("unc", reason.lower())

    def test_protected_safety_configuration_requires_confirmation(self) -> None:
        """Edits to protected project safety configuration require confirmation (force_ask)."""
        safety_paths = [
            str(self.workspace_root / "AGENTS.md"),
            str(self.workspace_root / ".agents" / "hooks.json"),
            str(self.workspace_root / "sgconfig.yml"),
            str(self.workspace_root / ".agents" / "rules" / "rule.yml"),
            "AGENTS.md",
            ".agents/hooks.json",
        ]
        for path in safety_paths:
            with self.subTest(path=path):
                payload = {
                    "toolCall": {"name": "edit_file", "args": {"TargetFile": path}},
                    "workspacePaths": [str(self.workspace_root)],
                }
                decision, reason = evaluate_file_payload(payload, self.workspace_root)
                self.assertEqual(decision, "force_ask", f"Expected force_ask for safety file: {path}")
                self.assertIsNotNone(reason)
                self.assertIn("safety", reason.lower())

    def test_protected_credentials_and_secrets_require_confirmation(self) -> None:
        """Edits to credential and secret files require confirmation (force_ask)."""
        secret_paths = [
            ".env",
            ".env.local",
            ".env.production",
            "config/config.yaml",
            "certs/private.key",
            "certs/server.pem",
        ]
        for path in secret_paths:
            with self.subTest(path=path):
                payload = {
                    "toolCall": {"name": "write_file", "args": {"filePath": path}},
                    "workspacePaths": [str(self.workspace_root)],
                }
                decision, reason = evaluate_file_payload(payload, self.workspace_root)
                self.assertEqual(decision, "force_ask", f"Expected force_ask for secret file: {path}")
                self.assertIsNotNone(reason)

    def test_ordinary_in_workspace_source_edits_are_allowed(self) -> None:
        """Normal source file edits within workspace must be allowed."""
        safe_paths = [
            "tools/watchdog_hook.py",
            "tests/test_safety_watchdog.py",
            "jarvis/core/pipeline.py",
            "README.md",
        ]
        for path in safe_paths:
            with self.subTest(path=path):
                payload = {
                    "toolCall": {"name": "edit_file", "args": {"Path": path}},
                    "workspacePaths": [str(self.workspace_root)],
                }
                decision, reason = evaluate_file_payload(payload, self.workspace_root)
                self.assertEqual(decision, "allow", f"Expected allow for: {path}")
                self.assertIsNone(reason)

    def test_multi_replace_file_content_evaluates_all_files(self) -> None:
        """multi_replace_file_content tool must evaluate every candidate path."""
        payload = {
            "toolCall": {
                "name": "multi_replace_file_content",
                "args": {
                    "files": [
                        {"filePath": "tools/safe.py"},
                        {"filePath": ".git/config"},
                    ]
                },
            },
            "workspacePaths": [str(self.workspace_root)],
        }
        decision, reason = evaluate_file_payload(payload, self.workspace_root)
        self.assertEqual(decision, "deny")
        self.assertIn(".git", reason)


class TestSafetyWatchdogLifecycleContract(unittest.TestCase):
    """Tests the top-level process_hook_payload entry point, error handling, and JSON contracts."""

    def test_empty_payload_fails_safe(self) -> None:
        """Empty input must fail-safe with a deny decision."""
        with io.StringIO() as stdout, io.StringIO() as stderr, patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            process_hook_payload("")
            result = json.loads(stdout.getvalue().strip())
            self.assertEqual(result.get("decision"), "deny")

    def test_malformed_json_fails_safe(self) -> None:
        """Malformed JSON input must fail-safe with a deny decision."""
        with io.StringIO() as stdout, io.StringIO() as stderr, patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            process_hook_payload("{not valid json")
            result = json.loads(stdout.getvalue().strip())
            self.assertEqual(result.get("decision"), "deny")

    def test_missing_tool_call_fails_safe(self) -> None:
        """Payload without toolCall object must fail-safe with a deny decision."""
        with io.StringIO() as stdout, io.StringIO() as stderr, patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            process_hook_payload(json.dumps({"someOtherKey": 123}))
            result = json.loads(stdout.getvalue().strip())
            self.assertEqual(result.get("decision"), "deny")

    def test_unmonitored_tool_defaults_to_allow(self) -> None:
        """Read-only tools such as read_file or search_web default to allow."""
        payload = {"toolCall": {"name": "read_file", "args": {"Path": "tools/lint.py"}}}
        with io.StringIO() as stdout, io.StringIO() as stderr, patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            process_hook_payload(json.dumps(payload))
            result = json.loads(stdout.getvalue().strip())
            self.assertEqual(result.get("decision"), "allow")

    def test_valid_json_emitted_for_allow_deny_force_ask(self) -> None:
        """Verifies stdout adheres strictly to Antigravity hook schema."""
        test_cases = [
            ({"toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}}}, "allow"),
            ({"toolCall": {"name": "run_command", "args": {"CommandLine": "git add ."}}}, "deny"),
            ({"toolCall": {"name": "run_command", "args": {"CommandLine": 'git commit -m "msg"'}}}, "force_ask"),
        ]
        for payload, expected_decision in test_cases:
            with io.StringIO() as stdout, io.StringIO() as stderr, patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                process_hook_payload(json.dumps(payload))
                raw_out = stdout.getvalue().strip()
                parsed = json.loads(raw_out)
                self.assertEqual(parsed.get("decision"), expected_decision)
                if expected_decision != "allow":
                    self.assertIn("reason", parsed)


if __name__ == "__main__":
    unittest.main()
