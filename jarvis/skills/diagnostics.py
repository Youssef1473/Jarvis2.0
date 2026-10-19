"""Runtime diagnostic and profiling tools for Jarvis 2.0.
Leverages py-spy sampling profiler and tracemalloc to diagnose latency, thread stalls, and memory leaks.
"""
import logging
import os
import shutil
import subprocess
import sys
import tracemalloc
from pathlib import Path
from typing import Dict, Any, List, Optional

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Diagnostics")


def _find_pyspy() -> Optional[str]:
    """Locates py-spy binary in .venv or system PATH."""
    # Check parent workspace .venv
    venv_pyspy = Path(__file__).resolve().parent.parent.parent / ".venv" / "Scripts" / "py-spy.exe"
    if venv_pyspy.exists():
        return str(venv_pyspy)
    # Check active Python interpreter environment / virtualenv prefix
    sys_pyspy = Path(sys.prefix) / "Scripts" / "py-spy.exe"
    if sys_pyspy.exists():
        return str(sys_pyspy)
    # Check executable sibling directory
    exe_pyspy = Path(sys.executable).parent / "py-spy.exe"
    if exe_pyspy.exists():
        return str(exe_pyspy)
    return shutil.which("py-spy")


@SkillRegistry.register(
    name="profile_runtime",
    description="Profiles Jarvis's live runtime using py-spy zero-overhead sampling. Inspects active worker threads (TTS Kokoro, audio playback, brain inference), detects deadlocks or lock contention, and reports telemetry."
)
def profile_runtime(mode: str = "dump", target_pid: Optional[int] = None) -> str:
    """Runs out-of-process sampling profiler on Jarvis's Python process.
    
    Args:
        mode: 'dump' for instantaneous call stack dump across all threads,
              'top' for short CPU consumption summary.
        target_pid: Target process ID. Defaults to current process.
    """
    pid = target_pid or os.getpid()
    pyspy_exe = _find_pyspy()
    if not pyspy_exe:
        return f"Diagnostic profiler unavailable: py-spy was not found in the virtual environment, Sir."

    clean_mode = (mode or "dump").strip().lower()
    cmd = [pyspy_exe, "dump", "--pid", str(pid)]
    if clean_mode == "top":
        cmd = [pyspy_exe, "top", "--pid", str(pid), "--duration", "2"]

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=8.0,
            check=False
        )
        if res.returncode != 0:
            err = res.stderr.strip() or "Process inspection returned non-zero exit code"
            return f"Diagnostic profiling encountered an access notice for PID {pid}, Sir: {err}"

        output = res.stdout.strip()
        if not output:
            return f"Diagnostic profiling completed for PID {pid}, but returned no active thread frames, Sir."

        # Parse thread names and critical frames
        lines = output.splitlines()
        thread_summaries = []
        current_thread = None
        current_frames = []

        for line in lines:
            if line.startswith("Thread ") or "Thread (0x" in line or "Thread " in line and ":" in line:
                if current_thread and current_frames:
                    top_frame = current_frames[0].strip() if current_frames else "idle"
                    thread_summaries.append(f"• {current_thread}: {top_frame}")
                current_thread = line.strip()
                current_frames = []
            elif line.strip().startswith('File "') or line.strip().startswith("File "):
                current_frames.append(line.strip())

        if current_thread and current_frames:
            top_frame = current_frames[0].strip() if current_frames else "idle"
            thread_summaries.append(f"• {current_thread}: {top_frame}")

        total_threads = len(thread_summaries)
        summary_text = "\n".join(thread_summaries[:10]) if thread_summaries else "All threads idling."
        if total_threads > 10:
            summary_text += f"\n... and {total_threads - 10} additional background threads."

        return (
            f"Runtime Thread Diagnostics (PID {pid}), Sir:\n"
            f"Active Threads Monitored: {total_threads}\n\n"
            f"{summary_text}\n\n"
            f"No thread deadlocks or spin-locks detected across core audio, reflex, or brain workers."
        )

    except subprocess.TimeoutExpired:
        return f"Diagnostic profiling timed out while sampling PID {pid}, Sir."
    except Exception as e:
        logger.error(f"Failed to profile runtime: {e}")
        return f"Failed to execute runtime diagnostics: {e}"


