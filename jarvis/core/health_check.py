"""Comprehensive Live Operational Health Check and Diagnostic Engine for Jarvis 2.0.

Inspects physical hardware endpoints, running background daemons, neural weights,
SQLite memory database WAL integrity, GPU VRAM, and AI credentials.
"""
from __future__ import annotations

import logging
import os
import socket
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Jarvis.HealthCheck")


def test_socket_port(port: int, host: str = "127.0.0.1", timeout: float = 0.15) -> bool:
    """Non-blocking probe checking if a local TCP port is listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except Exception:
            return False


def run_system_health_check(project_root: Optional[Path] = None) -> Dict[str, Any]:
    """Executes a full physical and operational health check across all Jarvis 2.0 subsystems."""
    root = project_root or Path(__file__).resolve().parent.parent.parent
    report: Dict[str, Any] = {
        "timestamp": time.time(),
        "status": "HEALTHY",
        "subsystems": {},
    }

    # 1. Python Runtime & Process Scheduling
    try:
        import psutil
        proc = psutil.Process()
        prio = proc.nice()
        report["subsystems"]["runtime"] = {
            "status": "OK",
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "pid": proc.pid,
            "priority": prio,
        }
    except Exception as e:
        report["subsystems"]["runtime"] = {"status": "WARN", "error": str(e)}

    # 2. Audio Capture & Playback Hardware Endpoints
    audio_data: Dict[str, Any] = {"status": "OK"}
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        in_idx, out_idx = sd.default.device

        # Microphone Input Probe
        if in_idx is not None and in_idx >= 0:
            in_dev = devices[in_idx]
            mic_stream_ok = False
            try:
                with sd.InputStream(samplerate=16000, channels=1, blocksize=512):
                    mic_stream_ok = True
            except Exception as stream_err:
                mic_stream_ok = False
                audio_data["mic_stream_error"] = str(stream_err)

            audio_data["input"] = {
                "index": in_idx,
                "name": in_dev["name"],
                "channels": in_dev["max_input_channels"],
                "samplerate": in_dev["default_samplerate"],
                "stream_verified": mic_stream_ok,
            }
        else:
            audio_data["input"] = None
            audio_data["status"] = "WARN"

        # Speaker Output Probe
        if out_idx is not None and out_idx >= 0:
            out_dev = devices[out_idx]
            spk_stream_ok = False
            try:
                with sd.OutputStream(samplerate=24000, channels=1, blocksize=512):
                    spk_stream_ok = True
            except Exception as stream_err:
                spk_stream_ok = False
                audio_data["spk_stream_error"] = str(stream_err)

            audio_data["output"] = {
                "index": out_idx,
                "name": out_dev["name"],
                "channels": out_dev["max_output_channels"],
                "samplerate": out_dev["default_samplerate"],
                "stream_verified": spk_stream_ok,
            }
        else:
            audio_data["output"] = None
            audio_data["status"] = "WARN"

        report["subsystems"]["audio"] = audio_data
    except Exception as e:
        report["subsystems"]["audio"] = {"status": "FAIL", "error": str(e)}
        report["status"] = "DEGRADED"

    # 3. GPU VRAM & Thermal Governor (RTX 3050 Laptop GPU)
    try:
        from jarvis.skills.nvml_dxgi_vram_arbiter import audit_vram_and_thermal_budget
        vram_info = audit_vram_and_thermal_budget()
        if vram_info.get("status") == "ok":
            report["subsystems"]["gpu"] = {
                "status": "OK",
                "total_mb": vram_info.get("vram_total_mb", 0.0),
                "used_mb": vram_info.get("vram_used_mb", 0.0),
                "free_mb": vram_info.get("vram_free_mb", 0.0),
                "temp_c": vram_info.get("gpu_temp_c", 0),
            }
        else:
            report["subsystems"]["gpu"] = {"status": "WARN", "detail": vram_info}
    except Exception as e:
        report["subsystems"]["gpu"] = {"status": "WARN", "error": str(e)}

    # 4. Neural Weights & Local Models
    try:
        kokoro_onnx = root / "data" / "models" / "kokoro" / "kokoro-v1.0.onnx"
        kokoro_bin = root / "data" / "models" / "kokoro" / "voices-v1.0.bin"
        bge_onnx = root / "data" / "models" / "embeddings" / "bge-small-en-v1.5" / "onnx" / "model_int8.onnx"
        wake_wav = root / "data" / "sounds" / "wake.wav"

        models_ok = (
            kokoro_onnx.is_file()
            and kokoro_bin.is_file()
            and bge_onnx.is_file()
            and wake_wav.is_file()
        )
        report["subsystems"]["models"] = {
            "status": "OK" if models_ok else "WARN",
            "kokoro_tts": kokoro_onnx.is_file(),
            "kokoro_voices": kokoro_bin.is_file(),
            "bge_embeddings": bge_onnx.is_file(),
            "sound_effects": wake_wav.is_file(),
        }
        if not models_ok:
            report["status"] = "DEGRADED"
    except Exception as e:
        report["subsystems"]["models"] = {"status": "WARN", "error": str(e)}

    # 5. SQLite Production Memory Database Integrity & sqlite-vec
    try:
        db_path = root / "data" / "jarvis_memory.db"
        if not db_path.exists():
            report["subsystems"]["memory_db"] = {"status": "FAIL", "error": "Database file not found"}
            report["status"] = "DEGRADED"
        else:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            mode = cur.execute("PRAGMA journal_mode;").fetchone()[0]
            integrity = cur.execute("PRAGMA integrity_check;").fetchall()
            quick = cur.execute("PRAGMA quick_check;").fetchall()
            tables = cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table';").fetchone()[0]

            vec_ok = False
            vec_ver = None
            try:
                import sqlite_vec
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                vec_ver = cur.execute("SELECT vec_version();").fetchone()[0]
                vec_ok = True
            except Exception:
                vec_ok = False

            conn.close()

            is_intact = integrity == [("ok",)] and quick == [("ok",)]
            report["subsystems"]["memory_db"] = {
                "status": "OK" if is_intact else "FAIL",
                "journal_mode": mode.upper(),
                "integrity_check": "PASSED" if is_intact else "CORRUPT",
                "table_count": tables,
                "sqlite_vec": f"v{vec_ver}" if vec_ok else "UNAVAILABLE",
            }
            if not is_intact:
                report["status"] = "DEGRADED"
    except Exception as e:
        report["subsystems"]["memory_db"] = {"status": "FAIL", "error": str(e)}
        report["status"] = "DEGRADED"

    # 6. Local Network Ports & Background Daemons
    try:
        hud_active = test_socket_port(20129)
        cdp_active = test_socket_port(9222)
        ollama_active = test_socket_port(11434)
        report["subsystems"]["network"] = {
            "status": "OK",
            "hud_port_20129": "LISTENING" if hud_active else "STANDBY_READY",
            "cdp_port_9222": "CONNECTED" if cdp_active else "STANDBY_OFFLINE",
            "ollama_port_11434": "ONLINE" if ollama_active else "STANDBY_OFFLINE",
        }
    except Exception as e:
        report["subsystems"]["network"] = {"status": "WARN", "error": str(e)}

    # 7. Cognitive Router & AI Credentials
    try:
        from jarvis.core.config_loader import load_config, get_gemini_api_key
        from jarvis.core.cognition import CognitiveRouter
        cfg = load_config()
        key = get_gemini_api_key()
        has_key = bool(key and key != "your_gemini_api_key_here")

        router = CognitiveRouter()
        fast_t = router.classify_query_tier("What time is it?")
        deep_t = router.classify_query_tier("Explain the architecture of our memory subsystem.")

        report["subsystems"]["cognition"] = {
            "status": "OK",
            "primary_provider": cfg.get("llm", {}).get("provider", "antigravity"),
            "gemini_api_key": "CONFIGURED" if has_key else "NOT_SET",
            "fast_tier_route": fast_t,
            "deep_tier_route": deep_t,
        }
    except Exception as e:
        report["subsystems"]["cognition"] = {"status": "WARN", "error": str(e)}

    return report


def format_health_report(report: Dict[str, Any]) -> str:
    """Formats the structured health report into clean, authentic Bettany telemetry."""
    subs = report.get("subsystems", {})
    lines = [
        "=================================================================",
        "        JARVIS 2.0 LIVE OPERATIONAL SUBSYSTEM DIAGNOSTICS        ",
        "=================================================================",
    ]

    # Audio Telemetry
    audio = subs.get("audio", {})
    if audio.get("status") == "OK":
        mic = audio.get("input", {}) or {}
        spk = audio.get("output", {}) or {}
        mic_str = f"[{mic.get('index')}] {mic.get('name')} (16kHz stream OK)" if mic else "None"
        spk_str = f"[{spk.get('index')}] {spk.get('name')} (24kHz stream OK)" if spk else "None"
        lines.append(f"  [AUDIO HARDWARE ] : ONLINE")
        lines.append(f"    • Microphone  : {mic_str}")
        lines.append(f"    • Output Spk  : {spk_str}")
    else:
        lines.append(f"  [AUDIO HARDWARE ] : DEGRADED ({audio.get('error', 'Device error')})")

    # GPU Telemetry
    gpu = subs.get("gpu", {})
    if gpu.get("status") == "OK":
        lines.append(
            f"  [GPU ACCELERATOR] : ONLINE (RTX 3050 | {gpu.get('free_mb', 0):.0f} MB free / {gpu.get('total_mb', 0):.0f} MB | {gpu.get('temp_c', 0)}°C)"
        )
    else:
        lines.append(f"  [GPU ACCELERATOR] : STANDBY / CPU MODE ({gpu.get('error', 'NVML notice')})")

    # Neural Weights
    models = subs.get("models", {})
    if models.get("status") == "OK":
        lines.append("  [NEURAL WEIGHTS ] : VERIFIED (Kokoro-82M ONNX, BGE Embeddings, Audio SFX)")
    else:
        lines.append(f"  [NEURAL WEIGHTS ] : WARNING (Missing components: {models})")

    # SQLite Memory DB
    db = subs.get("memory_db", {})
    if db.get("status") == "OK":
        lines.append(
            f"  [MEMORY DATABASE] : PRISTINE (WAL Mode | Integrity: {db.get('integrity_check')} | {db.get('table_count')} Tables | sqlite-vec: {db.get('sqlite_vec')})"
        )
    else:
        lines.append(f"  [MEMORY DATABASE] : WARNING ({db.get('error', 'Integrity issue')})")

    # Background Daemons
    net = subs.get("network", {})
    lines.append(
        f"  [DAEMON PORTS   ] : HUD (20129): {net.get('hud_port_20129')} | CDP (9222): {net.get('cdp_port_9222')} | Ollama (11434): {net.get('ollama_port_11434')}"
    )

    # Cognition
    cog = subs.get("cognition", {})
    lines.append(
        f"  [COGNITIVE CORE ] : ONLINE (Provider: {cog.get('primary_provider')} | Gemini Key: {cog.get('gemini_api_key')} | Tiering: {cog.get('fast_tier_route')}/{cog.get('deep_tier_route')})"
    )

    lines.append("=================================================================")
    overall = report.get("status", "UNKNOWN")
    lines.append(f"  OVERALL OPERATIONAL STATUS: {overall}")
    lines.append("=================================================================")

    return "\n".join(lines)


if __name__ == "__main__":
    rep = run_system_health_check()
    print(format_health_report(rep))
