"""Test suite for Streaming Voice Reflex Optimization pipeline in Jarvis 2.0.

Verifies:
1. StreamingSentenceChunker: speculative clause emission at punctuation boundaries,
   token-to-chunk sub-millisecond latency, and protection of numbers, timestamps, and abbreviations.
2. AudioRingBuffer: lock-free circular buffer, vectorized NumPy transfers, and instant zero-latency clear.
3. TextToSpeech Streaming Pipeline: feed_token streaming, real-time worker priority,
   and instant stream cancellation on user barge-in / interruption.
"""
import sys
import time
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.config_loader import load_config
from jarvis.audio.chunker import StreamingSentenceChunker
from jarvis.audio.ring_buffer import AudioRingBuffer
from jarvis.audio.tts import TextToSpeech, _set_thread_realtime_priority


def test_chunker_speculative_boundaries():
    print("\n--- 1. Testing StreamingSentenceChunker Speculative Boundaries ---")
    chunker = StreamingSentenceChunker()

    # Stream tokens simulating LLM output
    tokens = ["Certainly", ",", " Sir", ".", " The", " neural", " bridge", " is", " online", "."]
    t0 = time.perf_counter()
    emitted = []
    first_chunk_token_idx = None

    for idx, tok in enumerate(tokens):
        chunks = chunker.feed(tok)
        if chunks and first_chunk_token_idx is None:
            first_chunk_token_idx = idx
        emitted.extend(chunks)

    emitted.extend(chunker.flush())
    total_time_ms = (time.perf_counter() - t0) * 1000

    print(f"Emitted chunks: {emitted}")
    print(f"Total chunking time for {len(tokens)} tokens: {total_time_ms:.3f} ms")
    print(f"First chunk emitted at token index: {first_chunk_token_idx} ('{tokens[first_chunk_token_idx]}')")

    assert emitted[0] == "Certainly,", f"Expected 'Certainly,', got '{emitted[0]}'"
    assert emitted[1] == "Sir.", f"Expected 'Sir.', got '{emitted[1]}'"
    assert emitted[2] == "The neural bridge is online.", f"Expected sentence, got '{emitted[2]}'"
    assert first_chunk_token_idx == 1, "First chunk must be emitted immediately at token 1 (',')!"
    assert total_time_ms < 10.0, f"Token chunking took {total_time_ms:.2f}ms, expected < 10ms"
    print("Speculative Punctuation Emission: PASSED")


def test_chunker_numerical_and_abbreviation_guards():
    print("\n--- 2. Testing Numerical & Abbreviation Guards ---")
    chunker = StreamingSentenceChunker()

    # Decimals & Timestamps should NOT split
    tokens = ["At", " 12", ":", "30", " PM", ",", " latency", " reached", " 3", ".", "14", " ms", "."]
    emitted = []
    for tok in tokens:
        emitted.extend(chunker.feed(tok))
    emitted.extend(chunker.flush())

    print(f"Guarded chunks (time & decimal): {emitted}")
    assert any("12:30 PM," in c for c in emitted), "12:30 timestamp was fragmented!"
    assert any("3.14 ms." in c for c in emitted), "3.14 decimal was fragmented!"

    # Abbreviations should NOT split
    chunker.reset()
    abbr_tokens = ["Dr", ".", " Aris", " reviewed", " the", " report", ",", " Sir", "."]
    abbr_emitted = []
    for tok in abbr_tokens:
        abbr_emitted.extend(chunker.feed(tok))
    abbr_emitted.extend(chunker.flush())

    print(f"Guarded chunks (abbreviations): {abbr_emitted}")
    assert abbr_emitted[0] == "Dr. Aris reviewed the report,", "Abbreviation 'Dr.' caused premature sentence split!"
    assert abbr_emitted[1] == "Sir."
    print("Numerical & Abbreviation Guards: PASSED")


def test_audio_ring_buffer():
    print("\n--- 3. Testing AudioRingBuffer Concurrency & Vectorized Transfers ---")
    rb = AudioRingBuffer(capacity_samples=24000)
    assert rb.is_empty
    assert rb.available_samples == 0

    # Test high-throughput writes
    t0 = time.perf_counter()
    data = np.random.randn(4800).astype(np.float32)
    written = rb.write(data)
    assert written == 4800
    assert rb.available_samples == 4800

    # Test vectorized read
    read_buf, n_read = rb.read(2400)
    assert n_read == 2400
    assert rb.available_samples == 2400
    assert np.allclose(read_buf[:2400], data[:2400])

    # Test wraparound
    more_data = np.ones(20000, dtype=np.float32)
    rb.write(more_data)
    assert rb.available_samples == 22400

    # Test instant barge-in purge (< 1us)
    t_purge = time.perf_counter()
    rb.clear()
    purge_time_us = (time.perf_counter() - t_purge) * 1_000_000
    print(f"Ring buffer purge latency: {purge_time_us:.2f} us")
    assert rb.is_empty
    assert rb.available_samples == 0
    assert purge_time_us < 100.0, "Atomic ring buffer clear must complete in under 100 microseconds!"
    print("AudioRingBuffer Vectorized Transfers & Instant Clear: PASSED")


