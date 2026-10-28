"""Tool Orchestrator and Pipeline Execution Director for Jarvis 2.0.

Governs multi-step tool-use cycles, loop detection, taint tracking, and acoustic interim bridges.
"""
import asyncio
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jarvis.core.pipeline.interim_bridge import InterimSpeechBridge
from jarvis.core.security_boundaries import SecurityBoundaryViolation, TurnTaintTracker
from jarvis.skills.base import SkillRegistry, _active_orchestrator_scope

logger = logging.getLogger("Jarvis.Pipeline.ToolOrchestrator")


@dataclass
class OrchestratorStepResult:
    """Structured step execution result from ToolOrchestrator."""
    result: Any
    success: bool
    failure_category: str | None = None  # "cancelled", "loop_cycle", "orchestrator_denied", "exception"
    error_message: str | None = None


class ToolOrchestrator:
    """Coordinates tool execution with cycle guards, security gates, and interim acoustic pacing."""

    def __init__(
        self,
        interim_bridge: InterimSpeechBridge | None = None,
        taint_tracker: TurnTaintTracker | None = None,
        max_identical_cycles: int = 2,
        on_status_callback: Callable[[dict[str, Any]], None] | None = None,
        dual_track_coordinator: Any | None = None,
    ):
        self.interim_bridge = interim_bridge or InterimSpeechBridge()
        self.taint_tracker = taint_tracker or TurnTaintTracker.get_instance()
        self.max_identical_cycles = max_identical_cycles
        self.on_status_callback = on_status_callback
        self.dual_track_coordinator = dual_track_coordinator
        self._history: list[tuple[str, str]] = []

    def reset(self) -> None:
        """Resets trajectory history for a new conversation turn."""
        self._history.clear()
        if self.interim_bridge:
            self.interim_bridge.cancel_pending()

    def reset_turn(self) -> None:
        """Alias for reset() to align with UnifiedRunner call conventions."""
        self.reset()

    def execute_step_with_status(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        step_index: int = 1,
        dual_key_confirmed: bool = False,
    ) -> OrchestratorStepResult:
        """Executes a single tool step with security checks, returning a structured OrchestratorStepResult."""
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
                "allow_unmanaged",
                "_allow_unmanaged",
                "automated",
                "_automated",
            )
        }
        arg_signature = json.dumps(sanitized_args, sort_keys=True)
        # Check cancellation before dispatch or approval evaluation
        if getattr(self, "cancel_token", None) and getattr(self.cancel_token, "is_cancelled", False):
            logger.warning("Tool execution '%s' aborted by cancellation token.", tool_name)
            return OrchestratorStepResult(
                result="Action cancelled: Operation was aborted by user interrupt.",
                success=False,
                failure_category="cancelled",
                error_message="Operation was aborted by user interrupt.",
            )

        call_key = (tool_name, arg_signature)

        # 1. Cycle detection: Prevent recursive tool spin-loops
        prior_matches = [k for k in self._history if k == call_key]
        if len(prior_matches) >= self.max_identical_cycles:
            logger.warning("Recursive spin-loop detected on tool '%s' with identical arguments.", tool_name)
            msg = f"Loop cycle detected on tool '{tool_name}' with identical arguments. Halting recursion."
            return OrchestratorStepResult(
                result=msg,
                success=False,
                failure_category="loop_cycle",
                error_message=f"Loop cycle detected on tool '{tool_name}' with identical arguments.",
            )

        self._history.append(call_key)

        # 2. Dual-Key Safety Gate & Taint Tracking
        risk_level = SkillRegistry.get_risk_level(tool_name)
        try:
            decision = self.taint_tracker.evaluate_tool_execution(
                tool_name, sanitized_args, risk_level=risk_level, dual_key_confirmed=dual_key_confirmed
            )
        except SecurityBoundaryViolation as sec_err:
            logger.error("SecurityBoundaryViolation in tool execution: %s", sec_err)
            return OrchestratorStepResult(
                result=f"Error: {sec_err}",
                success=False,
                failure_category="orchestrator_denied",
                error_message=str(sec_err),
            )

        if not decision.allowed:
            return OrchestratorStepResult(
                result=f"Error: Security gate denied execution of tool '{tool_name}'.",
                success=False,
                failure_category="orchestrator_denied",
                error_message=f"Security gate denied execution of tool '{tool_name}'.",
            )

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
                return OrchestratorStepResult(
                    result=cached,
                    success=True,
                )

        # 3. Arm Acoustic Interim Bridge
        if self.interim_bridge:
            self.interim_bridge.on_tool_start(tool_name, step_index=step_index)

        # Check cancellation before physical actuation
        if getattr(self, "cancel_token", None) and getattr(self.cancel_token, "is_cancelled", False):
            logger.warning("Tool execution '%s' aborted by cancellation token.", tool_name)
            approval_mgr.clear_active_authorized(ctx_token)
            return OrchestratorStepResult(
                result="Action cancelled: Operation was aborted by user interrupt.",
                success=False,
                failure_category="cancelled",
                error_message="Operation was aborted by user interrupt.",
            )

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

        # 4. Physical Execution via SkillRegistry
        exec_kwargs = dict(sanitized_args)
        tool_fn = SkillRegistry.get_tool(tool_name)
        if tool_fn:
            import inspect
            try:
                sig = inspect.signature(tool_fn)
                if "user_approved" in sig.parameters:
                    exec_kwargs["user_approved"] = True
                if "automated" in sig.parameters:
                    exec_kwargs["automated"] = True
            except Exception:
                pass

        current_turn_id = None
        try:
            from jarvis.core.security_boundaries import TurnTaintTracker
            current_turn_id = TurnTaintTracker.get_instance().get_current_turn_id()
        except Exception:
            pass

        from jarvis.skills.base import ExecutionCapability
        cap = ExecutionCapability.create(
            tool_name=tool_name,
            arguments=exec_kwargs,
            turn_id=current_turn_id,
            ttl_seconds=10.0,
        )
        orch_token = _active_orchestrator_scope.set(cap)
        try:
            skill_res = SkillRegistry.execute_with_status(tool_name, **exec_kwargs)
            if self.dual_track_coordinator is not None and getattr(self.dual_track_coordinator, "is_read_only_tool", lambda t: False)(tool_name):
                if skill_res.success and skill_res.result is not None:
                    self.dual_track_coordinator._set_cached_result(cache_key, skill_res.result)
            return OrchestratorStepResult(
                result=skill_res.result,
                success=skill_res.success,
                failure_category="exception" if not skill_res.success else None,
                error_message=skill_res.error_message,
            )
        except Exception as err:
            logger.error("Exception during tool execution '%s': %s", tool_name, err)
            return OrchestratorStepResult(
                result=f"Error executing tool '{tool_name}': {err}",
                success=False,
                failure_category="exception",
                error_message=str(err),
            )
        finally:
            _active_orchestrator_scope.reset(orch_token)
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

    def execute_step(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        step_index: int = 1,
        dual_key_confirmed: bool = False,
    ) -> Any:
        """Executes a single tool step with security checks and interim acoustic bridge."""
        return self.execute_step_with_status(
            tool_name=tool_name,
            arguments=arguments,
            step_index=step_index,
            dual_key_confirmed=dual_key_confirmed,
        ).result

    async def execute_step_async(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_query: str = "",
        step_index: int = 1,
        dual_key_confirmed: bool = False,
        milestone_phrase: str | None = None,
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
                "allow_unmanaged",
                "_allow_unmanaged",
                "automated",
                "_automated",
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
            # Security: AI-generated arguments must NEVER set internal authorization or unmanaged bypass flags
            for forbidden_param in ("allow_unmanaged", "user_approved", "automated"):
                exec_kwargs.pop(forbidden_param, None)

            tool_fn = SkillRegistry.get_tool(tool_name)
            if tool_fn:
                import inspect
                try:
                    sig = inspect.signature(tool_fn)
                    if "user_approved" in sig.parameters:
                        exec_kwargs["user_approved"] = True
                    if "automated" in sig.parameters:
                        exec_kwargs["automated"] = True
                except Exception:
                    pass

            def _orch_run():
                current_turn_id = None
                try:
                    from jarvis.core.security_boundaries import TurnTaintTracker
                    current_turn_id = TurnTaintTracker.get_instance().get_current_turn_id()
                except Exception:
                    pass
                from jarvis.skills.base import ExecutionCapability
                cap = ExecutionCapability.create(
                    tool_name=tool_name,
                    arguments=exec_kwargs,
                    turn_id=current_turn_id,
                    ttl_seconds=10.0,
                )
                orch_token = _active_orchestrator_scope.set(cap)
                try:
                    return SkillRegistry.execute(tool_name, **exec_kwargs)
                finally:
                    _active_orchestrator_scope.reset(orch_token)

            res = await self.dual_track_coordinator.execute_with_masking(
                user_query=user_query,
                tool_name=tool_name,
                tool_args=sanitized_args,
                tool_coro=_orch_run,
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
