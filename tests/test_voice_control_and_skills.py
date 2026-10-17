"""Automated tests for Voice Control Skills, Blended Profiles, and Desktop Capabilities in Jarvis 2.0.
Verifies latent voice style blending, speech rate tuning, audio device routing, idempotent app launch/focus, and fast file search.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import numpy as np

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.skills.base import SkillRegistry
from jarvis.audio.tts import TextToSpeech
import jarvis.skills.voice_control as voice_skills
import jarvis.skills.os_control as os_skills
from jarvis.core.brain import JarvisBrain


class TestVoiceControlAndSkillsSuite(unittest.TestCase):
    """Verifies voice customization tools, latent blending, and enhanced desktop skills."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "_base_dir": self.temp_dir,
            "tts": {
                "engine": "kokoro",
                "kokoro_voice": "bm_george",
                "kokoro_speed": 1.05,
                "kokoro_lang": "en-gb",
                "cpu_threads": 2,
            },
            "llm": {
                "provider": "antigravity",
                "antigravity_model": "gemini-3.8-flash-low",
            },
            "memory": {"db_path": os.path.join(self.temp_dir, "test_mem.db")},
            "safety": {"confirm_destructive": True},
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_voice_profiles_blending_resolution(self):
        """Verifies that latent style blending computes normalized linear combination of style vectors."""
        tts = TextToSpeech(self.config)

        # Mock kokoro engine
        fake_kokoro = MagicMock()
        v1 = np.ones((510, 1, 256), dtype=np.float32)
        v2 = np.ones((510, 1, 256), dtype=np.float32) * 2.0

        def fake_get_style(v_name):
            if v_name == "bm_george":
                return v1
            elif v_name == "bm_daniel":
                return v2
            return v1

        fake_kokoro.get_voice_style.side_effect = fake_get_style
        fake_kokoro.get_voices.return_value = ["bm_george", "bm_daniel", "bm_lewis"]
        tts._kokoro = fake_kokoro

        # Test classic profile: 0.70 * bm_george + 0.30 * bm_daniel
        tts.set_voice_profile("jarvis_classic")
        blended = tts._resolve_voice()
        self.assertIsInstance(blended, np.ndarray)
        self.assertEqual(blended.shape, (510, 1, 256))
        # Expected value: 0.70 * 1.0 + 0.30 * 2.0 = 1.30
        self.assertAlmostEqual(float(blended[0, 0, 0]), 1.30, delta=0.01)

    def test_set_voice_profile_skill(self):
        """Verifies set_voice_profile tool updates active TTS profile."""
        tts = TextToSpeech(self.config)
        fake_kokoro = MagicMock()
        fake_kokoro.get_voices.return_value = ["bm_george", "bm_daniel"]
        tts._kokoro = fake_kokoro
        voice_skills.set_tts_instance(tts)

        res = SkillRegistry.execute("set_voice_profile", profile_name="jarvis_authoritative")
        self.assertIn("Voice profile successfully set to 'jarvis_authoritative'", res)
        self.assertEqual(tts.kokoro_voice, "jarvis_authoritative")

        # Unknown profile fallback test
        res_unknown = SkillRegistry.execute("set_voice_profile", profile_name="non_existent_voice")
        self.assertIn("Unknown voice profile", res_unknown)

    def test_set_speech_rate_skill(self):
        """Verifies set_speech_rate clamps values and updates TTS speed."""
        tts = TextToSpeech(self.config)
        voice_skills.set_tts_instance(tts)

        res = SkillRegistry.execute("set_speech_rate", speed=1.25)
        self.assertIn("Speech rate set to 1.25x", res)
        self.assertEqual(tts.kokoro_speed, 1.25)

        # Clamping check: > 1.50 clamps to 1.50
        SkillRegistry.execute("set_speech_rate", speed=2.50)
        self.assertEqual(tts.kokoro_speed, 1.50)

    def test_get_voice_settings_skill(self):
        """Verifies get_voice_settings formats active engine and profile correctly."""
        tts = TextToSpeech(self.config)
        voice_skills.set_tts_instance(tts)

        res = SkillRegistry.execute("get_voice_settings")
        self.assertIn("Active Speech Engine:", res)
        self.assertIn("Voice Profile / Timbre:", res)
        self.assertIn("Speech Rate:", res)

    def test_list_and_set_audio_devices(self):
        """Verifies list_audio_devices and set_audio_output_device query sound endpoints safely."""
        fake_devices = [
            {"name": "Speakers (Realtek Audio)", "max_output_channels": 2},
            {"name": "Headphones (Realtek Audio)", "max_output_channels": 2},
            {"name": "Microphone (Realtek Audio)", "max_output_channels": 0},
        ]
        with patch("sounddevice.query_devices", return_value=fake_devices), \
             patch("sounddevice.default") as mock_default:
            mock_default.device = (0, 0)
            list_res = SkillRegistry.execute("list_audio_devices")
            self.assertIn("Speakers", list_res)
            self.assertIn("Headphones", list_res)
            self.assertNotIn("Microphone", list_res)

            set_res = SkillRegistry.execute("set_audio_output_device", device_name_or_index="headphones")
            self.assertIn("Audio output successfully routed to [1] 'Headphones", set_res)

    def test_launch_or_focus_application_skill(self):
        """Verifies launch_or_focus_application focuses running window before attempting launch."""
        # Case 1: App already running -> switches focus
        with patch("jarvis.skills.windows_uia.focus_window_uia", return_value="Brave"):
            res = SkillRegistry.execute("launch_or_focus_application", app_name="brave")
            self.assertIn("Switched to running application 'Brave'", res)

        # Case 2: App not open -> launches application
        with patch("jarvis.skills.windows_uia.focus_window_uia", return_value=None), \
             patch("jarvis.skills.os_control.focus_window", return_value="No open Brave window was found"), \
             patch("jarvis.skills.os_control.launch_application", return_value="Application 'brave' launched, Sir.") as mock_launch:
            res = SkillRegistry.execute("launch_or_focus_application", app_name="brave")
            self.assertIn("launched", res)
            mock_launch.assert_called_once_with("brave")

    def test_search_desktop_files_fast(self):
        """Verifies search_desktop_files_fast locates files in user directories."""
        # Create a mock target file in temporary directory
        proj_dir = Path(self.temp_dir) / "Projects" / "ProjectX"
        proj_dir.mkdir(parents=True, exist_ok=True)
        target_file = proj_dir / "target_document.pdf"
        target_file.write_text("dummy content")

        with patch("pathlib.Path.home", return_value=Path(self.temp_dir)):
            res = SkillRegistry.execute("search_desktop_files_fast", query="target_document")
            self.assertIn("Found 1 file(s)", res)
            self.assertIn("target_document.pdf", res)

    def test_brain_fast_reflex_voice_profiles(self):
        """Verifies fast reflex routing (<15ms) for voice switching and speed controls."""
        mem = MagicMock()
        mem.db_path = Path(self.temp_dir) / "test_mem.db"
        safety = MagicMock()
        brain = JarvisBrain(self.config, mem, safety)

        with patch("jarvis.skills.base.SkillRegistry.execute") as mock_exec:
            mock_exec.return_value = "Voice profile successfully set to 'jarvis_classic', Sir."
            res = brain._try_fast_reflex("switch to classic voice")
            self.assertIsNotNone(res)
            mock_exec.assert_called_with("set_voice_profile", profile_name="jarvis_classic")

            brain._try_fast_reflex("speak faster")
            mock_exec.assert_called_with("set_speech_rate", speed=1.20)

            brain._try_fast_reflex("speak slower")
            mock_exec.assert_called_with("set_speech_rate", speed=0.90)


if __name__ == "__main__":
    unittest.main()
