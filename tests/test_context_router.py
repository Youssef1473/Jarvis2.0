"""Comprehensive Unit Tests and Evaluation Suite for the Deterministic Context Router.

Covers:
- All 10 domain categories + UNKNOWN fallback.
- Disambiguation between similar domains (e.g., UIA vs Process Containment).
- Negative keyword rejection.
- Multi-subsystem merging, deduplication, and risk tier escalation.
- Confidence scoring and uncertainty reporting.
- Input robustness: empty, whitespace, None, truncated, unicode, long, and adversarial text.
- Hook JSON protocol and PreInvocation response format.
- Invariants: zero subprocesses, zero network, zero filesystem mutation.
- Catalog path confinement and existence.
- Labelled evaluation dataset (25 prompts) and holdout dataset (15 prompts).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from tools.context_router import (
    _REGISTRY_CACHE,
    CONTEXT_CATALOG,
    MAX_LESSONS_BUDGET_CHARS,
    MAX_REGISTRY_BYTES,
    MAX_RETRIEVED_LESSONS,
    SCHEMA_VERSION,
    LessonEntry,
    build_context_packet,
    classify_task,
    classify_task_evidence,
    map_lesson_to_categories,
    parse_lesson_registry,
    process_hook_input,
    resolve_context,
    retrieve_relevant_lessons,
    sanitize_inert_text,
    serialize_hook_response,
)

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


class TestContextRouterClassification(unittest.TestCase):
    """Verifies single-domain classification across all 10 supported categories."""

    def test_audio_threading_classification(self) -> None:
        prompt = "Optimize WASAPI playback worker loop and handle Kokoro neural TTS synthesis"
        res = classify_task(prompt)
        self.assertEqual(res.primary_category, "AUDIO_THREADING")
        self.assertIn("AUDIO_THREADING", res.matched_categories)
        self.assertIn(res.confidence, ("HIGH", "MEDIUM"))
        self.assertEqual(res.risk_tier, "HIGH")

    def test_sqlite_persistence_classification(self) -> None:
        prompt = "Fix database transaction locking by routing writes to SingleWriterActor with WAL pragma"
        res = classify_task(prompt)
        self.assertEqual(res.primary_category, "SQLITE_PERSISTENCE")
        self.assertEqual(res.risk_tier, "HIGH")

    def test_security_boundary_classification(self) -> None:
        prompt = "Validate egress url destination to prevent SSRF and check dual-key approval manager"
        res = classify_task(prompt)
        self.assertEqual(res.primary_category, "SECURITY_BOUNDARY")
        self.assertEqual(res.risk_tier, "CRITICAL")

    def test_windows_ui_automation_classification(self) -> None:
        prompt = "Inspect accessibility element tree in notepad using pywinauto and UI Automation"
        res = classify_task(prompt)
        self.assertEqual(res.primary_category, "WINDOWS_UI_AUTOMATION")
        self.assertEqual(res.risk_tier, "MEDIUM")

    def test_windows_process_containment_classification(self) -> None:
        prompt = "Configure Win32 Job Object containment with CREATE_SUSPENDED and kill_on_job_close limit"
        res = classify_task(prompt)
        self.assertEqual(res.primary_category, "WINDOWS_PROCESS_CONTAINMENT")
        self.assertEqual(res.risk_tier, "CRITICAL")

    def test_api_compatibility_classification(self) -> None:
        prompt = "Ensure backward compatibility of public API signatures in SkillRegistry.register"
        res = classify_task(prompt)
        self.assertEqual(res.primary_category, "API_COMPATIBILITY")
        self.assertEqual(res.risk_tier, "MEDIUM")

    def test_ui_frontend_classification(self) -> None:
        prompt = "Update FastAPI HUD server WebSocket endpoint for streaming telemetry feed"
        res = classify_task(prompt)
        self.assertEqual(res.primary_category, "UI_FRONTEND")
        self.assertEqual(res.risk_tier, "MEDIUM")

    def test_testing_verification_classification(self) -> None:
        prompt = "Run fast tests runner with pytest mock assertions to check verification suite"
        res = classify_task(prompt)
        self.assertEqual(res.primary_category, "TESTING_VERIFICATION")
        self.assertEqual(res.risk_tier, "LOW")

    def test_architecture_classification(self) -> None:
        prompt = "Review subsystem boundaries according to master architectural blueprint and ADR index"
        res = classify_task(prompt)
        self.assertEqual(res.primary_category, "ARCHITECTURE")
        self.assertEqual(res.risk_tier, "MEDIUM")

    def test_documentation_classification(self) -> None:
        prompt = "Update session log in docs/agent/SESSION_LOG.md and record entry in LESSONS.md"
        res = classify_task(prompt)
        self.assertEqual(res.primary_category, "DOCUMENTATION")
        self.assertEqual(res.risk_tier, "LOW")


class TestDisambiguationAndNegativeRejection(unittest.TestCase):
    """Verifies disambiguation between similar domains and negative keyword rejection."""

    def test_disambiguation_uia_vs_job_object(self) -> None:
        prompt_uia = "Click the save button on the window using pywinauto and UIA focus"
        res_uia = classify_task(prompt_uia)
        self.assertEqual(res_uia.primary_category, "WINDOWS_UI_AUTOMATION")
        self.assertNotIn("WINDOWS_PROCESS_CONTAINMENT", res_uia.matched_categories)

        prompt_containment = "Contain child process inside a Win32 Job Object sandbox"
        res_containment = classify_task(prompt_containment)
        self.assertEqual(res_containment.primary_category, "WINDOWS_PROCESS_CONTAINMENT")
        self.assertNotIn("WINDOWS_UI_AUTOMATION", res_containment.matched_categories)

    def test_negative_keyword_rejection_unrelated_prompts(self) -> None:
        unrelated_prompts = [
            "What is the capital of Australia?",
            "Write a poem about the autumn leaves falling gently",
            "How do I bake sourdough bread with whole wheat flour?",
            "Calculate 42 multiplied by 137",
        ]
        for prompt in unrelated_prompts:
            res = classify_task(prompt)
            self.assertEqual(res.primary_category, "UNKNOWN")
            self.assertEqual(res.confidence, "LOW")
            self.assertEqual(res.risk_tier, "HIGH")  # Fail-safe escalation


class TestMultiSubsystemMerging(unittest.TestCase):
    """Verifies deduplicated merging and risk tier escalation across multi-domain prompts."""

    def test_audio_and_sqlite_merging(self) -> None:
        prompt = "Refactor WASAPI playback worker and persist audio transcription events to SQLite database"
        res = classify_task(prompt)
        self.assertIn("AUDIO_THREADING", res.matched_categories)
        self.assertIn("SQLITE_PERSISTENCE", res.matched_categories)

        packet = resolve_context(res)
        module_paths = [m["path"] for m in packet["canonical_modules"]]

        # Canonical modules from both subsystems must be included
        self.assertIn("jarvis/audio/tts.py", module_paths)
        self.assertIn("jarvis/core/memory/sqlite_base.py", module_paths)

        # Risk tier must be HIGH (max of both)
        self.assertEqual(packet["task_classification"]["risk_tier"], "HIGH")

        # No duplicate modules, rules, or invariants
        self.assertEqual(len(module_paths), len(set(module_paths)))
        invariants = packet["mandatory_invariants"]
        self.assertEqual(len(invariants), len(set(invariants)))

    def test_critical_risk_escalation(self) -> None:
        prompt = "Inspect UIA accessibility element tree and authorize mutating action through DualKeyApprovalManager"
        res = classify_task(prompt)
        self.assertIn("WINDOWS_UI_AUTOMATION", res.matched_categories)
        self.assertIn("SECURITY_BOUNDARY", res.matched_categories)

        packet = resolve_context(res)
        # WINDOWS_UI_AUTOMATION is MEDIUM, SECURITY_BOUNDARY is CRITICAL -> must escalate to CRITICAL
        self.assertEqual(packet["task_classification"]["risk_tier"], "CRITICAL")

        # Specialists from both must be included without duplicates
        specialists = packet["recommended_specialists"]
        self.assertIn("security-auditor", specialists)
        self.assertIn("systems-architect", specialists)
        self.assertEqual(len(specialists), len(set(specialists)))


class TestConfidenceAndUncertainties(unittest.TestCase):
    """Verifies evidence-based confidence calculations and uncertainty tracking."""

    def test_high_confidence_on_multiple_matches(self) -> None:
        prompt = "WASAPI playback thread with Kokoro neural TTS speech synthesis and epoch barge-in cancellation"
        res = classify_task(prompt)
        self.assertEqual(res.confidence, "HIGH")
        self.assertGreaterEqual(res.scores[res.primary_category], 3)

    def test_medium_confidence_on_two_matches(self) -> None:
        prompt = "Check SQLite database"
        res = classify_task(prompt)
        self.assertEqual(res.confidence, "MEDIUM")
        self.assertEqual(res.scores[res.primary_category], 2)

    def test_low_confidence_on_single_match(self) -> None:
        prompt = "Adjust sound volume"
        res = classify_task(prompt)
        self.assertEqual(res.confidence, "LOW")

    def test_tied_scores_flag_ambiguity(self) -> None:
        # 1 match for SQLite ('db'), 1 match for UIA ('uia')
        prompt = "Check db and uia"
        res = classify_task(prompt)
        self.assertEqual(res.confidence, "LOW")
        self.assertTrue(any("tied" in u or "Ambiguous" in u for u in res.uncertainties))

    def test_truncated_input_sets_uncertainty(self) -> None:
        prompt = "Investigate WASAPI audio"
        res = classify_task(prompt, input_completeness="TRUNCATED")
        self.assertEqual(res.input_completeness, "TRUNCATED")
        self.assertTrue(any("truncated" in u for u in res.uncertainties))


class TestInputRobustnessAndEdgeCases(unittest.TestCase):
    """Verifies graceful handling of non-standard, truncated, unicode, and adversarial inputs."""

    def test_empty_and_whitespace_prompts(self) -> None:
        for empty_val in ("", "   ", "\t\n\r  "):
            res = classify_task(empty_val)
            self.assertEqual(res.primary_category, "UNKNOWN")
            self.assertEqual(res.input_completeness, "EMPTY")
            self.assertEqual(res.risk_tier, "HIGH")

    def test_none_and_non_string_prompts(self) -> None:
        # None
        res_none = classify_task(None)  # type: ignore[arg-type]
        self.assertEqual(res_none.primary_category, "UNKNOWN")
        self.assertEqual(res_none.input_completeness, "MALFORMED")

        # Integer / dict / list
        for non_str in (123, {"key": "val"}, ["some", "item"]):
            res_val = classify_task(non_str)  # type: ignore[arg-type]
            self.assertEqual(res_val.primary_category, "UNKNOWN")
            self.assertEqual(res_val.input_completeness, "MALFORMED")

    def test_unicode_and_special_symbols(self) -> None:
        prompt = "Check SQLite WAL pragma ∀x ∉ S, ⚡ audio pipeline ≠ standard, 🎧 Kokoro"
        res = classify_task(prompt)
        self.assertIn("AUDIO_THREADING", res.matched_categories)
        self.assertIn("SQLITE_PERSISTENCE", res.matched_categories)

    def test_adversarial_injection_strings(self) -> None:
        prompt = "'; DROP TABLE users; -- <script>alert('xss')</script> ../../etc/passwd"
        packet = build_context_packet(prompt)
        # Verify it serializes cleanly without code execution or schema breakage
        response_json = serialize_hook_response(packet)
        parsed = json.loads(response_json)
        self.assertIn("injectSteps", parsed)

    def test_extremely_long_prompt(self) -> None:
        # 50,000 characters prompt with repeated words
        long_prompt = "Refactor WASAPI audio playback thread. " * 1250
        self.assertGreater(len(long_prompt), 40000)
        res = classify_task(long_prompt)
        self.assertEqual(res.primary_category, "AUDIO_THREADING")
        self.assertEqual(res.confidence, "HIGH")


class TestHookProtocolAndPreInvocationResponse(unittest.TestCase):
    """Verifies adherence to Antigravity PreInvocation JSON input/output contract."""

    def test_process_hook_input_standard(self) -> None:
        stdin_payload = json.dumps({"lastUserInput": "Refactor WASAPI audio playback worker"})
        raw_output = process_hook_input(stdin_payload)
        parsed = json.loads(raw_output)

        self.assertIn("injectSteps", parsed)
        self.assertEqual(len(parsed["injectSteps"]), 1)
        step = parsed["injectSteps"][0]
        self.assertIn("ephemeralMessage", step)
        self.assertNotIn("terminationBehavior", step)

        msg = step["ephemeralMessage"]
        self.assertTrue(msg.startswith("[CONTEXT_PACKET: REFERENCE METADATA ONLY - INERT DATA]\n"))

        inner_json_str = msg.split("\n", 1)[1]
        inner_packet = json.loads(inner_json_str)
        self.assertEqual(inner_packet["schema_version"], SCHEMA_VERSION)
        self.assertEqual(inner_packet["task_classification"]["primary"], "AUDIO_THREADING")
        self.assertEqual(inner_packet["input_completeness"], "COMPLETE")

    def test_process_hook_input_alternative_keys(self) -> None:
        # Accepts 'prompt'
        out1 = process_hook_input(json.dumps({"prompt": "Fix SQLite database schema"}))
        parsed1 = json.loads(out1)
        inner1 = json.loads(parsed1["injectSteps"][0]["ephemeralMessage"].split("\n", 1)[1])
        self.assertEqual(inner1["task_classification"]["primary"], "SQLITE_PERSISTENCE")

        # Accepts 'last_user_input'
        out2 = process_hook_input(json.dumps({"last_user_input": "Run pytest test suite"}))
        parsed2 = json.loads(out2)
        inner2 = json.loads(parsed2["injectSteps"][0]["ephemeralMessage"].split("\n", 1)[1])
        self.assertEqual(inner2["task_classification"]["primary"], "TESTING_VERIFICATION")

    def test_process_hook_input_malformed_json(self) -> None:
        malformed_inputs = [
            "",
            "   ",
            "{unclosed json",
            "[1, 2, 3]",
            "null",
            "just plain text without braces",
        ]
        for malformed in malformed_inputs:
            out = process_hook_input(malformed)
            parsed = json.loads(out)
            self.assertIn("injectSteps", parsed)
            inner = json.loads(parsed["injectSteps"][0]["ephemeralMessage"].split("\n", 1)[1])
            self.assertEqual(inner["task_classification"]["primary"], "UNKNOWN")
            self.assertEqual(inner["task_classification"]["risk_tier"], "HIGH")

    def test_process_hook_input_flagged_truncated(self) -> None:
        stdin_payload = json.dumps({
            "lastUserInput": "Refactor WASAPI audio",
            "isTruncated": True,
        })
        out = process_hook_input(stdin_payload)
        parsed = json.loads(out)
        inner = json.loads(parsed["injectSteps"][0]["ephemeralMessage"].split("\n", 1)[1])
        self.assertEqual(inner["input_completeness"], "TRUNCATED")

    def test_process_hook_input_invocation_num_zero(self) -> None:
        stdin_payload = json.dumps({
            "lastUserInput": "Refactor WASAPI audio",
            "invocationNum": 0,
        })
        out = process_hook_input(stdin_payload)
        parsed = json.loads(out)
        self.assertEqual(len(parsed["injectSteps"]), 1)
        self.assertIn("ephemeralMessage", parsed["injectSteps"][0])

    def test_process_hook_input_invocation_num_subsequent_suppressed(self) -> None:
        # On subsequent model calls in the same turn, PreInvocation should return empty injectSteps
        for inv_num in (1, 2, 5):
            stdin_payload = json.dumps({
                "lastUserInput": "Refactor WASAPI audio",
                "invocationNum": inv_num,
            })
            out = process_hook_input(stdin_payload)
            parsed = json.loads(out)
            self.assertEqual(parsed, {"injectSteps": []})

    def test_process_hook_input_stderr_diagnostics(self) -> None:
        import io
        import sys
        old_stderr = sys.stderr
        captured = io.StringIO()
        try:
            sys.stderr = captured
            out = process_hook_input(json.dumps({"lastUserInput": "Refactor WASAPI audio", "invocationNum": 0}))
            parsed = json.loads(out)
            self.assertIn("injectSteps", parsed)
            self.assertIn("[ContextRouter] Invocation #0: classified as AUDIO_THREADING", captured.getvalue())
        finally:
            sys.stderr = old_stderr


class TestInvariantSafetyAndConfinement(unittest.TestCase):
    """Verifies router operates with zero subprocesses, zero network, and strict path confinement."""

    def test_zero_subprocesses_and_zero_network(self) -> None:
        with patch.object(subprocess, "Popen") as mock_popen, \
             patch.object(subprocess, "run") as mock_run, \
             patch.object(os, "system") as mock_system, \
             patch.object(socket, "socket") as mock_socket, \
             patch.object(urllib.request, "urlopen") as mock_urlopen:

            payload = json.dumps({"lastUserInput": "Refactor WASAPI playback and SQLite WAL"})
            process_hook_input(payload)

            mock_popen.assert_not_called()
            mock_run.assert_not_called()
            mock_system.assert_not_called()
            mock_socket.assert_not_called()
            mock_urlopen.assert_not_called()

    def test_catalog_paths_are_confined_and_exist(self) -> None:
        """Verifies all paths in CONTEXT_CATALOG are workspace-relative, safe, and exist on disk."""
        for category, cat_data in CONTEXT_CATALOG.items():
            for mod in cat_data.get("canonical_modules", []):
                rel_path = mod["path"]
                self.assertFalse(os.path.isabs(rel_path), f"Path {rel_path} in {category} must not be absolute.")
                self.assertNotIn("..", rel_path, f"Path {rel_path} in {category} must not contain traversal.")
                full_path = WORKSPACE_ROOT / rel_path
                self.assertTrue(full_path.exists(), f"Canonical module {rel_path} does not exist on disk!")

            for rule in cat_data.get("applicable_rules", []):
                rel_path = rule["path"]
                self.assertFalse(os.path.isabs(rel_path))
                self.assertNotIn("..", rel_path)
                full_path = WORKSPACE_ROOT / rel_path
                self.assertTrue(full_path.exists(), f"Applicable rule {rel_path} does not exist on disk!")

            for adr in cat_data.get("applicable_adrs", []):
                rel_path = adr["path"]
                self.assertFalse(os.path.isabs(rel_path))
                self.assertNotIn("..", rel_path)
                full_path = WORKSPACE_ROOT / rel_path
                self.assertTrue(full_path.exists(), f"Applicable ADR {rel_path} does not exist on disk!")

            for test in cat_data.get("verification_suite", []):
                rel_path = test["path"]
                self.assertFalse(os.path.isabs(rel_path))
                self.assertNotIn("..", rel_path)
                full_path = WORKSPACE_ROOT / rel_path
                self.assertTrue(full_path.exists(), f"Verification test {rel_path} does not exist on disk!")

    def test_audio_threading_catalog_excludes_les_001(self) -> None:
        """Verifies LES-001 (sandbox loopback socket limitation) is decoupled from audio threading."""
        audio_lessons = CONTEXT_CATALOG["AUDIO_THREADING"].get("relevant_lessons", [])
        self.assertEqual(audio_lessons, [], "AUDIO_THREADING must not reference socket IPC lessons like LES-001.")

    def test_testing_verification_dynamically_retrieves_les_001(self) -> None:
        """Verifies LES-001 is dynamically discovered and retrieved for relevant testing prompts."""
        # Catalog itself has empty static lessons (dynamic discovery decoupled from static catalog)
        self.assertEqual(CONTEXT_CATALOG["TESTING_VERIFICATION"].get("relevant_lessons", []), [])

        # Relevant testing prompt retrieves LES-001
        prompt = "Investigate pytest failure in test_milestone1_security_hardening.py with loopback socket drop in sandbox"
        packet = build_context_packet(prompt)
        lesson_ids = [l["id"] for l in packet.get("relevant_lessons", [])]
        self.assertIn("LES-001", lesson_ids)

        # Unrelated testing prompt does NOT retrieve LES-001
        unrelated_test_prompt = "Run fast tests runner with pytest mock assertions to check verification suite"
        unrelated_packet = build_context_packet(unrelated_test_prompt)
        self.assertEqual(unrelated_packet.get("relevant_lessons", []), [])

    def test_affinity_mask_invariants_match_authoritative_source(self) -> None:
        """Verifies mandatory invariants cite exact authoritative affinity masks (0x0003 and 0x00FC)."""
        invariants = CONTEXT_CATALOG["AUDIO_THREADING"].get("mandatory_invariants", [])
        self.assertTrue(any("MASK_PCORE_AUDIO_ISOLATED (0x0003)" in inv for inv in invariants))
        self.assertTrue(any("MASK_PCORE_COMPUTE (0x00FC)" in inv for inv in invariants))


class TestLabelledEvaluationAndHoldoutBenchmark(unittest.TestCase):
    """Labelled Prompt Benchmark (25 prompts) and Separate Holdout Benchmark (15 prompts)."""

    EVALUATION_DATASET: ClassVar[list[dict[str, str]]] = [
        # AUDIO_THREADING
        {"prompt": "Investigate WASAPI playback worker underrun during Kokoro speech synthesis", "expected": "AUDIO_THREADING"},
        {"prompt": "Configure MMCSS Pro Audio elevation and P-core hardware affinity 0x0003 for wake-word worker", "expected": "AUDIO_THREADING"},
        {"prompt": "Fix epoch barge-in cancellation in cancellation.py when user starts speaking", "expected": "AUDIO_THREADING"},
        # SQLITE_PERSISTENCE
        {"prompt": "Diagnose SQLite database disk I/O lock during concurrent WAL reads and writes", "expected": "SQLITE_PERSISTENCE"},
        {"prompt": "Route memory consolidation transaction through SingleWriterActor with PRAGMA journal_mode=WAL", "expected": "SQLITE_PERSISTENCE"},
        {"prompt": "Search past interactions using sqlite-vec and fts5 bm25 index in interaction_memory.py", "expected": "SQLITE_PERSISTENCE"},
        # SECURITY_BOUNDARY
        {"prompt": "Audit tool invocation requests to ensure DualKeyApprovalManager gate is never bypassed", "expected": "SECURITY_BOUNDARY"},
        {"prompt": "Block outbound SSRF request in validate_egress_url using IP pinning", "expected": "SECURITY_BOUNDARY"},
        {"prompt": "Track untrusted web scrape data using TurnTaintTracker in security_boundaries.py", "expected": "SECURITY_BOUNDARY"},
        # WINDOWS_UI_AUTOMATION
        {"prompt": "Use pywinauto accessibility tree to locate the search field on Windows desktop", "expected": "WINDOWS_UI_AUTOMATION"},
        {"prompt": "Automate Edge browser navigation silently over Chrome DevTools Protocol CDP WebSocket", "expected": "WINDOWS_UI_AUTOMATION"},
        {"prompt": "Manage COM STA apartment thread lifecycle in windows_uia.py", "expected": "WINDOWS_UI_AUTOMATION"},
        # WINDOWS_PROCESS_CONTAINMENT
        {"prompt": "Spawn untrusted subprocess with CREATE_SUSPENDED and assign to Win32 Job Object sandbox", "expected": "WINDOWS_PROCESS_CONTAINMENT"},
        {"prompt": "Ensure all child processes are terminated on close via JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", "expected": "WINDOWS_PROCESS_CONTAINMENT"},
        {"prompt": "Prevent orphan process leaks by enforcing Job Object containment in job_object_sandbox.py", "expected": "WINDOWS_PROCESS_CONTAINMENT"},
        # API_COMPATIBILITY
        {"prompt": "Maintain backward compatibility of public API function signatures in skills/base.py", "expected": "API_COMPATIBILITY"},
        {"prompt": "Check SkillRegistry.register decorators to prevent breaking changes for test shims", "expected": "API_COMPATIBILITY"},
        # UI_FRONTEND
        {"prompt": "Implement Starlette WebSocket telemetry stream for Jarvis holographic HUD", "expected": "UI_FRONTEND"},
        {"prompt": "Update FastAPI ui/server.py dashboard routes to prevent event loop blocking", "expected": "UI_FRONTEND"},
        # TESTING_VERIFICATION
        {"prompt": "Execute verification runner with python tools/run_fast_tests.py --fast", "expected": "TESTING_VERIFICATION"},
        {"prompt": "Enforce test truthfulness rules and distinguish PASS from NOT VERIFIED states", "expected": "TESTING_VERIFICATION"},
        # ARCHITECTURE
        {"prompt": "Inspect epistemic authority hierarchy in docs/architecture/INDEX.md before refactoring", "expected": "ARCHITECTURE"},
        {"prompt": "Audit module ownership boundaries defined in JARVIS_2.0_MASTER_ARCHITECTURAL_BLUEPRINT.txt", "expected": "ARCHITECTURE"},
        # DOCUMENTATION
        {"prompt": "Record post-mortem findings into docs/agent/LESSONS.md and update PROJECT_STATE.md", "expected": "DOCUMENTATION"},
        # UNKNOWN
        {"prompt": "What are the key ingredients for making traditional Italian risotto?", "expected": "UNKNOWN"},
    ]

    HOLDOUT_DATASET: ClassVar[list[dict[str, str]]] = [
        {"prompt": "Tuning WASAPI ring-buffer sizes to prevent audio dropout on high CPU load", "expected": "AUDIO_THREADING"},
        {"prompt": "Enforce PRAGMA synchronous=NORMAL on SQLite connection pool initialization", "expected": "SQLITE_PERSISTENCE"},
        {"prompt": "Validate egress URL against localhost and private IPv4 ranges to stop SSRF", "expected": "SECURITY_BOUNDARY"},
        {"prompt": "Send click event to calculator window button using UI Automation tree elements", "expected": "WINDOWS_UI_AUTOMATION"},
        {"prompt": "Assign child python process to Win32 Job Object before resuming main thread", "expected": "WINDOWS_PROCESS_CONTAINMENT"},
        {"prompt": "Add deprecation warning shim for renamed method in SkillRegistry while preserving public API", "expected": "API_COMPATIBILITY"},
        {"prompt": "Render system metrics on HTML dashboard served by Starlette ASGI app", "expected": "UI_FRONTEND"},
        {"prompt": "Write pytest fixture asserting zero exit code on fast test runner execution", "expected": "TESTING_VERIFICATION"},
        {"prompt": "Check ADR index and master architectural blueprint for consensus on persistence", "expected": "ARCHITECTURE"},
        {"prompt": "Append today's task achievements to docs/agent/SESSION_LOG.md", "expected": "DOCUMENTATION"},
        {"prompt": "Explain the differences between quantum mechanics and general relativity", "expected": "UNKNOWN"},
        {"prompt": "How to repair a punctured bicycle tire on the road?", "expected": "UNKNOWN"},
        {"prompt": "Write a regex matching valid email addresses", "expected": "UNKNOWN"},
        {"prompt": "Investigate Kokoro audio synthesis latency during simultaneous speech playback", "expected": "AUDIO_THREADING"},
        {"prompt": "Check whether untrusted input taint tracker correctly tags external webhook payload", "expected": "SECURITY_BOUNDARY"},
    ]

    def test_labelled_evaluation_dataset(self) -> None:
        """Evaluates classification accuracy on the 25-prompt primary evaluation set."""
        correct = 0
        total = len(self.EVALUATION_DATASET)
        failures = []

        for item in self.EVALUATION_DATASET:
            prompt = item["prompt"]
            expected = item["expected"]
            res = classify_task(prompt)
            if res.primary_category == expected:
                correct += 1
            else:
                failures.append(f"Prompt '{prompt[:40]}...' Expected {expected}, got {res.primary_category} (scores={res.scores})")

        accuracy = (correct / total) * 100.0
        self.assertEqual(
            correct,
            total,
            f"Evaluation set accuracy {accuracy:.1f}% ({correct}/{total}). Failures: {failures}",
        )

    def test_holdout_dataset(self) -> None:
        """Evaluates classification accuracy on the 15-prompt independent holdout set."""
        correct = 0
        total = len(self.HOLDOUT_DATASET)
        failures = []

        for item in self.HOLDOUT_DATASET:
            prompt = item["prompt"]
            expected = item["expected"]
            res = classify_task(prompt)
            if res.primary_category == expected:
                correct += 1
            else:
                failures.append(f"Holdout '{prompt[:40]}...' Expected {expected}, got {res.primary_category} (scores={res.scores})")

        accuracy = (correct / total) * 100.0
        self.assertEqual(
            correct,
            total,
            f"Holdout set accuracy {accuracy:.1f}% ({correct}/{total}). Failures: {failures}",
        )


class TestLessonRegistryParsingAndDynamicRetrieval(unittest.TestCase):
    """Verifies safe deterministic registry parsing, dynamic lesson retrieval, and sanitization."""

    def test_parse_valid_registry_fields(self) -> None:
        lessons = parse_lesson_registry()
        self.assertGreaterEqual(len(lessons), 1)

        les_001 = next((l for l in lessons if l.id == "LES-001"), None)
        self.assertIsNotNone(les_001, "LES-001 not found in parsed registry")
        assert les_001 is not None

        self.assertEqual(les_001.id, "LES-001")
        self.assertIn("Antigravity Sandbox Loopback Socket Limitation", les_001.title)
        self.assertIn("Testing", les_001.subsystem_domain)
        self.assertEqual(les_001.date_recorded, "2026-11-04")
        self.assertGreater(len(les_001.observed_problem), 20)
        self.assertGreater(len(les_001.reproduction_evidence), 20)
        self.assertGreater(len(les_001.root_cause), 20)
        self.assertGreater(len(les_001.prevention_rules), 20)
        self.assertIn("HIGH", les_001.confidence)
        self.assertIn("Antigravity Standard Sandbox Mode", les_001.applicability)
        self.assertIn("TESTING_VERIFICATION", les_001.applicable_categories)
        self.assertIn("SECURITY_BOUNDARY", les_001.applicable_categories)

    def test_parse_missing_file_fails_soft(self) -> None:
        missing_path = WORKSPACE_ROOT / "docs" / "nonexistent_lessons_dir" / "NO_LESSONS.md"
        lessons = parse_lesson_registry(missing_path)
        self.assertEqual(lessons, [])

    def test_parse_oversized_file_fails_soft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            oversized_file = Path(tmp_dir) / "OVERSIZED_LESSONS.md"
            # Write 130KB (> MAX_REGISTRY_BYTES = 128KB)
            oversized_file.write_text("A" * (MAX_REGISTRY_BYTES + 2048), encoding="utf-8")
            lessons = parse_lesson_registry(oversized_file, allow_external_fixtures=True)
            self.assertEqual(lessons, [])

    def test_parse_malformed_and_duplicate_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_file = Path(tmp_dir) / "TEST_LESSONS.md"
            content = """# Header

