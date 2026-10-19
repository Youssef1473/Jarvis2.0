"""Main entry point for Jarvis 2.0."""
import os
import sys
import logging
import atexit
import signal
import warnings
from pathlib import Path

# Suppress third-party deprecation warnings (e.g. NumPy 2.5 shape assignment in sounddevice)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*Setting the shape on a NumPy array has been deprecated.*")

# Laptop Thermal Profile: prevent OpenMP, BLAS & MKL threads from spin-waiting at 100% CPU
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
os.environ.setdefault("KMP_BLOCKTIME", "0")

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis.core.config_loader import load_config, get_gemini_api_key
from jarvis.core.orchestrator import JarvisOrchestrator
from jarvis.core.services import kill_orphaned_jarvis_processes
from jarvis.ui.tray import SystemTrayApp


def setup_logging():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("phonemizer").setLevel(logging.ERROR)
    try:
        import phonemizer.logger
        phonemizer.logger.get_logger("quiet")
    except Exception:
        pass


def check_api_key():
    key = get_gemini_api_key()
    if not key or key == "your_gemini_api_key_here":
        print("\n" + "!" * 60)
        print(" [ATTENTION] GEMINI_API_KEY is not configured yet!")
        print(" You can get a completely free key from Google AI Studio:")
        print(" 👉 https://aistudio.google.com/app/apikey")
        print("!" * 60)
        entered_key = input("Enter your Gemini API Key now (or press Enter to run offline): ").strip()
        if entered_key:
            env_file = Path(__file__).resolve().parent / ".env"
            with open(env_file, "w", encoding="utf-8") as f:
                f.write(f"GEMINI_API_KEY={entered_key}\n")
            os.environ["GEMINI_API_KEY"] = entered_key
            print("API Key saved to .env!\n")


def main():
    setup_logging()

    # Process Elevation & Performance Scheduling
    # Elevates main process to ABOVE_NORMAL_PRIORITY_CLASS to prioritize
    # audio streaming callbacks, Kokoro synthesis, and ring buffer I/O over OS background tasks.
    try:
        import psutil
        proc = psutil.Process()
        proc.nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
        logging.getLogger("Jarvis").info("Process Elevation: Set to ABOVE_NORMAL_PRIORITY_CLASS for high-performance audio scheduling.")
    except Exception as e:
        try:
            import psutil
            psutil.Process().nice(psutil.NORMAL_PRIORITY_CLASS)
            logging.getLogger("Jarvis").info("Process Elevation: Set to NORMAL_PRIORITY_CLASS.")
        except Exception:
            pass

    if "--check-health" in sys.argv:
        from jarvis.core.health_check import run_system_health_check, format_health_report
        report = run_system_health_check()
        print(format_health_report(report))
        sys.exit(0 if report.get("status") == "HEALTHY" else 1)

    if "--omniroute" in sys.argv:
        os.environ["LLM_PROVIDER"] = "omniroute"

    is_stealth = any(arg in sys.argv for arg in ("--stealth", "--background", "-b", "-s"))
    config = load_config()
    if is_stealth:
        from jarvis.ui.tray import set_console_visible
        set_console_visible(False)
    elif config.get("llm", {}).get("provider", "ollama").lower() == "gemini":
        check_api_key()

    # Clean up any orphaned Jarvis processes left from prior crashes or forced terminal kills
    kill_orphaned_jarvis_processes()

    logging.getLogger("Jarvis").info("Initializing neural models and subsystems (CUDA STT/TTS)...")
    orchestrator = JarvisOrchestrator(config)

    # Register automatic teardown so all tools and child processes terminate regardless of exit path
    atexit.register(orchestrator.stop)

    def _on_signal(signum, frame):
        logging.getLogger("Jarvis").info("Termination signal %s caught. Shutting down all tools and child services...", signum)
        orchestrator.stop()
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception:
        pass

    # Launch full-featured system tray app in background
    tray_app = SystemTrayApp(orchestrator=orchestrator, on_quit_callback=orchestrator.stop)
    tray_app.run_in_background()

    # Run main interactive loop
    orchestrator.run_interactive_loop()


if __name__ == "__main__":
    main()
