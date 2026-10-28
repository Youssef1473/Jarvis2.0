"""Hardware Thermal, Battery & System Watchdog Sentinel for Jarvis 2.0."""
import logging
import os
import subprocess
import threading
import time
from typing import Any

import psutil

from jarvis.skills.base import SkillRegistry
from jarvis.skills.os_control import get_raw_battery_info

logger = logging.getLogger("Jarvis.Watchdog")

_watchdog_thread: threading.Thread | None = None
_stop_event = threading.Event()
_watchdog_lock = threading.Lock()
_watchdog_active = False

# Hysteresis & cooldown tracking (prevent repetitive audio spam)
_ALERT_COOLDOWN_SECONDS = 600.0  # 10 minutes
_last_alerts: dict[str, float] = {
    "battery": 0.0,
    "cpu": 0.0,
    "ram": 0.0,
    "gpu": 0.0,
}
_consecutive_high_cpu = 0


def _speak_watchdog_alert(alert_message: str):
    """Dispatches a priority audio chime and asynchronous SAPI5 speech warning."""
    try:
        import winsound
        sound_path = os.path.abspath("data/sounds/alert.wav")
        if not os.path.exists(sound_path):
            sound_path = os.path.abspath("data/sounds/done.wav")
        if os.path.exists(sound_path):
            winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        logger.debug(f"Watchdog chime notice: {e}")

    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        sapi = win32com.client.Dispatch("SAPI.SpVoice")
        sapi.Rate = 1
        sapi.Speak(f"System alert, Sir: {alert_message}", 1)
    except Exception as e:
        logger.warning(f"Watchdog speech notice: {e}")


def _query_gpu_telemetry() -> dict[str, Any] | None:
    """Reads GPU temperature and VRAM in <0.1ms via nvml.dll with nvidia-smi fallback."""
    try:
        from jarvis.skills.nvml_dxgi_vram_arbiter import audit_vram_and_thermal_budget
        vram_info = audit_vram_and_thermal_budget(reclaim_if_pressured=True)
        if vram_info.get("status") == "ok":
            return {
                "name": "NVIDIA GPU",
                "temp_c": vram_info.get("gpu_temp_c", 0),
                "vram_used": int(vram_info.get("vram_used_mb", 0)),
                "vram_total": int(vram_info.get("vram_total_mb", 0)),
            }
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,temperature.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = [p.strip() for p in res.stdout.strip().split(",")]
            if len(parts) >= 4:
                return {
                    "name": parts[0],
                    "temp_c": int(parts[1]),
                    "vram_used": int(parts[2]),
                    "vram_total": int(parts[3]),
                }
    except Exception:
        pass
    return None


def _evaluate_system_health() -> list[str]:
    """Inspects all hardware telemetry and returns active warning messages."""
    global _consecutive_high_cpu
    warnings = []
    now = time.time()

    # 1. Battery Telemetry
    batt = get_raw_battery_info()
    if batt.get("has_battery") and not batt.get("charging"):
        pct = batt.get("percent", 100)
        if pct <= 10:
            if now - _last_alerts["battery"] > _ALERT_COOLDOWN_SECONDS:
                warnings.append(f"Battery is critically low at {pct}%. Please connect AC power immediately.")
                _last_alerts["battery"] = now
        elif pct <= 20:
            if now - _last_alerts["battery"] > _ALERT_COOLDOWN_SECONDS:
                warnings.append(f"Battery has dropped to {pct}%. Workstation is running on reserved power.")
                _last_alerts["battery"] = now
    elif batt.get("charging") or batt.get("percent", 0) > 30:
        # Reset alert latch
        _last_alerts["battery"] = 0.0

    # 2. CPU Utilization (requires consecutive sustained spike to trigger)
    cpu_pct = psutil.cpu_percent(interval=None)
    if cpu_pct >= 92.0:
        _consecutive_high_cpu += 1
        if _consecutive_high_cpu >= 2:
            if now - _last_alerts["cpu"] > _ALERT_COOLDOWN_SECONDS:
                warnings.append(f"CPU utilization is sustained at {cpu_pct:.1f}%.")
                _last_alerts["cpu"] = now
    else:
        _consecutive_high_cpu = 0
        _last_alerts["cpu"] = 0.0

    # 3. Memory / RAM Utilization
    mem = psutil.virtual_memory()
    if mem.percent >= 92.0:
        if now - _last_alerts["ram"] > _ALERT_COOLDOWN_SECONDS:
            warnings.append(f"RAM utilization is critical at {mem.percent:.1f}%.")
            _last_alerts["ram"] = now
    elif mem.percent < 85.0:
        _last_alerts["ram"] = 0.0

    # 4. GPU Thermals
    gpu = _query_gpu_telemetry()
    if gpu and gpu.get("temp_c", 0) >= 86:
        if now - _last_alerts["gpu"] > _ALERT_COOLDOWN_SECONDS:
            warnings.append(f"GPU temperature has reached {gpu['temp_c']}°C on {gpu['name']}.")
            _last_alerts["gpu"] = now
    elif gpu and gpu.get("temp_c", 0) < 75:
        _last_alerts["gpu"] = 0.0

    return warnings


