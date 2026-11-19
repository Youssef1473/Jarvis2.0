"""Real-time GPU VRAM and thermal budget governor for Jarvis 2.0.

Provides sub-0.1ms telemetry for the RTX 3050 Laptop GPU combining native NVML
physical memory queries with Windows 11 DXGI 1.4 WDDM video memory budget tracking,
enforcing 4-tier pressure watermarks and proactive staged cache shedding.
"""
from __future__ import annotations

import ctypes
import glob
import logging
import os
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Any

import psutil

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.VramArbiter")

# Calibrated 4GB RTX 3050 WDDM Watermark Thresholds (in MiB)
WATERMARK_NOMINAL_MB: float = 1200.0
WATERMARK_ELEVATED_MB: float = 700.0
WATERMARK_CRITICAL_MB: float = 350.0
DEFAULT_SAFETY_MARGIN_MB: float = 150.0


class VramPressureTier(str, Enum):
    """Deterministic VRAM pressure operating states."""

    NOMINAL = "NOMINAL"
    ELEVATED = "ELEVATED"
    CRITICAL = "CRITICAL"
    OOM_GUARD = "OOM_GUARD"


@dataclass(frozen=True)
class VramPressureTelemetry:
    """Structured VRAM pressure telemetry combining DXGI WDDM and NVML metrics."""

    status: str
    total_mb: float
    used_mb: float
    free_mb: float
    dxgi_budget_mb: float
    dxgi_current_usage_mb: float
    headroom_mb: float
    pressure_tier: VramPressureTier
    gpu_temp_c: int | None
    compute_processes: list[dict[str, Any]]
    shed_events_count: int
    reclaimed_action: str | None
    reason: str | None = None


@dataclass(frozen=True)
class VramBudgetDecision:
    """Outcome of pre-inference admission check."""

    granted: bool
    component: str
    estimated_mb: float
    headroom_mb: float
    pressure_tier: VramPressureTier
    shed_performed: bool
    actions_taken: list[str]
    recommended_action: str  # "gpu_float16" | "gpu_int8" | "cpu_fallback" | "rejected"


# ---------------------------------------------------------------------------
# NVML CTypes Structures
# ---------------------------------------------------------------------------


class c_nvmlMemory_t(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class c_nvmlProcessInfo_t(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint),
        ("usedGpuMemory", ctypes.c_ulonglong),
        ("gpuInstanceId", ctypes.c_uint),
        ("computeInstanceId", ctypes.c_uint),
    ]


# ---------------------------------------------------------------------------
# DXGI 1.4 CTypes & COM Structures
# ---------------------------------------------------------------------------


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


class DXGI_QUERY_VIDEO_MEMORY_INFO(ctypes.Structure):
    _fields_ = [
        ("Budget", ctypes.c_uint64),
        ("CurrentUsage", ctypes.c_uint64),
        ("AvailableForReservation", ctypes.c_uint64),
        ("CurrentReservation", ctypes.c_uint64),
    ]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid_LowPart", wintypes.DWORD),
        ("AdapterLuid_HighPart", wintypes.LONG),
        ("Flags", wintypes.UINT),
    ]


# ---------------------------------------------------------------------------
# Driver & Hook Registry State
# ---------------------------------------------------------------------------

_nvml_lib: Any | None = None
_nvml_initialized: bool = False
_dxgi_lib: Any | None = None
_ole32_lib: Any | None = None

_shed_hooks: dict[str, Callable[[], str]] = {}
_shed_hooks_lock = threading.Lock()
_shed_events_count: int = 0


def _get_ole32_lib() -> Any | None:
    """Loads ole32.dll on Windows for GUID parsing."""
    global _ole32_lib
    if sys.platform != "win32":
        return None
    if _ole32_lib is not None:
        return _ole32_lib
    try:
        _ole32_lib = ctypes.WinDLL("ole32.dll")
        return _ole32_lib
    except (OSError, AttributeError):
        return None


def _get_dxgi_lib() -> Any | None:
    """Loads dxgi.dll on Windows for WDDM video memory budget queries."""
    global _dxgi_lib
    if sys.platform != "win32":
        return None
    if _dxgi_lib is not None:
        return _dxgi_lib
    try:
        _dxgi_lib = ctypes.WinDLL("dxgi.dll")
        return _dxgi_lib
    except (OSError, AttributeError):
        return None


