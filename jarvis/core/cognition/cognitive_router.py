"""Cognitive router for Jarvis 2.0.

Decouples semantic prompt tier classification, model target resolution,
provider cascading, and voice-driven model switching from the monolithic
JarvisBrain orchestrator.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.CognitiveRouter")

# Canonical multi-provider candidate model pools (immutable)
GEMINI_CANDIDATE_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
)

GROQ_CANDIDATE_MODELS: tuple[str, ...] = (
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
)

ANTIGRAVITY_CANDIDATE_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
    "gemini-4-argon-high",
)

OPENROUTER_FREE_MODELS: tuple[str, ...] = (
    "nvidia/nemotron-3.5-super-vl:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "cohere/north-mini-code:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
)

CODE_TOOL_NAMES: frozenset[str] = frozenset({
    "list_directory",
    "read_file",
    "read_multiple_files",
    "search_codebase",
    "search_everything",
    "write_file",
    "patch_file",
    "git_diff",
    "git_status",
    "execute_powershell",
    "run_project_tests",
    "web_search",
    "create_or_update_custom_skill",
    "list_custom_skills",
    "delete_custom_skill",
    "inspect_screen",
    "see_screen",
})

READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset({
    "get_current_time_and_date",
    "list_directory",
    "read_file",
    "read_multiple_files",
    "search_codebase",
    "search_everything",
    "get_weather",
    "get_battery_status",
    "list_running_applications",
    "get_active_window",
    "check_unread_emails",
    "read_email_details",
    "get_user_preferences",
    "recall_fact",
    "recall_past_conversation",
    "recall_episodic_lessons",
    "list_pending_tasks",
    "list_calendar_events",
    "get_system_vitals",
    "get_tech_news_headlines",
    "inspect_ram_hogs",
    "inspect_screen",
    "see_screen",
    "take_desktop_screenshot",
    "list_photos",
    "list_open_browser_tabs",
    "list_active_timers",
    "get_voice_settings",
    "list_audio_devices",
    "inspect_file_locks",
    "inspect_port_holders",
    "audit_vram_and_thermal_budget",
    "get_recent_workspace_mutations",
    "list_compiled_reflex_macros",
    "list_custom_skills",
    "jarvis_doctor",
    "profile_runtime",
    "audit_memory_heap",
    "verify_learned_habits",
    "web_search",
})

TOOL_INTENT_GROUPS: tuple[tuple[tuple[str, ...], set[str]], ...] = (
    (("add feature", "create feature", "build yourself", "create a skill", "add a skill", "create command", "add command", "build a command", "create tool", "add tool", "new tool", "new skill", "evolve", "self build", "teach yourself", "learn to", "learn this skill", "learn a skill", "learn skill", "custom skill", "edit yourself", "modify yourself", "enhance yourself", "optimize yourself", "improve yourself", "upgrade yourself", "inspect your code", "check your code", "look at your code", "fix yourself", "refactor yourself", "your codebase", "your files", "jarvis 2.0", "jarvis_2.0", "self edit", "self optimize", "self improve", "self enhance"), {"read_file", "read_multiple_files", "patch_file", "write_file", "search_codebase", "list_directory", "execute_powershell", "run_project_tests", "git_diff", "git_status", "create_or_update_custom_skill", "list_custom_skills", "delete_custom_skill"}),
    (("brave", "browser tab", "new tab", "open tab", "chrome tab", "edge tab", "brave tab", "close tab", "terminate tab", "close browser tab", "kill tab", "terminate a tab", "close a tab", "close the tab", "terminate the tab", "terminate a tab on my browser", "close current tab", "terminate current tab"), {"open_browser_tab", "close_browser_tab", "launch_application", "list_open_browser_tabs"}),
    (("focus window", "switch to window", "bring to front", "focus app", "switch window"), {"focus_window", "list_running_applications"}),
    (("hotkey", "press key", "shortcut", "press enter", "press escape", "press ctrl", "keyboard shortcut", "theater mode", "theatre mode", "cinema mode", "fullscreen", "full screen"), {"press_hotkey", "control_media"}),
    (("type text", "type into", "write text"), {"type_text"}),
    (("volume", "mute", "unmute", "sound", "louder", "quieter"), {"adjust_volume"}),
    (("play", "pause", "skip", "next song", "previous song", "media", "spotify"), {"control_media"}),
    (("show desktop", "minimize all"), {"minimize_all_windows"}),
    (("battery", "charging", "charge level"), {"get_battery_status"}),
    (("time", "date", "day", "clock"), {"get_current_time_and_date"}),
    (("weather", "temperature", "forecast"), {"get_weather"}),
    (("remind", "reminder", "to-do", "todo", "task"), {"add_task_or_reminder", "list_pending_tasks", "complete_task"}),
    (("calendar", "meeting", "appointment", "schedule", "starkboard", "stark board", "cancel event", "dismiss event", "no meeting", "don't have a meeting", "dont have a meeting", "timetable", "classes", "class", "upcoming", "what do i have", "whats on my", "what's on my", "delete calendar", "remove calendar", "clear calendar", "clear schedule"), {"schedule_calendar_event", "list_calendar_events", "cancel_calendar_event", "delete_calendar"}),
    (("remember", "save this", "store this", "commit", "commit to memory", "commit this", "commit that", "add to memory", "save to memory", "store in memory", "in your memory", "to memory"), {"remember_fact", "recall_fact"}),
    (("recall", "previous conversation", "past conversation", "we discussed", "you said earlier"), {"recall_fact", "recall_past_conversation"}),
    (("lesson", "mistake", "error history", "past failure", "avoid mistake", "what went wrong", "reflection"), {"recall_episodic_lessons", "record_episodic_lesson"}),
    (("email", "inbox", "unread mail", "gmail", "mail", "send email", "search email", "read email", "draft email"), {"check_unread_emails", "search_emails", "read_email_details", "send_email"}),
    (("news", "headlines"), {"web_search", "get_tech_news_headlines"}),
    (("search", "latest", "current", "look up", "lookup"), {"web_search", "get_wikipedia_summary"}),
    (("http://", "https://", "webpage", "website", "open url"), {"browse_and_read_webpage", "open_url_in_desktop_browser", "capture_webpage_screenshot"}),
    (("open", "launch", "start", "software", "program", "browser", "app"), {"launch_application"}),
    (("close", "terminate", "exit app", "kill app", "quit app"), {"close_application"}),
    (("see my screen", "see the screen", "see screen", "look at my screen", "look at the screen", "inspect my screen", "inspect screen", "what's on my screen", "what is on my screen", "describe my screen", "read my screen", "read the screen", "screen vision"), {"inspect_screen", "see_screen", "get_active_window"}),
    (("running", "open apps", "open windows", "active window", "focused window", "what am i looking at", "on my screen", "on screen", "my screen", "what is open"), {"get_active_window", "list_running_applications", "inspect_screen"}),
    (("vitals", "cpu", "ram", "memory usage", "hardware", "system status", "specs", "gpu"), {"get_system_vitals", "get_battery_status"}),
    (("clipboard", "copy", "copied", "paste"), {"read_clipboard", "copy_to_clipboard"}),
    (("lock", "lock pc", "lock computer", "lock workstation"), {"lock_workstation"}),
    (("screenshot", "snapshot", "capture screen"), {"take_desktop_screenshot", "inspect_screen", "see_screen"}),
    (("timer", "countdown", "stopwatch", "alarm", "how much time left"), {"start_timer", "cancel_timer", "list_active_timers"}),
    (("protocol", "work protocol", "lockdown", "relax protocol", "night protocol", "coding mode", "focus mode"), {"execute_protocol"}),
    (("briefing", "morning briefing", "daily briefing", "morning protocol", "good morning"), {"get_daily_briefing"}),
    (("ram hog", "memory hog", "eating ram", "eating", "who is eating", "top memory", "high memory", "memory consumer"), {"inspect_ram_hogs"}),
    (("optimize", "cleanup", "clean pc", "clean workstation", "flush dns", "speed up"), {"optimize_workstation", "inspect_ram_hogs"}),
    (("youtube", "play song", "play video", "play music", "soundtrack"), {"play_youtube_media"}),
    (("open project", "launch project", "open repo"), {"open_project"}),
    (("open folder", "downloads folder", "screenshots folder", "documents folder", "desktop folder"), {"open_user_folder"}),
    (("note", "notes", "take note", "take a note", "note down", "write down", "jot down", "save note", "record note", "my notes", "read notes", "search notes"), {"take_quick_note", "read_recent_notes", "search_notes"}),
    (("hardware watchdog", "thermal monitor", "watchdog", "system health check", "hardware health", "check hardware"), {"check_hardware_health", "start_hardware_watchdog", "stop_hardware_watchdog"}),
    (("deep research", "research topic", "investigate", "in-depth research", "compile report"), {"deep_research", "web_search"}),
    (("pdf", "document", "documents", "staged", "import pdf", "uploaded", "that pdf", "the pdf", "syllabus", "intake", "file explorer", "search document", "search documents", "read document", "archived document", "document archive", "my documents"), {"process_staged_document", "list_staged_documents", "search_archived_documents", "read_archived_document"}),
    (("voice profile", "change voice", "switch voice", "voice style", "classic voice", "friday voice", "speed up voice", "speak faster", "speak slower", "audio device", "sound device", "headphones", "switch speaker"), {"set_voice_profile", "set_speech_rate", "get_voice_settings", "list_audio_devices", "set_audio_output_device"}),
    (("find file", "search file", "where is", "look for file", "locate file", "search desktop", "find my file", "everything search", "search everything", "find file everywhere", "search all drives", "search hard drive", "search disk", "locate file everywhere", "instant search"), {"search_everything", "search_desktop_files_fast"}),
    (("preference", "user preference", "my preference", "call me", "i prefer", "always use", "default to", "my profile", "my habit", "remember", "remember that", "remember this", "remember it", "commit to memory", "commit that", "add to memory", "add that", "save to memory", "store in memory"), {"update_user_preference", "get_user_preferences", "remember_fact", "recall_fact"}),
    (("diagnose test", "fix test", "test failure", "traceback", "debug test", "failing test", "pytest failed", "syntax error in", "patch function", "surgical patch", "apply patch"), {"diagnose_test_failure_ast", "apply_and_verify_surgical_patch"}),
    (("file lock", "locked file", "who is using file", "who has file open", "restart manager", "release lock", "unlock file", "port in use", "port holder", "who is on port"), {"inspect_file_locks", "inspect_port_holders", "release_file_lock"}),
    (("vram", "gpu memory", "vram usage", "thermal budget", "gpu temp", "gpu temperature", "nvml", "dxgi"), {"audit_vram_and_thermal_budget", "get_system_vitals"}),
    (("workspace mutation", "recent file changes", "what changed in workspace", "usn journal", "file mutations", "changed files"), {"get_recent_workspace_mutations", "record_workspace_mutation"}),
    (("workflow reflex", "compile reflex", "reflex macro", "record reflex", "macro workflow", "list macros", "execute macro"), {"compile_workflow_reflex", "execute_workflow_reflex", "list_compiled_reflex_macros", "delete_compiled_reflex_macro"}),
    (("click element", "click web", "click on page", "type into web", "type in browser", "semantic click", "semantic type"), {"click_web_element_semantic", "type_into_web_element_semantic", "open_browser_tab"}),
    (("job sandbox", "sandboxed process", "run sandboxed", "isolated process"), {"run_job_sandboxed_process"}),
    (("photo", "photos", "picture", "pictures", "image file", "photo vault", "search photos", "my photos"), {"ingest_photo", "search_photos_semantic", "list_recent_photos", "describe_photo"}),
)

# Tools strictly categorized as local OS / device operations suitable for offline 0-token execution
OS_ACTION_TOOLS: frozenset[str] = frozenset({
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
SELF_BUILD_TRIGGERS: tuple[str, ...] = (
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
DEEP_INDICATORS: tuple[str, ...] = (
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
FAST_INDICATORS: tuple[str, ...] = (
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
        if (
            "claude" in target_model
            and antigravity_bridge
            and hasattr(antigravity_bridge, "is_claude_exhausted")
            and antigravity_bridge.is_claude_exhausted()
        ):
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
        if (
            "gemini-3.8-flash" in target_model
            and antigravity_bridge
            and hasattr(antigravity_bridge, "is_gemini_flash_exhausted")
            and antigravity_bridge.is_gemini_flash_exhausted()
        ):
            claude_exhausted = False
            if hasattr(antigravity_bridge, "is_claude_exhausted"):
                claude_exhausted = antigravity_bridge.is_claude_exhausted()
            if not claude_exhausted:
                logger.info(
                    "Unified Gemini 3.8 Flash pool exhausted. Automatically switching across quota boundary to Claude Sonnet 4.6, Sir."
                )
                target_model = "claude-sonnet-4-6"

        return target_model

    def parse_model_switch_command(self, norm_text: str) -> tuple[str, str | None] | None:
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
        active_tool_names: frozenset[str],
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

    def should_capture_screen(self, query: str) -> bool:
        """Determines if the prompt requests visual inspection of screen pixels."""
        q = (query or "").lower()
        # Exclude OS actions, window listings, screenshots, and lock commands
        if any(w in q for w in ("running", "open apps", "open windows", "active window", "focused window", "screenshot", "lock", "minimize")):
            return False

        vision_triggers = (
            "look at this", "look here", "look at my screen", "look at the screen",
            "what do you see", "error on my screen", "read my screen", "read the screen",
            "inspect my screen", "inspect the screen", "can you see my screen",
            "describe my screen", "see on my screen", "visualize screen",
            "what am i looking at", "what is on my screen", "what is this error",
            "what error is this", "look at this error", "look at the error",
            "look at that error", "inspect screen", "see screen",
        )
        return any(t in q for t in vision_triggers)

    @staticmethod
    def is_code_task(query: str) -> bool:
        """Routes repository and software engineering requests through the focused code workflow."""
        q = (query or "").lower()
        cancel_triggers = (
            "stop coding", "stop code", "exit coding", "switch to general",
            "cancel coding", "stop writing code", "done coding", "enough coding",
        )
        if any(c in q for c in cancel_triggers):
            return False
        indicators = (
            "code", "coding", "codebase", "repository", "repo", "source file", "multiple files",
            "read files", "implement", "debug", "bug", "refactor", "function", "class", "module",
            "script", "python", "javascript", "typescript", "powershell", "pull request", "build an app",
            "write an app", "fix this", "fix the", "programming", "software project", "unit test",
            "error message", "traceback", "stack trace", "exception", "compile error", "dependency conflict",
            "application code", "app crashes", "app is crashing", "won't start", "won't compile",
            "edit yourself", "modify yourself", "enhance yourself", "optimize yourself", "improve yourself",
            "upgrade yourself", "inspect your code", "check your code", "look at your code", "fix yourself",
            "refactor yourself", "your codebase", "your files", "jarvis 2.0", "jarvis_2.0", "self edit",
            "self optimize", "self improve", "self enhance", "build yourself", "self build", "self-evolution",
            "evolve", "custom skill", "patch file", "run tests", "edit code", "code changes",
        )
        return any(indicator in q for indicator in indicators)

    def is_code_followup(self, user_text: str, active_task_mode: str = "general") -> bool:
        """Determines if a user turn is a conversational follow-up to an ongoing coding task."""
        if active_task_mode != "code":
            return False

        norm = (user_text or "").lower().strip()
        words = norm.split()

        cancel_triggers = (
            "stop", "cancel", "never mind", "nevermind", "forget it", "exit",
            "reset", "switch to general", "enough coding", "change topic",
        )
        if any(c in norm for c in cancel_triggers):
            return False

        reflex_triggers = (
            "weather", "temperature", "forecast", "time is it", "what time",
            "battery", "charging", "volume", "mute", "unmute", "hello", "hi", "hey",
        )
        if any(r in norm for r in reflex_triggers):
            return False

        followup_phrases = (
            "yes", "yeah", "yep", "sure", "ok", "okay", "agreed", "sounds good",
            "proceed", "continue", "go ahead", "do it", "do that", "apply",
            "option a", "option b", "option c", "option 1", "option 2", "option 3",
            "first option", "second option", "fix that", "fix this", "try that",
            "try this", "run tests", "run the tests", "check tests", "retest",
            "commit", "push", "what about", "why did", "show diff", "diff",
        )
        if any(p in norm for p in followup_phrases):
            return True

        return bool(len(words) <= 8 and not self.should_capture_screen(user_text))

    def select_tools_for_query(
        self,
        user_text: str,
        active_task_mode: str = "general",
        provider: str = "antigravity",
        last_assistant_msg: str = "",
    ) -> frozenset[str]:
        """Limit each request to skills that its wording plausibly needs."""
        if active_task_mode == "code":
            return frozenset(CODE_TOOL_NAMES)
        if self.should_capture_screen(user_text):
            if provider == "gemini":
                return frozenset()
            return frozenset({"inspect_screen", "see_screen"})

        query = (user_text or "").lower()
        selected: set[str] = set()
        for keywords, tools in TOOL_INTENT_GROUPS:
            if any(keyword in query for keyword in keywords):
                selected.update(tools)

        clean_words = query.strip().split()
        if len(clean_words) <= 5 and any(w in query for w in ("yes", "yeah", "yep", "sure", "please", "go ahead", "do that", "add it", "remember it", "remember that", "commit it", "do it")):
            if any(k in last_assistant_msg for k in ("commit", "memory", "archives", "remember", "record", "calibrated")):
                selected.update({"remember_fact", "update_user_preference", "recall_fact"})
            elif any(k in last_assistant_msg for k in ("build", "skill", "feature", "tool", "option a", "option b")):
                selected.update(CODE_TOOL_NAMES)

        all_tools = SkillRegistry.get_all_tools()
        query_words = set(re.findall(r"\b[a-z0-9_-]+\b", query))
        for tool_name in all_tools:
            if tool_name not in selected:
                tool_tokens = set(tool_name.lower().split("_"))
                if tool_tokens.issubset(query_words) or (len(tool_tokens) > 1 and len(tool_tokens & query_words) >= 2):
                    selected.add(tool_name)

        return frozenset(selected)