## LES-001: First Valid Lesson
- **Lesson ID:** `LES-001`
- **Subsystem Domain:** Testing
- **Date Recorded:** 2026-11-04
- **Observed Problem:** Problem 1
- **Prevention Rules:** Rule 1
- **Confidence & Applicability Boundaries:**
  - *Confidence:* HIGH
  - *Applicability:* Sandbox

## LES-001: Duplicate Lesson ID
- **Lesson ID:** `LES-001`
- **Subsystem Domain:** Audio
- **Date Recorded:** 2026-11-05
- **Prevention Rules:** Rule dup

## LES-002: Malformed Missing Prevention Rules
- **Lesson ID:** `LES-002`
- **Subsystem Domain:** Audio
- **Date Recorded:** 2026-11-05
"""
            test_file.write_text(content, encoding="utf-8")
            lessons = parse_lesson_registry(test_file, allow_external_fixtures=True)
            # Only the first LES-001 should be parsed; duplicate and malformed skipped
            self.assertEqual(len(lessons), 1)
            self.assertEqual(lessons[0].id, "LES-001")
            self.assertEqual(lessons[0].title, "First Valid Lesson")

    def test_map_lesson_to_categories_deterministic(self) -> None:
        cats_audio = map_lesson_to_categories("Audio / WASAPI Playback", "WASAPI buffer underrun")
        self.assertIn("AUDIO_THREADING", cats_audio)

        cats_sqlite = map_lesson_to_categories("SQLite Persistence", "Database lock contention")
        self.assertIn("SQLITE_PERSISTENCE", cats_sqlite)

        cats_uia = map_lesson_to_categories("Windows UI Automation", "Accessibility tree search")
        self.assertIn("WINDOWS_UI_AUTOMATION", cats_uia)

    def test_retrieve_relevant_lessons_matching_prompt(self) -> None:
        prompt = "Investigate pytest failure with loopback socket drop in sandbox"
        retrieved = retrieve_relevant_lessons(prompt, ["TESTING_VERIFICATION", "SECURITY_BOUNDARY"])
        self.assertEqual(len(retrieved), 1)
        item = retrieved[0]
        self.assertEqual(item["id"], "LES-001")
        self.assertIn("docs/agent/LESSONS.md", item["provenance"])
        self.assertIn("INERT HISTORICAL REFERENCE", item["provenance"])
        self.assertIn("prevention_rules", item)
        self.assertIn("applicability", item)

    def test_retrieve_relevant_lessons_unrelated_domain(self) -> None:
        prompt = "Tune WASAPI ring-buffer sizes for Kokoro neural TTS"
        retrieved = retrieve_relevant_lessons(prompt, ["AUDIO_THREADING"])
        self.assertEqual(retrieved, [])

    def test_retrieve_relevant_lessons_negative_applicability(self) -> None:
        prompt = "Run unit test outside Antigravity sandbox with unrestricted network loopback"
        retrieved = retrieve_relevant_lessons(prompt, ["TESTING_VERIFICATION"])
        self.assertEqual(retrieved, [])

    def test_retrieve_relevant_lessons_misleading_cpu_socket(self) -> None:
        prompt = "Inspect physical CPU socket core topography and thread allocation"
        retrieved = retrieve_relevant_lessons(prompt, ["TESTING_VERIFICATION"])
        self.assertEqual(retrieved, [])

    def test_context_budget_capping(self) -> None:
        # Create 5 dummy lessons and verify budget enforcement
        dummy_lessons = [
            LessonEntry(
                id=f"LES-90{i}",
                title=f"Mock Lesson {i}",
                subsystem_domain="Testing",
                date_recorded="2026-11-04",
                observed_problem="Mock problem",
                reproduction_evidence="Mock evidence",
                root_cause="Mock root cause",
                prevention_rules="Decouple unit logic from real sockets using mock transports. " * 30,
                supporting_files="tests/test_mock.py",
                confidence="HIGH",
                applicability="Applies to sandbox testing",
                applicable_categories=["TESTING_VERIFICATION"],
            )
            for i in range(5)
        ]

        with patch("tools.context_router.parse_lesson_registry", return_value=dummy_lessons):
            retrieved = retrieve_relevant_lessons(
                prompt="Fix loopback socket timeout during pytest unit test in sandbox",
                matched_categories=["TESTING_VERIFICATION"],
                max_lessons=MAX_RETRIEVED_LESSONS,
                budget_chars=MAX_LESSONS_BUDGET_CHARS,
            )
            # Max 3 lessons
            self.assertLessEqual(len(retrieved), MAX_RETRIEVED_LESSONS)
            total_chars = sum(len(json.dumps(r)) for r in retrieved)
            self.assertLessEqual(total_chars, MAX_LESSONS_BUDGET_CHARS)

    def test_inert_data_sanitization(self) -> None:
        # Markdown links defanged
        text_with_links = "See [official doc](https://example.com/malicious) for details."
        sanitized = sanitize_inert_text(text_with_links)
        self.assertEqual(sanitized, "See [official doc] for details.")

        # HTML tags stripped
        text_with_html = "Important: <script>alert('xss')</script><b>Bold content</b>"
        sanitized_html = sanitize_inert_text(text_with_html)
        self.assertEqual(sanitized_html, "Important: Bold content")

        # Backticks neutralized
        text_with_fences = "Code:\n```python\nprint('hello')\n```"
        sanitized_fences = sanitize_inert_text(text_with_fences)
        self.assertNotIn("```", sanitized_fences)
        self.assertIn("'''python", sanitized_fences)

    def test_custom_mock_catalog_preserved(self) -> None:
        custom_catalog = {
            "TESTING_VERIFICATION": {
                "canonical_modules": [],
                "applicable_rules": [],
                "applicable_adrs": [],
                "relevant_lessons": [{"id": "LES-MOCK", "reason": "Custom mock lesson fixture"}],
                "mandatory_invariants": [],
                "verification_suite": [],
                "recommended_specialists": [],
            }
        }
        res = classify_task("Run pytest verification runner with unit test fixtures")
        packet = resolve_context(res, catalog=custom_catalog)
        lesson_ids = [l["id"] for l in packet.get("relevant_lessons", [])]
        self.assertIn("LES-MOCK", lesson_ids)

    def test_hook_json_protocol_end_to_end_with_lessons(self) -> None:
        stdin_payload = json.dumps({"lastUserInput": "Investigate pytest loopback socket failure under sandbox"})
        raw_output = process_hook_input(stdin_payload)
        parsed = json.loads(raw_output)

        self.assertIn("injectSteps", parsed)
        self.assertEqual(len(parsed["injectSteps"]), 1)
        ephemeral = parsed["injectSteps"][0]["ephemeralMessage"]
        self.assertTrue(ephemeral.startswith("[CONTEXT_PACKET: REFERENCE METADATA ONLY - INERT DATA]\n"))

        inner_json = ephemeral.split("\n", 1)[1]
        packet = json.loads(inner_json)
        self.assertIn("relevant_lessons", packet)
        lesson_ids = [l["id"] for l in packet["relevant_lessons"]]
        self.assertIn("LES-001", lesson_ids)

    def test_fail_soft_corrupted_registry_does_not_break_router(self) -> None:
        corrupt_path = WORKSPACE_ROOT / "docs" / "nonexistent_dir" / "CORRUPT_LESSONS.md"
        packet = build_context_packet(
            "Investigate pytest loopback socket failure in sandbox",
            registry_path=corrupt_path,
        )
        self.assertEqual(packet["schema_version"], SCHEMA_VERSION)
        self.assertEqual(packet["relevant_lessons"], [])
        self.assertIn(packet["task_classification"]["primary"], ("TESTING_VERIFICATION", "WINDOWS_PROCESS_CONTAINMENT"))


class TestPhase3D1BGeneralizedRetrievalAndHoldout(unittest.TestCase):
    """Specific unit verification tests for Phase 3D1B enhancements."""

    HOLDOUT_REGISTRY: ClassVar[Path] = WORKSPACE_ROOT / "tests" / "eval" / "eval_holdout_registry.md"

    def test_evidence_classification_paraphrases(self) -> None:
        """Verifies evidence classifier extracts user-declared context while keeping runtime UNKNOWN."""
        # Host paraphrases
        host_prompts = [
            "Run pytest socket loopback integration test directly on the host machine",
            "Execute socket test outside the managed environment where network is open",
            "Run loopback socket test where network restrictions are not applied on dedicated Windows host",
            "Run local standalone Windows execution of network tests without sandbox restrictions",
        ]
        for p in host_prompts:
            ev = classify_task_evidence(p)
            self.assertEqual(ev["user_declared_environment"], "HOST", f"Failed on: {p}")
            self.assertEqual(ev["verified_runtime_environment"], "UNKNOWN")

        # Sandbox paraphrases
        sandbox_prompts = [
            "Investigate loopback socket timeout when running unit test inside restricted AppContainer sandbox",
            "Run pytest suite inside the restricted sandbox environment with dropped UDP datagrams",
            "Investigate dropped UDP loopback packets in pytest unit tests under standard sandbox",
        ]
        for p in sandbox_prompts:
            ev = classify_task_evidence(p)
            self.assertEqual(ev["user_declared_environment"], "SANDBOX", f"Failed on: {p}")
            self.assertEqual(ev["verified_runtime_environment"], "UNKNOWN")

        # Scope classification
        prod_prompt = "Evaluate sending telemetry packet over loopback socket from live production audio worker thread"
        ev_prod = classify_task_evidence(prod_prompt)
        self.assertEqual(ev_prod["user_declared_scope"], "PRODUCTION")

        test_prompt = "Run pytest unit test on mock assertions"
        ev_test = classify_task_evidence(test_prompt)
        self.assertEqual(ev_test["user_declared_scope"], "TEST")

    def test_lifecycle_metadata_filtering(self) -> None:
        """Verifies DEPRECATED and SUPERSEDED lessons are filtered out from normal retrieval."""
        # LES-007 is marked DEPRECATED
        deprecated_prompt = "Configure legacy WASAPI playback buffer size 512 samples for low latency"
        ret_dep = retrieve_relevant_lessons(deprecated_prompt, ["AUDIO_THREADING"], registry_path=self.HOLDOUT_REGISTRY)
        self.assertEqual(ret_dep, [], "Deprecated lesson LES-007 was unexpectedly retrieved")

        # LES-008 is marked SUPERSEDED
        superseded_prompt = "Tune SQLite performance by setting PRAGMA synchronous = OFF"
        ret_sup = retrieve_relevant_lessons(superseded_prompt, ["SQLITE_PERSISTENCE"], registry_path=self.HOLDOUT_REGISTRY)
        self.assertEqual(ret_sup, [], "Superseded lesson LES-008 was unexpectedly retrieved")

    def test_environment_mutual_exclusion(self) -> None:
        """Verifies host and sandbox tasks retrieve mutually exclusive lessons."""
        # Host query retrieves host lesson LES-012, never sandbox LES-001
        host_prompt = "Run standalone Windows integration tests on host machine outside sandbox where network loopback is permitted"
        ret_host = retrieve_relevant_lessons(host_prompt, ["TESTING_VERIFICATION"], registry_path=self.HOLDOUT_REGISTRY)
        self.assertEqual(len(ret_host), 1)
        self.assertEqual(ret_host[0]["id"], "LES-012")

        # Sandbox query retrieves sandbox lesson LES-001, never host LES-012
        sandbox_prompt = "Investigate loopback socket timeout when running unit test inside restricted AppContainer sandbox"
        ret_sandbox = retrieve_relevant_lessons(sandbox_prompt, ["TESTING_VERIFICATION"], registry_path=self.HOLDOUT_REGISTRY)
        self.assertEqual(len(ret_sandbox), 1)
        self.assertEqual(ret_sandbox[0]["id"], "LES-001")

    def test_multi_lesson_retrieval_and_tie_breaking(self) -> None:
        """Verifies tasks matching multiple applicable lessons retrieve both with deterministic ordering."""
        # Matches LES-002 (deadlock) and LES-003 (KNN search)
        multi_prompt = "Fix SQLite SingleWriterActor deadlock in write transaction and tune sqlite-vec KNN search query batch size"
        ret = retrieve_relevant_lessons(multi_prompt, ["SQLITE_PERSISTENCE"], registry_path=self.HOLDOUT_REGISTRY)
        ret_ids = [r["id"] for r in ret]
        self.assertEqual(ret_ids, ["LES-002", "LES-003"])

        # Capacity capping: 4 lessons matched, capped to top 3 with deterministic ID tie-breaking
        capping_prompt = "Optimize SQLite database transactions: fix SingleWriterActor deadlock, batch sqlite-vec KNN search, expand read pool connections, and check FTS5 index"
        ret_cap = retrieve_relevant_lessons(capping_prompt, ["SQLITE_PERSISTENCE"], registry_path=self.HOLDOUT_REGISTRY)
        self.assertEqual(len(ret_cap), 3)
        self.assertEqual([r["id"] for r in ret_cap], ["LES-002", "LES-003", "LES-004"])

    def test_in_process_registry_stat_caching(self) -> None:
        """Verifies registry parsing utilizes stat-based in-process caching for fast retrieval."""
        cache_key = str(self.HOLDOUT_REGISTRY.resolve())
        _REGISTRY_CACHE.pop(cache_key, None)

        # First call parses from file and populates cache
        lessons_1 = parse_lesson_registry(self.HOLDOUT_REGISTRY)
        self.assertIn(cache_key, _REGISTRY_CACHE)

        # Second call returns the exact cached object
        lessons_2 = parse_lesson_registry(self.HOLDOUT_REGISTRY)
        self.assertIs(lessons_1, lessons_2)

    def test_aud01_lifecycle_parsing_fail_closed(self) -> None:
        """AUD-01: Verifies strict normalized lifecycle parsing, fail-closed handling, and duplicate IDs."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            reg_file = Path(tmp_dir) / "AUD01_LESSONS.md"
            content = """# Lifecycle Test Registry

## LES-020: Draft Lesson
- **Lesson ID:** `LES-020`
- **Subsystem Domain:** Testing / Verification
- **Status:** DRAFT
- **Observed Problem:** Problem 20
- **Prevention Rules:** Prevention 20

## LES-021: Rejected Lesson
- **Lesson ID:** `LES-021`
- **Subsystem Domain:** Testing / Verification
- **Status:** REJECTED
- **Observed Problem:** Problem 21
- **Prevention Rules:** Prevention 21

## LES-022: Candidate Lesson
- **Lesson ID:** `LES-022`
- **Subsystem Domain:** Testing / Verification
- **Status:** CANDIDATE
- **Observed Problem:** Problem 22
- **Prevention Rules:** Prevention 22

## LES-023: Pending Review Lesson
- **Lesson ID:** `LES-023`
- **Subsystem Domain:** Testing / Verification
- **Status:** PENDING_REVIEW
- **Observed Problem:** Problem 23
- **Prevention Rules:** Prevention 23

## LES-024: Proposed Lesson
- **Lesson ID:** `LES-024`
- **Subsystem Domain:** Testing / Verification
- **Status:** PROPOSED
- **Observed Problem:** Problem 24
- **Prevention Rules:** Prevention 24

## LES-025: Revoked Lesson
- **Lesson ID:** `LES-025`
- **Subsystem Domain:** Testing / Verification
- **Status:** REVOKED
- **Observed Problem:** Problem 25
- **Prevention Rules:** Prevention 25

## LES-026: Empty Status Lesson
- **Lesson ID:** `LES-026`
- **Subsystem Domain:** Testing / Verification
- **Status:**
- **Observed Problem:** Problem 26
- **Prevention Rules:** Prevention 26

## LES-027: Whitespace-Only Status Lesson
- **Lesson ID:** `LES-027`
- **Subsystem Domain:** Testing / Verification
- **Status:**    \t
- **Observed Problem:** Problem 27
- **Prevention Rules:** Prevention 27

## LES-028: Substring Anti-Pattern Lesson
- **Lesson ID:** `LES-028`
- **Subsystem Domain:** Testing / Verification
- **Status:** NOT DEPRECATED
- **Observed Problem:** Problem 28
- **Prevention Rules:** Prevention 28

## LES-029: Mixed Case Active Lesson
- **Lesson ID:** `LES-029`
- **Subsystem Domain:** Testing / Verification
- **Status:** `aCtiVe`
- **Observed Problem:** Problem 29
- **Prevention Rules:** Prevention 29

## LES-030: Multiple Identical Status Lesson
- **Lesson ID:** `LES-030`
- **Subsystem Domain:** Testing / Verification
- **Status:** ACTIVE
- **Status:** `ACTIVE`
- **Observed Problem:** Problem 30
- **Prevention Rules:** Prevention 30

## LES-031: Conflicting Status Declarations
- **Lesson ID:** `LES-031`
- **Subsystem Domain:** Testing / Verification
- **Status:** ACTIVE
- **Status:** DEPRECATED
- **Observed Problem:** Problem 31
- **Prevention Rules:** Prevention 31

## LES-032: Malformed Header First Then Active Duplicate
- **Lesson ID:** `LES-032`
- **Subsystem Domain:** Testing / Verification
- **Status:** DRAFT
- **Observed Problem:** Problem 32
- **Prevention Rules:** Prevention 32

## LES-032: Duplicate Header Attempting Promotion
- **Lesson ID:** `LES-032`
- **Subsystem Domain:** Testing / Verification
- **Status:** ACTIVE
- **Observed Problem:** Problem 32 Duplicate
- **Prevention Rules:** Prevention 32 Duplicate

## LES-033: Missing Status Legacy
- **Lesson ID:** `LES-033`
- **Subsystem Domain:** Testing / Verification
- **Observed Problem:** Problem 33
- **Prevention Rules:** Prevention 33
"""
            reg_file.write_text(content, encoding="utf-8")
            lessons = parse_lesson_registry(reg_file, allow_external_fixtures=True)
            by_id = {l.id: l for l in lessons}

            # 1. Non-active statuses fail closed to UNSUPPORTED
            self.assertEqual(by_id["LES-020"].status, "UNSUPPORTED")
            self.assertEqual(by_id["LES-021"].status, "UNSUPPORTED")
            self.assertEqual(by_id["LES-022"].status, "UNSUPPORTED")
            self.assertEqual(by_id["LES-023"].status, "UNSUPPORTED")
            self.assertEqual(by_id["LES-024"].status, "UNSUPPORTED")
            self.assertEqual(by_id["LES-025"].status, "UNSUPPORTED")
            self.assertEqual(by_id["LES-026"].status, "UNSUPPORTED")
            self.assertEqual(by_id["LES-027"].status, "UNSUPPORTED")

            # 2. Substring match anti-pattern fails closed to UNSUPPORTED
            self.assertEqual(by_id["LES-028"].status, "UNSUPPORTED")

            # 3. Normalized case
            self.assertEqual(by_id["LES-029"].status, "ACTIVE")

            # 4. Multiple identical statuses accepted
            self.assertEqual(by_id["LES-030"].status, "ACTIVE")

            # 5. Conflicting status declarations fail closed to UNSUPPORTED
            self.assertEqual(by_id["LES-031"].status, "UNSUPPORTED")

            # 6. Duplicate lesson ID cannot hijack or promote earlier entry
            self.assertEqual(by_id["LES-032"].status, "UNSUPPORTED")
            self.assertEqual(by_id["LES-032"].observed_problem, "Problem 32")

            # 7. Missing status defaults to ACTIVE_LEGACY
            self.assertEqual(by_id["LES-033"].status, "ACTIVE_LEGACY")

            # 8. Unrecognized and unsupported statuses can never enter retrieval results
            retrieved = retrieve_relevant_lessons(
                prompt="Investigate testing verification problem 20",
                matched_categories=["TESTING_VERIFICATION"],
                registry_path=reg_file,
                allow_external_fixtures=True,
            )
            ret_ids = [r["id"] for r in retrieved]
            self.assertNotIn("LES-020", ret_ids)
            self.assertNotIn("LES-028", ret_ids)
            self.assertNotIn("LES-031", ret_ids)
            self.assertNotIn("LES-032", ret_ids)

    def test_aud02_14_adversarial_environment_scenarios(self) -> None:
        """AUD-02: Verifies symmetric, conservative evidence classification on all 14 adversarial prompt variations."""
        scenarios = [
            ("1. Explicit affirmative host statement", "Run integration test directly on the host machine", "HOST", "UNKNOWN"),
            ("2. Explicit affirmative sandbox statement", "Execute tests inside restricted AppContainer sandbox", "SANDBOX", "UNKNOWN"),
            ("3. Host and sandbox in the same statement", "Compare behavior on dedicated host vs inside standard sandbox", "UNKNOWN", "UNKNOWN"),
            ("4. Reversed phrase ordering", "Run inside standard sandbox and also on host machine", "UNKNOWN", "UNKNOWN"),
            ("5. Negated host evidence", "Run unit tests without dedicated host, not on host", "UNKNOWN", "TEST"),
            ("6. Negated sandbox evidence", "Run tests not in sandbox, not inside container", "UNKNOWN", "TEST"),
            ("7. Quoted host log", 'Investigate test log: "Passed on dedicated host machine" during run', "UNKNOWN", "UNKNOWN"),
            ("8. Quoted sandbox log", 'Investigate error message: "Failed inside restricted AppContainer sandbox" on dedicated Windows host', "HOST", "UNKNOWN"),
            ("9. Historical host followed by current sandbox", "In previous session ran on host machine, but now executing inside standard sandbox", "SANDBOX", "UNKNOWN"),
            ("10. Historical sandbox followed by current host", "Previously failed in standard sandbox, but now running on dedicated Windows host", "HOST", "UNKNOWN"),
            ("11. Hypothetical host execution", "What if we were running on bare metal host machine?", "UNKNOWN", "UNKNOWN"),
            ("12. Multiple deployment and testing targets", "Deploy services across both cloud host and sandboxed container environments", "UNKNOWN", "UNKNOWN"),
            ("13. Ambiguous environment statement", "Run verification script in the default workspace", "UNKNOWN", "UNKNOWN"),
            ("14. Mixed scope and environment statements", "Execute pytest unit tests on dedicated Windows host", "HOST", "TEST"),
        ]

        for name, prompt, expected_env, expected_scope in scenarios:
            ev = classify_task_evidence(prompt)
            self.assertEqual(
                ev["user_declared_environment"],
                expected_env,
                f"AUD-02 Env failure on '{name}': got {ev['user_declared_environment']}, expected {expected_env}",
            )
            self.assertEqual(
                ev["verified_runtime_environment"],
                "UNKNOWN",
                f"AUD-02 Runtime env must always be UNKNOWN on '{name}'",
            )
            if expected_scope != "UNKNOWN":
                self.assertEqual(
                    ev["user_declared_scope"],
                    expected_scope,
                    f"AUD-02 Scope failure on '{name}': got {ev['user_declared_scope']}, expected {expected_scope}",
                )

    def test_aud04_registry_path_trust_boundary(self) -> None:
        """AUD-04: Verifies workspace containment enforcement, traversal rejection, and test fixture allowances."""
        # 1. Default (None) uses production workspace root registry
        prod_lessons = parse_lesson_registry()
        self.assertGreater(len(prod_lessons), 0)

        # 2. Valid workspace-relative path succeeds
        rel_lessons = parse_lesson_registry("docs/agent/LESSONS.md")
        self.assertEqual(len(rel_lessons), len(prod_lessons))

        # 3. Valid resolved absolute workspace path succeeds
        abs_lessons = parse_lesson_registry(WORKSPACE_ROOT / "docs" / "agent" / "LESSONS.md")
        self.assertEqual(len(abs_lessons), len(prod_lessons))

        # 4. Traversal escaping workspace root fails closed (returns [])
        traversal_path = Path("docs") / ".." / ".." / "Windows" / "win.ini"
        traversal_lessons = parse_lesson_registry(traversal_path, allow_external_fixtures=False)
        self.assertEqual(traversal_lessons, [], "Path traversal outside workspace must fail closed")

        # 5. External absolute path fails closed (returns [])
        ext_path = Path("C:/Windows/win.ini")
        ext_lessons = parse_lesson_registry(ext_path, allow_external_fixtures=False)
        self.assertEqual(ext_lessons, [], "External absolute path must fail closed when allow_external_fixtures=False")

        # 6. allow_external_fixtures=True allows isolated temporary directory
        with tempfile.TemporaryDirectory() as tmp_dir:
            fixture_file = Path(tmp_dir) / "FIXTURE_LESSONS.md"
            fixture_file.write_text(
                "## LES-999: Fixture\n- **Subsystem Domain:** Audio\n- **Prevention Rules:** Rule\n- **Status:** ACTIVE\n",
                encoding="utf-8",
            )
            fixture_lessons = parse_lesson_registry(fixture_file, allow_external_fixtures=True)
            self.assertEqual(len(fixture_lessons), 1)
            self.assertEqual(fixture_lessons[0].id, "LES-999")

        # 7. Missing file fails soft
        missing_path = WORKSPACE_ROOT / "docs" / "agent" / "NONEXISTENT_LESSONS.md"
        missing_lessons = parse_lesson_registry(missing_path)
        self.assertEqual(missing_lessons, [])

        # 8. Symlink resolving outside workspace fails closed
        with tempfile.TemporaryDirectory() as tmp_dir:
            ext_target = Path(tmp_dir) / "EXTERNAL_TARGET.md"
            ext_target.write_text("## LES-888: Symlink Target\n- **Subsystem Domain:** Testing\n- **Prevention Rules:** R\n", encoding="utf-8")
            symlink_in_ws = WORKSPACE_ROOT / "docs" / "temp_symlink_test.md"
            try:
                symlink_in_ws.symlink_to(ext_target)
                try:
                    sym_lessons = parse_lesson_registry(symlink_in_ws, allow_external_fixtures=False)
                    self.assertEqual(sym_lessons, [], "Symlink resolving outside workspace root must be rejected")
                finally:
                    if symlink_in_ws.exists() or symlink_in_ws.is_symlink():
                        symlink_in_ws.unlink()
            except (OSError, NotImplementedError):
                # Symlink creation requires elevated permissions on Windows without Developer Mode
                pass


if __name__ == "__main__":
    unittest.main()
