"""Concurrency tests for the lock-free SPSC shared-memory audio ring.

Producer and consumer run in SEPARATE PROCESSES (spawn, as on Windows) so the
header words are exercised through real shared memory, not shared Python state.
Samples are float32 integers (exact below 2**24), so every sample's value is its
absolute stream index and any lost update, re-read, or torn copy is detectable.
"""
import multiprocessing as mp
import time
import unittest
import uuid

import numpy as np

from jarvis.audio.shm_transport import HEADER_BYTES, ShmAudioRingTransport

TOTAL_SAMPLES = 1_500_000  # < 2**24 so float32 holds exact indices
TIMEOUT_S = 60.0


def _producer_main(name: str, capacity: int, total: int, flow_control: bool, seed: int) -> None:
    """Writes the sample stream 0..total-1 in random-sized chunks."""
    rng = np.random.default_rng(seed)
    tx = ShmAudioRingTransport(name=name, capacity_samples=capacity, create=False)
    try:
        sent = 0
        while sent < total:
            n = int(min(rng.integers(1, 481), total - sent))
            if flow_control:
                deadline = time.monotonic() + TIMEOUT_S
                while tx.free_samples < n:
                    if time.monotonic() > deadline:
                        return
                    time.sleep(0)
            tx.write_pcm(np.arange(sent, sent + n, dtype=np.float32))
            sent += n
    finally:
        tx.close()


class TestShmSpscDeterministic(unittest.TestCase):
    def setUp(self):
        self.name = f"test_spsc_{uuid.uuid4().hex[:12]}"
        self.open = []

    def tearDown(self):
        for t in reversed(self.open):
            try:
                t.close()
            except Exception:
                pass

    def _pair(self, capacity):
        p = ShmAudioRingTransport(self.name, capacity_samples=capacity, create=True)
        self.open.append(p)
        c = ShmAudioRingTransport(self.name, capacity_samples=capacity, create=False)
        self.open.append(c)
        return p, c

    def test_header_is_cache_line_sized(self):
        self.assertEqual(HEADER_BYTES, 64)

    def test_each_side_leaves_the_other_sides_index_alone(self):
        """The core of the fix: producer never writes read_idx, consumer never writes write_idx."""
        p, c = self._pair(1000)
        p.write_pcm(np.ones(300, dtype=np.float32))
        out = np.zeros(100, dtype=np.float32)
        self.assertEqual(c.read_pcm_into(out), 100)
        self.assertEqual((p.write_index, p.read_index), (300, 100))

        # Simulate the old race: consumer advanced read_idx; producer writes next.
        p.write_pcm(np.ones(50, dtype=np.float32))
        self.assertEqual(p.read_index, 100, "producer must not rewrite consumer's read_idx")
        self.assertEqual(p.write_index, 350)
        c.read_pcm_into(out)
        self.assertEqual(c.write_index, 350, "consumer must not rewrite producer's write_idx")
        self.assertEqual(c.read_index, 200)

    def test_wrap_and_oversize_write_keep_freshest(self):
        p, c = self._pair(100)
        p.write_pcm(np.ones(70, dtype=np.float32))
        c.read_pcm_into(np.zeros(70, dtype=np.float32))
        p.write_pcm(np.full(50, 2.5, dtype=np.float32))
        out = np.zeros(50, dtype=np.float32)
        self.assertEqual(c.read_pcm_into(out), 50)
        np.testing.assert_allclose(out, 2.5)
        big = np.arange(120, dtype=np.float32)
        self.assertEqual(p.write_pcm(big), 100)
        out = np.zeros(100, dtype=np.float32)
        self.assertEqual(c.read_pcm_into(out), 100)
        np.testing.assert_allclose(out, big[-100:])

    def test_consumer_side_overflow_skips_to_fresh_audio(self):
        p, c = self._pair(100)
        for k in range(5):  # 5 x 60 samples into a 100-sample ring, consumer idle
            p.write_pcm(np.arange(k * 60, (k + 1) * 60, dtype=np.float32))
        self.assertEqual(p.read_index, 0, "producer must not move read_idx on overflow")
        out = np.zeros(100, dtype=np.float32)
        n = c.read_pcm_into(out)
        self.assertGreater(n, 0)
        # What it returns must be a contiguous, most-recent run ending at the newest sample.
        np.testing.assert_array_equal(np.diff(out[:n]), np.ones(n - 1))
        self.assertEqual(out[n - 1], 299.0)
        self.assertGreaterEqual(c.stats["overruns"], 1)

    def test_barge_in_drops_queued_audio_but_keeps_later_audio(self):
        p, c = self._pair(500)
        p.write_pcm(np.full(100, 1.0, dtype=np.float32))  # old turn
        self.assertEqual(p.trigger_barge_in_clear(), 1)
        self.assertEqual(c.barge_in_epoch, 1)
        p.write_pcm(np.full(40, 7.0, dtype=np.float32))  # new turn, written before consumer wakes
        out = np.zeros(200, dtype=np.float32)
        n = c.read_pcm_into(out)
        self.assertEqual(n, 40, "post-trigger audio must survive the flush")
        np.testing.assert_allclose(out[:n], 7.0)
        self.assertEqual(c.stats["barge_in_dropped"], 100)

    def test_barge_in_with_nothing_queued_reads_zero(self):
        p, c = self._pair(500)
        p.write_pcm(np.ones(100, dtype=np.float32))
        p.trigger_barge_in_clear()
        self.assertEqual(c.read_pcm_into(np.zeros(100, dtype=np.float32)), 0)

    def test_late_attacher_ignores_old_barge_ins(self):
        p = ShmAudioRingTransport(self.name, capacity_samples=500, create=True)
        self.open.append(p)
        p.write_pcm(np.ones(50, dtype=np.float32))
        p.trigger_barge_in_clear()
        p.write_pcm(np.full(50, 3.0, dtype=np.float32))
        c = ShmAudioRingTransport(self.name, capacity_samples=500, create=False)
        self.open.append(c)
        out = np.zeros(200, dtype=np.float32)
        self.assertEqual(c.read_pcm_into(out), 100)  # backlog intact; stale cut ignored

    def test_close_is_idempotent(self):
        p, c = self._pair(100)
        c.close()
        c.close()
        p.close()
        p.close()