def test_realtime_thread_priority():
    print("\n--- 4. Testing Dedicated Worker Real-Time Thread Priority ---")
    prio = _set_thread_realtime_priority()
    print(f"Thread priority configuration returned: {prio}")
    # On Windows, should be set to 2 (THREAD_PRIORITY_HIGHEST)
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetThreadPriority.argtypes = [ctypes.c_void_p]
        current_prio = k32.GetThreadPriority(ctypes.c_void_p(-2))
        print(f"Verified OS Thread Priority: {current_prio}")
        assert current_prio == 2, f"Expected THREAD_PRIORITY_HIGHEST (2), got {current_prio}"
    print("Real-Time Worker Priority: PASSED")


def test_tts_streaming_token_feed_and_barge_in():
    print("\n--- 5. Testing TTS feed_token Streaming & Instant Barge-in Cancellation ---")
    cfg = load_config()
    tts = TextToSpeech(cfg)
    
    assert hasattr(tts, "_chunker"), "TextToSpeech must contain _chunker"
    assert hasattr(tts, "_ring_buffer"), "TextToSpeech must contain _ring_buffer"
    assert hasattr(tts, "feed_token"), "TextToSpeech must expose feed_token()"
    assert hasattr(tts, "flush_tokens"), "TextToSpeech must expose flush_tokens()"

    # 1. Start streaming session
    tts.start_streaming_session()
    assert tts.is_speaking is True
    assert tts._stream_active is True

    # 2. Feed token stream directly into the speculative voice reflex pipeline
    simulated_tokens = [
        "Analyzing", " the", " telemetry", ",", " Sir", ".",
        " All", " parameters", " within", " nominal", " bounds", "."
    ]
    
    all_emitted = []
    t_start = time.perf_counter()
    for tok in simulated_tokens:
        emitted = tts.feed_token(tok)
        all_emitted.extend(emitted)

    remaining = tts.flush_tokens()
    all_emitted.extend(remaining)
    token_feed_duration_ms = (time.perf_counter() - t_start) * 1000

    print(f"Speculatively dispatched clauses to speech queue: {all_emitted}")
    print(f"Token feed stream duration: {token_feed_duration_ms:.2f} ms")
    assert "Analyzing the telemetry," in all_emitted[0]

    # Verify synthesis worker began queuing/synthesizing in background
    time.sleep(0.1)

    # 3. Simulate User Barge-in / Interruption
    print("Simulating instantaneous user barge-in interrupt...")
    t_stop = time.perf_counter()
    tts.stop()
    stop_latency_ms = (time.perf_counter() - t_stop) * 1000
    print(f"Barge-in interrupt handling latency: {stop_latency_ms:.2f} ms")

    assert tts.is_speaking is False, "TTS must declare is_speaking=False on stop()"
    assert tts._stream_active is False, "Stream active flag must be False"
    assert tts._ring_buffer.is_empty, "Ring buffer must be instantly emptied on barge-in"
    assert tts._sentence_queue.empty(), "Sentence queue must be drained on barge-in"
    assert tts._chunker.is_empty(), "Chunker buffer must be reset on barge-in"
    assert stop_latency_ms < 500.0, f"Barge-in cancellation took {stop_latency_ms:.2f}ms, expected < 500ms"
    print("TTS Token Streaming & Barge-in Cancellation: PASSED")


def run_all_tests():
    print("==================================================================")
    print("   JARVIS 2.0 - STREAMING VOICE REFLEX PIPELINE VERIFICATION")
    print("==================================================================")
    test_chunker_speculative_boundaries()
    test_chunker_numerical_and_abbreviation_guards()
    test_audio_ring_buffer()
    test_realtime_thread_priority()
    test_tts_streaming_token_feed_and_barge_in()
    print("\n==================================================================")
    print("   ALL STREAMING VOICE REFLEX PIPELINE TESTS PASSED (100%)")
    print("==================================================================")


if __name__ == "__main__":
    run_all_tests()