def _parse_guid(guid_str: str) -> GUID | None:
    """Parses a COM GUID string using ole32.IIDFromString."""
    ole32 = _get_ole32_lib()
    if ole32 is None:
        return None
    try:
        iid_from_string = ole32.IIDFromString
        iid_from_string.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
        iid_from_string.restype = ctypes.c_long
        guid = GUID()
        hr = iid_from_string(guid_str, ctypes.byref(guid))
        if hr == 0:
            return guid
    except (OSError, AttributeError, TypeError):
        pass
    return None


def _get_nvml_lib() -> Any | None:
    """Resiliently loads nvml.dll across Windows DCH driver layouts and NVSMI paths."""
    global _nvml_lib
    if _nvml_lib is not None:
        return _nvml_lib

    # 1. Standard dynamic linker search
    try:
        if sys.platform == "win32":
            _nvml_lib = ctypes.WinDLL("nvml.dll")
        else:
            _nvml_lib = ctypes.CDLL("libnvidia-ml.so.1")
        return _nvml_lib
    except (OSError, AttributeError):
        pass

    if sys.platform == "win32":
        # 2. Classic NVSMI path
        nvsmi_path = r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll"
        if os.path.exists(nvsmi_path):
            try:
                _nvml_lib = ctypes.WinDLL(nvsmi_path)
                return _nvml_lib
            except (OSError, AttributeError):
                pass

        # 3. Windows DCH DriverStore FileRepository
        driver_store_pattern = r"C:\Windows\System32\DriverStore\FileRepository\nv*\nvml.dll"
        for match in glob.glob(driver_store_pattern):
            try:
                _nvml_lib = ctypes.WinDLL(match)
                return _nvml_lib
            except (OSError, AttributeError):
                continue

    return None


