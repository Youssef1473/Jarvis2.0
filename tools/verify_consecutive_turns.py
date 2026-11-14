"""Consecutive Turn Verification and Context Refresh Probe (ENG-OS-02 Phase 2C1).

Evaluates the multi-domain context refresh sequence:
1. WASAPI playback and Kokoro synthesis
2. SQLite writer serialization
3. Windows UI Automation
4. Process containment
5. Audio-plus-SQLite combined task
6. Unrelated general question

Tests both:
Scenario A: Host resets invocationNum to 0 at turn boundary (ideal behavior).
Scenario B: Host monotonically increments invocationNum across turns (potential failure mode).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

CONSECUTIVE_TURNS = [
    {
        "turn": 1,
        "name": "WASAPI playback and Kokoro synthesis",
        "prompt": "Optimize WASAPI playback worker loop and handle Kokoro neural TTS synthesis with epoch cancellation",
        "expected_primary": "AUDIO_THREADING",
        "expected_risk": "HIGH",
    },
    {
        "turn": 2,
        "name": "SQLite writer serialization",
        "prompt": "Fix database transaction locking by routing writes to SingleWriterActor with WAL pragma",
        "expected_primary": "SQLITE_PERSISTENCE",
        "expected_risk": "HIGH",
    },
    {
        "turn": 3,
        "name": "Windows UI Automation",
        "prompt": "Inspect accessibility element tree in notepad using pywinauto and UI Automation",
        "expected_primary": "WINDOWS_UI_AUTOMATION",
        "expected_risk": "MEDIUM",
    },
    {
        "turn": 4,
        "name": "Process containment",
        "prompt": "Configure Win32 Job Object containment with CREATE_SUSPENDED and kill_on_job_close limit",
        "expected_primary": "WINDOWS_PROCESS_CONTAINMENT",
        "expected_risk": "CRITICAL",
    },
    {
        "turn": 5,
        "name": "Audio-plus-SQLite combined task",
        "prompt": "Refactor WASAPI playback worker and persist audio transcription events to SQLite database",
        "expected_primary": "SQLITE_PERSISTENCE",  # or AUDIO_THREADING (multi-subsystem)
        "expected_risk": "HIGH",
    },
    {
        "turn": 6,
        "name": "Unrelated general question (clean non-technical)",
        "prompt": "What is the traditional recipe and fermentation time for sourdough bread?",
        "expected_primary": "UNKNOWN",
        "expected_risk": "HIGH",
    },
]


def run_probe() -> dict:
    cmd = [
        "cmd.exe",
        "/c",
        "if exist tools\\context_router.py (python tools\\context_router.py) else (python ..\\tools\\context_router.py)",
    ]

    scenario_a_results = []
    # Scenario A: Host resets invocationNum=0 at start of each user turn
    for t in CONSECUTIVE_TURNS:
        # Invocation 0 (turn start)
        payload_inv0 = json.dumps({
            "lastUserInput": t["prompt"],
            "invocationNum": 0,
            "workspacePaths": [str(WORKSPACE_ROOT)],
        })
        t0 = time.perf_counter_ns()
        proc0 = subprocess.run(cmd, check=False, input=payload_inv0, capture_output=True, text=True, cwd=str(WORKSPACE_ROOT), timeout=5)
        t1 = time.perf_counter_ns()
        latency_inv0 = (t1 - t0) / 1_000_000.0

        parsed0 = json.loads(proc0.stdout)
        steps0 = parsed0.get("injectSteps", [])
        has_packet = len(steps0) == 1 and "ephemeralMessage" in steps0[0]

        packet_data = {}
        fingerprint = "NONE"
        if has_packet:
            inner_str = steps0[0]["ephemeralMessage"].split("\n", 1)[1]
            packet_data = json.loads(inner_str)
            fingerprint = hashlib.sha256(inner_str.encode("utf-8")).hexdigest()[:12]

        classification = packet_data.get("task_classification", {})
        primary = classification.get("primary", "ERROR")
        risk = classification.get("risk_tier", "ERROR")
        modules = [m["path"] for m in packet_data.get("canonical_modules", [])]
        invariants = packet_data.get("mandatory_invariants", [])

        # Invocation 1 (tool call return within same turn)
        payload_inv1 = json.dumps({
            "lastUserInput": t["prompt"],
            "invocationNum": 1,
            "workspacePaths": [str(WORKSPACE_ROOT)],
        })
        proc1 = subprocess.run(cmd, check=False, input=payload_inv1, capture_output=True, text=True, cwd=str(WORKSPACE_ROOT), timeout=5)
        parsed1 = json.loads(proc1.stdout)
        steps1 = parsed1.get("injectSteps", [])
        dup_suppressed = (steps1 == [])

        scenario_a_results.append({
            "turn": t["turn"],
            "name": t["name"],
            "prompt": t["prompt"],
            "first_invocation_num": 0,
            "detected_primary": primary,
            "risk_tier": risk,
            "fingerprint": fingerprint,
            "modules_count": len(modules),
            "modules": modules,
            "invariants_count": len(invariants),
            "invariants": invariants,
            "duplicate_suppressed_on_inv1": dup_suppressed,
            "latency_ms": round(latency_inv0, 2),
            "status": "PASS" if has_packet and primary == t["expected_primary"] and dup_suppressed else "FAIL",
        })

    # Scenario B Audit: What if invocationNum is NOT reset by host, but monotonically increments?
    scenario_b_findings = []
    monotonic_inv_num = 0
    for t in CONSECUTIVE_TURNS:
        payload = json.dumps({
            "lastUserInput": t["prompt"],
            "invocationNum": monotonic_inv_num,
            "workspacePaths": [str(WORKSPACE_ROOT)],
        })
        proc = subprocess.run(cmd, check=False, input=payload, capture_output=True, text=True, cwd=str(WORKSPACE_ROOT), timeout=5)
        parsed = json.loads(proc.stdout)
        steps = parsed.get("injectSteps", [])
        injected = len(steps) > 0
        scenario_b_findings.append({
            "turn": t["turn"],
            "simulated_monotonic_inv_num": monotonic_inv_num,
            "injected": injected,
        })
        monotonic_inv_num += 2  # 2 invocations per turn

    return {
        "scenario_a": scenario_a_results,
        "scenario_b": scenario_b_findings,
    }


if __name__ == "__main__":
    out = run_probe()
    print(json.dumps(out, indent=2))