class TestShmSpscCrossProcess(unittest.TestCase):
    def setUp(self):
        self.name = f"test_spsc_mp_{uuid.uuid4().hex[:12]}"
        self.ctx = mp.get_context("spawn")

    def _run(self, capacity, flow_control, seed):
        rx = ShmAudioRingTransport(self.name, capacity_samples=capacity, create=True)
        proc = self.ctx.Process(
            target=_producer_main,
            args=(self.name, capacity, TOTAL_SAMPLES, flow_control, seed),
            daemon=True,
        )
        proc.start()
        return rx, proc

    def test_no_lost_updates_no_rereads_in_order(self):
        """Backpressured producer: every one of N samples must arrive exactly once, in order."""
        rx, proc = self._run(capacity=4096, flow_control=True, seed=1234)
        try:
            out = np.zeros(300, dtype=np.float32)
            expected = 0
            deadline = time.monotonic() + TIMEOUT_S
            while expected < TOTAL_SAMPLES and time.monotonic() < deadline:
                n = rx.read_pcm_into(out)
                if n == 0:
                    time.sleep(0)
                    continue
                chunk = out[:n]
                self.assertEqual(chunk[0], expected, f"gap or re-read at stream index {expected}")
                if n > 1:
                    self.assertTrue(np.all(np.diff(chunk) == 1.0), "torn / non-contiguous chunk")
                expected += n
            self.assertEqual(expected, TOTAL_SAMPLES, "samples were lost")
            self.assertEqual(rx.stats, {"overruns": 0, "barge_in_dropped": 0})
            proc.join(10)
        finally:
            if proc.is_alive():
                proc.terminate()
            rx.close()

    def test_overrunning_producer_never_yields_torn_or_backward_audio(self):
        """Unthrottled producer on a tiny ring: data may be skipped, but never corrupted or replayed."""
        rx, proc = self._run(capacity=2048, flow_control=False, seed=99)
        try:
            out = np.zeros(256, dtype=np.float32)
            last_end = -1
            got = 0
            deadline = time.monotonic() + TIMEOUT_S
            while time.monotonic() < deadline:
                n = rx.read_pcm_into(out)
                if n == 0:
                    if not proc.is_alive() and rx.read_index >= rx.write_index:
                        break
                    time.sleep(0)
                    continue
                chunk = out[:n]
                if n > 1:
                    self.assertTrue(np.all(np.diff(chunk) == 1.0), "torn chunk (overwritten mid-copy)")
                self.assertGreater(chunk[0], last_end, "audio went backwards (stale re-read)")
                last_end = chunk[n - 1]
                got += n
            self.assertGreater(got, 0)
            proc.join(10)
        finally:
            if proc.is_alive():
                proc.terminate()
            rx.close()


if __name__ == "__main__":
    unittest.main()
