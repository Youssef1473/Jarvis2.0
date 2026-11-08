"""High-Performance Four-Tier Test Runner and Regression Governor for Jarvis 2.0.

Provides rigorous, parent-supervised process tree isolation and bounded test execution:
- Tier 1: Hermetic Unit (parallelized with pytest-xdist, zero network/hardware/browser)
- Tier 2: Async and IPC (Subtier 2A: Async Safe, Subtier 2B: Loopback & IPC)
- Tier 3: Windows OS Integration (serial, desktop handles, MMCSS, Win32 Job Objects)
- Tier 4: Hardware Integration (serial, CUDA, Kokoro ONNX neural TTS, faster-whisper, WASAPI)
- Browser Integration: Isolated real Chromium / Playwright execution

Guarantees:
- Hard process-tree containment under Win32 Job Objects with KILL_ON_JOB_CLOSE.
- Wall-clock deadline supervision (eliminates the historical ~26% runner stall).
- Disaggregated scorecards (Passed, Failed, Skipped, Timed Out, Environment Blocked).
- Zero global monkeypatching.
"""
from __future__ import annotations

import argparse
import errno
import json
import select
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

# Ensure workspace root is in sys.path
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.test_classifier import (
    TIER_1_HERMETIC_UNIT,
    TIER_2A_ASYNC_SAFE,
    TIER_2B_LOOPBACK_IPC,
    TIER_3_WINDOWS_INTEGRATION,
    TIER_4_HARDWARE_INTEGRATION,
    TIER_BROWSER_INTEGRATION,
    TestClassifier,
    get_python_executable,
)
from tools.test_supervisor import TestSupervisor


class LoopbackStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    BLOCKED_FILTER = "BLOCKED_FILTER"
    REFUSED = "REFUSED"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


@dataclass
class LoopbackProbeResult:
    """Rigorous diagnostic result of an empirical localhost loopback Winsock probe."""
    status: LoopbackStatus
    error_code: int | None = None
    detail: str = ""
    duration_ms: float = 0.0

    def __bool__(self) -> bool:
        """Enables backward-compatible truthiness checks: `if probe_loopback_available():`."""
        return self.status == LoopbackStatus.AVAILABLE


# Environmental Probes
def probe_loopback_available(timeout_sec: float = 0.1) -> LoopbackProbeResult:
    """Probes whether localhost loopback TCP connectivity is functional in this process.

    Implements a fail-closed 2-way Winsock handshake test:
    - Binds an ephemeral listener on 127.0.0.1:0.
    - Initiates non-blocking connect from client socket.
    - Treats WSAEWOULDBLOCK / BlockingIOError as pending connection.
    - Bounded select() wait on listener (read) and connector (write/except).
    - Inspects SO_ERROR to distinguish OS access blocks (10013) from refused (10061) or timeouts.
    - Completes server-side accept and verifies 2-way byte transfer.
    - Closes listener, client, and accepted connection on every path.
    """
    s = None
    c = None
    server_conn = None
    t0 = time.perf_counter()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setblocking(False)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]

        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c.setblocking(False)
        try:
            c.connect(("127.0.0.1", port))
        except BlockingIOError:
            pass  # Pending connection under non-blocking Winsock

        r, w, x = select.select([s], [c], [c], timeout_sec)
        dur = (time.perf_counter() - t0) * 1000.0

        if not r and not w and not x:
            return LoopbackProbeResult(
                status=LoopbackStatus.TIMEOUT,
                error_code=10060,
                detail=f"Loopback connection timed out after {dur:.1f}ms without response (SYN dropped).",
                duration_ms=dur,
            )

        # Inspect connector socket error
        err = c.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err in (10013, getattr(errno, "EACCES", 13)):
            return LoopbackProbeResult(
                status=LoopbackStatus.BLOCKED_FILTER,
                error_code=err,
                detail="Winsock returned WSAEACCES (10013); loopback connection forbidden by host sandbox filter.",
                duration_ms=dur,
            )
        if err in (10061, getattr(errno, "ECONNREFUSED", 111)):
            return LoopbackProbeResult(
                status=LoopbackStatus.REFUSED,
                error_code=err,
                detail="Winsock returned WSAECONNREFUSED (10061); target port refused connection.",
                duration_ms=dur,
            )
        if err != 0:
            return LoopbackProbeResult(
                status=LoopbackStatus.ERROR,
                error_code=err,
                detail=f"Winsock error {err} during loopback connection.",
                duration_ms=dur,
            )

        # If connector succeeded, verify server-side accept and two-way handshake
        if s in r:
            server_conn, _ = s.accept()
            server_conn.setblocking(False)
            c.send(b"\x00")
            sr, _, _ = select.select([server_conn], [], [], 0.05)
            if sr and server_conn.recv(1) == b"\x00":
                return LoopbackProbeResult(
                    status=LoopbackStatus.AVAILABLE,
                    error_code=0,
                    detail=f"Loopback 2-way handshake confirmed in {dur:.2f}ms.",
                    duration_ms=dur,
                )

        return LoopbackProbeResult(
            status=LoopbackStatus.ERROR,
            error_code=None,
            detail="Loopback connection incomplete: handshake verification failed.",
            duration_ms=dur,
        )
    except (OSError, ValueError) as exc:
        dur = (time.perf_counter() - t0) * 1000.0
        return LoopbackProbeResult(
            status=LoopbackStatus.ERROR,
            error_code=getattr(exc, "winerror", getattr(exc, "errno", None)),
            detail=f"Probe encountered unexpected exception: {exc}",
            duration_ms=dur,
        )
    finally:
        if server_conn:
            try:
                server_conn.close()
            except (OSError, ValueError):
                pass
        if c:
            try:
                c.close()
            except (OSError, ValueError):
                pass
        if s:
            try:
                s.close()
            except (OSError, ValueError):
                pass


