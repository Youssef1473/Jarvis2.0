"""Cognitive router for Jarvis 2.0.

Decouples semantic prompt tier classification, model target resolution,
provider cascading, and voice-driven model switching from the monolithic
JarvisBrain orchestrator.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger("Jarvis.CognitiveRouter")

# Tools strictly categorized as local OS / device operations suitable for offline 0-token execution
OS_ACTION_TOOLS: FrozenSet[str] = frozenset({
    "launch_application",
    "close_application",
    "adjust_volume",
    "control_media",
    "minimize_all_windows",
    "lock_workstation",
    "get_system_vitals",
    "get_battery_status",
    "take_desktop_screenshot",
    "start_timer",
    "cancel_timer",
    "list_active_timers",
    "execute_protocol",
    "get_daily_briefing",
    "inspect_ram_hogs",
    "optimize_workstation",
    "play_youtube_media",
    "open_project",
    "open_user_folder",
    "take_quick_note",
    "read_recent_notes",
    "search_notes",
    "check_hardware_health",
    "start_hardware_watchdog",
    "stop_hardware_watchdog",
})

# Patterns that indicate self-engineering / self-evolution where the explicit model choice must never be overridden
SELF_BUILD_TRIGGERS: Tuple[str, ...] = (
    "add feature",
    "create feature",
    "build yourself",
    "create a skill",
    "add a skill",
    "create command",
    "add command",
    "teach yourself",
    "build a command",
    "create tool",
    "add tool",
    "new tool",
    "new skill",
    "evolve",
    "self build",
    "build a feature",
)

# Deep reasoning indicators
DEEP_INDICATORS: Tuple[str, ...] = (
    "code",
    "python",
    "script",
    "debug",
    "refactor",
    "git",
    "function",
    "build",
    "develop",
    "codebase",
    "repo",
    "folder",
    "patch",
    "skill",
    "yourself",
    "write an email",
    "draft",
    "compose",
    "summarize",
    "analyze",
    "explain in detail",
    "plan",
    "research",
    "compare",
    "why does",
    "how do i implement",
    "architecture",
    "solve",
    "math",
    "calculate",
    "timetable",
    "calendar",
    "schedule",
    "syllabus",
    "exam",
    "classes",
    "review",
    "investigate",
    "audit",
    "test suite",
)

# Fast conversational / reflex indicators
FAST_INDICATORS: Tuple[str, ...] = (
    "volume",
    "mute",
    "unmute",
    "sound",
    "louder",
    "quieter",
    "battery",
    "charge",
    "power",
    "time",
    "date",
    "day",
    "today",
    "weather",
    "forecast",
    "temperature",
    "clock",
    "open",
    "launch",
    "start",
    "close",
    "kill",
    "exit",
    "play",
    "pause",
    "skip",
    "next song",
    "previous song",
    "hello",
    "hi",
    "hey",
    "good morning",
    "good evening",
    "good afternoon",
    "how are you",
    "who are you",
    "status",
    "are you there",
    "thank you",
    "thanks",
    "done",
    "test",
    "ping",
)


class CognitiveRouter:
    """Coordinates intent classification, provider dispatch, and model failover cascades."""

    def __init__(self, default_model: str = "gemini-3.8-flash-medium") -> None:
        self.default_model = default_model

    def classify_query_tier(self, query: str, has_screen_intent: bool = False) -> str:
        """Determines whether a query is suitable for Fast LPU Tier vs Deep Frontier Tier.

        Returns:
            'fast' or 'deep'
        """
        q = (query or "").lower().strip()

        # 1. Screen Vision is ALWAYS Deep Tier
        if has_screen_intent:
            return "deep"

        # 2. Questions starting with explanatory or analytical words -> Deep Tier
        if any(q.startswith(p) for p in ("why ", "how come ", "explain ", "what causes ", "compare ", "analyze ")):
            return "deep"

        # 3. Explicit deep indicators (coding, scripting, planning, technical troubleshooting, self-engineering)
        if any(re.search(r"\b" + re.escape(ind) + r"\b", q) if len(ind) <= 4 else ind in q for ind in DEEP_INDICATORS):
            return "deep"

        # Long prompts (> 18 words) or multi-sentence prompts require deeper reasoning
        if len(q.split()) > 18 or len(re.findall(r"[.!?]", q)) > 1:
            return "deep"

        # 4. Fast Tier: Routine OS commands, device queries, volume, time, media, greetings
        if any(re.search(r"\b" + re.escape(ind) + r"\b", q) if len(ind) <= 4 else ind in q for ind in FAST_INDICATORS):
            return "fast"

        # Default to deep tier if ambiguous to preserve maximum intellect
        return "deep"

    def resolve_target_model(
        self,
        user_text: str,
        active_task_mode: str,
        active_configured_model: str,
        antigravity_bridge: Any,
    ) -> str:
        """Resolves target model candidate factoring in self-evolution locks, task mode,
        and quota exhaustion states across Claude Sonnet and Gemini Flash pools.
        """
        text_lower = (user_text or "").lower()

        # 1. When Sir asks to add a feature or build yourself, NEVER automatically change model on your own.
        # Strictly use the model Sir has chosen/active.
        if any(t in text_lower for t in SELF_BUILD_TRIGGERS):
            target_model = active_configured_model
        else:
            if antigravity_bridge and hasattr(antigravity_bridge, "classify_prompt_tier"):
                target_model = antigravity_bridge.classify_prompt_tier(user_text, task_mode=active_task_mode)
            else:
                target_model = active_configured_model or self.default_model

        # 2. If target model is Claude Sonnet and Claude is token-exhausted, switch based on task complexity
        if "claude" in target_model and antigravity_bridge and hasattr(antigravity_bridge, "is_claude_exhausted"):
            if antigravity_bridge.is_claude_exhausted():
                is_complex = False
                if hasattr(antigravity_bridge, "is_task_complex"):
                    is_complex = antigravity_bridge.is_task_complex(user_text, task_mode=active_task_mode)
                fallback_model = "gemini-3.8-flash-high" if is_complex else "gemini-3.8-flash-medium"
                logger.info(
                    "Claude Sonnet tokens exhausted. Automatically switching to %s (task complexity: %s), Sir.",
                    fallback_model,
                    "high" if is_complex else "moderate",
                )
                target_model = fallback_model

        # 3. If target model is in unified Gemini Flash pool and Flash is exhausted, failover to Claude Sonnet
        if "gemini-3.8-flash" in target_model and antigravity_bridge and hasattr(antigravity_bridge, "is_gemini_flash_exhausted"):
            if antigravity_bridge.is_gemini_flash_exhausted():
                claude_exhausted = False
                if hasattr(antigravity_bridge, "is_claude_exhausted"):
                    claude_exhausted = antigravity_bridge.is_claude_exhausted()
                if not claude_exhausted:
                    logger.info(
                        "Unified Gemini 3.8 Flash pool exhausted. Automatically switching across quota boundary to Claude Sonnet 4.6, Sir."
                    )
                    target_model = "claude-sonnet-4-6"

        return target_model

    def parse_model_switch_command(self, norm_text: str) -> Optional[Tuple[str, Optional[str]]]:
        """Detects voice commands instructing Jarvis to change model or provider.

        Returns:
            Tuple of (action_type, parameter) or None
            - ('hybrid', None)
            - ('auto', None)
            - ('github', None)
            - ('model', model_name)
        """
        # Hybrid token-shield mode
        hybrid_match = re.match(
            r"^(?:switch to|use|activate|enable|turn on)\s+(?:hybrid|hybrid mode|token shield|token saver)$",
            norm_text,
            re.IGNORECASE,
        )
        if hybrid_match:
            return ("hybrid", None)

        # Dynamic model tiering (auto)
        auto_match = re.match(
            r"^(?:switch to|use|activate|set model to|enable)\s+(?:auto|automatic|dynamic)(?:\s+(?:tiering|mode|model))?$",
            norm_text,
            re.IGNORECASE,
        )
        if auto_match:
            return ("auto", None)

        # GitHub Copilot CLI
        github_switch_match = re.match(
            r"^(?:switch to|use|activate|set model to|enable)\s+(?:github(?:\s+copilot)?|copilot|gh)$",
            norm_text,
            re.IGNORECASE,
        )
        if github_switch_match:
            return ("github", None)

        # Specific models: pro, flash, claude, sonnet, opus, gemini, argon
        switch_match = re.match(
            r"^(?:switch to|use|activate|set model to|enable)\s+(pro|flash|claude|sonnet|opus|gemini|argon(?:\s+4)?|3\.8|3\.1|4)(?:\s+(low|medium|med|high))?(?:\s+(?:mode|model))?$",
            norm_text,
            re.IGNORECASE,
        )
        if switch_match:
            base_model = switch_match.group(1).lower().strip()
            variant = (switch_match.group(2) or "").lower().strip()

            if base_model in ("pro", "3.1", "gemini-pro"):
                new_model = "gemini-3.8-flash-high"
            elif base_model.startswith("argon") or base_model == "4":
                eff = "medium" if variant in ("medium", "med") else ("low" if variant == "low" else "high")
                new_model = f"gemini-4-argon-{eff}"
            elif base_model in ("flash", "3.8", "gemini-flash", "gemini"):
                if variant in ("high",):
                    new_model = "gemini-3.8-flash-high"
                elif variant in ("medium", "med"):
                    new_model = "gemini-3.8-flash-medium"
                else:
                    new_model = "gemini-3.8-flash-low"
            elif base_model in ("claude", "sonnet"):
                new_model = "claude-sonnet-4-6"
            elif base_model in ("opus",):
                new_model = "claude-opus-4-6-thinking"
            else:
                new_model = "gemini-3.8-flash-medium"
            return ("model", new_model)

        return None

    def determine_hybrid_route(
        self,
        user_text: str,
        active_tool_names: FrozenSet[str],
        has_screen_intent: bool,
        active_task_mode: str,
        has_antigravity: bool,
        has_ollama: bool,
        has_groq: bool,
        has_gemini: bool,
        has_copilot: bool,
    ) -> str:
        """Determines the optimal execution route under Hybrid / Auto mode.

        Returns one of:
            'gemini_vision', 'ollama_os', 'antigravity_deep', 'groq_reflex',
            'antigravity_default', 'copilot_default', 'ollama_default', 'gemini_default'
        """
        # 1. Screen Vision queries -> Route to Gemini or local Ollama vision
        if has_screen_intent and has_gemini:
            return "gemini_vision"

        # 2. Local OS & workstation device actions -> Route to local Ollama (0ms network latency, 0 tokens)
        if any(t in active_tool_names for t in OS_ACTION_TOOLS) and has_ollama:
            return "ollama_os"

        # 3. Classify query complexity: Deep/Code tasks vs Fast Conversational Reflex
        query_tier = self.classify_query_tier(user_text, has_screen_intent=has_screen_intent)

        # Route Deep reasoning, code, academic schedules, planning to Google AI Pro Apex
        if (query_tier == "deep" or active_task_mode == "code") and has_antigravity:
            return "antigravity_deep"

        # 4. Fast Reflex Tier: Casual speech, short chit-chat -> Route to Groq LPU
        if has_groq:
            return "groq_reflex"

        # 5. Fallback cascade
        if has_antigravity:
            return "antigravity_default"
        if has_copilot:
            return "copilot_default"
        if has_ollama:
            return "ollama_default"
        if has_gemini:
            return "gemini_default"

        return "none"