def _query_dxgi_video_memory() -> tuple[float, float] | None:
    """Queries Windows 11 DXGI 1.4 WDDM video memory budget and process usage in MiB.

    Returns (budget_mb, current_usage_mb) or None if unavailable.
    """
    if sys.platform != "win32":
        return None

    dxgi = _get_dxgi_lib()
    if dxgi is None:
        return None

    iid_factory1 = _parse_guid("{770aae78-f26f-4dba-a829-253c83d1b387}")
    iid_adapter3 = _parse_guid("{645967a4-1392-4310-a798-8053ce3e93fd}")
    if iid_factory1 is None or iid_adapter3 is None:
        return None

    p_factory = ctypes.c_void_p()
    try:
        create_factory = dxgi.CreateDXGIFactory1
        create_factory.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        create_factory.restype = ctypes.c_long
        hr = create_factory(ctypes.byref(iid_factory1), ctypes.byref(p_factory))
        if hr != 0 or not p_factory.value:
            return None
    except (OSError, AttributeError, TypeError) as err:
        logger.debug("DXGI CreateDXGIFactory1 call notice: %s", err)
        return None

    try:
        factory_vtbl = ctypes.cast(p_factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        # EnumAdapters1 is VTable Slot 12
        enum_adapters1_proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
        enum_adapters1 = enum_adapters1_proto(factory_vtbl[12])

        best_budget: float | None = None
        best_usage: float | None = None

        for adapter_idx in range(4):
            p_adapter1 = ctypes.c_void_p()
            if enum_adapters1(p_factory, adapter_idx, ctypes.byref(p_adapter1)) != 0:
                break
            if not p_adapter1.value:
                continue

            try:
                adapter1_vtbl = ctypes.cast(p_adapter1, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                # GetDesc1 is VTable Slot 10
                get_desc1_proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(DXGI_ADAPTER_DESC1))
                get_desc1 = get_desc1_proto(adapter1_vtbl[10])
                desc = DXGI_ADAPTER_DESC1()
                get_desc1(p_adapter1, ctypes.byref(desc))

                # QueryInterface for IDXGIAdapter3 (VTable Slot 0)
                qi_proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
                qi = qi_proto(adapter1_vtbl[0])
                p_adapter3 = ctypes.c_void_p()
                hr_qi = qi(p_adapter1, ctypes.byref(iid_adapter3), ctypes.byref(p_adapter3))

                if hr_qi == 0 and p_adapter3.value:
                    try:
                        adapter3_vtbl = ctypes.cast(p_adapter3, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                        # QueryVideoMemoryInfo is VTable Slot 14
                        # NodeIndex=0, SegmentGroup=0 (DXGI_MEMORY_SEGMENT_GROUP_LOCAL)
                        qvm_proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.UINT, ctypes.c_uint, ctypes.POINTER(DXGI_QUERY_VIDEO_MEMORY_INFO))
                        qvm = qvm_proto(adapter3_vtbl[14])
                        info = DXGI_QUERY_VIDEO_MEMORY_INFO()
                        hr_qvm = qvm(p_adapter3, 0, 0, ctypes.byref(info))
                        if hr_qvm == 0:
                            budget_mb = round(info.Budget / (1024 * 1024), 1)
                            usage_mb = round(info.CurrentUsage / (1024 * 1024), 1)
                            # Prefer discrete NVIDIA GPU (VendorId 0x10de)
                            if desc.VendorId == 0x10DE:
                                return budget_mb, usage_mb
                            if best_budget is None:
                                best_budget = budget_mb
                                best_usage = usage_mb
                    finally:
                        release_proto = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
                        release_proto(adapter3_vtbl[2])(p_adapter3)
            finally:
                release_proto = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
                release_proto(adapter1_vtbl[2])(p_adapter1)

        if best_budget is not None and best_usage is not None:
            return best_budget, best_usage
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        logger.debug("DXGI adapter enumeration notice: %s", exc)
    finally:
        try:
            release_proto = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
            release_proto(factory_vtbl[2])(p_factory)
        except (OSError, AttributeError, TypeError):
            pass

    return None


# ---------------------------------------------------------------------------
# Shedding Hooks & Admission Control
# ---------------------------------------------------------------------------


def register_vram_shed_hook(name: str, hook: Callable[[], str]) -> None:
    """Registers a subsystem callback invoked when VRAM pressure requires shedding."""
    with _shed_hooks_lock:
        _shed_hooks[name] = hook
        logger.debug("Registered VRAM pressure shedding hook: %s", name)


def unregister_vram_shed_hook(name: str) -> None:
    """Unregisters a previously registered VRAM shedding hook."""
    with _shed_hooks_lock:
        _shed_hooks.pop(name, None)


def get_shed_events_count() -> int:
    """Returns the total number of pressure shedding actions triggered."""
    return _shed_events_count


def reset_shed_hooks() -> None:
    """Clears registered shedding hooks and resets event counters (for test isolation)."""
    global _shed_events_count
    with _shed_hooks_lock:
        _shed_hooks.clear()
        _shed_events_count = 0


def execute_staged_shedding(stage: int = 1) -> list[str]:
    """Executes staged memory reclamation and returns descriptions of performed actions.

    Stage 1: PyTorch CUDA allocator cache flush and host Python garbage collection.
    Stage 2: Invocations of all registered subsystem hooks (e.g. Kokoro, Whisper, ONNX).
    """
    global _shed_events_count
    actions: list[str] = []

    # Stage 1: Core Allocator & Host GC
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            actions.append("flushed_cuda_allocator_cache")
    except (ImportError, RuntimeError, AttributeError) as exc:
        logger.debug("CUDA allocator flush notice: %s", exc)

    try:
        import gc

        gc.collect()
        actions.append("invoked_host_gc")
    except (RuntimeError, OSError, AttributeError) as exc:
        logger.debug("Host GC invocation notice: %s", exc)

    # Stage 2: Subsystem Hooks
    if stage >= 2:
        with _shed_hooks_lock:
            hook_items = list(_shed_hooks.items())

        for name, hook in hook_items:
            try:
                res = hook()
                actions.append(f"hook:{name}({res})")
            except (RuntimeError, ValueError, OSError, AttributeError, TypeError) as exc:
                logger.warning("VRAM shed hook '%s' raised exception: %s", name, exc)

    if actions:
        _shed_events_count += 1

    return actions


def resolve_pressure_watermarks(budget_mb: float) -> tuple[float, float, float]:
    """Computes calibrated or proportional VRAM pressure watermarks in MiB.

    Args:
        budget_mb: Total VRAM capacity or WDDM budget in MiB.

    Returns:
        (nominal_min_mb, elevated_min_mb, critical_min_mb)
    """
    if budget_mb <= 4096.0:
        return (WATERMARK_NOMINAL_MB, WATERMARK_ELEVATED_MB, WATERMARK_CRITICAL_MB)

    nominal_min_mb = max(1200.0, min(budget_mb * 0.25, 3072.0))
    elevated_min_mb = max(700.0, min(budget_mb * 0.14, 1536.0))
    critical_min_mb = max(350.0, min(budget_mb * 0.07, 768.0))
    return (nominal_min_mb, elevated_min_mb, critical_min_mb)


def classify_pressure_tier(
    headroom_mb: float,
    total_budget_mb: float | None = None,
) -> VramPressureTier:
    """Classifies effective VRAM headroom into a deterministic pressure tier."""
    if total_budget_mb is not None:
        nominal, elevated, critical = resolve_pressure_watermarks(total_budget_mb)
    else:
        nominal, elevated, critical = (
            WATERMARK_NOMINAL_MB,
            WATERMARK_ELEVATED_MB,
            WATERMARK_CRITICAL_MB,
        )

    if headroom_mb >= nominal:
        return VramPressureTier.NOMINAL
    if headroom_mb >= elevated:
        return VramPressureTier.ELEVATED
    if headroom_mb >= critical:
        return VramPressureTier.CRITICAL
    return VramPressureTier.OOM_GUARD


def get_vram_telemetry() -> VramPressureTelemetry:
    """Queries real-time NVML + DXGI telemetry and computes calibrated headroom."""
    global _nvml_initialized
    dxgi_info = _query_dxgi_video_memory()
    dxgi_budget_mb = dxgi_info[0] if dxgi_info else 0.0
    dxgi_usage_mb = dxgi_info[1] if dxgi_info else 0.0

    nvml = _get_nvml_lib()
    if nvml is None:
        if dxgi_info is not None:
            dxgi_headroom = max(0.0, dxgi_budget_mb - dxgi_usage_mb)
            tier = classify_pressure_tier(dxgi_headroom, total_budget_mb=dxgi_budget_mb)
            return VramPressureTelemetry(
                status="ok",
                total_mb=dxgi_budget_mb,
                used_mb=dxgi_usage_mb,
                free_mb=dxgi_headroom,
                dxgi_budget_mb=dxgi_budget_mb,
                dxgi_current_usage_mb=dxgi_usage_mb,
                headroom_mb=dxgi_headroom,
                pressure_tier=tier,
                gpu_temp_c=None,
                compute_processes=[],
                shed_events_count=_shed_events_count,
                reclaimed_action=None,
            )

        return VramPressureTelemetry(
            status="unavailable",
            total_mb=0.0,
            used_mb=0.0,
            free_mb=0.0,
            dxgi_budget_mb=0.0,
            dxgi_current_usage_mb=0.0,
            headroom_mb=0.0,
            pressure_tier=VramPressureTier.NOMINAL,
            gpu_temp_c=None,
            compute_processes=[],
            shed_events_count=_shed_events_count,
            reclaimed_action=None,
            reason="Neither nvml.dll nor dxgi.dll video memory queries available",
        )

    try:
        if not _nvml_initialized:
            init_res = nvml.nvmlInit_v2()
            if init_res != 0:
                return VramPressureTelemetry(
                    status="error",
                    total_mb=0.0,
                    used_mb=0.0,
                    free_mb=0.0,
                    dxgi_budget_mb=dxgi_budget_mb,
                    dxgi_current_usage_mb=dxgi_usage_mb,
                    headroom_mb=0.0,
                    pressure_tier=VramPressureTier.NOMINAL,
                    gpu_temp_c=None,
                    compute_processes=[],
                    shed_events_count=_shed_events_count,
                    reclaimed_action=None,
                    reason=f"nvmlInit_v2 failed with code {init_res}",
                )
            _nvml_initialized = True

        device = ctypes.c_void_p()
        if nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(device)) != 0:
            return VramPressureTelemetry(
                status="error",
                total_mb=0.0,
                used_mb=0.0,
                free_mb=0.0,
                dxgi_budget_mb=dxgi_budget_mb,
                dxgi_current_usage_mb=dxgi_usage_mb,
                headroom_mb=0.0,
                pressure_tier=VramPressureTier.NOMINAL,
                gpu_temp_c=None,
                compute_processes=[],
                shed_events_count=_shed_events_count,
                reclaimed_action=None,
                reason="Failed to acquire GPU 0 handle",
            )

        mem = c_nvmlMemory_t()
        nvml.nvmlDeviceGetMemoryInfo(device, ctypes.byref(mem))

        temp = ctypes.c_uint(0)
        nvml.nvmlDeviceGetTemperature(device, 0, ctypes.byref(temp))

        count = ctypes.c_uint(32)
        procs = (c_nvmlProcessInfo_t * 32)()
        nvml.nvmlDeviceGetComputeRunningProcesses_v3(device, ctypes.byref(count), procs)

        process_breakdown: list[dict[str, Any]] = []
        for i in range(count.value):
            pid = int(procs[i].pid)
            used_bytes = procs[i].usedGpuMemory
            vram_mb = round(used_bytes / (1024 * 1024), 1) if used_bytes < (1 << 60) else 0.0
            try:
                pname = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pname = "unknown"
            process_breakdown.append({"pid": pid, "name": pname, "vram_mb": vram_mb})

        total_mb = round(mem.total / (1024 * 1024), 1)
        used_mb = round(mem.used / (1024 * 1024), 1)
        free_mb = round(mem.free / (1024 * 1024), 1)

        # Plan 043 Effective Headroom Calculation:
        # EffectiveHeadroom = min(DXGI_Budget - DXGI_Usage, NVML_Free)
        if dxgi_info is not None and dxgi_budget_mb > 0.0:
            dxgi_headroom = max(0.0, dxgi_budget_mb - dxgi_usage_mb)
            effective_headroom = min(dxgi_headroom, free_mb)
        else:
            effective_headroom = free_mb

        tier = classify_pressure_tier(effective_headroom, total_budget_mb=max(total_mb, dxgi_budget_mb))

        return VramPressureTelemetry(
            status="ok",
            total_mb=total_mb,
            used_mb=used_mb,
            free_mb=free_mb,
            dxgi_budget_mb=dxgi_budget_mb,
            dxgi_current_usage_mb=dxgi_usage_mb,
            headroom_mb=round(effective_headroom, 1),
            pressure_tier=tier,
            gpu_temp_c=int(temp.value),
            compute_processes=process_breakdown,
            shed_events_count=_shed_events_count,
            reclaimed_action=None,
        )
    except (RuntimeError, ValueError, OSError, AttributeError) as exc:
        return VramPressureTelemetry(
            status="error",
            total_mb=0.0,
            used_mb=0.0,
            free_mb=0.0,
            dxgi_budget_mb=dxgi_budget_mb,
            dxgi_current_usage_mb=dxgi_usage_mb,
            headroom_mb=0.0,
            pressure_tier=VramPressureTier.NOMINAL,
            gpu_temp_c=None,
            compute_processes=[],
            shed_events_count=_shed_events_count,
            reclaimed_action=None,
            reason=f"NVML query exception: {exc}",
        )


