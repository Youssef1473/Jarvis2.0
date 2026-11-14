"""Workstation vitals, hardware sensor polling, and latency telemetry for Jarvis 2.0 HUD."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, ClassVar

import psutil

from jarvis.skills.os_control import get_raw_battery_info

logger = logging.getLogger("Jarvis.UI.Metrics")


class WorkstationMetricsCollector:
    """Collects system vitals via psutil and native NVML bindings strictly off the event loop."""

    _nvml_initialized: bool = False
    _device_handle: Any = None
    _nvml_module: Any = None
    _nvml_lock: threading.Lock = threading.Lock()

    _DEFAULT_GPU_METRICS: ClassVar[dict[str, Any]] = {
        "gpu_name": "NVIDIA GeForce RTX 3050 Laptop GPU",
        "gpu_load": 0,
        "vram_used_mb": 0,
        "vram_total_mb": 4096,
        "gpu_temp_c": 0,
    }

    @classmethod
    def _init_nvml(cls) -> bool:
        """Initializes NVML and acquires device handle 0 inside a thread-safe singleton gate."""
        with cls._nvml_lock:
            if cls._nvml_initialized and cls._device_handle is not None:
                return True

            try:
                import pynvml
                cls._nvml_module = pynvml
                cls._nvml_module.nvmlInit()
                cls._device_handle = cls._nvml_module.nvmlDeviceGetHandleByIndex(0)
                cls._nvml_initialized = True
                return True
            except (ImportError, OSError, RuntimeError, AttributeError) as exc:
                cls._nvml_initialized = False
                cls._device_handle = None
                cls._nvml_module = None
                logger.debug("Native NVML initialization bypassed (driver or module unavailable): %s", exc)
                return False
            except Exception as exc:  # noqa: BLE001 - Driver ABI boundary safety
                cls._nvml_initialized = False
                cls._device_handle = None
                cls._nvml_module = None
                logger.debug("Unexpected NVML driver initialization failure: %s", exc)
                return False

    @classmethod
    def _get_gpu_metrics_native(cls) -> dict[str, Any]:
        """Polls GPU hardware states natively via pynvml bindings without subprocess forks."""
        if not cls._init_nvml():
            # Fallback to secondary watchdog query if available
            try:
                from jarvis.skills.system_watchdog import _query_gpu_telemetry
                cached = _query_gpu_telemetry()
                if cached:
                    return {
                        "gpu_name": cached.get("name", cls._DEFAULT_GPU_METRICS["gpu_name"]),
                        "gpu_load": cached.get("load_pct", 0),
                        "vram_used_mb": cached.get("vram_used", 0),
                        "vram_total_mb": cached.get("vram_total", 4096),
                        "gpu_temp_c": cached.get("temp_c", 0),
                    }
            except (AttributeError, KeyError, TypeError, ImportError, OSError) as err:
                logger.debug("GPU cache inspection notice: %s", err)
            return dict(cls._DEFAULT_GPU_METRICS)

        try:
            handle = cls._device_handle
            nvml = cls._nvml_module

            raw_name = nvml.nvmlDeviceGetName(handle)
            gpu_name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)

            util_rates = nvml.nvmlDeviceGetUtilizationRates(handle)
            gpu_load = int(util_rates.gpu)

            mem_info = nvml.nvmlDeviceGetMemoryInfo(handle)
            vram_used_mb = int(mem_info.used / (1024 * 1024))
            vram_total_mb = int(mem_info.total / (1024 * 1024))

            gpu_temp_c = int(nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))

            return {
                "gpu_name": gpu_name,
                "gpu_load": gpu_load,
                "vram_used_mb": vram_used_mb,
                "vram_total_mb": vram_total_mb,
                "gpu_temp_c": gpu_temp_c,
            }
        except Exception as exc:  # noqa: BLE001 - Driver read failure resets device handle
            logger.debug("NVML hardware poll failed; resetting handle: %s", exc)
            with cls._nvml_lock:
                cls._nvml_initialized = False
                cls._device_handle = None
            return dict(cls._DEFAULT_GPU_METRICS)

    @classmethod
    def shutdown_nvml(cls):
        """Cleanly finalizes the NVML driver session during host termination."""
        with cls._nvml_lock:
            if cls._nvml_initialized and cls._nvml_module is not None:
                try:
                    cls._nvml_module.nvmlShutdown()
                except Exception as err:  # noqa: BLE001 - Driver-level cleanup boundary must not block host teardown
                    logger.debug("NVML driver shutdown notice: %s", err)
                finally:
                    cls._nvml_initialized = False
                    cls._device_handle = None
                    cls._nvml_module = None

    @staticmethod
    def sample_metrics_sync(orchestrator: Any = None) -> dict[str, Any]:
        """Synchronous vitals collection combining psutil host memory/CPU with native NVML GPU telemetry."""
        bat = get_raw_battery_info()
        try:
            cpu_val = round(psutil.cpu_percent(interval=None), 1)
        except (TypeError, ValueError, OSError):
            cpu_val = 0.0

        vmem = psutil.virtual_memory()
        ram_pct = round(vmem.percent, 1)
        ram_used_gb = round(vmem.used / (1024 ** 3), 1)
        ram_total_gb = round(vmem.total / (1024 ** 3), 1)
        ram_used_mb = int(vmem.used / (1024 * 1024))
        ram_total_mb = int(vmem.total / (1024 * 1024))

        gpu_data = WorkstationMetricsCollector._get_gpu_metrics_native()

        state = "idle"
        active_model = "qwen/qwen3.8-27b"
        tier = "TRI-TIER HYBRID"
        provider = "hybrid"
        is_muted = False
        specialist_status = {"gemini": "ACTIVE", "groq": "ACTIVE", "grok": "METERED / OFF", "claude": "METERED / OFF"}
        latencies = {"ollama": 0.0, "groq": 0.0, "gemini": 0.0}
        memory_label = "TIER 2 (ACTIVE)"

        if orchestrator is not None:
            if hasattr(orchestrator, "is_muted") and callable(orchestrator.is_muted):
                try:
                    is_muted = bool(orchestrator.is_muted())
                except (AttributeError, TypeError, RuntimeError, OSError) as err:
                    logger.debug("Orchestrator is_muted probe notice: %s", err)
                    is_muted = False

            if hasattr(orchestrator, "state"):
                state = str(getattr(orchestrator, "state", "idle"))

            if hasattr(orchestrator, "brain"):
                b = orchestrator.brain
                p = getattr(b, "provider", "hybrid")
                provider = p
                tier = "TRI-TIER HYBRID" if p in ("hybrid", "auto") else ("LOCAL" if p == "ollama" else "CLOUD")
                active_model = str(getattr(b, "openai_model", getattr(b, "provider", active_model)))
                status_provider = getattr(b, "specialist_status", None)
                if callable(status_provider):
                    try:
                        specialist_status = status_provider()
                    except (AttributeError, TypeError, RuntimeError) as err:
                        logger.debug("Specialist status probe notice: %s", err)
                lat_getter = getattr(b, "get_latencies", None)
                if callable(lat_getter):
                    try:
                        latencies = lat_getter()
                    except (AttributeError, TypeError, RuntimeError) as err:
                        logger.debug("Provider latencies probe notice: %s", err)
                if hasattr(b, "memory"):
                    try:
                        active_ws = getattr(orchestrator, "active_workspace_id", "ws_default")
                        if hasattr(b.memory, "get_all_memory_nodes"):
                            m_nodes = b.memory.get_all_memory_nodes(limit=1, workspace_id=active_ws)
                            memory_label = f"{m_nodes.get('total', 0)} NODES STORED"
                        else:
                            facts = b.memory.get_all_facts(workspace_id=active_ws)
                            memory_label = f"{len(facts)} FACTS STORED"
                    except (AttributeError, TypeError, RuntimeError, OSError, sqlite3.Error) as err:
                        logger.debug("Memory nodes probe notice: %s", err)

        timer_count = 0
        try:
            from jarvis.skills.timers import _active_timers, _timer_lock
            with _timer_lock:
                timer_count = len(_active_timers)
        except (ImportError, AttributeError, RuntimeError) as err:
            logger.debug("Active timers registry probe notice: %s", err)

        latency_percentiles = {}
        p99_history = []
        profiler = None
        if orchestrator is not None and hasattr(orchestrator, "brain"):
            b = orchestrator.brain
            if hasattr(b, "latency_profiler") and b.latency_profiler:
                profiler = b.latency_profiler
        if profiler is None and orchestrator is not None and hasattr(orchestrator, "latency_profiler") and orchestrator.latency_profiler:
            profiler = orchestrator.latency_profiler
        if profiler is None:
            try:
                from jarvis.skills.diagnostics import get_global_latency_profiler
                profiler = get_global_latency_profiler()
            except (ImportError, AttributeError):
                profiler = None

        if profiler is not None:
            try:
                latency_percentiles = profiler.get_percentiles()
            except (AttributeError, TypeError, RuntimeError) as err:
                logger.debug("Latency percentiles notice: %s", err)
                latency_percentiles = {}
            try:
                if hasattr(profiler, "get_p99_history"):
                    p99_history = profiler.get_p99_history()
            except (AttributeError, TypeError, RuntimeError) as err:
                logger.debug("P99 history notice: %s", err)
                p99_history = []

        return {
            # Single-character keys required by Council Telemetry Directive
            "c": cpu_val,
            "rp": ram_pct,
            "ru": ram_used_mb,
            "rt": ram_total_mb,
            "gp": gpu_data["gpu_load"],
            "gl": gpu_data["gpu_load"],
            "vu": gpu_data["vram_used_mb"],
            "vt": gpu_data["vram_total_mb"],
            "gt": gpu_data["gpu_temp_c"],
            "st": state,
            "md": active_model,
            "m": 1 if is_muted else 0,
            "ts": int(time.time() * 1000),
            # Backwards-compatible fields for existing test assertions & components
            "cpu": cpu_val,
            "ram_pct": ram_pct,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "gpu": gpu_data["gpu_name"],
            "gpu_name": gpu_data["gpu_name"],
            "gpu_load": gpu_data["gpu_load"],
            "gpu_temp": gpu_data["gpu_temp_c"],
            "vram_used": f"{gpu_data['vram_used_mb']} MB",
            "vram_used_mb": gpu_data["vram_used_mb"],
            "vram_total_mb": gpu_data["vram_total_mb"],
            "model": active_model,
            "tier": tier,
            "provider": provider,
            "state": state,
            "specialists": specialist_status,
            "active_timers": timer_count,
            "latencies": latencies,
            "memory_label": memory_label,
            "is_muted": is_muted,
            "battery": bat.get("percent", 100),
            "charging": bat.get("charging", False),
            "has_battery": bat.get("has_battery", False),
            "timestamp": time.strftime("%H:%M:%S"),
            "latency_percentiles": latency_percentiles,
            "lp": latency_percentiles,
            "p99_history": p99_history,
            "p99_h": p99_history,
        }
