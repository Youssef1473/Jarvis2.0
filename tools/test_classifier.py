"""Canonical Test Dependency Classifier and Inventory Governor for Jarvis 2.0.

Provides rigorous, test-level node classification across 4 operational tiers:
- Tier 1: Hermetic Unit (parallelizable, zero network, zero hardware, zero browser)
- Tier 2A: Async Safe (pure in-memory coroutines/parsers, no raw loopback sockets)
- Tier 2B: Loopback & IPC (requires real loopback TCP/UDP sockets, TestClient, or servers)
- Tier 3: Windows OS Integration (Win32 UIA, desktop handles, Job Objects, MMCSS)
- Tier 4: Hardware Integration (CUDA, Kokoro ONNX neural TTS, faster-whisper, WASAPI)
- Browser Integration: Real Chromium / Playwright process launches
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


def get_python_executable(workspace_root: Path | None = None) -> str:
    """Resolves the preferred python executable containing project dependencies."""
    root = workspace_root or WORKSPACE_ROOT
    candidates = [
        root / ".venv" / "Scripts" / "python.exe",
        root.parent / ".venv" / "Scripts" / "python.exe",
        Path(sys.executable),
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return sys.executable

# Tier Name Constants
TIER_1_HERMETIC_UNIT = "tier1_hermetic_unit"
TIER_2A_ASYNC_SAFE = "tier2a_async_safe"
TIER_2B_LOOPBACK_IPC = "tier2b_loopback_ipc"
TIER_3_WINDOWS_INTEGRATION = "tier3_windows_integration"
TIER_4_HARDWARE_INTEGRATION = "tier4_hardware_integration"
TIER_BROWSER_INTEGRATION = "tier_browser_integration"

ALL_TIERS = (
    TIER_1_HERMETIC_UNIT,
    TIER_2A_ASYNC_SAFE,
    TIER_2B_LOOPBACK_IPC,
    TIER_3_WINDOWS_INTEGRATION,
    TIER_4_HARDWARE_INTEGRATION,
    TIER_BROWSER_INTEGRATION,
)

# Explicit Node-Level Rules (for files with mixed unit and integration/loopback tests)
NODE_RULES: dict[str, str] = {
    # Browser live integration
    "tests/test_browser_network_boundaries.py::TestLiveChromiumNetworkGuards": TIER_BROWSER_INTEGRATION,
    "tests/test_browser_agent.py::TestBrowserAgentLiveChromiumSSRF": TIER_BROWSER_INTEGRATION,
    # Background tasks: only the Starlette TestClient endpoint test needs loopback
    "tests/test_background_tasks.py::TestBackgroundTasksSuite::test_server_screenshot_and_tasks_endpoints": TIER_2B_LOOPBACK_IPC,
    # Browser network boundaries: 11 tests requiring live EphemeralHttpServer loopback sockets
    "tests/test_browser_network_boundaries.py::TestBrowserNetworkBoundaries::test_cdp_navigation_installs_network_guards": TIER_2B_LOOPBACK_IPC,
    "tests/test_browser_network_boundaries.py::TestBrowserNetworkBoundaries::test_cdp_new_tab_guard_failure_fails_closed": TIER_2B_LOOPBACK_IPC,
    "tests/test_browser_network_boundaries.py::TestBrowserNetworkBoundaries::test_existing_tab_guard_failure_aborts_navigation": TIER_2B_LOOPBACK_IPC,
    "tests/test_browser_network_boundaries.py::TestBrowserNetworkBoundaries::test_head_method_rejected_falls_back_to_get_cleanly": TIER_2B_LOOPBACK_IPC,
    "tests/test_browser_network_boundaries.py::TestBrowserNetworkBoundaries::test_legitimate_local_dev_redirect_allowed": TIER_2B_LOOPBACK_IPC,
    "tests/test_browser_network_boundaries.py::TestBrowserNetworkBoundaries::test_max_hop_limit_exceeded_aborted": TIER_2B_LOOPBACK_IPC,
    "tests/test_browser_network_boundaries.py::TestBrowserNetworkBoundaries::test_os_control_open_browser_tab_blocks_redirect_to_disallowed": TIER_2B_LOOPBACK_IPC,
    "tests/test_browser_network_boundaries.py::TestBrowserNetworkBoundaries::test_redirect_loop_detected_and_aborted": TIER_2B_LOOPBACK_IPC,
    "tests/test_browser_network_boundaries.py::TestBrowserNetworkBoundaries::test_redirect_to_cloud_metadata_rejected": TIER_2B_LOOPBACK_IPC,
    "tests/test_browser_network_boundaries.py::TestBrowserNetworkBoundaries::test_redirect_to_private_rfc1918_rejected_from_remote": TIER_2B_LOOPBACK_IPC,
    "tests/test_browser_network_boundaries.py::TestBrowserNetworkBoundaries::test_redirect_to_protected_localhost_port_rejected": TIER_2B_LOOPBACK_IPC,
    # Browser agent: 11 Playwright/SSRF redirect inspection tests requiring browser integration
    "tests/test_browser_agent.py::TestBrowserAgentSSRFSuite::test_browse_and_read_webpage_and_screenshot_enforce_service_workers_block": TIER_BROWSER_INTEGRATION,
    "tests/test_browser_agent.py::TestBrowserAgentSSRFSuite::test_browse_and_read_webpage_fallback_successful_extraction": TIER_BROWSER_INTEGRATION,
    "tests/test_browser_agent.py::TestBrowserAgentSSRFSuite::test_browse_and_read_webpage_fallback_uses_hardened_opener": TIER_BROWSER_INTEGRATION,
    "tests/test_browser_agent.py::TestBrowserAgentSSRFSuite::test_browse_and_read_webpage_playwright_reports_ssrf_on_redirect": TIER_BROWSER_INTEGRATION,
    "tests/test_browser_agent.py::TestBrowserAgentSSRFSuite::test_capture_webpage_screenshot_navigates_to_safe_url": TIER_BROWSER_INTEGRATION,
    "tests/test_browser_agent.py::TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_aborts_multi_hop_redirect_chain_to_private": TIER_BROWSER_INTEGRATION,
    "tests/test_browser_agent.py::TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_blocks_https_to_http_downgrade": TIER_BROWSER_INTEGRATION,
    "tests/test_browser_agent.py::TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_fulfills_safe_public_response": TIER_BROWSER_INTEGRATION,
    "tests/test_browser_agent.py::TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_hop_limit_exceeded": TIER_BROWSER_INTEGRATION,
    "tests/test_browser_agent.py::TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_redirect_loop_detection": TIER_BROWSER_INTEGRATION,
    "tests/test_browser_agent.py::TestBrowserAgentSSRFSuite::test_playwright_ssrf_guard_relative_location_resolution": TIER_BROWSER_INTEGRATION,
    # Live Uvicorn access log redaction in Win32 sandbox test
    "tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_end_to_end_live_uvicorn_percent_encoded_access_log_redaction": TIER_2B_LOOPBACK_IPC,
    # ASGI access log redaction middleware tests invoking asyncio.run() (Windows Proactor socketpair loopback)
    "tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_access_log_redaction_oversized_query_beginning_and_end": TIER_2A_ASYNC_SAFE,
    "tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_access_log_redaction_percent_encoded_parameters": TIER_2A_ASYNC_SAFE,
    "tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_access_log_redaction_preserves_legitimate_query_params": TIER_2A_ASYNC_SAFE,
    "tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_access_log_redaction_queries_exceeding_10mb_failsafe": TIER_2A_ASYNC_SAFE,
    "tests/test_job_object_sandbox_hardening.py::TestJobSandboxRealWindows::test_access_log_redaction_timing_at_response_start": TIER_2A_ASYNC_SAFE,
    # Async coroutines in hardware test files
    "tests/test_onnx_embeddings.py::TestOnnxEmbeddingEngine::test_async_embedding_event_loop": TIER_2A_ASYNC_SAFE,
    "tests/test_semantic_endpointing.py::test_adaptive_tripwire_trigger": TIER_2A_ASYNC_SAFE,
    # Self-evolution out-of-process canary and dynamic execution under Win32 Job Object
    "tests/test_self_evolution.py::TestSelfEvolution::test_dynamic_skill_creation_and_hot_reload": TIER_3_WINDOWS_INTEGRATION,
}

# Suites containing hardware / audio / GPU dependencies (serial)
HARDWARE_FILES = {
    "tests/test_tts.py",
    "tests/test_audio_streaming_pipeline.py",
    "tests/test_audio_affinity.py",
    "tests/test_bettany_voice_and_dsp.py",
    "tests/test_council_batch_2.py",
    "tests/test_council_batch_3.py",
    "tests/test_streaming_voice_reflex.py",
    "tests/test_voice_control_and_skills.py",
    "tests/test_speech_normalizer.py",
    "tests/test_whisper_prosody.py",
    "tests/test_wake_word.py",
    "tests/test_os_skills.py",
    "tests/test_phase3_onnx_router.py",
    "tests/test_onnx_embeddings.py",
    "tests/test_sfx.py",
    "tests/test_nvml_vram_arbiter.py",
    "tests/test_dsp_ring_ownership.py",
    "tests/test_audio_reliability.py",
    "tests/test_semantic_endpointing.py",
    "tests/test_tts_lifecycle_hardening.py",
}

# Suites containing Win32 OS / UIA / desktop dependencies (serial)
WINDOWS_FILES = {
    "tests/test_windows_uia.py",
    "tests/test_win32_affinity.py",
    "tests/test_window_hook_daemon.py",
    "tests/test_job_object_sandbox.py",
    "tests/test_job_object_sandbox_hardening.py",
    "tests/test_oneshot_process_containment.py",
    "tests/test_rstrtmgr_lock_surgeon.py",
    "tests/test_skills_subprocess_containment.py",
    "tests/test_antigravity_job_containment.py",
    "tests/test_privacy_guard_zorder.py",
    "tests/test_proactive_desktop_presence.py",
    "tests/test_appcontainer_sandbox.py",
}

# Suites requiring real loopback TCP/UDP sockets or live local HTTP/WS servers
LOOPBACK_IPC_FILES = {
    "tests/test_ui_server.py",
    "tests/test_ui_enhancements.py",
    "tests/test_ui_concurrency_lifecycle.py",
    "tests/test_live_telemetry_stream.py",
    "tests/test_hud_calendar_integration.py",
    "tests/test_hud_security_dsp.py",
    "tests/test_egress_proxy.py",
    "tests/test_live_rebinding_and_browser_bypass.py",
    "tests/test_exception_hardening.py",
    "tests/test_milestone1_security_hardening.py",
    "tests/test_milestone6_residual_hardening.py",
    "tests/test_self_evolution_sandbox.py",
    "tests/test_ui_approval_workflow.py",
}

# Suites containing in-memory async coroutines / parsers (Async Safe)
ASYNC_SAFE_FILES = {
    "tests/test_telemetry_connection_manager.py",
    "tests/test_phase5_p99_telemetry.py",
    "tests/test_streaming_tool_parser.py",
    "tests/test_ro_pool.py",
    "tests/test_managed_browser.py",
    "tests/test_brain_pipeline_and_interim.py",
    "tests/test_document_provenance.py",
    "tests/test_dual_track_coordinator.py",
    "tests/test_p0_action_bound_authorization.py",
    "tests/test_seven_dimension_operational_profile.py",
    "tests/test_unified_runner_reliability.py",
    "tests/test_security_boundaries.py",
    "tests/test_tool_authorization_hardening.py",
}


@dataclass
class ClassificationAccounting:
    """Rigorous accounting matrix ensuring every single test node is accounted for."""
    total_collected: int
    assigned_by_tier: dict[str, int]
    node_ids_by_tier: dict[str, list[str]]
    excluded_node_ids: list[str] = field(default_factory=list)
    explicitly_skipped_node_ids: list[str] = field(default_factory=list)
    overlapping_markers: list[dict[str, Any]] = field(default_factory=list)
    unclassified_node_ids: list[str] = field(default_factory=list)

    @property
    def is_fully_accounted(self) -> bool:
        assigned_sum = sum(self.assigned_by_tier.values())
        return (len(self.unclassified_node_ids) == 0) and (self.total_collected == assigned_sum)


class TestClassifier:
    """Classifies pytest node IDs according to verified environmental boundaries."""

    def __init__(self, workspace_root: Path | None = None):
        self.workspace_root = workspace_root or WORKSPACE_ROOT

    def collect_all_node_ids(self) -> list[str]:
        """Collects all pytest test node IDs directly from the test suite."""
        py_exec = get_python_executable(self.workspace_root)
        local_bin = Path(py_exec).parent / "pytest.exe"
        cmd = [str(local_bin)] if local_bin.exists() else [py_exec, "-m", "pytest"]
        cmd.extend(["--collect-only", "-q"])

        res = subprocess.run(cmd, cwd=str(self.workspace_root), capture_output=True, text=True, check=False)
        node_ids: list[str] = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if "::" in line:
                node_ids.append(line)
        return node_ids

    def classify_node(self, node_id: str) -> tuple[str, list[str]]:
        """Classifies a single test node ID into an operational tier.

        Returns (tier_name, list_of_markers).
        """
        norm_node = node_id.replace("\\", "/")
        fpath = norm_node.split("::")[0]

        # 1. Exact node-level rule overrides
        for rule_prefix, tier in NODE_RULES.items():
            if norm_node.startswith(rule_prefix):
                return tier, [tier]

        # 2. Real Browser integration check
        if "TestLiveChromiumNetworkGuards" in norm_node or "TestBrowserAgentLiveChromiumSSRF" in norm_node:
            return TIER_BROWSER_INTEGRATION, [TIER_BROWSER_INTEGRATION, "browser_integration"]

        # 3. Hardware suites
        if fpath in HARDWARE_FILES:
            return TIER_4_HARDWARE_INTEGRATION, [TIER_4_HARDWARE_INTEGRATION, "hardware"]

        # 4. Windows OS integration suites
        if fpath in WINDOWS_FILES:
            return TIER_3_WINDOWS_INTEGRATION, [TIER_3_WINDOWS_INTEGRATION, "windows_integration"]

        # 5. Loopback & IPC suites
        if fpath in LOOPBACK_IPC_FILES:
            return TIER_2B_LOOPBACK_IPC, [TIER_2B_LOOPBACK_IPC, "loopback", "ipc"]

        # 6. Async Safe suites
        if fpath in ASYNC_SAFE_FILES:
            return TIER_2A_ASYNC_SAFE, [TIER_2A_ASYNC_SAFE, "async_safe"]

        # 7. Default to Hermetic Unit
        return TIER_1_HERMETIC_UNIT, [TIER_1_HERMETIC_UNIT, "unit"]

    def build_accounting_matrix(self, node_ids: list[str] | None = None) -> ClassificationAccounting:
        """Constructs an exact accounting matrix across all collected test node IDs."""
        nodes = node_ids if node_ids is not None else self.collect_all_node_ids()
        by_tier: dict[str, list[str]] = {t: [] for t in ALL_TIERS}
        unclassified: list[str] = []

        for nid in nodes:
            tier, _ = self.classify_node(nid)
            if tier in by_tier:
                by_tier[tier].append(nid)
            else:
                unclassified.append(nid)

        assigned_counts = {t: len(by_tier[t]) for t in ALL_TIERS}

        return ClassificationAccounting(
            total_collected=len(nodes),
            assigned_by_tier=assigned_counts,
            node_ids_by_tier=by_tier,
            unclassified_node_ids=unclassified,
        )

    def export_manifest(self, output_path: str | Path, node_ids: list[str] | None = None) -> Path:
        """Exports the full classification accounting matrix to JSON."""
        matrix = self.build_accounting_matrix(node_ids)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(asdict(matrix), indent=2), encoding="utf-8")
        return out

    def get_manifest(self, force_refresh: bool = False) -> dict[str, Any]:
        """Returns the canonical test classification manifest, loading from disk or regenerating."""
        manifest_path = self.workspace_root / "data" / "test_classification_manifest.json"
        if not force_refresh and manifest_path.exists():
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        matrix = self.build_accounting_matrix()
        data = asdict(matrix)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return data

    def find_orphaned_nodes(
        self,
        manifest_data: dict[str, Any] | None = None,
        collected_nodes: list[str] | None = None,
    ) -> list[str]:
        """Detects test nodes existing on disk/collected that are absent from the classification manifest."""
        if manifest_data is None:
            manifest_data = self.get_manifest()

        manifest_nodes = set()
        for node_list in manifest_data.get("node_ids_by_tier", {}).values():
            manifest_nodes.update(node_list)
        manifest_nodes.update(manifest_data.get("unclassified_node_ids", []))
        manifest_nodes.update(manifest_data.get("excluded_node_ids", []))

        if collected_nodes is None:
            collected_nodes = self.collect_all_node_ids()

        orphaned = [node for node in collected_nodes if node not in manifest_nodes]
        return sorted(orphaned)

    def verify_manifest_integrity(
        self,
        manifest_data: dict[str, Any] | None = None,
        collected_nodes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Performs a comprehensive structural and completeness audit of the manifest.

        Verifies:
        1. All tier mappings are mutually disjoint (zero duplicate nodes across tiers).
        2. Sum of tier counts matches total_collected.
        3. Zero unclassified nodes.
        4. Zero orphaned nodes relative to repository collection.
        """
        if manifest_data is None:
            manifest_data = self.get_manifest()

        node_ids_by_tier = manifest_data.get("node_ids_by_tier", {})
        seen_nodes: dict[str, str] = {}
        duplicates: list[dict[str, str]] = []
        for tier, node_list in node_ids_by_tier.items():
            for node in node_list:
                if node in seen_nodes:
                    duplicates.append({"node_id": node, "first_tier": seen_nodes[node], "second_tier": tier})
                seen_nodes[node] = tier

        total_in_tiers = sum(len(nl) for nl in node_ids_by_tier.values())
        reported_total = manifest_data.get("total_collected", 0)
        unclassified = manifest_data.get("unclassified_node_ids", [])
        orphaned = self.find_orphaned_nodes(manifest_data, collected_nodes=collected_nodes)

        is_valid = (
            len(duplicates) == 0
            and total_in_tiers == reported_total
            and len(unclassified) == 0
            and len(orphaned) == 0
        )

        return {
            "valid": is_valid,
            "total_collected": reported_total,
            "total_in_tiers": total_in_tiers,
            "duplicate_count": len(duplicates),
            "duplicates": duplicates,
            "unclassified_count": len(unclassified),
            "unclassified_node_ids": unclassified,
            "orphaned_count": len(orphaned),
            "orphaned_node_ids": orphaned,
        }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Jarvis 2.0 Test Dependency Classifier")
    parser.add_argument("--report", action="store_true", help="Print complete classification scorecard")
    parser.add_argument("--export", type=str, default=None, help="Export manifest to JSON file path")
    parser.add_argument("--check-orphaned", action="store_true", help="Check for orphaned test nodes not in manifest")
    args = parser.parse_args()

    classifier = TestClassifier()

    if args.check_orphaned:
        print("Checking for orphaned test nodes not registered in manifest...")
        manifest = classifier.get_manifest()
        orphans = classifier.find_orphaned_nodes(manifest)
        integrity = classifier.verify_manifest_integrity(manifest)
        if not integrity["valid"] or orphans:
            print("FAILED: Manifest integrity or orphan check failed!")
            if orphans:
                print(f"Found {len(orphans)} orphaned test node(s):")
                for o in orphans:
                    print(f"  - {o}")
            if integrity["duplicates"]:
                print(f"Found {integrity['duplicate_count']} duplicate node(s):")
                for d in integrity["duplicates"]:
                    print(f"  - {d}")
            if integrity["unclassified_node_ids"]:
                print(f"Found {integrity['unclassified_count']} unclassified node(s):")
                for u in integrity["unclassified_node_ids"]:
                    print(f"  - {u}")
            import sys
            sys.exit(1)
        else:
            print(f"PASSED: Manifest integrity confirmed. 0 orphans, 0 duplicates, {manifest.get('total_collected', 0)} nodes verified.")
            return

    print("Collecting and classifying all test nodes in repository...")
    matrix = classifier.build_accounting_matrix()

    print("\n" + "=" * 70)
    print(" JARVIS 2.0 TEST REPOSITORY ACCOUNTING MATRIX")
    print("=" * 70)
    print(f"Total Collected Node IDs: {matrix.total_collected}")
    print("-" * 70)
    for tier, count in matrix.assigned_by_tier.items():
        pct = (count / matrix.total_collected * 100) if matrix.total_collected else 0.0
        print(f"  {tier:30s}: {count:5d} tests ({pct:5.1f}%)")
    print("-" * 70)
    print(f"Unclassified Node IDs:         {len(matrix.unclassified_node_ids):5d}")
    print(f"Fully Accounted Invariant:     {'PASS (100%)' if matrix.is_fully_accounted else 'FAIL'}")
    print("=" * 70 + "\n")

    if args.export:
        out_p = classifier.export_manifest(args.export)
        print(f"Manifest exported to: {out_p}")


if __name__ == "__main__":
    main()