def probe_hardware_available() -> dict[str, bool]:
    """Probes availability of local GPU, CUDA, and ONNX execution providers."""
    res = {"cuda": False, "onnx": False, "audio": True}
    try:
        import torch
        res["cuda"] = bool(torch.cuda.is_available())
    except ImportError:
        pass

    try:
        import onnxruntime as ort
        res["onnx"] = bool(ort.get_available_providers())
    except ImportError:
        pass

    return res


def probe_browser_available() -> bool:
    """Probes availability of local Chromium executable."""
    candidate_paths = [
        Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ]
    return any(p.exists() for p in candidate_paths)


@dataclass
class TierScorecard:
    """Disaggregated accounting scorecard for a single execution tier."""
    tier_name: str
    status: str  # "COMPLETED", "TIMEOUT", "ENVIRONMENT_BLOCKED", "HARDWARE_UNAVAILABLE", "FAILED"
    total_collected: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    timed_out: int = 0
    env_blocked: int = 0
    hardware_unavailable: int = 0
    duration_sec: float = 0.0
    exit_code: int = 0
    reason: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)


def format_scorecard_table(scorecards: list[TierScorecard], total_wall_clock: float) -> str:
    """Renders a disaggregated ASCII scorecard table."""
    lines = [
        "=" * 98,
        "                         JARVIS 2.0 FOUR-TIER TEST REGRESSION SCORECARD",
        "=" * 98,
        f"{'Tier':<28} {'Status':<16} {'Total':>6} {'Pass':>6} {'Fail':>6} {'Skip':>6} {'TimeOut':>7} {'Blocked':>7} {'Duration':>9}",
        "-" * 98,
    ]
    tot_total = tot_pass = tot_fail = tot_skip = tot_timeout = tot_blocked = 0
    for sc in scorecards:
        tot_total += sc.total_collected
        tot_pass += sc.passed
        tot_fail += sc.failed
        tot_skip += sc.skipped
        tot_timeout += sc.timed_out
        tot_blocked += (sc.env_blocked + sc.hardware_unavailable)
        dur_str = f"{sc.duration_sec:.2f}s"
        lines.append(
            f"{sc.tier_name:<28} {sc.status:<16} {sc.total_collected:>6} {sc.passed:>6} {sc.failed:>6} {sc.skipped:>6} {sc.timed_out:>7} {(sc.env_blocked + sc.hardware_unavailable):>7} {dur_str:>9}"
        )
    lines.append("-" * 98)
    lines.append(
        f"{'TOTALS':<28} {'':<16} {tot_total:>6} {tot_pass:>6} {tot_fail:>6} {tot_skip:>6} {tot_timeout:>7} {tot_blocked:>7} {total_wall_clock:>8.2f}s"
    )
    lines.append("=" * 98)
    return "\n".join(lines)


