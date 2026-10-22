"""Ring ownership and header-access rules for the DSP companion and TTS producer."""
import pathlib
import unittest
import uuid
from unittest import mock

import numpy as np

import jarvis.audio.dsp_process as dsp
from jarvis.audio.shm_transport import DEFAULT_RING_NAME, ShmAudioRingTransport

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestRingOwnership(unittest.TestCase):
    def setUp(self):
        self.name = f"test_ring_{uuid.uuid4().hex[:12]}"
        self.open = []

    def tearDown(self):
        for t in reversed(self.open):
            try:
                t.close()
            except Exception:
                pass

    def test_single_ring_name_shared_by_dsp_and_tts(self):
        self.assertEqual(dsp.DEFAULT_SHM_NAME, DEFAULT_RING_NAME)
        tts_src = (ROOT / "jarvis/audio/tts.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_RING_NAME", tts_src)
        self.assertNotIn('"JarvisAudioShmRing"', tts_src)

    def test_open_or_create_creates_first_then_attaches(self):
        first = ShmAudioRingTransport.open_or_create(self.name, capacity_samples=1000)
        self.open.append(first)
        second = ShmAudioRingTransport.open_or_create(self.name, capacity_samples=1000)
        self.open.append(second)
        self.assertTrue(first._is_owner)
        self.assertFalse(second._is_owner)
        first.write_pcm(np.arange(10, dtype=np.float32))
        out = np.zeros(10, dtype=np.float32)
        self.assertEqual(second.read_pcm_into(out), 10)
        np.testing.assert_array_equal(out, np.arange(10, dtype=np.float32))

    def test_dsp_process_has_no_raw_header_access(self):
        src = (ROOT / "jarvis/audio/dsp_process.py").read_text(encoding="utf-8")
        for banned in ("pack_into", "unpack_from", "import struct", "HEADER_SIZE"):
            self.assertNotIn(banned, src)

    def _client(self):
        client = dsp.AudioDSPProcessClient(shm_name=self.name)
        patches = [
            mock.patch.object(dsp.mp, "Process"),
            mock.patch.object(dsp, "_create_or_open_event", return_value=None),
            mock.patch.object(dsp, "_set_named_event", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return client

    def test_client_attaches_to_ring_already_created_by_tts(self):
        tts_side = ShmAudioRingTransport.open_or_create(self.name, dsp.RING_CAPACITY_SAMPLES, dsp.SAMPLE_RATE)
        self.open.append(tts_side)
        client = self._client()
        self.assertTrue(client.start(), "second creator must attach, not fail")
        self.assertFalse(client.transport._is_owner)
        self.assertEqual(client.write_pcm(np.ones(10, dtype=np.float32)), 10)
        self.assertEqual(tts_side.write_index, 10)
        self.assertTrue(client.trigger_barge_in())
        self.assertEqual(tts_side.barge_in_epoch, 1)
        client.stop()

    def test_client_creates_ring_when_tts_has_not(self):
        client = self._client()
        self.assertTrue(client.start())
        self.assertTrue(client.transport._is_owner)
        late = ShmAudioRingTransport.open_or_create(self.name, dsp.RING_CAPACITY_SAMPLES, dsp.SAMPLE_RATE)
        self.open.append(late)
        self.assertFalse(late._is_owner)
        client.stop()

    def test_start_failure_has_no_raw_fallback(self):
        client = self._client()
        with mock.patch.object(ShmAudioRingTransport, "open_or_create", side_effect=OSError("boom")):
            self.assertFalse(client.start())
        self.assertIsNone(client.transport)
        self.assertEqual(client.write_pcm(np.ones(4, dtype=np.float32)), 0)


if __name__ == "__main__":
    unittest.main()
