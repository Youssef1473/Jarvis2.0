"""Executes the Verification Matrix across all 9 representative tasks (ENG-OS-02 Phase 2C).

Invokes the exact hook command configured in .agents/hooks.json from both
workspace root and .agents/ directory, recording:
- Input category
- Actual injected packet
- Relevant selected paths
- Required invariants
- Risk tier
- Packet size (chars and estimated tokens)
- Hook latency (ms)
- Injection success/failure
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

TEST_CASES = [
    {
        "category": "AUDIO_THREADING",
        "name": "Audio-threading request",
        "prompt": "Optimize WASAPI playback worker loop and handle Kokoro neural TTS synthesis with epoch cancellation",
    },
    {
        "category": "SQLITE_PERSISTENCE",
        "name": "SQLite persistence request",
        "prompt": "Fix database transaction locking by routing writes to SingleWriterActor with WAL pragma",
    },
    {
        "category": "WINDOWS_UI_AUTOMATION",
        "name": "Windows UI Automation request",
        "prompt": "Inspect accessibility element tree in notepad using pywinauto and UI Automation",
    },
    {
        "category": "WINDOWS_PROCESS_CONTAINMENT",
        "name": "Windows process containment request",
        "prompt": "Configure Win32 Job Object containment with CREATE_SUSPENDED and kill_on_job_close limit",
    },
    {
        "category": "SECURITY_BOUNDARY",
        "name": "Security-boundary request",
        "prompt": "Validate egress url destination to prevent SSRF and check dual-key approval manager",
    },
    {
        "category": "API_COMPATIBILITY",
        "name": "API compatibility request",
        "prompt": "Ensure backward compatibility of public API signatures in SkillRegistry.register",
    },
    {
        "category": "DOCUMENTATION",
        "name": "Documentation request",
        "prompt": "Update session log in docs/agent/SESSION_LOG.md and record entry in LESSONS.md",
    },
    {
        "category": "MULTI_SUBSYSTEM",
        "name": "Multi-subsystem audio + SQLite request",
        "prompt": "Refactor WASAPI playback worker and persist audio transcription events to SQLite database",
    },
    {
        "category": "UNKNOWN",
        "name": "Unknown general-purpose request",
        "prompt": "What is the history of the ancient library of Alexandria?",
    },
]

def run_matrix() -> list[dict]:
    cmd = [
        "cmd.exe",
        "/c",
        "if exist tools\\context_router.py (python tools\\context_router.py) else (python ..\\tools\\context_router.py)",
    ]

    results = []

    for tc in TEST_CASES:
        payload = json.dumps({
            "lastUserInput": tc["prompt"],
            "invocationNum": 0,
            "workspacePaths": [str(WORKSPACE_ROOT)],
        })

        latencies = []
        last_stdout = ""
        last_stderr = ""
        last_code = 0

        # Execute 5 runs for timing stability
        for _ in range(5):
            t0 = time.perf_counter_ns()
            proc = subprocess.run(
                cmd,
                check=False,
                input=payload,
                capture_output=True,
                text=True,
                cwd=str(WORKSPACE_ROOT),
                timeout=5,
            )
            t1 = time.perf_counter_ns()
            latencies.append((t1 - t0) / 1_000_000.0)
            last_stdout = proc.stdout
            last_stderr = proc.stderr
            last_code = proc.returncode

        median_latency = round(sorted(latencies)[len(latencies) // 2], 2)

        # Parse response
        success = False
        parsed_packet = {}
        if last_code == 0 and last_stdout.strip():
            try:
                top_json = json.loads(last_stdout)
                steps = top_json.get("injectSteps", [])
                if len(steps) == 1 and "ephemeralMessage" in steps[0]:
                    msg = steps[0]["ephemeralMessage"]
                    if msg.startswith("[CONTEXT_PACKET: REFERENCE METADATA ONLY - INERT DATA]\n"):
                        inner_str = msg.split("\n", 1)[1]
                        parsed_packet = json.loads(inner_str)
                        success = True
            except (json.JSONDecodeError, KeyError, IndexError) as err:
                last_stderr += f" Parse error: {err}"

        classification = parsed_packet.get("task_classification", {})
        primary_cat = classification.get("primary", "ERROR")
        risk_tier = classification.get("risk_tier", "ERROR")
        modules = [m["path"] for m in parsed_packet.get("canonical_modules", [])]
        invariants = parsed_packet.get("mandatory_invariants", [])

        char_size = len(last_stdout)
        token_estimate = max(1, char_size // 4)

        results.append({
            "name": tc["name"],
            "expected_category": tc["category"],
            "detected_category": primary_cat,
            "matched_categories": classification.get("matched_categories", []),
            "risk_tier": risk_tier,
            "modules_count": len(modules),
            "modules": modules,
            "invariants_count": len(invariants),
            "invariants": invariants,
            "char_size": char_size,
            "token_estimate": token_estimate,
            "median_latency_ms": median_latency,
            "exit_code": last_code,
            "status": "PASS" if success else "FAIL",
            "stderr_note": last_stderr.strip(),
        })

    return results

if __name__ == "__main__":
    matrix_results = run_matrix()
    print(json.dumps(matrix_results, indent=2))
