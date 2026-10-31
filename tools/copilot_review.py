#!/usr/bin/env python3
"""tools/copilot_review.py — Autonomous Peer Review Bridge to GPT-6 Luna via GitHub Copilot CLI.

Enables deep, zero-cost architectural, security, and concurrency reviews by dispatching
prompts, diffs, and subsystem files to the Copilot CLI runtime (resolving to GPT-6 Luna).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SYSTEM_DIRECTIVE = """[SYSTEM / ADVERSARIAL PRINCIPAL ARCHITECT DIRECTIVE]
You are GPT-6 Luna acting as the Principal Systems Architect, Windows OS Concurrency Specialist,
and Security Reviewer for Jarvis 2.0.

Perform an exhaustive, deep chain-of-thought review. You do NOT accept surface-level correctness.
Scrutinize specifically:
1. Concurrency & Race Conditions: Thread affinity, Win32 message pumping, condition variable signaling,
   async event loop starvation, lock ordering, thread-safe cancellation (<10ns epoch invalidation).
2. Win32 Handle & Resource Lifecycles: Leak-free CloseHandle / FreeSid / SafeHandle semantics,
   proper cleanup in finally blocks, Job Object containment, AppContainer ACLs.
3. Security Boundaries & Invariants: Confined paths, SSRF egress validation, AST sandboxing,
   fail-closed privilege drop, no command injection or shell=True.
4. Backward Compatibility & Public APIs: Re-export shims, formal type signatures, test harness expectations.

