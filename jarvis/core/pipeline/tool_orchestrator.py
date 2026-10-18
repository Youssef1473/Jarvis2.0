"""Tool Orchestrator and Pipeline Execution Director for Jarvis 2.0.

Governs multi-step tool-use cycles, loop detection, taint tracking, and acoustic interim bridges.
"""
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from jarvis.skills.base import SkillRegistry
from jarvis.core.security_boundaries import TurnTaintTracker, SecurityBoundaryViolation
from jarvis.core.pipeline.interim_bridge import InterimSpeechBridge

logger = logging.getLogger("Jarvis.Pipeline.ToolOrchestrator")


class ToolOrchestrator:
    """Coordinates tool execution with cycle guards, security gates, and interim acoustic pacing."""

    def __init__(
        self,
        interim_bridge: Optional[InterimSpeechBridge] = None,
        taint_tracker: Optional[TurnTaintTracker] = None,
        max_identical_cycles: int = 2,
        on_status_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.interim_bridge = interim_bridge or InterimSpeechBridge()
        self.taint_tracker = taint_tracker or TurnTaintTracker.get_instance()
        self.max_identical_cycles = max_identical_cycles
        self.on_status_callback = on_status_callback
        self._history: List[Tuple[str, str]] = []

    def reset(self) -> None:
        """Resets trajectory history for a new conversation turn."""
        self._history.clear()
        if self.interim_bridge:
            self.interim_bridge.cancel_pending()

    def execute_step(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        step_index: int = 1,
        dual_key_confirmed: bool = False,
    ) -> Any:
        """Executes a single tool step with security checks and interim acoustic bridge."""
        arg_signature = json.dumps(arguments or {}, sort_keys=True)
        call_key = (tool_name, arg_signature)

        # 1. Cycle detection: Prevent recursive tool spin-loops
        prior_matches = [k for k in self._history if k == call_key]
        if len(prior_matches) >= self.max_identical_cycles:
            logger.warning("Recursive spin-loop detected on tool '%s' with identical arguments.", tool_name)
            return f"Loop cycle detected on tool '{tool_name}' with identical arguments. Halting recursion."

        self._history.append(call_key)

        # 2. Dual-Key Safety Gate & Taint Tracking
        risk_level = SkillRegistry.get_risk_level(tool_name)
        try:
            self.taint_tracker.check_tool_execution(
                tool_name, arguments, risk_level=risk_level, dual_key_confirmed=dual_key_confirmed
            )
        except SecurityBoundaryViolation as sec_err:
            logger.error("SecurityBoundaryViolation in tool execution: %s", sec_err)
            return f"Error: {sec_err}"

        # 3. Arm Acoustic Interim Bridge
        if self.interim_bridge:
            self.interim_bridge.on_tool_start(tool_name, step_index=step_index)

        # Check cancellation before physical actuation
        if getattr(self, "cancel_token", None) and getattr(self.cancel_token, "is_cancelled", False):
            logger.warning("Tool execution '%s' aborted by cancellation token.", tool_name)
            return "Action cancelled: Operation was aborted by user interrupt."

        # Broadcast tool start event
        if getattr(self, "on_status_callback", None):
            try:
                self.on_status_callback({
                    "type": "tool_status",
                    "tool": tool_name,
                    "status": "running",
                    "step": step_index,
                })
            except Exception as e:
                logger.debug("Tool status callback notice (start): %s", e)

        try:
            # 4. Physical Execution via SkillRegistry
            result = SkillRegistry.execute(tool_name, **arguments)
            return result
        except Exception as err:
            logger.error("Exception during tool execution '%s': %s", tool_name, err)
            return f"Error executing tool '{tool_name}': {err}"
        finally:
            # 5. Disarm Interim Bridge on completion
            if self.interim_bridge:
                self.interim_bridge.on_tool_end()
            if getattr(self, "on_status_callback", None):
                try:
                    self.on_status_callback({
                        "type": "tool_status",
                        "tool": tool_name,
                        "status": "completed",
                        "step": step_index,
                    })
                except Exception as e:
                    logger.debug("Tool status callback notice (end): %s", e)
