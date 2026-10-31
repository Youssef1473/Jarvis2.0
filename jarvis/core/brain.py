"""Jarvis 3.0 local-first brain with opt-in cloud specialist consultations."""
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jarvis.skills.ast_code_surgeon
import jarvis.skills.briefing
import jarvis.skills.browser_agent
import jarvis.skills.browser_cdp
import jarvis.skills.calendar_manager
import jarvis.skills.chrome_devtools
import jarvis.skills.deep_research
import jarvis.skills.dev_shell
import jarvis.skills.diagnostics
import jarvis.skills.document_ingest
import jarvis.skills.email_manager
import jarvis.skills.everything_search
import jarvis.skills.gemini_intelligence
import jarvis.skills.hybrid_actuator
import jarvis.skills.job_object_sandbox
import jarvis.skills.mcp_manager
import jarvis.skills.media_dj
import jarvis.skills.modern_web
import jarvis.skills.notes
import jarvis.skills.nvml_dxgi_vram_arbiter

# Ensure skills are imported and registered
import jarvis.skills.os_control
import jarvis.skills.photo_manager
import jarvis.skills.productivity
import jarvis.skills.project_manager
import jarvis.skills.protocols
import jarvis.skills.reflex_macro_compiler
import jarvis.skills.rstrtmgr_lock_surgeon
import jarvis.skills.screen_vision
import jarvis.skills.self_evolution
import jarvis.skills.system_maintenance
import jarvis.skills.system_watchdog
import jarvis.skills.timers
import jarvis.skills.usn_journal_delta_radar
import jarvis.skills.voice_control
import jarvis.skills.web_search
import jarvis.skills.windows_uia
from jarvis.context.context_orchestrator import (
    BASE_SYSTEM_PROMPT as CONTEXT_BASE_SYSTEM_PROMPT,
)
from jarvis.context.context_orchestrator import ContextOrchestrator
from jarvis.core.antigravity_bridge import AntigravityBridge
from jarvis.core.brain_antigravity import AntigravityProviderMixin
from jarvis.core.brain_gemini import GeminiProviderMixin
from jarvis.core.brain_openai import OpenAIProviderMixin
from jarvis.core.brain_specialist import SpecialistConsultationMixin
from jarvis.core.cognition import CognitiveRouter
from jarvis.core.github_bridge import GitHubCopilotBridge
from jarvis.core.memory import MemoryManager
from jarvis.core.pipeline.latency_profiler import PipelineLatencyProfiler
from jarvis.core.reflex_dispatcher import ReflexDispatcher
from jarvis.core.reflex_engine import DynamicReflexEngine
from jarvis.core.safety import SafetyGate
from jarvis.core.services import ManagedServiceManager
from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Brain")