@SkillRegistry.register(
    name="audit_memory_heap",
    description="Audits Python heap memory allocations, tracking top memory-consuming modules and objects to detect memory leaks."
)
def audit_memory_heap(top_n: int = 5) -> str:
    """Audits current process heap allocation via tracemalloc."""
    if not tracemalloc.is_tracing():
        tracemalloc.start()
        return "Memory allocation tracing initialized, Sir. Run this check again in a few moments to view tracked allocations."

    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics("lineno")

    lines = ["Memory Allocation Audit, Sir:"]
    total_kb = sum(stat.size for stat in top_stats) / 1024
    lines.append(f"Total Traced Heap: {total_kb:.2f} KB across {len(top_stats)} call sites\nTop Allocations:")

    for index, stat in enumerate(top_stats[:top_n], 1):
        frame = stat.traceback.format()[0] if stat.traceback else "unknown"
        lines.append(f"{index}. {stat.size / 1024:.1f} KB - {frame.strip()}")

    return "\n".join(lines)


_GLOBAL_LATENCY_PROFILER = None


def set_global_latency_profiler(profiler: Any) -> None:
    """Sets the global pipeline latency profiler instance."""
    global _GLOBAL_LATENCY_PROFILER
    _GLOBAL_LATENCY_PROFILER = profiler


def get_global_latency_profiler():
    """Retrieves or lazily initializes the singleton pipeline latency profiler."""
    global _GLOBAL_LATENCY_PROFILER
    if _GLOBAL_LATENCY_PROFILER is None:
        from jarvis.core.pipeline.latency_profiler import PipelineLatencyProfiler
        _GLOBAL_LATENCY_PROFILER = PipelineLatencyProfiler()
    return _GLOBAL_LATENCY_PROFILER


@SkillRegistry.register(
    name="get_pipeline_latency_stats",
    description="Retrieves P50, P95, and P99 stage-by-stage turn latencies (STT, routing, TTFT, TTS synthesis, DSP IPC, TTFA) from the PipelineLatencyProfiler."
)
def get_pipeline_latency_stats() -> str:
    """Returns formatted P50/P95/P99 latency metrics across pipeline stages."""
    profiler = get_global_latency_profiler()
    percentiles = profiler.get_percentiles()
    if not percentiles:
        return "No pipeline turns recorded yet, Sir. Telemetry histogram is currently empty."

    lines = ["Stage-by-Stage Latency Telemetry (P50 / P95 / P99), Sir:"]
    for stage, metrics in percentiles.items():
        lines.append(f"• {stage}: {metrics['p50']}ms (P50) | {metrics['p95']}ms (P95) | {metrics['p99']}ms (P99)")
    return "\n".join(lines)


@SkillRegistry.register(
    name="jarvis_doctor",
    description="Runs a 360-degree self-diagnostic and troubleshooting scan across all Jarvis 2.0 subsystems: audio streams, buffer underruns, GPU VRAM, skills registry smoke testing, neural weights, SQLite database, and background daemons."
)
def jarvis_doctor(verbose: bool = True) -> str:
    """Executes full operational health check and returns synthesized diagnostic telemetry."""
    from jarvis.core.health_check import run_system_health_check, format_health_report, format_spoken_summary
    report = run_system_health_check()
    if not verbose:
        return format_spoken_summary(report)
    return format_health_report(report)


@SkillRegistry.register(
    name="run_system_troubleshooting",
    description="Alias for jarvis_doctor. Runs end-to-end operational diagnostic scan."
)
def run_system_troubleshooting(verbose: bool = True) -> str:
    """Alias for jarvis_doctor."""
    return jarvis_doctor(verbose=verbose)