def _watchdog_loop(interval_sec: int):
    """Background polling sentinel for hardware vitals."""
    logger.info("Hardware Watchdog Sentinel background thread initiated.")
    while not _stop_event.is_set():
        try:
            warnings = _evaluate_system_health()
            for w in warnings:
                logger.warning(f"Watchdog trigger: {w}")
                _speak_watchdog_alert(w)
        except Exception as e:
            logger.debug(f"Watchdog cycle error: {e}")

        # Sleep in small increments for rapid stop response
        for _ in range(max(1, interval_sec)):
            if _stop_event.is_set():
                break
            time.sleep(1.0)
    logger.info("Hardware Watchdog Sentinel background thread exited cleanly.")


@SkillRegistry.register(
    name="check_hardware_health",
    description="Performs an immediate diagnostic scan of workstation hardware health, thermals, battery levels, and resource headroom."
)
def check_hardware_health() -> str:
    """Performs an immediate health evaluation and returns clear diagnostic status."""
    warnings = _evaluate_system_health()
    cpu_pct = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    batt = get_raw_battery_info()
    gpu = _query_gpu_telemetry()

    status_lines = [
        f"• CPU Load: {cpu_pct:.1f}%",
        f"• Memory Usage: {mem.percent:.1f}% ({mem.used / (1024**3):.1f} GB / {mem.total / (1024**3):.1f} GB)",
    ]

    if batt.get("has_battery"):
        p_state = "Charging" if batt.get("charging") else "On Battery"
        status_lines.append(f"• Battery Level: {batt.get('percent')}% ({p_state})")

    if gpu:
        status_lines.append(f"• GPU: {gpu['name']} at {gpu['temp_c']}°C ({gpu['vram_used']} MB VRAM used)")

    health_summary = "ALL SYSTEMS NOMINAL" if not warnings else "ATTENTION REQUIRED"
    warning_block = "\nActive Warnings:\n" + "\n".join(f"⚠️ {w}" for w in warnings) if warnings else "\nNo thermal or power anomalies detected."

    return f"Workstation Hardware Diagnostic [{health_summary}], Sir:\n" + "\n".join(status_lines) + warning_block


@SkillRegistry.register(
    name="start_hardware_watchdog",
    description="Starts the proactive background hardware watchdog sentinel to monitor thermals, CPU load, and battery in real time."
)
def start_hardware_watchdog(interval_seconds: int = 60) -> str:
    """Starts the continuous background watchdog thread."""
    global _watchdog_thread, _watchdog_active
    with _watchdog_lock:
        if _watchdog_active and _watchdog_thread and _watchdog_thread.is_alive():
            return "Hardware watchdog sentinel is already active and monitoring workstation systems, Sir."

        _stop_event.clear()
        _watchdog_active = True
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop,
            args=(max(10, interval_seconds),),
            daemon=True,
            name="JarvisHardwareWatchdog"
        )
        _watchdog_thread.start()

    return f"Hardware watchdog sentinel engaged, Sir. Monitoring thermals, battery, and load every {interval_seconds} seconds."


@SkillRegistry.register(
    name="stop_hardware_watchdog",
    description="Stops the background hardware watchdog sentinel."
)
def stop_hardware_watchdog() -> str:
    """Stops the active background watchdog thread."""
    global _watchdog_active
    with _watchdog_lock:
        if not _watchdog_active:
            return "Hardware watchdog sentinel is not currently running, Sir."

        _stop_event.set()
        _watchdog_active = False

    return "Hardware watchdog sentinel has been disengaged, Sir."
