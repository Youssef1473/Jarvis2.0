"""Pytest fixtures for hermetic test isolation in Jarvis 2.0."""
from __future__ import annotations

import pytest

from jarvis.core.security_boundaries import DualKeyApprovalManager, TurnTaintTracker


@pytest.fixture(autouse=True)
def _isolate_turn_and_security_state():
    """Isolates taint state and approval handlers across every test execution."""
    TurnTaintTracker.get_instance().reset_turn()
    DualKeyApprovalManager.get_instance().reset()
    yield
    TurnTaintTracker.get_instance().reset_turn()
    DualKeyApprovalManager.get_instance().reset()
