"""Deterministic validation of the lock-free partial token slot 
and adaptive syntactic threshold modulation.
"""
from __future__ import annotations

import asyncio
import time
from typing import AsyncGenerator
import pytest

from jarvis.audio.partial_streamer import ShmPartialTokenChannel
from jarvis.audio.semantic_endpointing import SemanticVADCoordinator


@pytest.fixture
def shm_channel():
    channel_name = f"TestWhisperShm_{int(time.time() * 1000)}"
    producer = ShmPartialTokenChannel(name=channel_name, create=True)
    yield producer, channel_name
    producer.close()


def test_lock_free_conflation(shm_channel):
    producer, channel_name = shm_channel
    consumer = ShmPartialTokenChannel(name=channel_name, create=False)

    try:
        # Publish two rapid partial states
        producer.publish_partial("Jarvis", is_final=False)
        producer.publish_partial("Jarvis, list all active", is_final=False)

        # Consumer must read only the freshest state
        state = consumer.read_latest()
        assert state is not None
        text, is_final = state
        assert text == "Jarvis, list all active"
        assert is_final is False

        # Subsequent immediate read returns None (no unconsumed mutations)
        assert consumer.read_latest() is None
    finally:
        consumer.close()


def test_syntactic_threshold_modulation():
    coordinator = SemanticVADCoordinator(channel_name="DummyChannel")

    # Incomplete hanging thought -> expanded threshold
    assert coordinator.evaluate_syntactic_completeness("Jarvis, check the status of") == 850
    assert coordinator.evaluate_syntactic_completeness("Build the project and") == 850

    # Grammatically closed thought -> aggressive cutoff
    assert coordinator.evaluate_syntactic_completeness("Jarvis, close the active window.") == 150
    assert coordinator.evaluate_syntactic_completeness("Execute tests") == 150

    # Ambiguous input -> baseline threshold
    assert coordinator.evaluate_syntactic_completeness("System telemetry") == 500


def test_adaptive_tripwire_trigger(shm_channel):
    producer, channel_name = shm_channel
    coordinator = SemanticVADCoordinator(channel_name=channel_name)
    tripwire_event = asyncio.Event()

    async def mock_vad_silence() -> AsyncGenerator[float, None]:
        silence = 0.0
        while silence < 300.0:
            await asyncio.sleep(0.02)
            silence += 20.0
            yield silence

    # Publish syntactically closed partial
    producer.publish_partial("Deploy update.", is_final=False)

    async def _run():
        transcript, is_authoritative = await coordinator.monitor_utterance(
            mock_vad_silence(),
            on_speculative_tripwire=tripwire_event,
        )

        assert transcript == "Deploy update."
        assert not is_authoritative
        assert tripwire_event.is_set()

    try:
        asyncio.run(_run())
    finally:
        coordinator.close()