class JarvisBrain(
    OpenAIProviderMixin,
    GeminiProviderMixin,
    AntigravityProviderMixin,
    SpecialistConsultationMixin,
):
    """Runs normal conversation locally and exposes explicit specialist consultations."""

    BASE_SYSTEM_PROMPT = CONTEXT_BASE_SYSTEM_PROMPT

    GEMINI_CANDIDATE_MODELS = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-flash-latest",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-pro-latest",
    ]

    GROQ_CANDIDATE_MODELS = [
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
    ]

    ANTIGRAVITY_CANDIDATE_MODELS = [
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-low",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
        "gemini-4-argon-high",
    ]

    # Keep in sync with OpenRouterProvider's currently listed free candidates.
    OPENROUTER_FREE_MODELS = [
        "nvidia/nemotron-3.5-super-vl:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "cohere/north-mini-code:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "google/gemma-4-31b-it:free",
    ]

    CODE_TOOL_NAMES = {
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
    }

    TOOL_INTENT_GROUPS = (
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

    def __init__(
        self,
        config: dict[str, Any],
        memory_manager: MemoryManager,
        safety_gate: SafetyGate,
        service_manager: ManagedServiceManager | None = None,
    ):
        self.config = config
        self.memory = memory_manager
        self.safety = safety_gate
        self.service_manager = service_manager or ManagedServiceManager()
        self.temperature = config.get("llm", {}).get("temperature", 0.6)
        self.active_task_mode = "general"
        self.active_tool_names: frozenset[str] = frozenset()
        
        # Use local inference by default; hosted providers are opt-in.
        self.provider = config.get("llm", {}).get("provider", "ollama").lower()

        # Multi-provider hybrid references
        self.ollama_client = None
        self.ollama_model = config.get("llm", {}).get("ollama_model", "qwen3.5:2b-q4_K_M")
        self.groq_client = None
        configured_groq_model = config.get("llm", {}).get("groq_model", "qwen/qwen3.8-27b")
        self.groq_candidates = [configured_groq_model] + [
            m for m in self.GROQ_CANDIDATE_MODELS if m != configured_groq_model
        ]
        self.groq_model = self.groq_candidates[0]

        # OpenAI-compatible (OmniRoute / Groq / xAI / Ollama) state
        self.openai_client = None
        self.openai_model = ""
        self.openai_messages: list[dict[str, Any]] = []

        # Gemini state
        configured_gemini_model = config.get("llm", {}).get("model", "gemini-3.8-flash")
        self.gemini_candidates = [configured_gemini_model] + [m for m in self.GEMINI_CANDIDATE_MODELS if m != configured_gemini_model]
        self.gemini_model_idx = 0
        self.gemini_model = self.gemini_candidates[self.gemini_model_idx]
        self.gemini_client = None
        self.gemini_chat = None
        self.gemini_task_mode = None
        self.gemini_tool_names: frozenset[str] = frozenset()

        # Antigravity CLI Bridge (Google AI Pro integration)
        self.antigravity_bridge = AntigravityBridge(config)
        configured_ag_model = config.get("llm", {}).get("antigravity_model", "gemini-3.8-flash-medium")
        self.antigravity_candidates = [configured_ag_model] + [
            m for m in self.ANTIGRAVITY_CANDIDATE_MODELS if m != configured_ag_model
        ]
        self.antigravity_model = self.antigravity_candidates[0]
        self.antigravity_auto_tiering = config.get("llm", {}).get("antigravity_auto_tiering", True)

        # GitHub Copilot CLI bridge (restricted to a text-only custom agent)
        self.github_bridge = GitHubCopilotBridge(config)

        # OpenRouter free AI provider state.
        self.openrouter_provider = None

        # Dynamic round-trip latency tracking in milliseconds
        self.last_latencies: dict[str, float] = {
            "ollama": 0.0,
            "groq": 0.0,
            "gemini": 0.0,
            "antigravity": 0.0,
            "github": 0.0,
            "openrouter": 0.0,
        }

        # Monotonic cancellation token for preempting in-flight tool actuation on speech barge-in
        self._active_cancel_token: Any | None = None

        # Real-time nanosecond stage-by-stage latency profiler
        self.latency_profiler: PipelineLatencyProfiler = jarvis.skills.diagnostics.get_global_latency_profiler()
        self._current_turn_id: str = ""

        # Initialize requested provider
        if self.provider in ("hybrid", "auto"):
            self._init_hybrid_providers()
        elif self.provider in ("ollama", "omniroute", "groq", "grok"):
            self._init_openai_compatible()
        elif self.provider == "gemini":
            self._init_gemini()
        elif self.provider in ("antigravity", "agy"):
            self._init_antigravity()
        elif self.provider in ("github", "github_copilot", "github-copilot", "copilot", "gh"):
            self._init_github()
        elif self.provider in ("openrouter", "open_router", "or"):
            self._init_openrouter()
        else:
            logger.warning("Unknown configured LLM provider '%s'; defaulting to hybrid tri-tier.", self.provider)
            self.provider = "hybrid"
            self._init_hybrid_providers()

        raw_db_path = getattr(self.memory, "db_path", "data/jarvis_memory.db")
        if isinstance(raw_db_path, (str, Path)) and not hasattr(raw_db_path, "_mock_name"):
            db_path = raw_db_path
        else:
            db_path = "data/jarvis_memory.db"
        self.reflex_engine = DynamicReflexEngine(db_path=db_path)
        import jarvis.skills.self_evolution as self_evo
        self_evo.set_reflex_engine(self.reflex_engine)
        self._current_turn_executed_tools: list[tuple[str, dict[str, Any]]] = []
        self._current_turn_resolved_tool_outputs: dict[str, Any] = {}
        self.tts = None

        from jarvis.conversation.bookmarks import ReasoningBookmarkStack
        from jarvis.core.pipeline import (
            DualTrackCoordinator,
            InterimSpeechBridge,
            ToolOrchestrator,
            TurnTaintTracker,
        )

        self.interim_bridge = InterimSpeechBridge(
            speak_fn=self._speak_interim_phrase,
            is_speaking_fn=self._is_speaking,
        )
        self.dual_track_coordinator = DualTrackCoordinator(
            tts_enqueue_fn=self._async_enqueue_tts,
            tts_wait_clause_done_fn=self._async_wait_clause_done,
            wasapi_flush_fn=self._flush_wasapi_playback,
            bookmark_stack=ReasoningBookmarkStack.get_instance(),
            filler_threshold_ms=320.0,
            cache_ttl_seconds=60.0,
        )
        self.tool_orchestrator = ToolOrchestrator(
            interim_bridge=self.interim_bridge,
            taint_tracker=TurnTaintTracker.get_instance(),
            dual_track_coordinator=self.dual_track_coordinator,
        )
        self.cognitive_router = CognitiveRouter(default_model=self.antigravity_model)
        self.context_orchestrator = ContextOrchestrator(config=self.config)
        self.reflex_dispatcher = ReflexDispatcher(reflex_engine=self.reflex_engine)

        from jarvis.core.mcp_manager import MCPManager
        self.mcp_manager = MCPManager(config=self.config)
        if (
            self.config.get("mcp", {}).get("auto_start", False)
            and not os.environ.get("JARVIS_TEST_MODE")
            and "unittest" not in sys.modules
        ):
            threading.Thread(
                target=self.mcp_manager.start_configured_servers,
                kwargs={"auto_register_skills": True},
                daemon=True,
                name="mcp-startup-worker",
            ).start()

    def set_tts_instance(self, tts: Any):
        """Sets the TextToSpeech instance for acoustic interim feedback."""
        self.tts = tts

    def set_tool_status_callback(self, callback: Any):
        """Sets a callback to broadcast tool execution lifecycle events to the HUD."""
        if hasattr(self, "tool_orchestrator") and self.tool_orchestrator:
            self.tool_orchestrator.on_status_callback = callback

    def _speak_interim_phrase(self, phrase: str):
        if hasattr(self, "tts") and self.tts:
            try:
                if hasattr(self.tts, "queue_sentence"):
                    self.tts.queue_sentence(phrase)
                elif hasattr(self.tts, "speak_sync"):
                    self.tts.speak_sync(phrase)
            except Exception as e:
                logger.debug("Interim speech error: %s", e)

    def _is_speaking(self) -> bool:
        if hasattr(self, "tts") and self.tts:
            return getattr(self.tts, "is_speaking", False)
        return False

    async def _async_enqueue_tts(self, phrase: str, epoch: int) -> None:
        """Asynchronously enqueues Track A interim milestone phrase to TTS."""
        self._speak_interim_phrase(phrase)

    async def _async_wait_clause_done(self) -> None:
        """Acoustic Collision Gate: waits for active TTS clause synthesis/drain before Track B handoff."""
        import asyncio
        start_wait = time.monotonic()
        while self._is_speaking() and (time.monotonic() - start_wait < 1.8):
            if self._active_cancel_token and getattr(self._active_cancel_token, "is_cancelled", False):
                break
            await asyncio.sleep(0.04)

    def _flush_wasapi_playback(self) -> None:
        """Hardware flush callback invoked during voice barge-in to stop playback instantly."""
        if hasattr(self, "tts") and self.tts:
            try:
                self.tts.stop()
            except Exception as e:
                logger.debug("TTS stop notice during barge-in flush: %s", e)

    def _dispatch_tool_pipeline(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        step_index: int = 1,
        sentence_callback: Callable[[str], None] | None = None,
        dual_key_confirmed: bool = False,
    ) -> tuple[Any, bool]:
        """Single authoritative gateway for all tool authorization, safety checks, and execution across all providers.

        Returns:
            (tool_result, is_success): tool_result is the output or error string;
            is_success is True if executed without safety halts or cancellation.
        """
        # 1. Narrate action to UI and audio
        if sentence_callback:
            try:
                narrative = self._narrate_tool_action(tool_name, tool_args)
                sentence_callback(narrative)
            except Exception as e:
                logger.debug("Tool narration notice: %s", e)

        # 2. Centralized SafetyGate check (destructive commands, forbidden URLs, etc.)
        is_safe, warning = self.safety.check_tool_call(tool_name, tool_args)
        if not is_safe:
            halt_msg = f"Action halted: {warning}"
            if sentence_callback:
                sentence_callback(halt_msg)
            return halt_msg, False

        # 3. Monotonic Cancellation Preemption check
        if self._active_cancel_token and hasattr(self._active_cancel_token, "raise_if_cancelled"):
            self._active_cancel_token.raise_if_cancelled()

        # 4. Physical Execution via ToolOrchestrator (governs loop recursion, taint checking, and execution)
        tool_result = self._execute_tool_with_orchestrator(
            tool_name, tool_args, step_index=step_index, dual_key_confirmed=dual_key_confirmed
        )
        self._record_tool_execution(tool_name, tool_args, tool_result)
        return tool_result, True

    def _execute_tool_with_orchestrator(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        step_index: int = 1,
        dual_key_confirmed: bool = False,
    ) -> Any:
        """Executes a tool call through ToolOrchestrator for taint tracking, loop guards, and interim audio."""
        if self._active_cancel_token and hasattr(self._active_cancel_token, "raise_if_cancelled"):
            self._active_cancel_token.raise_if_cancelled()
        if not hasattr(self, "tool_orchestrator") or not self.tool_orchestrator:
            from jarvis.core.pipeline.tool_orchestrator import ToolOrchestrator
            self.tool_orchestrator = ToolOrchestrator()
        return self.tool_orchestrator.execute_step(
            tool_name, tool_args, step_index=step_index, dual_key_confirmed=dual_key_confirmed
        )

    def shutdown(self, timeout_seconds: float = 2.0):
        """Cleanly terminates managed service subprocesses and background workers."""
        if hasattr(self, "antigravity_bridge") and self.antigravity_bridge:
            try:
                self.antigravity_bridge.stop()
            except Exception as e:
                logger.debug("Antigravity bridge shutdown notice: %s", e)

        if hasattr(self, "mcp_manager") and self.mcp_manager:
            try:
                self.mcp_manager.shutdown_all()
            except Exception as e:
                logger.debug("MCP manager shutdown notice: %s", e)

        if hasattr(self, "service_manager") and self.service_manager:
            try:
                self.service_manager.stop_all(timeout_seconds=timeout_seconds)
            except Exception as e:
                logger.debug("Service manager shutdown notice: %s", e)

        if hasattr(self, "tts") and self.tts:
            try:
                if hasattr(self.tts, "close"):
                    self.tts.close(timeout=timeout_seconds)
                elif hasattr(self.tts, "stop"):
                    self.tts.stop()
            except Exception as e:
                logger.debug("TTS shutdown notice during brain teardown: %s", e)

    def __del__(self):
        """Ensures spawned processes are not orphaned when the brain instance is collected."""
        try:
            self.shutdown(timeout_seconds=1.0)
        except Exception:
            pass

    def _synthesize_error_lesson(self, tool_name: str, tool_args: dict[str, Any], failure_signature: str) -> str:
        """Synthesizes a deterministic, zero-latency natural language lesson from a tool failure (Reflexion architecture)."""
        args_dict = dict(tool_args or {})
        args_str = json.dumps(args_dict) if args_dict else "{}"

        if tool_name == "focus_window":
            target = args_dict.get("app_name") or args_dict.get("window_title") or "target application"
            return f"Verify '{target}' is running via list_running_applications or start it via launch_application before attempting to focus."

        if tool_name == "close_application":
            target = args_dict.get("app_name") or "target process"
            return f"Verify '{target}' is active via list_running_applications before attempting termination."

        if tool_name in ("browse_and_read_webpage", "open_url_in_desktop_browser"):
            url = args_dict.get("url", "")
            return f"Verify URL '{url}' has a valid scheme (http:// or https://) and network endpoint is accessible."

        if tool_name in ("patch_file", "write_file"):
            path = args_dict.get("file_path") or args_dict.get("path") or "target file"
            return f"Read '{path}' with read_file first to confirm exact line contents and signatures before modifying."

        if tool_name == "execute_powershell":
            cmd = args_dict.get("command", "")[:60]
            return f"Validate syntax, paths, and flags for powershell command '{cmd}'; ensure prerequisites exist."

        if tool_name in ("adjust_volume", "control_media"):
            return "Ensure audio endpoint is initialized and media application is running."

        # General / fallback heuristic
        clean_sig = re.sub(r"^(?:Error|Failed to|Action halted|Could not find)[:\s]*", "", failure_signature, flags=re.IGNORECASE).strip()
        return f"When invoking '{tool_name}' with {args_str}: Pre-validate arguments and environment to avoid '{clean_sig[:120]}'."

    def _record_tool_execution(self, tool_name: str, tool_args: dict[str, Any], tool_result: Any = None):
        """Tracks executed tools during the current turn for habit promotion, or records episodic failure lessons."""
        if not hasattr(self, "_current_turn_executed_tools"):
            self._current_turn_executed_tools = []
        res_str = str(tool_result or "")
        if any(res_str.startswith(err) for err in ("Failed to", "Action halted", "Could not find", "Error:")):
            try:
                lesson = self._synthesize_error_lesson(tool_name, tool_args, res_str)
                trigger_ctx = f"tool:{tool_name}"
                if tool_args:
                    trigger_ctx += f" args:{json.dumps(tool_args)}"
                self.memory.record_episodic_lesson(
                    tool_name=tool_name,
                    trigger_context=trigger_ctx,
                    failure_signature=res_str[:250],
                    lesson=lesson,
                )
            except Exception as e:
                logger.debug("Episodic reflection recording notice: %s", e)
            return
        self._current_turn_executed_tools.append((tool_name, dict(tool_args or {})))
        if not hasattr(self, "_current_turn_resolved_tool_outputs"):
            self._current_turn_resolved_tool_outputs = {}
        self._current_turn_resolved_tool_outputs[tool_name] = tool_result

        # Temporal Habit Graph unified observation (Argon Dimension 1.3)
        if hasattr(self, "habit_graph") and self.habit_graph:
            try:
                cog_state = self.cognitive_twin.state.value if hasattr(self, "cognitive_twin") and self.cognitive_twin else "ROUTINE_ADMIN"
                self.habit_graph.observe_action(
                    action=f"tool:{tool_name}",
                    cognitive_state=cog_state,
                    domain="tool",
                )
            except Exception as h_err:
                logger.debug("HabitGraph tool observation notice: %s", h_err)

    def get_current_turn_tool_outputs(self) -> dict[str, Any]:
        """Returns the dictionary of resolved tool outputs from the current turn."""
        return dict(getattr(self, "_current_turn_resolved_tool_outputs", {}))


    def _get_reflex_dispatcher(self) -> ReflexDispatcher:
        """Retrieves or lazily instantiates the ReflexDispatcher coordinator."""
        if not hasattr(self, "reflex_dispatcher") or not self.reflex_dispatcher:
            engine = getattr(self, "reflex_engine", None)
            self.reflex_dispatcher = ReflexDispatcher(reflex_engine=engine)
        elif hasattr(self, "reflex_engine") and self.reflex_dispatcher.reflex_engine is not self.reflex_engine:
            self.reflex_dispatcher.reflex_engine = self.reflex_engine
        return self.reflex_dispatcher

    def _finalize_turn_habit(self, user_text: str, turn_start_time: float):
        """If the turn successfully executed safe actions, promote it to a learned habit."""
        if not hasattr(self, "_current_turn_executed_tools") or not hasattr(self, "reflex_engine"):
            return
        executed = self._current_turn_executed_tools
        if not executed:
            return
        self._get_reflex_dispatcher().finalize_turn_habit(
            user_text=user_text,
            turn_start_time=turn_start_time,
            executed_tools=executed,
        )

    def _get_system_instruction(
        self,
        user_text: str = "",
        tier1_profile_xml: str = "",
        flow_state_xml: str = "",
        bookmark_context: str = "",
    ) -> str:
        """Constructs the definitive sovereign system prompt for Jarvis 2.0,
        calibrated to Sir's 7-Dimension Operational Profile.
        """
        available_tools = SkillRegistry.get_openai_tools(self.active_tool_names)
        active_model = self.openai_model or self.gemini_model
        workspace = self.config.get("_base_dir", "")

        if not flow_state_xml:
            flow_state_xml = getattr(self, "_current_turn_flow_xml", "")
        if not bookmark_context:
            bookmark_context = getattr(self, "_current_turn_bookmark_xml", "")

        if not hasattr(self, "context_orchestrator") or not self.context_orchestrator:
            self.context_orchestrator = ContextOrchestrator(config=getattr(self, "config", {}))

        return self.context_orchestrator.assemble_system_prompt(
            memory=getattr(self, "memory", None),
            available_tools=available_tools,
            provider=getattr(self, "provider", "antigravity"),
            active_model=active_model,
            workspace=workspace,
            active_task_mode=getattr(self, "active_task_mode", "general"),
            user_text=user_text,
            tier1_profile_xml=tier1_profile_xml,
            flow_state_xml=flow_state_xml,
            bookmark_context=bookmark_context,
            base_system_prompt=getattr(self, "BASE_SYSTEM_PROMPT", None),
        )

    def build_ephemeral_user_envelope(self, user_text: str) -> str:
        """Wraps user turn with query-biased RRF memory, past lessons, and live telemetry envelope.

        Preserves 100% prefix KV cache hits on Google AI Studio / Gemini server-side prompt
        cache by keeping the system prompt static and injecting dynamic context at the turn tail.
        """
        if not hasattr(self, "context_orchestrator") or not self.context_orchestrator:
            self.context_orchestrator = ContextOrchestrator(config=getattr(self, "config", {}))

        return self.context_orchestrator.build_ephemeral_user_envelope(
            user_text=user_text,
            memory=getattr(self, "memory", None),
            cognitive_twin=getattr(self, "cognitive_twin", None),
        )

    load_curated_prompt = staticmethod(ContextOrchestrator.load_curated_prompt)


    def _select_tools_for_query(self, user_text: str) -> frozenset[str]:
        """Limit each request to skills that its wording plausibly needs.

        Tool schemas are part of every model prompt. Keeping routine conversation
        schema-free reduces prompt processing and makes a small local model less
        likely to spend a turn considering unrelated actions.
        """
        if self.active_task_mode == "code":
            return frozenset(self.CODE_TOOL_NAMES)
        if self.should_capture_screen(user_text):
            if self.provider == "gemini":
                # The direct Gemini API session embeds the current screenshot directly.
                return frozenset()
            return frozenset({"inspect_screen", "see_screen"})

        query = user_text.lower()
        selected: set[str] = set()
        for keywords, tools in self.TOOL_INTENT_GROUPS:
            if any(keyword in query for keyword in keywords):
                selected.update(tools)

        # Multi-turn affirmative follow-up detection for active learning and co-engineering
        clean_words = query.strip().split()
        if len(clean_words) <= 5 and any(w in query for w in ("yes", "yeah", "yep", "sure", "please", "go ahead", "do that", "add it", "remember it", "remember that", "commit it", "do it")):
            last_assistant_msg = ""
            for msg in reversed(getattr(self, "openai_messages", [])):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        last_assistant_msg = content.lower()
                    break
            if any(k in last_assistant_msg for k in ("commit", "memory", "archives", "remember", "record", "calibrated")):
                selected.update({"remember_fact", "update_user_preference", "recall_fact"})
            elif any(k in last_assistant_msg for k in ("build", "skill", "feature", "tool", "option a", "option b")):
                selected.update(self.CODE_TOOL_NAMES)

        # Dynamic Voyager Discovery: Scan registered tools in SkillRegistry
        # to ensure custom skills and unmapped tools whose identifiers match the query are included
        all_tools = SkillRegistry.get_all_tools()
        query_words = set(re.findall(r"\b[a-z0-9_-]+\b", query))
        for tool_name, tool_fn in all_tools.items():
            if tool_name not in selected:
                tool_tokens = set(tool_name.lower().split("_"))
                if tool_tokens.issubset(query_words) or (len(tool_tokens) > 1 and len(tool_tokens & query_words) >= 2):
                    selected.add(tool_name)

        return frozenset(selected)

    def should_capture_screen(self, query: str) -> bool:
        """Determines if the prompt requests visual inspection of screen pixels."""

        q = query.lower()
        # Exclude OS actions, window listings, screenshots, and lock commands
        if any(w in q for w in ("running", "open apps", "open windows", "active window", "focused window", "screenshot", "lock", "minimize")):
            return False

        vision_triggers = [
            "look at this", "look here", "look at my screen", "look at the screen",
            "what do you see", "error on my screen", "read my screen", "read the screen",
            "inspect my screen", "inspect the screen", "can you see my screen",
            "describe my screen", "see on my screen", "visualize screen",
            "what am i looking at", "what is on my screen", "what is this error",
            "what error is this", "look at this error", "look at the error",
            "look at that error", "inspect screen", "see screen"
        ]
        return any(t in q for t in vision_triggers)

    def is_fast_reflex(self, norm_text: str) -> bool:
        """Determines if a query matches a fast reflex command without executing any tools."""
        return self._get_reflex_dispatcher().is_fast_reflex(norm_text)

    def _try_fast_reflex(self, norm_text: str, dry_run: bool = False) -> str | bool | None:
        """Executes unambiguous local OS, desktop, media, and browser commands in <10ms.
        Bypasses slow cloud round-trips for instant physical and spoken execution.
        If dry_run=True, tests for a reflex hit without executing any skills.
        """
        return self._get_reflex_dispatcher().try_fast_reflex(norm_text, dry_run=dry_run)

    @staticmethod
    def _narrate_tool_action(fn_name: str, fn_args: dict[str, Any]) -> str:
        """Returns a natural, safe spoken phrase describing an action before execution."""
        return ReflexDispatcher.narrate_tool_action(fn_name, fn_args)

    def process_query(
        self,
        user_text: str,
        sentence_callback: Any = None,
        flow_state_xml: str = "",
        bookmark_context: str = "",
    ) -> str:
        """Routes normal conversation to the configured primary model; consultations are explicit."""
        turn_start_time = time.perf_counter()
        self._current_turn_id = f"turn_{int(time.time() * 1000)}"
        if hasattr(self, "latency_profiler") and self.latency_profiler:
            self.latency_profiler.begin_turn(self._current_turn_id)
            self.latency_profiler.mark(self._current_turn_id, "stt_final_ns")
        self._current_turn_executed_tools = []
        self._current_turn_resolved_tool_outputs = {}
        self._current_turn_flow_xml = flow_state_xml
        self._current_turn_bookmark_xml = bookmark_context
        from jarvis.core.security_boundaries import TurnTaintTracker
        TurnTaintTracker.get_instance().reset_turn()
        if hasattr(self, "tool_orchestrator") and self.tool_orchestrator:
            self.tool_orchestrator.reset()

        # Strip leading/trailing punctuation and whitespace for robust voice command recognition
        norm_text = re.sub(r"^[\s,;:.]+|[\s,!?:.]+$", "", (user_text or "").strip()).strip()

        # Voice model switching commands for Google AI Pro fleet & Hybrid token shield
        if hasattr(self, "cognitive_router") and self.cognitive_router:
            switch_cmd = self.cognitive_router.parse_model_switch_command(norm_text)
            if switch_cmd:
                action, val = switch_cmd
                if action == "hybrid":
                    self.provider = "hybrid"
                    self.antigravity_auto_tiering = True
                    reply = "Hybrid token-shield mode activated, Sir. Local commands run offline at zero tokens, casual chat on free Groq LPU, and deep reasoning routed to your Google AI Pro fleet."
                    if sentence_callback:
                        sentence_callback(reply)
                    return reply
                elif action == "auto" and getattr(self, "antigravity_bridge", None) and self.antigravity_bridge.is_available():
                    self.antigravity_auto_tiering = True
                    reply = "Dynamic model tiering activated, Sir. Routing automatically between Gemini 3.8 Flash High for reasoning, Gemini 3.8 Flash Low for speed, Gemini 3.8 Flash Medium for general tasks, and Claude for code."
                    if sentence_callback:
                        sentence_callback(reply)
                    return reply
                elif action == "github":
                    self.provider = "github"
                    reply = "GitHub Copilot CLI provider activated, Sir. The text-only Jarvis agent has no tool access."
                    if sentence_callback:
                        sentence_callback(reply)
                    return reply
                elif action == "model" and getattr(self, "antigravity_bridge", None) and self.antigravity_bridge.is_available():
                    self.antigravity_model = val
                    self.antigravity_auto_tiering = False
                    self.provider = "antigravity"
                    reply = f"Cognitive link locked to {val}, Sir. Ready for your command."
                    if sentence_callback:
                        sentence_callback(reply)
                    return reply

        if any(norm_text.lower() == p for p in ["what model are you using", "what is your active model", "current model", "active model", "what model is active", "what provider is active", "which model are you using"]):
            if self.provider in ("hybrid", "auto"):
                auto_tier_txt = "dynamic auto-tiering" if getattr(self, "antigravity_auto_tiering", True) else f"locked to {self.antigravity_model}"
                reply = (
                    f"Operating in Hybrid Token-Shield mode, Sir. "
                    f"Workstation actions run offline (0 tokens), casual chat on Groq LPU, and deep cognition on Google AI Pro ({auto_tier_txt})."
                )
            elif self.provider in ("github", "github_copilot", "github-copilot", "copilot", "gh"):
                reply = "My active cognitive uplink is currently locked to GitHub Copilot CLI, Sir."
            else:
                reply = f"My active cognitive uplink is currently locked to {self.provider.upper()} ({self.antigravity_model}), Sir."
            if sentence_callback:
                sentence_callback(reply)
            return reply

        # Fast Deterministic Reflex Dispatcher (<10ms execution, 0 tokens)
        reflex_reply = self._try_fast_reflex(norm_text)
        if reflex_reply:
            if hasattr(self, "latency_profiler") and self.latency_profiler:
                self.latency_profiler.mark(self._current_turn_id, "route_complete_ns")
                self.latency_profiler.mark(self._current_turn_id, "llm_first_token_ns")
                self.latency_profiler.complete_turn(self._current_turn_id)
            if sentence_callback:
                sentence_callback(reflex_reply)
            self.memory.log_interaction(user_text, reflex_reply, None)
            logger.info("Fast Reflex executed locally in <10ms: %s", reflex_reply[:80])
            return reflex_reply

        consultation = re.match(
            r"^\s*(?:/consult|consult)\s+(gemini|gemini-pro|gemini-flash|pro|flash|groq|grok|claude|sonnet|opus|gpt-oss|antigravity|agy|github|github-copilot|copilot|gh)\s*:?\s*(.+)$",
            user_text,
            re.IGNORECASE | re.DOTALL,
        )
        if consultation:
            provider, prompt = consultation.group(1).lower(), consultation.group(2).strip()
            result = self._consult_specialist(provider, prompt)
            if hasattr(self, "latency_profiler") and self.latency_profiler:
                self.latency_profiler.mark(self._current_turn_id, "route_complete_ns")
                self.latency_profiler.mark(self._current_turn_id, "llm_first_token_ns")
                self.latency_profiler.complete_turn(self._current_turn_id)
            if sentence_callback:
                speech_buffer = result
                while speech_buffer:
                    speech_chunk, speech_buffer = self._extract_next_speech_chunk(speech_buffer)
                    if speech_chunk is None:
                        speech_chunk, speech_buffer = speech_buffer, ""
                    spoken = self._clean_speech_clause(speech_chunk)
                    if spoken:
                        sentence_callback(spoken)
            return result

        is_code = (
            (self._is_code_task(user_text) or self._is_code_followup(user_text))
            and not self.should_capture_screen(user_text)
        )
        self.active_task_mode = "code" if is_code else "general"
        self.active_tool_names = self._select_tools_for_query(user_text)

        # Smart Tri-Tier Hybrid Dispatch & Model Execution
        reply = self._dispatch_llm_query(user_text, sentence_callback)
        if hasattr(self, "latency_profiler") and self.latency_profiler:
            self.latency_profiler.mark(self._current_turn_id, "llm_first_token_ns")
            self.latency_profiler.complete_turn(self._current_turn_id)
        self._finalize_turn_habit(norm_text, turn_start_time)
        return reply

    async def generate_turn(
        self,
        utterance: str,
        flow_state_xml: str = "",
        bookmark_context: str = "",
    ) -> tuple[str, str, dict[str, Any]]:
        """Asynchronous cognitive turn generator for orchestrator-driven conversation.

        Returns:
            Tuple of (response_text, topic_label, resolved_tool_outputs)
        """
        import asyncio
        loop = asyncio.get_running_loop()
        reply = await loop.run_in_executor(
            None,
            lambda: self.process_query(
                user_text=utterance,
                flow_state_xml=flow_state_xml,
                bookmark_context=bookmark_context,
            ),
        )
        # Infer topic label from utterance
        q = (utterance or "").strip()
        pattern = re.compile(
            r"^(?:jarvis,?\s*|hey jarvis,?\s*|can you\s+|could you\s+|please\s+|tell me about\s+|explain\s+|what is\s+|how does\s+|why does\s+|what are\s+|how do\s+)",
            flags=re.IGNORECASE,
        )
        while True:
            new_q = pattern.sub("", q).strip()
            if new_q == q:
                break
            q = new_q
        clean_q = re.sub(r"[?!.,;:]+$", "", q).strip()
        words = clean_q.split()
        topic_label = " ".join(words[:4]).strip().lower() if words else "the prior topic"

        tool_outputs = self.get_current_turn_tool_outputs()
        return reply, topic_label, tool_outputs

    def _dispatch_llm_query(self, user_text: str, sentence_callback: Any = None) -> str:
        """Dispatches LLM reasoning across configured providers."""
        if self.provider in ("hybrid", "auto"):
            has_antigravity = getattr(self, "antigravity_bridge", None) and self.antigravity_bridge.is_available()
            has_ollama = getattr(self, "ollama_client", None) is not None
            has_groq = getattr(self, "groq_client", None) is not None
            has_gemini = getattr(self, "gemini_client", None) is not None
            has_copilot = getattr(self, "github_bridge", None) and self.github_bridge.is_available()
            has_screen = self.should_capture_screen(user_text)

            route = self.cognitive_router.determine_hybrid_route(
                user_text=user_text,
                active_tool_names=self.active_tool_names,
                has_screen_intent=has_screen,
                active_task_mode=self.active_task_mode,
                has_antigravity=has_antigravity,
                has_ollama=has_ollama,
                has_groq=has_groq,
                has_gemini=has_gemini,
                has_copilot=has_copilot,
            )
            if hasattr(self, "latency_profiler") and self.latency_profiler:
                self.latency_profiler.mark(getattr(self, "_current_turn_id", ""), "route_complete_ns")

            if route == "gemini_vision":
                logger.info("Hybrid Routing: Screen vision requested -> GEMINI MULTIMODAL")
                reply = self._process_gemini_query(user_text, sentence_callback)
                if not any(bad in reply.lower() for bad in ("anomaly in my cognitive circuits", "temporarily congested", "experiencing high network congestion")):
                    return reply
                logger.warning("Gemini vision query encountered error. Falling back to local Ollama vision...")

            elif route == "ollama_os":
                logger.info("Hybrid Routing: OS / Device command -> LOCAL OLLAMA (0ms network latency, 0 tokens consumed)")
                self.openai_client = self.ollama_client
                self.openai_model = self.ollama_model
                return self._process_openai_compatible_query(user_text, sentence_callback)

            elif route == "antigravity_deep":
                logger.info("Hybrid Routing: Deep Cognitive / Engineering query -> GOOGLE AI PRO APEX (Antigravity)")
                return self._process_antigravity_query(user_text, sentence_callback)

            elif route == "groq_reflex":
                try:
                    logger.info(f"Hybrid Routing: Fast Reflex query -> GROQ LPU ({self.groq_model})")
                    self.openai_client = self.groq_client
                    self.openai_model = self.groq_model
                    return self._process_openai_compatible_query(user_text, sentence_callback)
                except Exception as e:
                    logger.warning(f"Groq query failed ({e}). Seamlessly falling back...")
                    if has_antigravity:
                        logger.info("Hybrid Fallback: Routing to ANTIGRAVITY FLASH...")
                        return self._process_antigravity_query(user_text, sentence_callback)
                    elif has_ollama:
                        self.openai_client = self.ollama_client
                        self.openai_model = self.ollama_model
                        return self._process_openai_compatible_query(user_text, sentence_callback)

            elif route == "antigravity_default":
                return self._process_antigravity_query(user_text, sentence_callback)
            elif route == "copilot_default":
                return self._process_github_query(user_text, sentence_callback)
            elif route == "ollama_default":
                self.openai_client = self.ollama_client
                self.openai_model = self.ollama_model
                return self._process_openai_compatible_query(user_text, sentence_callback)
            elif route == "gemini_default":
                return self._process_gemini_query(user_text, sentence_callback)

        if self.provider in ("antigravity", "agy"):
            return self._process_antigravity_query(user_text, sentence_callback)
        if self.provider in ("github", "github_copilot", "github-copilot", "copilot", "gh"):
            return self._process_github_query(user_text, sentence_callback)
        if self.provider == "ollama" and not self.openai_client:
            return "My local Ollama link is unavailable. Start Ollama and confirm the configured model is installed."
        if self.provider == "groq" and not self.openai_client:
            return "Groq is not connected. Configure GROQ_API_KEY; I have not switched to another provider."
        if self.provider in ("ollama", "omniroute", "groq", "grok") and self.openai_client:
            return self._process_openai_compatible_query(user_text, sentence_callback)
        if self.provider == "gemini" and not self.gemini_client:
            return "Gemini is not connected. Configure GEMINI_API_KEY; I have not switched to another provider."
        if self.provider == "gemini":
            return self._process_gemini_query(user_text, sentence_callback)
        if self.provider in ("openrouter", "open_router", "or"):
            if not self.openrouter_provider or not self.openrouter_provider.is_available():
                return "OpenRouter is not connected. Add OPENROUTER_API_KEY to .env, Sir."
            return self._process_openrouter_query(user_text, sentence_callback)
        return f"No active model is configured for provider '{self.provider}', Sir."

    def get_latencies(self) -> dict[str, float]:
        """Returns the most recent measured round-trip inference latencies in milliseconds."""
        return dict(self.last_latencies)

    def get_latency_profiler(self) -> PipelineLatencyProfiler:
        """Returns the nanosecond stage-by-stage PipelineLatencyProfiler instance."""
        return self.latency_profiler

    @staticmethod
    def _is_code_task(query: str) -> bool:
        """Routes repository and software engineering requests through the focused code workflow."""
        q = query.lower()
        cancel_triggers = (
            "stop coding", "stop code", "exit coding", "switch to general",
            "cancel coding", "stop writing code", "done coding", "enough coding"
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
            "evolve", "custom skill", "patch file", "run tests", "edit code", "code changes"
        )
        return any(indicator in q for indicator in indicators)

    def _is_code_followup(self, user_text: str) -> bool:
        """Determines if a user turn is a conversational follow-up to an ongoing coding task."""
        if getattr(self, "active_task_mode", "general") != "code":
            return False

        norm = user_text.lower().strip()
        words = norm.split()

        # Explicit topic termination or reset
        cancel_triggers = (
            "stop", "cancel", "never mind", "nevermind", "forget it", "exit",
            "reset", "switch to general", "enough coding", "change topic"
        )
        if any(c in norm for c in cancel_triggers):
            return False

        # Fast reflexes or device inquiries are not code follow-ups
        reflex_triggers = (
            "weather", "temperature", "forecast", "time is it", "what time",
            "battery", "charging", "volume", "mute", "unmute", "hello", "hi", "hey"
        )
        if any(r in norm for r in reflex_triggers):
            return False

        # Common follow-up directives during interactive coding / co-engineering
        followup_phrases = (
            "yes", "yeah", "yep", "sure", "ok", "okay", "agreed", "sounds good",
            "proceed", "continue", "go ahead", "do it", "do that", "apply",
            "option a", "option b", "option c", "option 1", "option 2", "option 3",
            "first option", "second option", "fix that", "fix this", "try that",
            "try this", "run tests", "run the tests", "check tests", "retest",
            "commit", "push", "what about", "why did", "show diff", "diff"
        )
        if any(p in norm for p in followup_phrases):
            return True

        # Short turns (<= 8 words) in an active code session are considered follow-ups
        if len(words) <= 8 and not self.should_capture_screen(user_text):
            return True

        return False

    @staticmethod
    def _clean_speech_clause(text: str) -> str:
        """Normalizes times, dates, numbers, units, and markdown for vocal delivery."""
        if not text:
            return ""
        from jarvis.audio.normalizer import SpeechNormalizer
        cleaned = SpeechNormalizer.normalize(text)
        if not re.search(r"[a-zA-Z0-9]", cleaned):
            return ""
        return cleaned

    @staticmethod
    def _extract_next_speech_chunk(buffer: str) -> tuple[str | None, str]:
        """Extracts the next natural spoken clause or sentence from the streaming buffer
        with speculative punctuation boundary emission for sub-100ms voice reflex latency."""
        # Terminal punctuation (. ! ? \n)
        m_term = re.search(r'(?<!\d)(?<!\b[A-Za-z])[.!?\n]+(?!\d)(\s+|$)', buffer)
        # Speculative clause punctuation (, ; : — –) guarding colons/commas between digits
        m_clause = re.search(r'(?:[,;—–]|(?<!\d):(?!\d))(\s+|$)', buffer)

        candidates = []
        if m_term and m_term.start() >= 2:
            candidates.append((m_term.end(), m_term.start()))
        if m_clause and m_clause.start() >= 3:
            prefix_candidate = buffer[:m_clause.start()].strip()
            if prefix_candidate and len(prefix_candidate.split()) >= 1:
                candidates.append((m_clause.end(), m_clause.start()))

        if candidates:
            # Sort by earliest boundary in stream
            candidates.sort(key=lambda x: x[1])
            cut_idx = candidates[0][0]
            prefix = buffer[:cut_idx].strip()
            if prefix:
                return prefix, buffer[cut_idx:]

        # Fallback if buffer grows long without punctuation
        if len(buffer) >= 80:
            last_space = buffer.rfind(' ')
            if last_space > 20:
                return buffer[:last_space].strip(), buffer[last_space:]

        return None, buffer

    def classify_query_tier(self, query: str) -> str:
        """Determines whether a query is suitable for Fast LPU Tier vs Deep Frontier Tier."""
        has_screen = self.should_capture_screen(query)
        if not hasattr(self, "cognitive_router") or not self.cognitive_router:
            self.cognitive_router = CognitiveRouter(default_model=getattr(self, "antigravity_model", "gemini-3.8-flash-medium"))
        return self.cognitive_router.classify_query_tier(query, has_screen_intent=has_screen)

    def _extract_memory_insights(self, user_text: str, reply_text: str) -> list[dict[str, Any]]:
        """Asynchronously extracts permanent personal preferences, habits, active project context,
        and technical discussion topics from conversational exchanges."""
        system_prompt = (
            "You are an ambient background memory and behavior extractor for Sir's personal AI partner JARVIS. "
            "Analyze the conversation exchange between Sir and JARVIS.\n"
            "Identify two categories of durable knowledge:\n"
            "1. User Behavior & Habits: Personal preferences, working style, behavioral directives, habits, "
            "preferred tools/languages, communication tone, or operational rules about Sir. "
            "(category: 'preference' or 'habit' or 'identity')\n"
            "2. Project & Topical Continuity: The active technical project, architecture decisions, research topics, "
            "or key external facts discussed, so JARVIS maintains seamless continuity without Sir having to repeat context. "
            "(category: 'project' or 'topic' or 'knowledge')\n\n"
            "Output a JSON array of objects with keys:\n"
            "- 'category': 'preference' | 'habit' | 'identity' | 'project' | 'topic' | 'knowledge' | 'general'\n"
            "- 'key': snake_case, concise unique identifier (e.g. 'preferred_formatter', 'auth_architecture', 'active_project_goal')\n"
            "- 'value': clear, factual, self-contained description\n\n"
            "If the exchange is purely transient (e.g. routine greeting, standard window focus, checking volume), output: []\n"
            "Output ONLY valid JSON without markdown fences."
        )
        prompt = f"User (Sir): \"{user_text}\"\nJARVIS: \"{reply_text}\""

        # 1. Try Antigravity batch if available
        if hasattr(self, "antigravity_bridge") and self.antigravity_bridge and self.antigravity_bridge.is_available():
            try:
                res = self.antigravity_bridge._generate_batch(
                    prompt=f"[System Context & Directives]\n{system_prompt}\n\n[User Request]\n{prompt}",
                    model="gemini-3.8-flash-low",
                    timeout=15.0,
                )
                text = (res.get("response") or "").strip()
                if text:
                    if text.startswith("```"):
                        text = re.sub(r"^```(?:json)?\n?", "", text)
                        text = re.sub(r"\n?```$", "", text)
                    data = json.loads(text)
                    if isinstance(data, list):
                        return data
            except Exception as e:
                logger.debug("Antigravity memory extraction notice: %s", e)

        # 2. Try Gemini direct client
        if getattr(self, "gemini_client", None):
            try:
                response = self.gemini_client.models.generate_content(
                    model="gemini-3.8-flash-low",
                    contents=f"{system_prompt}\n\n{prompt}",
                )
                text = (response.text or "").strip()
                if text:
                    if text.startswith("```"):
                        text = re.sub(r"^```(?:json)?\n?", "", text)
                        text = re.sub(r"\n?```$", "", text)
                    data = json.loads(text)
                    if isinstance(data, list):
                        return data
            except Exception as e:
                logger.debug("Gemini memory extraction notice: %s", e)

        # 3. Fallback to Groq if key exists
        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        if groq_key:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1", timeout=8.0)
                res = client.chat.completions.create(
                    model=self.config.get("llm", {}).get("groq_model", "qwen/qwen3.8-27b"),
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=250,
                    temperature=0.0,
                )
                text = res.choices[0].message.content.strip()
                if text.startswith("```"):
                    text = re.sub(r"^```(?:json)?\n?", "", text)
                    text = re.sub(r"\n?```$", "", text)
                data = json.loads(text)
                if isinstance(data, list):
                    return data
            except Exception as e:
                logger.debug("Groq memory extraction notice: %s", e)

        return []

    def stop(self):
        """Halts brain background components, provider runners, and unloads models from VRAM."""
        if hasattr(self, "antigravity_bridge") and self.antigravity_bridge:
            try:
                self.antigravity_bridge.stop()
            except Exception:
                pass

        if hasattr(self, "unified_runner") and self.unified_runner:
            try:
                self.unified_runner.shutdown()
            except Exception:
                pass

        # Unload Ollama models from VRAM if local Ollama server is running
        try:
            ollama_url = self.config.get("llm", {}).get("ollama_base_url", "http://localhost:11434/v1")
            base = ollama_url.split("/v1", 1)[0].rstrip("/")
            import json
            import urllib.request
            req = urllib.request.Request(
                f"{base}/api/generate",
                data=json.dumps({"model": getattr(self, "ollama_model", "qwen3.5:2b-q4_K_M"), "keep_alive": 0}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=1.0):
                pass
            logger.info("Unloaded Ollama models from GPU VRAM.")
        except Exception:
            pass

    def set_cancellation_token(self, token: Any) -> None:
        """Sets the active BargeInCancellationToken for in-flight tool execution loops."""
        self._active_cancel_token = token
        if hasattr(self, "tool_orchestrator") and self.tool_orchestrator:
            self.tool_orchestrator.cancel_token = token

    def get_cancellation_token(self) -> Any | None:
        """Returns the active BargeInCancellationToken if set."""
        return self._active_cancel_token

    def cancel_active_turn(self) -> None:
        """Aborts any in-flight inference, tool execution, or Antigravity child processes."""
        if self._active_cancel_token and hasattr(self._active_cancel_token, "cancel"):
            self._active_cancel_token.cancel()
        if hasattr(self, "tool_orchestrator") and self.tool_orchestrator:
            try:
                self.tool_orchestrator.reset()
            except Exception:
                pass
        if hasattr(self, "antigravity_bridge") and self.antigravity_bridge:
            try:
                self.antigravity_bridge.abort_active_turn()
            except Exception:
                pass
