"""Lifecycle coordinator and Win32 process containment for Jarvis 2.0 orchestrator."""
from __future__ import annotations

import gc
import logging
import sys
from typing import Any

logger = logging.getLogger("Jarvis.Lifecycle")


def trim_process_memory(aggressive: bool = False) -> None:
    """Runs garbage collection and conditionally trims the Windows working set.

    Guarded with strict 64-bit ctypes ABI declarations for kernel32.GetCurrentProcess
    and psapi.EmptyWorkingSet to prevent 0xc0000008 (STATUS_INVALID_HANDLE) on x64.
    """
    gc.collect(0)
    if not aggressive:
        return
    gc.collect()

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            if hasattr(ctypes, "windll"):
                kernel32 = ctypes.windll.kernel32
                kernel32.GetCurrentProcess.argtypes = []
                kernel32.GetCurrentProcess.restype = wintypes.HANDLE

                psapi = ctypes.windll.psapi
                psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
                psapi.EmptyWorkingSet.restype = wintypes.BOOL

                h_proc = kernel32.GetCurrentProcess()
                if h_proc:
                    psapi.EmptyWorkingSet(h_proc)
        except (AttributeError, OSError) as exc:
            logger.debug("Win32 working set trim notice: %s", exc)


class OrchestratorShutdownCoordinator:
    """Coordinates graceful, phased termination of Jarvis workers, tools, and child services."""

    @staticmethod
    def shutdown(orchestrator: Any) -> None:
        """Executes a phased shutdown sequence across sentinels, subsystems, child processes, and hardware caches."""
        logger.info("Initiating orchestrated Jarvis shutdown...")

        # Phase 1: Stop global skill sentinels & background threads
        OrchestratorShutdownCoordinator._stop_skill_sentinels()

        # Phase 2: Stop core subsystems
        OrchestratorShutdownCoordinator._stop_core_subsystems(orchestrator)

        # Phase 3: Stop managed services and sweep child process tree
        OrchestratorShutdownCoordinator._stop_managed_services(orchestrator)

        # Phase 4: Flush GPU VRAM and run garbage collection
        OrchestratorShutdownCoordinator._cleanup_hardware_resources()

        # Phase 5: Trim working set to prevent memory pressure
        trim_process_memory(aggressive=True)

        logger.info("Jarvis shutdown complete: All tools, child processes, and hardware threads terminated.")

        # Phase 6: Flush streams and shutdown logging handlers
        OrchestratorShutdownCoordinator._flush_streams_and_logging()

    @staticmethod
    def _stop_skill_sentinels() -> None:
        try:
            from jarvis.skills.system_watchdog import stop_hardware_watchdog
            stop_hardware_watchdog()
        except (ImportError, AttributeError, RuntimeError) as exc:
            logger.debug("System watchdog shutdown notice: %s", exc)

        try:
            from jarvis.skills.timers import cancel_timer
            cancel_timer("all")
        except (ImportError, AttributeError, RuntimeError) as exc:
            logger.debug("Timer cancellation notice: %s", exc)

        try:
            from jarvis.skills.photo_manager import shutdown_photo_manager
            shutdown_photo_manager()
        except (ImportError, AttributeError, RuntimeError) as exc:
            logger.debug("Photo manager shutdown notice: %s", exc)

        try:
            from jarvis.skills.browser_cdp import clear_all_tab_sessions
            clear_all_tab_sessions()
        except (OSError, RuntimeError, ImportError, AttributeError) as exc:
            logger.debug("CDP tab session cleanup notice during shutdown: %s", exc)

    @staticmethod
    def _stop_core_subsystems(orchestrator: Any) -> None:
        cleanup_steps: list[tuple[str, Any]] = [
            ("Web HUD", getattr(orchestrator, "ui_service", None)),
            ("heartbeat", getattr(orchestrator, "heartbeat", None)),
            ("hotkey listener", getattr(orchestrator, "hotkey_listener", None)),
            ("wake detector", getattr(orchestrator, "wake_detector", None)),
            ("text-to-speech", getattr(orchestrator, "tts", None)),
            ("speech-to-text", getattr(orchestrator, "stt", None)),
            ("desktop HUD", getattr(orchestrator, "hud", None)),
            ("brain", getattr(orchestrator, "brain", None)),
            ("memory", getattr(orchestrator, "memory", None)),
            ("background tasks", getattr(orchestrator, "task_manager", None)),
        ]
        for name, component in cleanup_steps:
            if component is None:
                continue
            try:
                if hasattr(component, "close"):
                    component.close()
                elif hasattr(component, "stop"):
                    component.stop()
                elif hasattr(component, "stop_all"):
                    component.stop_all()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not stop Jarvis %s cleanly: %s", name, exc)

    @staticmethod
    def _stop_managed_services(orchestrator: Any) -> None:
        service_mgr = getattr(orchestrator, "service_manager", None)
        if service_mgr and hasattr(service_mgr, "stop_all"):
            try:
                service_mgr.stop_all()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not stop service manager cleanly: %s", exc)

    @staticmethod
    def _cleanup_hardware_resources() -> None:
        try:
            gc.collect()
            if "torch" in sys.modules:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Hardware cache cleanup notice: %s", exc)

    @staticmethod
    def _flush_streams_and_logging() -> None:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            logging.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Logging shutdown notice: %s", exc)
