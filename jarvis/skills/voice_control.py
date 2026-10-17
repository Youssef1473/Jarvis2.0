"""Voice Control & Speech Settings Management for Jarvis 2.0.
Provides tools to dynamically adjust voice personas, speech rate, and audio output routing.
"""
import logging
from typing import Any, Dict, List, Optional
import sounddevice as sd

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.VoiceControl")

_tts_instance = None


def set_tts_instance(tts) -> None:
    """Sets active TextToSpeech instance for dynamic voice manipulation."""
    global _tts_instance
    _tts_instance = tts


def get_tts_instance():
    """Returns active TextToSpeech instance, or None if not yet initialized."""
    global _tts_instance
    return _tts_instance


@SkillRegistry.register(
    name="set_voice_profile",
    description="Switches Jarvis's speech persona or voice timbre to a named profile (e.g. 'jarvis_bettany', 'jarvis_classic', 'jarvis_authoritative', 'jarvis_crisp', 'friday', 'george', 'daniel', 'lewis', 'emma')."
)
def set_voice_profile(profile_name: str) -> str:
    """Switches active voice profile or native Kokoro voice model."""
    tts = get_tts_instance()
    if not tts:
        return "Voice engine instance not attached to tool registry, Sir."
    return tts.set_voice_profile(profile_name)


@SkillRegistry.register(
    name="set_speech_rate",
    description="Adjusts speaking tempo and playback speed multiplier between 0.75x and 1.50x (e.g. 1.0, 1.15, 1.25)."
)
def set_speech_rate(speed: float) -> str:
    """Adjusts speech tempo multiplier."""
    tts = get_tts_instance()
    if not tts:
        return "Voice engine instance not attached to tool registry, Sir."
    return tts.set_speed(speed)


@SkillRegistry.register(
    name="get_voice_settings",
    description="Inspects active speech synthesis settings including voice profile, speed multiplier, language, and available personas."
)
def get_voice_settings() -> str:
    """Returns formatted overview of active voice configuration."""
    tts = get_tts_instance()
    if not tts:
        return "Voice engine is currently offline or uninitialized, Sir."

    settings = tts.get_voice_settings()
    engine = settings.get("engine", "kokoro")
    voice = settings.get("voice", "bm_george")
    speed = settings.get("speed", 1.05)
    profiles = ", ".join(settings.get("profiles", [])[:5])

    return (
        f"Active Speech Engine: {engine.upper()}\n"
        f"Voice Profile / Timbre: {voice}\n"
        f"Speech Rate: {speed:.2f}x\n"
        f"Available Profiles: {profiles}..."
    )


@SkillRegistry.register(
    name="list_audio_devices",
    description="Lists available physical audio output devices (speakers, headphones, HDMI displays) on the Windows workstation."
)
def list_audio_devices() -> str:
    """Queries and formats available Windows audio playback endpoints."""
    try:
        devices = sd.query_devices()
        outputs = []
        seen_names = set()
        for idx, dev in enumerate(devices):
            if dev.get("max_output_channels", 0) > 0:
                name = dev.get("name", "Unknown Device").strip()
                # Deduplicate redundant MME / DirectSound virtual mirrors
                if name not in seen_names:
                    seen_names.add(name)
                    outputs.append(f"[{idx}] {name}")

        if not outputs:
            return "No audio output devices detected, Sir."

        return "Available Audio Output Devices, Sir:\n" + "\n".join(outputs[:12])
    except Exception as e:
        logger.error(f"Failed to query audio devices: {e}")
        return f"Unable to query audio endpoints: {e}"


@SkillRegistry.register(
    name="set_audio_output_device",
    description="Routes Jarvis audio playback to a specific device index or device name keyword (e.g. 'headphone', 'speaker', '5')."
)
def set_audio_output_device(device_name_or_index: str) -> str:
    """Switches sounddevice default playback device."""
    query = str(device_name_or_index or "").strip()
    if not query:
        return "Please specify a device index or name keyword, Sir."

    try:
        devices = sd.query_devices()
        target_idx = None
        target_name = None

        # Check if numeric index
        if query.isdigit():
            idx = int(query)
            if 0 <= idx < len(devices) and devices[idx].get("max_output_channels", 0) > 0:
                target_idx = idx
                target_name = devices[idx].get("name")
        else:
            q_lower = query.lower()
            for idx, dev in enumerate(devices):
                if dev.get("max_output_channels", 0) > 0:
                    d_name = dev.get("name", "").lower()
                    if q_lower in d_name:
                        target_idx = idx
                        target_name = dev.get("name")
                        break

        if target_idx is None:
            return f"Could not find an audio output device matching '{query}', Sir."

        # Update sounddevice default output device
        curr_in, _ = sd.default.device
        sd.default.device = (curr_in, target_idx)
        return f"Audio output successfully routed to [{target_idx}] '{target_name}', Sir."
    except Exception as e:
        logger.error(f"Error setting audio device: {e}")
        return f"Failed to switch audio output device: {e}"
