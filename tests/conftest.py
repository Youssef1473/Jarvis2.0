"""Pytest fixtures for hermetic test isolation in Jarvis 2.0."""
from __future__ import annotations

import pytest

from jarvis.core.cancellation import reset_global_barge_in
from jarvis.core.security_boundaries import DualKeyApprovalManager, TurnTaintTracker
from jarvis.skills.base import SkillRegistry

# Certify test tools exclusively within the test harness
TEST_HARNESS_READ_ONLY_TOOLS = (
    "_test_cached_ro_tool",
    "test_conv_read",
    "fast_readonly_tool",
    "slow_readonly_tool",
    "scoped_read_tool",
    "epoch_sensitive_read",
    "file_reader_tool",
    "read_file_tool",
    "git_status_inspection",
    "git_diff_inspection",
)
for _t_name in TEST_HARNESS_READ_ONLY_TOOLS:
    SkillRegistry.certify_test_read_only_tool(_t_name)


@pytest.fixture(autouse=True)
def _isolate_turn_and_security_state():
    """Isolates taint state, approval handlers, and barge-in state across every test execution."""
    TurnTaintTracker.get_instance().reset_turn()
    DualKeyApprovalManager.get_instance().reset()
    reset_global_barge_in()
    yield
    TurnTaintTracker.get_instance().reset_turn()
    DualKeyApprovalManager.get_instance().reset()
    reset_global_barge_in()
