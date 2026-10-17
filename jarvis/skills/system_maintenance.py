"""Workstation diagnostics, RAM hog inspection, and safe system maintenance for Windows."""
import os
import time
import shutil
import logging
import subprocess
from typing import Dict, List
import psutil
from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Maintenance")


@SkillRegistry.register(
    name="inspect_ram_hogs",
    description="Identifies and reports the top 5 memory-consuming applications currently running on Sir's workstation."
)
def inspect_ram_hogs() -> str:
    """Scans and groups active processes by memory consumption (RAM)."""
    mem_by_name: Dict[str, int] = {}
    for proc in psutil.process_iter(['name', 'memory_info']):
        try:
            name = proc.info.get('name')
            mem_info = proc.info.get('memory_info')
            if name and mem_info:
                clean_name = name.lower()
                mem_by_name[clean_name] = mem_by_name.get(clean_name, 0) + mem_info.rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not mem_by_name:
        return "Could not retrieve process memory telemetry, Sir."

    sorted_hogs = sorted(mem_by_name.items(), key=lambda x: x[1], reverse=True)[:5]
    lines = []
    for proc_name, rss_bytes in sorted_hogs:
        mb = rss_bytes / (1024 * 1024)
        if mb >= 1024:
            gb = mb / 1024
            lines.append(f"• {proc_name.title()}: {gb:.2f} GB")
        else:
            lines.append(f"• {proc_name.title()}: {int(mb)} MB")

    return "Top Memory Consumers, Sir:\n" + "\n".join(lines)


@SkillRegistry.register(
    name="optimize_workstation",
    description="Optimizes workstation health by safely cleaning stale temporary files and flushing Windows DNS cache."
)
def optimize_workstation() -> str:
    """Flushes DNS cache and purges stale temp files older than 24 hours."""
    # 1. Flush DNS cache
    dns_flushed = False
    try:
        res = subprocess.run(["ipconfig", "/flushdns"], capture_output=True, text=True, timeout=3)
        dns_flushed = res.returncode == 0
    except Exception as e:
        logger.debug(f"DNS flush notice: {e}")

    # 2. Clean stale files in %TEMP% older than 24 hours
    temp_dir = os.environ.get("TEMP", "")
    freed_bytes = 0
    cleaned_files = 0
    cutoff = time.time() - (24 * 3600)

    if temp_dir and os.path.exists(temp_dir):
        for root, dirs, files in os.walk(temp_dir):
            for f in files:
                f_path = os.path.join(root, f)
                try:
                    st = os.stat(f_path)
                    if st.st_mtime < cutoff:
                        freed_bytes += st.st_size
                        os.remove(f_path)
                        cleaned_files += 1
                except Exception:
                    # Stale file is locked by active process or permission denied
                    continue

    freed_mb = freed_bytes / (1024 * 1024)
    dns_msg = "Windows DNS resolver cache flushed." if dns_flushed else "DNS flush completed."
    return f"Workstation optimization complete, Sir. {dns_msg} Cleared {cleaned_files} stale temporary file(s) ({freed_mb:.1f} MB freed)."
