"""Tool Orchestrator and Pipeline Execution Director for Jarvis 2.0.

Governs multi-step tool-use cycles, loop detection, taint tracking, and acoustic interim bridges.
"""
import asyncio
import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from jarvis.core.pipeline.dual_track import DualTrackCoordinator

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
        dual_track_coordinator: Optional[Any] = None,
    ):
        self.interim_bridge = interim_bridge or InterimSpeechBridge()
        self.taint_tracker = taint_tracker or TurnTaintTracker.get_instance()
        self.max_identical_cycles = max_identical_cycles
        self.on_status_callback = on_status_callback
        self.dual_track_coordinator = dual_track_coordinator
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
        # Ensure active turn taint state exists in current execution context
        self.taint_tracker._get_context_state(create_if_missing=True)

        # Strip model-injected authorization fields from arguments to prevent prompt injection bypasses
        sanitized_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in (
                "dual_key_confirmed",
                "user_approved",
                "confirmed",
                "_dual_key_token",
                "allow_uncontained",
                "_allow_uncontained",
            )
        }
        arg_signature = json.dumps(sanitized_args, sort_keys=True)
        # Check cancellation before dispatch or approval evaluation
        if getattr(self, "cancel_token", None) and getattr(self.cancel_token, "is_cancelled", False):
            logger.warning("Tool execution '%s' aborted by cancellation token.", tool_name)
            return "Action cancelled: Operation was aborted by user interrupt."

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
            decision = self.taint_tracker.evaluate_tool_execution(
                tool_name, sanitized_args, risk_level=risk_level, dual_key_confirmed=dual_key_confirmed
            )
        except SecurityBoundaryViolation as sec_err:
            logger.error("SecurityBoundaryViolation in tool execution: %s", sec_err)
            return f"Error: {sec_err}"

        if not decision.allowed:
            return f"Error: Security gate denied execution of tool '{tool_name}'."

        from jarvis.core.security_boundaries import DualKeyApprovalManager
        approval_mgr = DualKeyApprovalManager.get_instance()
        ctx_token = None
        if decision.grant is not None:
            ctx_token = approval_mgr.bind_active_grant(decision.grant)
        else:
            ctx_token = approval_mgr.bind_disarmed_context()

        # Check read-only fast cache if DualTrackCoordinator is present
        cache_key = f"{tool_name}:{arg_signature}"
        if self.dual_track_coordinator is not None and getattr(self.dual_track_coordinator, "is_read_only_tool", lambda t: False)(tool_name):
            cached = self.dual_track_coordinator._get_cached_result(cache_key)
            if cached is not None:
                logger.debug("RO Cache Hit in ToolOrchestrator for '%s'", tool_name)
                approval_mgr.clear_active_authorized(ctx_token)
                return cached

        # 3. Arm Acoustic Interim Bridge
        if self.interim_bridge:
            self.interim_bridge.on_tool_start(tool_name, step_index=step_index)

        # Check cancellation before physical actuation
        if getattr(self, "cancel_token", None) and getattr(self.cancel_token, "is_cancelled", False):
            logger.warning("Tool execution '%s' aborted by cancellation token.", tool_name)
            approval_mgr.clear_active_authorized(ctx_token)
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
            exec_kwargs = dict(sanitized_args)
            tool_fn = SkillRegistry.get_tool(tool_name)
            if tool_fn:
                import inspect
                try:
                    sig = inspect.signature(tool_fn)
                    if "user_approved" in sig.parameters:
                        exec_kwargs["user_approved"] = True
                except Exception:
                    pass

            result = SkillRegistry.execute(tool_name, **exec_kwargs)
            if self.dual_track_coordinator is not None and getattr(self.dual_track_coordinator, "is_read_only_tool", lambda t: False)(tool_name):
                if result is not None and not str(result).startswith("Error"):
                    self.dual_track_coordinator._set_cached_result(cache_key, result)
            return result
        except Exception as err:
            logger.error("Exception during tool execution '%s': %s", tool_name, err)
            return f"Error executing tool '{tool_name}': {err}"
        finally:
            from jarvis.core.security_boundaries import DualKeyApprovalManager
            DualKeyApprovalManager.get_instance().clear_active_authorized(ctx_token)
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

    async def execute_step_async(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        user_query: str = "",
        step_index: int = 1,
        dual_key_confirmed: bool = False,
        milestone_phrase: Optional[str] = None,
    ) -> Any:
        """Asynchronously executes a tool step using DualTrackCoordinator for masking and gating."""
        # Ensure active turn taint state exists in caller context before spawning worker threads
        self.taint_tracker._get_context_state(create_if_missing=True)

        if self.dual_track_coordinator is None:
            return await asyncio.to_thread(
                self.execute_step,
                tool_name,
                arguments,
                step_index=step_index,
                dual_key_confirmed=dual_key_confirmed,
            )

        # Strip model-injected authorization fields from arguments to prevent prompt injection bypasses
        sanitized_args = {
            k: v for k, v in (arguments or {}).items()
            if k not in (
                "dual_key_confirmed",
                "user_approved",
                "confirmed",
                "_dual_key_token",
                "allow_uncontained",
                "_allow_uncontained",
            )
        }
        arg_signature = json.dumps(sanitized_args, sort_keys=True)
        # Check cancellation before dispatch or approval evaluation
        if getattr(self, "cancel_token", None) and getattr(self.cancel_token, "is_cancelled", False):
            logger.warning("Tool execution '%s' aborted by cancellation token.", tool_name)
            return "Action cancelled: Operation was aborted by user interrupt."

        call_key = (tool_name, arg_signature)

        # 1. Cycle detection
        prior_matches = [k for k in self._history if k == call_key]
        if len(prior_matches) >= self.max_identical_cycles:
            logger.warning("Recursive spin-loop detected on tool '%s' with identical arguments.", tool_name)
            return f"Loop cycle detected on tool '{tool_name}' with identical arguments. Halting recursion."

        self._history.append(call_key)

        # 2. Dual-Key Safety Gate & Taint Tracking
        risk_level = SkillRegistry.get_risk_level(tool_name)
        from jarvis.core.security_boundaries import DualKeyApprovalManager
        approval_mgr = DualKeyApprovalManager.get_instance()
        cancel_event = threading.Event()
        if getattr(self, "cancel_token", None) and getattr(self.cancel_token, "is_cancelled", False):
            cancel_event.set()

        decision = None
        try:
            decision = await asyncio.to_thread(
                self.taint_tracker.evaluate_tool_execution,
                tool_name,
                sanitized_args,
                risk_level=risk_level,
                dual_key_confirmed=dual_key_confirmed,
                cancel_event=cancel_event,
            )
        except asyncio.CancelledError:
            approval_mgr.cancel_approval_request(cancel_event)
            if decision is not None and getattr(decision, "grant", None) is not None:
                approval_mgr.revoke_grant(decision.grant)
            raise
        except SecurityBoundaryViolation as sec_err:
            logger.error("SecurityBoundaryViolation in tool execution: %s", sec_err)
            return f"Error: {sec_err}"

        if not decision.allowed:
            return f"Error: Security gate denied execution of tool '{tool_name}'."

        ctx_token = None
        if decision.grant is not None:
            ctx_token = approval_mgr.bind_active_grant(decision.grant)
        else:
            ctx_token = approval_mgr.bind_disarmed_context()

        try:
            # 3. Execution routing
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

            exec_kwargs = dict(sanitized_args)
            tool_fn = SkillRegistry.get_tool(tool_name)
            if tool_fn:
                import inspect
                try:
                    sig = inspect.signature(tool_fn)
                    if "user_approved" in sig.parameters:
                        exec_kwargs["user_approved"] = True
                except Exception:
                    pass

            res = await self.dual_track_coordinator.execute_with_masking(
                user_query=user_query,
                tool_name=tool_name,
                tool_args=sanitized_args,
                tool_coro=lambda: SkillRegistry.execute(tool_name, **exec_kwargs),
                milestone_phrase=milestone_phrase,
            )
            return res
        except Exception as err:
            logger.error("Exception during async tool execution '%s': %s", tool_name, err)
            return f"Error executing tool '{tool_name}': {err}"
        finally:
            if ctx_token is not None:
                approval_mgr.clear_active_authorized(ctx_token)
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