class FourTierTestRunner:
    """Orchestrates test execution across the four operational tiers."""

    def __init__(
        self,
        workspace_root: Path | None = None,
        supervisor: TestSupervisor | None = None,
        classifier: TestClassifier | None = None,
    ):
        self.workspace_root = workspace_root or WORKSPACE_ROOT
        self.supervisor = supervisor or TestSupervisor(workspace_root=self.workspace_root)
        self.classifier = classifier or TestClassifier(workspace_root=self.workspace_root)
        self._manifest_cache: dict[str, Any] | None = None

    def _get_manifest(self) -> dict[str, Any]:
        if self._manifest_cache is None:
            self._manifest_cache = self.classifier.get_manifest()
        return self._manifest_cache

    def _build_tier_targets(self, tier_nodes: list[str], manifest: dict[str, Any]) -> list[str]:
        """Builds minimal safe target arguments, collapsing non-split files and isolating split-file nodes."""
        tier_files = sorted({n.split("::")[0] for n in tier_nodes})
        targets: list[str] = []
        for fpath in tier_files:
            file_tier_nodes = [n for n in tier_nodes if n.split("::")[0] == fpath]
            all_file_nodes = [
                n for t_nodes in manifest.get("node_ids_by_tier", {}).values()
                if isinstance(t_nodes, list) for n in t_nodes if n.split("::")[0] == fpath
            ]
            if len(file_tier_nodes) < len(all_file_nodes):
                targets.extend(file_tier_nodes)
            else:
                targets.append(fpath)
        return targets

    def run_tier1_hermetic_unit(
        self,
        workers: int = 4,
        timeout_sec: float = 300.0,
        verbose: bool = False,
    ) -> TierScorecard:
        """Executes Tier 1: Hermetic Unit tests sharded across worker processes."""
        manifest = self._get_manifest()
        nodes = manifest.get("node_ids_by_tier", {}).get(TIER_1_HERMETIC_UNIT, [])
        total_collected = len(nodes)

        t1_files = sorted({n.split("::")[0] for n in nodes})
        if not t1_files:
            return TierScorecard(
                tier_name="Tier 1: Hermetic Unit",
                status="COMPLETED",
                total_collected=0,
            )

        pytest_args = [
            "-n", str(workers),
            "--dist", "loadfile",
            "-m", "not browser_integration and not loopback and not windows_integration and not hardware",
        ]
        if verbose:
            pytest_args.append("-v")
        else:
            pytest_args.append("-q")

        print(f"\n[Tier 1] Launching Hermetic Unit suite ({total_collected} tests across {len(t1_files)} files, {workers} workers)...")
        result = self.supervisor.run_pytest(
            targets=t1_files,
            pytest_args=pytest_args,
            timeout_sec=timeout_sec,
            run_id="tier1_hermetic_unit",
            record_junit=True,
        )

        status = result.status
        if result.timed_out:
            status = "TIMEOUT"
        elif result.exit_code != 0 and result.failed_count > 0:
            status = "FAILED"
        elif result.exit_code == 0:
            status = "COMPLETED"

        print(f"[Tier 1] Completed in {result.duration_sec:.2f}s (Status: {status}, Exit code: {result.exit_code})")
        print(f"Passed: {result.passed_count}, Failed: {result.failed_count}, Skipped: {result.skipped_count}\n")

        return TierScorecard(
            tier_name="Tier 1: Hermetic Unit",
            status=status,
            total_collected=total_collected,
            passed=result.passed_count,
            failed=result.failed_count,
            skipped=result.skipped_count,
            timed_out=1 if result.timed_out else 0,
            duration_sec=result.duration_sec,
            exit_code=result.exit_code,
            details=result.details,
        )

    def run_tier2_async_and_ipc(
        self,
        timeout_sec: float = 120.0,
        verbose: bool = False,
    ) -> list[TierScorecard]:
        """Executes Tier 2: Async Safe (2A) and Loopback & IPC (2B)."""
        manifest = self._get_manifest()
        nodes_2a = manifest.get("node_ids_by_tier", {}).get(TIER_2A_ASYNC_SAFE, [])
        nodes_2b = manifest.get("node_ids_by_tier", {}).get(TIER_2B_LOOPBACK_IPC, [])

        scorecards = []
        loopback_available = probe_loopback_available()

        # Subtier 2B: Loopback & IPC
        if not loopback_available:
            print(f"\n[Tier 2B] Localhost loopback TCP socketpair blocked by sandbox; marking {len(nodes_2b)} tests ENVIRONMENT_BLOCKED.")
            scorecards.append(
                TierScorecard(
                    tier_name="Tier 2B: Loopback & IPC",
                    status="ENVIRONMENT_BLOCKED",
                    total_collected=len(nodes_2b),
                    env_blocked=len(nodes_2b),
                    reason="Localhost loopback TCP connection is blocked by host/sandbox environment filter.",
                )
            )
        else:
            t2b_files = sorted({n.split("::")[0] for n in nodes_2b})
            targets_2b = self._build_tier_targets(nodes_2b, manifest)
            print(f"\n[Tier 2B] Launching Loopback & IPC suite ({len(nodes_2b)} tests across {len(t2b_files)} files)...")
            res_2b = self.supervisor.run_pytest(
                targets=targets_2b,
                pytest_args=["-v" if verbose else "-q"],
                timeout_sec=timeout_sec,
                run_id="tier2b_loopback_ipc",
                record_junit=True,
            )
            st_2b = "COMPLETED" if res_2b.exit_code == 0 else ("TIMEOUT" if res_2b.timed_out else "FAILED")
            scorecards.append(
                TierScorecard(
                    tier_name="Tier 2B: Loopback & IPC",
                    status=st_2b,
                    total_collected=len(nodes_2b),
                    passed=res_2b.passed_count,
                    failed=res_2b.failed_count,
                    skipped=res_2b.skipped_count,
                    timed_out=1 if res_2b.timed_out else 0,
                    duration_sec=res_2b.duration_sec,
                    exit_code=res_2b.exit_code,
                    details=res_2b.details,
                )
            )

        # Subtier 2A: Async Safe
        # On Windows, asyncio event loops require socket.socketpair(). If loopback is blocked, event loops hang.
        if not loopback_available:
            print(f"\n[Tier 2A] Windows ProactorEventLoop requires loopback socketpair; marking {len(nodes_2a)} async tests ENVIRONMENT_BLOCKED.")
            scorecards.append(
                TierScorecard(
                    tier_name="Tier 2A: Async Safe",
                    status="ENVIRONMENT_BLOCKED",
                    total_collected=len(nodes_2a),
                    env_blocked=len(nodes_2a),
                    reason="Windows asyncio event loop initialization requires socket.socketpair() loopback access.",
                )
            )
        else:
            t2a_files = sorted({n.split("::")[0] for n in nodes_2a})
            targets_2a = self._build_tier_targets(nodes_2a, manifest)
            print(f"\n[Tier 2A] Launching Async Safe suite ({len(nodes_2a)} tests across {len(t2a_files)} files)...")
            res_2a = self.supervisor.run_pytest(
                targets=targets_2a,
                pytest_args=["-v" if verbose else "-q"],
                timeout_sec=timeout_sec,
                run_id="tier2a_async_safe",
                record_junit=True,
            )
            st_2a = "COMPLETED" if res_2a.exit_code == 0 else ("TIMEOUT" if res_2a.timed_out else "FAILED")
            scorecards.append(
                TierScorecard(
                    tier_name="Tier 2A: Async Safe",
                    status=st_2a,
                    total_collected=len(nodes_2a),
                    passed=res_2a.passed_count,
                    failed=res_2a.failed_count,
                    skipped=res_2a.skipped_count,
                    timed_out=1 if res_2a.timed_out else 0,
                    duration_sec=res_2a.duration_sec,
                    exit_code=res_2a.exit_code,
                    details=res_2a.details,
                )
            )

        return scorecards

    def run_tier3_windows_integration(
        self,
        timeout_sec: float = 30.0,
        verbose: bool = False,
    ) -> TierScorecard:
        """Executes Tier 3: Windows OS Integration serially file-by-file with per-file bounds."""
        manifest = self._get_manifest()
        nodes = manifest.get("node_ids_by_tier", {}).get(TIER_3_WINDOWS_INTEGRATION, [])
        t3_files = sorted({n.split("::")[0] for n in nodes})

        print(f"\n[Tier 3] Launching Windows OS Integration suite ({len(nodes)} tests across {len(t3_files)} files, serial)...")
        total_passed = total_failed = total_skipped = total_timed_out = 0
        total_duration = 0.0
        max_exit = 0
        all_details = []

        for fpath in t3_files:
            run_id = f"tier3_{Path(fpath).stem}_{int(time.time() * 1000) % 1000000}"
            file_tier_nodes = [n for n in nodes if n.split("::")[0] == fpath]
            all_file_nodes = [n for t_nodes in manifest.get("node_ids_by_tier", {}).values() if isinstance(t_nodes, list) for n in t_nodes if n.split("::")[0] == fpath]
            targets = file_tier_nodes if len(file_tier_nodes) < len(all_file_nodes) else fpath
            res = self.supervisor.run_pytest(
                targets=targets,
                pytest_args=["-v" if verbose else "-q"],
                timeout_sec=timeout_sec,
                run_id=run_id,
                record_junit=True,
            )
            total_duration += res.duration_sec
            total_passed += res.passed_count
            total_failed += res.failed_count
            total_skipped += res.skipped_count
            if res.timed_out:
                total_timed_out += 1
                max_exit = max(max_exit, 124)
            elif res.exit_code != 0:
                max_exit = max(max_exit, res.exit_code)
            all_details.extend(res.details)

        if total_timed_out > 0:
            status = "TIMEOUT"
        elif total_failed > 0 or max_exit != 0:
            status = "FAILED"
        else:
            status = "COMPLETED"

        return TierScorecard(
            tier_name="Tier 3: Windows Integration",
            status=status,
            total_collected=len(nodes),
            passed=total_passed,
            failed=total_failed,
            skipped=total_skipped,
            timed_out=total_timed_out,
            duration_sec=total_duration,
            exit_code=max_exit,
            details=all_details,
        )

    def run_tier4_hardware_integration(
        self,
        timeout_sec: float = 30.0,
        verbose: bool = False,
    ) -> TierScorecard:
        """Executes Tier 4: Hardware Integration serially file-by-file, probing CUDA / ONNX."""
        manifest = self._get_manifest()
        nodes = manifest.get("node_ids_by_tier", {}).get(TIER_4_HARDWARE_INTEGRATION, [])
        t4_files = sorted({n.split("::")[0] for n in nodes})

        hw_info = probe_hardware_available()
        if not (hw_info["cuda"] or hw_info["onnx"]):
            print(f"\n[Tier 4] Required hardware (CUDA/ONNX) not available; reporting {len(nodes)} tests as HARDWARE_UNAVAILABLE.")
            return TierScorecard(
                tier_name="Tier 4: Hardware Integration",
                status="HARDWARE_UNAVAILABLE",
                total_collected=len(nodes),
                hardware_unavailable=len(nodes),
                reason="Local GPU, CUDA, and ONNX execution providers are not available.",
            )

        print(f"\n[Tier 4] Launching Hardware Integration suite ({len(nodes)} tests across {len(t4_files)} files, serial)...")
        total_passed = total_failed = total_skipped = total_timed_out = 0
        total_duration = 0.0
        max_exit = 0
        all_details = []

        for fpath in t4_files:
            run_id = f"tier4_{Path(fpath).stem}_{int(time.time() * 1000) % 1000000}"
            file_tier_nodes = [n for n in nodes if n.split("::")[0] == fpath]
            all_file_nodes = [n for t_nodes in manifest.get("node_ids_by_tier", {}).values() if isinstance(t_nodes, list) for n in t_nodes if n.split("::")[0] == fpath]
            targets = file_tier_nodes if len(file_tier_nodes) < len(all_file_nodes) else fpath
            res = self.supervisor.run_pytest(
                targets=targets,
                pytest_args=["-v" if verbose else "-q"],
                timeout_sec=timeout_sec,
                run_id=run_id,
                record_junit=True,
            )
            total_duration += res.duration_sec
            total_passed += res.passed_count
            total_failed += res.failed_count
            total_skipped += res.skipped_count
            if res.timed_out:
                total_timed_out += 1
                max_exit = max(max_exit, 124)
            elif res.exit_code != 0:
                max_exit = max(max_exit, res.exit_code)
            all_details.extend(res.details)

        if total_timed_out > 0:
            status = "TIMEOUT"
        elif total_failed > 0 or max_exit != 0:
            status = "FAILED"
        else:
            status = "COMPLETED"

        return TierScorecard(
            tier_name="Tier 4: Hardware Integration",
            status=status,
            total_collected=len(nodes),
            passed=total_passed,
            failed=total_failed,
            skipped=total_skipped,
            timed_out=total_timed_out,
            duration_sec=total_duration,
            exit_code=max_exit,
            details=all_details,
        )

    def run_browser_integration(
        self,
        timeout_sec: float = 60.0,
        verbose: bool = False,
    ) -> TierScorecard:
        """Executes Browser Integration suite serially."""
        manifest = self._get_manifest()
        nodes = manifest.get("node_ids_by_tier", {}).get(TIER_BROWSER_INTEGRATION, [])
        browser_files = sorted({n.split("::")[0] for n in nodes})

        browser_avail = probe_browser_available()
        loopback_avail = probe_loopback_available()

        if not browser_avail or not loopback_avail:
            reason = "Chromium executable not located" if not browser_avail else "Sandbox blocks loopback DevTools socket"
            print(f"\n[Browser] Environment blocked ({reason}); marking {len(nodes)} tests ENVIRONMENT_BLOCKED.")
            return TierScorecard(
                tier_name="Browser Integration",
                status="ENVIRONMENT_BLOCKED",
                total_collected=len(nodes),
                env_blocked=len(nodes),
                reason=reason,
            )

        print(f"\n[Browser] Launching Browser Integration suite ({len(nodes)} tests across {len(browser_files)} files)...")
        result = self.supervisor.run_pytest(
            targets=browser_files,
            pytest_args=["-m", "browser_integration", "-v" if verbose else "-q"],
            timeout_sec=timeout_sec,
            run_id="browser_integration",
            record_junit=True,
        )

        status = "COMPLETED" if result.exit_code == 0 else ("TIMEOUT" if result.timed_out else "FAILED")
        return TierScorecard(
            tier_name="Browser Integration",
            status=status,
            total_collected=len(nodes),
            passed=result.passed_count,
            failed=result.failed_count,
            skipped=result.skipped_count,
            timed_out=1 if result.timed_out else 0,
            duration_sec=result.duration_sec,
            exit_code=result.exit_code,
            details=result.details,
        )

    def run_targeted(
        self,
        target: str,
        timeout_sec: float = 60.0,
        verbose: bool = False,
    ) -> int:
        """Runs an individual targeted test file or directory with supervisor containment."""
        t_path = Path(target)
        if not t_path.is_absolute() and not str(target).startswith("tests"):
            t_path = Path("tests") / target

        print(f"\n[Targeted] Running tests in '{t_path}' under supervisor...")
        args = ["-v"] if verbose else ["-q"]
        result = self.supervisor.run_pytest(
            targets=str(t_path),
            pytest_args=args,
            timeout_sec=timeout_sec,
            run_id=f"targeted_{int(time.time())}",
            record_junit=True,
        )
        print(f"[Targeted] Completed in {result.duration_sec:.2f}s (Status: {result.status}, Exit code: {result.exit_code})")
        if result.timed_out:
            print(f"[Targeted] Execution TIMED OUT after {timeout_sec:.1f}s.")
        print(f"Passed: {result.passed_count}, Failed: {result.failed_count}, Skipped: {result.skipped_count}\n")
        return result.exit_code

    def run_all(
        self,
        workers: int = 4,
        verbose: bool = False,
        scorecard_json_path: Path | None = None,
    ) -> tuple[int, list[TierScorecard]]:
        """Executes all tiers in deterministic sequence and compiles disaggregated scorecard."""
        t_start = time.perf_counter()
        scorecards: list[TierScorecard] = []

        # 1. Tier 1: Hermetic Unit
        sc1 = self.run_tier1_hermetic_unit(workers=workers, verbose=verbose)
        scorecards.append(sc1)

        # 2. Tier 2: Async and IPC (2A and 2B)
        sc2 = self.run_tier2_async_and_ipc(verbose=verbose)
        scorecards.extend(sc2)

        # 3. Tier 3: Windows OS Integration
        sc3 = self.run_tier3_windows_integration(verbose=verbose)
        scorecards.append(sc3)

        # 4. Tier 4: Hardware Integration
        sc4 = self.run_tier4_hardware_integration(verbose=verbose)
        scorecards.append(sc4)

        # 5. Browser Integration
        sc_b = self.run_browser_integration(verbose=verbose)
        scorecards.append(sc_b)

        total_elapsed = time.perf_counter() - t_start
        table_str = format_scorecard_table(scorecards, total_elapsed)
        print("\n" + table_str)

        if scorecard_json_path:
            payload = {
                "timestamp": time.time(),
                "total_wall_clock_sec": total_elapsed,
                "scorecards": [asdict(sc) for sc in scorecards],
            }
            scorecard_json_path.parent.mkdir(parents=True, exist_ok=True)
            with open(scorecard_json_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            print(f"\n[Scorecard] Exported disaggregated metrics to {scorecard_json_path}")

        # Exit code logic: fail if any tier failed or timed out
        has_failure = any(sc.failed > 0 or sc.timed_out > 0 for sc in scorecards)
        max_exit_code = max(sc.exit_code for sc in scorecards)
        return (1 if has_failure else max_exit_code), scorecards


def main():
    parser = argparse.ArgumentParser(description="Jarvis 2.0 Four-Tier High-Performance Test Runner")
    parser.add_argument("target", nargs="?", default=None, help="Specific test file or directory to run")
    parser.add_argument("--unit", action="store_true", help="Run Tier 1: Hermetic Unit tests")
    parser.add_argument("--async", dest="run_async", action="store_true", help="Run Tier 2: Async and IPC tests")
    parser.add_argument("--win32", action="store_true", help="Run Tier 3: Windows OS Integration tests")
    parser.add_argument("--hardware", action="store_true", help="Run Tier 4: Hardware Integration tests")
    parser.add_argument("--browser", action="store_true", help="Run Browser Integration tests")
    parser.add_argument("--all", action="store_true", help="Run all tiers in sequence and generate scorecard")
    parser.add_argument("--fast", action="store_true", help="Synonym for --unit (for backward compatibility)")
    parser.add_argument("-w", "--workers", type=int, default=4, help="Worker count for parallel tests (default: 4)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose pytest output")
    parser.add_argument("--timeout", type=float, default=None, help="Custom timeout override in seconds")
    parser.add_argument("--scorecard-json", type=str, default=None, help="Path to export JSON scorecard")
    parser.add_argument("--failed", action="store_true", help="Rerun only last failed tests (--lf)")

    args = parser.parse_args()
    runner = FourTierTestRunner()

    if args.failed:
        py_exec = get_python_executable()
        cmd = [py_exec, "-m", "pytest", "--lf"]
        if args.verbose:
            cmd.append("-v")
        return subprocess.run(cmd, cwd=str(WORKSPACE_ROOT), check=False).returncode

    if args.target:
        timeout = args.timeout if args.timeout is not None else 60.0
        return runner.run_targeted(args.target, timeout_sec=timeout, verbose=args.verbose)

    if args.all:
        json_path = Path(args.scorecard_json) if args.scorecard_json else None
        code, _ = runner.run_all(workers=args.workers, verbose=args.verbose, scorecard_json_path=json_path)
        return code

    if args.hardware:
        timeout = args.timeout if args.timeout is not None else 180.0
        sc = runner.run_tier4_hardware_integration(timeout_sec=timeout, verbose=args.verbose)
        return sc.exit_code

    if args.win32:
        timeout = args.timeout if args.timeout is not None else 120.0
        sc = runner.run_tier3_windows_integration(timeout_sec=timeout, verbose=args.verbose)
        return sc.exit_code

    if args.run_async:
        timeout = args.timeout if args.timeout is not None else 120.0
        scs = runner.run_tier2_async_and_ipc(timeout_sec=timeout, verbose=args.verbose)
        return max((s.exit_code for s in scs), default=0)

    if args.browser:
        timeout = args.timeout if args.timeout is not None else 60.0
        sc = runner.run_browser_integration(timeout_sec=timeout, verbose=args.verbose)
        return sc.exit_code

    # Default / --unit / --fast
    timeout = args.timeout if args.timeout is not None else 300.0
    sc = runner.run_tier1_hermetic_unit(workers=args.workers, timeout_sec=timeout, verbose=args.verbose)
    return sc.exit_code


if __name__ == "__main__":
    sys.exit(main())