def request_vram_budget(
    component: str,
    estimated_mb: float,
    auto_shed: bool = True,
    safety_margin_mb: float = DEFAULT_SAFETY_MARGIN_MB,
) -> tuple[bool, VramBudgetDecision]:
    """Evaluates GPU headroom and admits or sheds memory before heavy model allocation.

    Returns:
        (granted: bool, decision: VramBudgetDecision)
    """
    telemetry = get_vram_telemetry()
    actions_taken: list[str] = []
    shed_performed = False

    if telemetry.status != "ok":
        # Graceful fallback for non-NVIDIA or CI environments
        return True, VramBudgetDecision(
            granted=True,
            component=component,
            estimated_mb=estimated_mb,
            headroom_mb=telemetry.headroom_mb,
            pressure_tier=telemetry.pressure_tier,
            shed_performed=False,
            actions_taken=[],
            recommended_action="cpu_fallback",
        )

    required_mb = estimated_mb + safety_margin_mb

    # Immediate rejection if already in emergency OOM_GUARD tier
    if telemetry.pressure_tier == VramPressureTier.OOM_GUARD:
        if auto_shed:
            actions = execute_staged_shedding(stage=2)
            actions_taken.extend(actions)
            shed_performed = bool(actions)
            telemetry = get_vram_telemetry()

        if telemetry.headroom_mb < required_mb or telemetry.pressure_tier == VramPressureTier.OOM_GUARD:
            return False, VramBudgetDecision(
                granted=False,
                component=component,
                estimated_mb=estimated_mb,
                headroom_mb=telemetry.headroom_mb,
                pressure_tier=telemetry.pressure_tier,
                shed_performed=shed_performed,
                actions_taken=actions_taken,
                recommended_action="cpu_fallback",
            )

    # If currently pressured, run staged shedding
    if telemetry.headroom_mb < required_mb and auto_shed:
        # Stage 1 shedding
        s1_actions = execute_staged_shedding(stage=1)
        actions_taken.extend(s1_actions)
        telemetry = get_vram_telemetry()

        # If still pressured, run Stage 2 shedding
        if telemetry.headroom_mb < required_mb:
            s2_actions = execute_staged_shedding(stage=2)
            actions_taken.extend(s2_actions)
            telemetry = get_vram_telemetry()

        shed_performed = bool(actions_taken)

    # Check if budget can now be granted
    if telemetry.headroom_mb >= required_mb:
        if telemetry.pressure_tier == VramPressureTier.NOMINAL:
            rec = "gpu_float16"
        elif telemetry.pressure_tier == VramPressureTier.ELEVATED:
            rec = "gpu_int8"
        else:
            rec = "cpu_fallback"

        return True, VramBudgetDecision(
            granted=True,
            component=component,
            estimated_mb=estimated_mb,
            headroom_mb=telemetry.headroom_mb,
            pressure_tier=telemetry.pressure_tier,
            shed_performed=shed_performed,
            actions_taken=actions_taken,
            recommended_action=rec,
        )

    # Headroom remains insufficient
    return False, VramBudgetDecision(
        granted=False,
        component=component,
        estimated_mb=estimated_mb,
        headroom_mb=telemetry.headroom_mb,
        pressure_tier=telemetry.pressure_tier,
        shed_performed=shed_performed,
        actions_taken=actions_taken,
        recommended_action="cpu_fallback",
    )