Deliver your analysis directly, stating exact files, line numbers, failure scenarios, and concrete remediation diffs.
If code is completely sound, state your verification reasoning plainly without filler.
"""


def run_copilot(prompt: str, timeout: int = 480) -> dict[str, str]:
    """Execute non-interactive prompt against Copilot CLI and parse model telemetry + output."""
    full_prompt = f"{SYSTEM_DIRECTIVE}\n\n{prompt}"
    cmd = [
        "copilot",
        "--output-format",
        "json",
        "--no-ask-user",
        "--allow-all",
        "-s",
    ]

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            input=full_prompt,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"error": f"Copilot CLI timed out after {timeout}s", "status": "timeout"}
    except (OSError, ValueError) as e:
        return {"error": f"Failed to execute Copilot CLI: {e}", "status": "error"}

    duration = time.perf_counter() - start

    model_used = "unknown"
    reasoning_snippets: list[str] = []
    assistant_messages: list[str] = []

    for line in proc.stdout.splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        try:
            event = json.loads(line_str)
        except json.JSONDecodeError:
            continue

        ev_type = event.get("type", "")
        data = event.get("data", {})

        if ev_type == "session.auto_mode_resolved":
            model_used = data.get("chosenModel", model_used)
        elif ev_type == "model.call_start":
            model_used = data.get("model", model_used)
        elif ev_type == "assistant.reasoning_delta":
            delta = data.get("deltaContent", "")
            if delta:
                reasoning_snippets.append(delta)
        elif ev_type == "assistant.message":
            content = data.get("content", "")
            if content and content.strip():
                assistant_messages.append(content.strip())
        elif "error" in ev_type.lower():
            err_msg = data.get("message") or str(data)
            assistant_messages.append(f"[Copilot Error: {ev_type}] {err_msg}")

    final_text = assistant_messages[-1] if assistant_messages else ""
    if not final_text:
        if proc.returncode != 0:
            final_text = f"CLI Exit {proc.returncode}: {proc.stderr.strip()}"
        elif proc.stdout.strip():
            # Extract first 500 chars of stdout for diagnostics
            final_text = f"No assistant message in events. Raw stdout: {proc.stdout[:500]}"

    return {
        "status": "success" if proc.returncode == 0 else "failed",
        "returncode": str(proc.returncode),
        "model": model_used,
        "duration_sec": f"{duration:.2f}",
        "reasoning_length": str(sum(len(s) for s in reasoning_snippets)),
        "response": final_text,
    }


def review_diff(targets: list[str] | None = None) -> None:
    """Review git diff against HEAD or for specific files/subsystems with empirical test evidence."""
    cmd = ["git", "diff"]
    if targets:
        cmd.extend(["--"] + targets)
    diff = subprocess.check_output(cmd, text=True, errors="replace")
    if not diff.strip():
        print(f"[copilot_review] No diff found for: {targets or 'working tree'}")
        return

    label = ", ".join(targets) if targets else "working tree"
    print(f"[copilot_review] Dispatching {len(diff.splitlines())} lines of git diff ({label}) to GPT-6 Luna...")

    # Automatically discover and run targeted test suites to provide empirical verification evidence
    test_evidence = ""
    test_files = [t for t in (targets or []) if "test" in t and Path(t).is_file()]
    if not test_files:
        for t in targets or []:
            stem = Path(t).stem
            candidate = Path(f"tests/test_{stem}_lifecycle_hardening.py")
            if candidate.is_file():
                test_files.append(str(candidate))
            candidate2 = Path(f"tests/test_{stem}.py")
            if candidate2.is_file():
                test_files.append(str(candidate2))

    if test_files:
        test_outputs = []
        for tf in test_files:
            try:
                print(f"[copilot_review] Running verification test {tf}...")
                res = subprocess.run(
                    [sys.executable, "tools/run_fast_tests.py", tf],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                test_outputs.append(
                    f"$ python tools/run_fast_tests.py {tf}\n"
                    f"Exit code: {res.returncode}\n"
                    f"{res.stdout.strip()}"
                )
            except Exception as ex:  # noqa: BLE001
                test_outputs.append(f"$ python tools/run_fast_tests.py {tf}\nExecution note: {ex}")
        test_evidence = "\n\n### Empirical Test Verification Output:\n```text\n" + "\n\n".join(test_outputs) + "\n```"

    prompt = (
        f"Please review this uncommitted git diff for `{label}` against the codebase architecture, "
        f"concurrency invariants, and security rules:\n\n```diff\n{diff}\n```{test_evidence}"
    )
    result = run_copilot(prompt)
    _print_result(result)


def review_file(file_path: str) -> None:
    """Review a specific file."""
    path = Path(file_path)
    if not path.is_file():
        print(f"[copilot_review] File not found: {file_path}")
        sys.exit(1)

    content = path.read_text(encoding="utf-8", errors="replace")
    print(f"[copilot_review] Dispatching {file_path} ({len(content.splitlines())} lines) to GPT-6 Luna...")
    prompt = f"Please review the implementation of `{file_path}` for concurrency bugs, Win32 handle leaks, and architectural flaws:\n\n```python\n{content}\n```"
    result = run_copilot(prompt)
    _print_result(result)


def review_query(query: str) -> None:
    """Send an arbitrary engineering query to GPT-6 Luna."""
    print("[copilot_review] Dispatching query to GPT-6 Luna...")
    result = run_copilot(query)
    _print_result(result)


def _print_result(res: dict[str, str]) -> None:
    print(f"\n{'='*70}")
    print(f"Reviewer: {res.get('model', 'unknown')} | Latency: {res.get('duration_sec', '0')}s | Status: {res.get('status', 'unknown')}")
    print(f"{'='*70}\n")
    response_text = res.get("response") or res.get("error") or "(No response received)"
    try:
        print(response_text)
    except Exception:  # noqa: BLE001
        print(response_text.encode("ascii", errors="replace").decode("ascii"))
    print(f"\n{'='*70}\n")

    try:
        out_dir = Path("data")
        out_dir.mkdir(exist_ok=True)
        (out_dir / "last_review_verdict.md").write_text(response_text, encoding="utf-8")
    except Exception:  # noqa: BLE001, S110
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Copilot (GPT-6 Luna) Peer Review Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # diff
    p_diff = subparsers.add_parser("diff", help="Review current uncommitted git diff")
    p_diff.add_argument("targets", nargs="*", default=None, help="Optional git target files or base ref")

    # file
    p_file = subparsers.add_parser("file", help="Review a specific file")
    p_file.add_argument("path", help="Path to file")

    # query
    p_query = subparsers.add_parser("query", help="Ask a specific architectural query")
    p_query.add_argument("prompt", help="Query prompt")

    args = parser.parse_args()

    if args.command == "diff":
        review_diff(args.targets)
    elif args.command == "file":
        review_file(args.path)
    elif args.command == "query":
        review_query(args.prompt)


if __name__ == "__main__":
    main()