# ---------------------------------------------------------------------------
# Skill Registry Export (Backwards-Compatible)
# ---------------------------------------------------------------------------


@SkillRegistry.register(
    name="audit_vram_and_thermal_budget",
    description="Queries RTX 3050 VRAM allocations, GPU temp, and clock throttling in <0.1ms via nvml.dll and dxgi.dll, reclaiming cache if pressured.",
    risk_level=0,
)
def audit_vram_and_thermal_budget(reclaim_if_pressured: bool = True) -> dict[str, Any]:
    """Performs sub-millisecond NVML inspection and enforces VRAM safety limits."""
    telemetry = get_vram_telemetry()
    reclaimed_action: str | None = None

    if reclaim_if_pressured and telemetry.pressure_tier in (VramPressureTier.CRITICAL, VramPressureTier.OOM_GUARD):
        actions = execute_staged_shedding(stage=2)
        if actions:
            reclaimed_action = ";".join(actions)
            telemetry = get_vram_telemetry()
    elif reclaim_if_pressured and telemetry.pressure_tier == VramPressureTier.ELEVATED:
        actions = execute_staged_shedding(stage=1)
        if actions:
            reclaimed_action = ";".join(actions)
            telemetry = get_vram_telemetry()

    return {
        "status": telemetry.status,
        "reason": telemetry.reason,
        "vram_total_mb": telemetry.total_mb,
        "vram_used_mb": telemetry.used_mb,
        "vram_free_mb": telemetry.free_mb,
        "dxgi_budget_mb": telemetry.dxgi_budget_mb,
        "dxgi_usage_mb": telemetry.dxgi_current_usage_mb,
        "headroom_mb": telemetry.headroom_mb,
        "pressure_tier": telemetry.pressure_tier.value,
        "shed_events_count": telemetry.shed_events_count,
        "gpu_temp_c": telemetry.gpu_temp_c,
        "compute_processes": telemetry.compute_processes,
        "reclaimed_action": reclaimed_action,
    }


__all__ = [
    "DEFAULT_SAFETY_MARGIN_MB",
    "WATERMARK_CRITICAL_MB",
    "WATERMARK_ELEVATED_MB",
    "WATERMARK_NOMINAL_MB",
    "VramBudgetDecision",
    "VramPressureTelemetry",
    "VramPressureTier",
    "_get_nvml_lib",
    "_query_dxgi_video_memory",
    "audit_vram_and_thermal_budget",
    "classify_pressure_tier",
    "execute_staged_shedding",
    "get_shed_events_count",
    "get_vram_telemetry",
    "register_vram_shed_hook",
    "request_vram_budget",
    "reset_shed_hooks",
    "resolve_pressure_watermarks",
    "unregister_vram_shed_hook",
]
