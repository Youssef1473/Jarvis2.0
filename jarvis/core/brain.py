"""Jarvis 3.0 local-first brain with opt-in cloud specialist consultations."""
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from google import genai
from google.genai import types
from PIL import Image

from jarvis.skills.base import SkillRegistry
# Ensure skills are imported and registered
import jarvis.skills.os_control
import jarvis.skills.web_search
import jarvis.skills.dev_shell
import jarvis.skills.productivity
import jarvis.skills.calendar_manager
import jarvis.skills.browser_agent
import jarvis.skills.email_manager
import jarvis.skills.screen_vision
import jarvis.skills.timers
import jarvis.skills.protocols
import jarvis.skills.system_maintenance
import jarvis.skills.media_dj
import jarvis.skills.briefing
import jarvis.skills.project_manager
import jarvis.skills.notes
import jarvis.skills.system_watchdog
import jarvis.skills.deep_research
import jarvis.skills.document_ingest
import jarvis.skills.self_evolution
import jarvis.skills.photo_manager
import jarvis.skills.diagnostics
import jarvis.skills.browser_cdp
import jarvis.skills.windows_uia
import jarvis.skills.voice_control
import jarvis.skills.everything_search
import jarvis.skills.rstrtmgr_lock_surgeon
import jarvis.skills.job_object_sandbox
import jarvis.skills.nvml_dxgi_vram_arbiter
import jarvis.skills.usn_journal_delta_radar
import jarvis.skills.reflex_macro_compiler
import jarvis.skills.ast_code_surgeon
import jarvis.skills.hybrid_actuator
import jarvis.skills.mcp_manager
import jarvis.skills.modern_web
import jarvis.skills.gemini_intelligence
import jarvis.skills.chrome_devtools
from jarvis.vision.screen_capture import ScreenPerception
from jarvis.core.memory import MemoryManager
from jarvis.core.safety import SafetyGate
from jarvis.core.services import ManagedServiceManager
from jarvis.core.antigravity_bridge import AntigravityBridge
from jarvis.core.github_bridge import GitHubCopilotBridge
from jarvis.core.reflex_engine import DynamicReflexEngine
from jarvis.core.cognition import CognitiveRouter
from jarvis.context.context_orchestrator import ContextOrchestrator, BASE_SYSTEM_PROMPT as CONTEXT_BASE_SYSTEM_PROMPT
from jarvis.core.reflex_dispatcher import ReflexDispatcher
from jarvis.core.pipeline.latency_profiler import PipelineLatencyProfiler, TurnTrace

logger = logging.getLogger("Jarvis.Brain")


class JarvisBrain:
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
        config: Dict[str, Any],
        memory_manager: MemoryManager,
        safety_gate: SafetyGate,
        service_manager: Optional[ManagedServiceManager] = None,
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
        self.openai_messages: List[Dict[str, Any]] = []

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
        self.last_latencies: Dict[str, float] = {
            "ollama": 0.0,
            "groq": 0.0,
            "gemini": 0.0,
            "antigravity": 0.0,
            "github": 0.0,
            "openrouter": 0.0,
        }

        # Monotonic cancellation token for preempting in-flight tool actuation on speech barge-in
        self._active_cancel_token: Optional[Any] = None

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
        self._current_turn_executed_tools: List[Tuple[str, Dict[str, Any]]] = []
        self._current_turn_resolved_tool_outputs: Dict[str, Any] = {}
        self.tts = None

        from jarvis.core.pipeline import InterimSpeechBridge, ToolOrchestrator, TurnTaintTracker
        self.interim_bridge = InterimSpeechBridge(
            speak_fn=self._speak_interim_phrase,
            is_speaking_fn=self._is_speaking,
        )
        self.tool_orchestrator = ToolOrchestrator(
            interim_bridge=self.interim_bridge,
            taint_tracker=TurnTaintTracker.get_instance(),
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

    def _execute_tool_with_orchestrator(self, tool_name: str, tool_args: Dict[str, Any], step_index: int = 1) -> Any:
        """Executes a tool call through ToolOrchestrator for taint tracking, loop guards, and interim audio."""
        if self._active_cancel_token and hasattr(self._active_cancel_token, "raise_if_cancelled"):
            self._active_cancel_token.raise_if_cancelled()
        if hasattr(self, "tool_orchestrator") and self.tool_orchestrator:
            return self.tool_orchestrator.execute_step(tool_name, tool_args, step_index=step_index)
        return SkillRegistry.execute(tool_name, **tool_args)

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

    def __del__(self):
        """Ensures spawned processes are not orphaned when the brain instance is collected."""
        try:
            self.shutdown(timeout_seconds=1.0)
        except Exception:
            pass

    def _synthesize_error_lesson(self, tool_name: str, tool_args: Dict[str, Any], failure_signature: str) -> str:
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

    def _record_tool_execution(self, tool_name: str, tool_args: Dict[str, Any], tool_result: Any = None):
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

    def get_current_turn_tool_outputs(self) -> Dict[str, Any]:
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

    def _init_hybrid_providers(self) -> None:
        """Initializes Tri-Tier Hybrid Providers: Local Ollama, Groq LPU, and Gemini Multimodal."""
        from openai import OpenAI
        llm_cfg = self.config.get("llm", {})

        # 1. Local Ollama (Tier 1: private, offline, fast local OS actions)
        ollama_base = llm_cfg.get("ollama_base_url", "http://127.0.0.1:11434/v1")
        self._ensure_local_ollama(ollama_base)
        try:
            self.ollama_client = OpenAI(
                api_key="ollama",
                base_url=ollama_base,
                max_retries=0,
                timeout=float(llm_cfg.get("ollama_timeout_seconds", 180)),
            )
            logger.info(f"Hybrid Tier 1 (Local Ollama) active with model: {self.ollama_model}")
        except Exception as e:
            logger.warning(f"Could not connect to local Ollama: {e}")

        # 2. Groq Cloud LPU (Tier 2: ultra-fast 500+ tok/s 27B intellect for spoken dialogue & code)
        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        if groq_key:
            try:
                self.groq_client = OpenAI(
                    api_key=groq_key,
                    base_url="https://api.groq.com/openai/v1",
                    max_retries=0,
                    timeout=12.0,
                )
                logger.info(f"Hybrid Tier 2 (Groq LPU) active with model: {self.groq_model}")
            except Exception as e:
                logger.warning(f"Could not initialize Groq client: {e}")

        # 3. Google Gemini (Tier 3: multimodal vision & deep context reasoning)
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        if gemini_key:
            try:
                self._init_gemini()
                logger.info(f"Hybrid Tier 3 (Gemini Multimodal) active with {len(self.gemini_candidates)} candidate models")
            except Exception as e:
                logger.warning(f"Could not initialize Gemini client: {e}")

        # Set default active OpenAI client for general queries
        self.openai_client = self.groq_client if self.groq_client else self.ollama_client
        self.openai_model = self.groq_model if self.groq_client else self.ollama_model

        self.openai_messages = [
            {"role": "system", "content": self._get_system_instruction()}
        ]
        recent_turns = self.memory.get_recent_interactions(limit=6)
        for turn in recent_turns:
            u_text = turn.get("user_input", "").strip()
            j_text = turn.get("jarvis_reply", "").strip()
            if u_text and j_text:
                if any(bad in j_text.lower() for bad in ("limit for tool steps", "cannot launch applications", "not a web browser or an os")):
                    continue
                self.openai_messages.append({"role": "user", "content": u_text})
                self.openai_messages.append({"role": "assistant", "content": j_text})
        logger.info(f"Jarvis Tri-Tier Hybrid Core active ({len(recent_turns)} prior turns restored).")

    def _init_openai_compatible(self) -> bool:
        """Initializes one explicitly selected OpenAI-compatible provider."""
        omniroute_key = os.getenv("OMNIROUTE_API_KEY", "").strip()
        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        xai_key = os.getenv("XAI_API_KEY", "").strip() or os.getenv("GROK_API_KEY", "").strip()

        api_key = ""
        base_url = ""

        # Local inference never requires an API key or falls through to a hosted service.
        if self.provider == "ollama":
            llm_cfg = self.config.get("llm", {})
            api_key = "ollama"
            base_url = llm_cfg.get("ollama_base_url", "http://127.0.0.1:11434/v1")
            self.openai_model = llm_cfg.get("ollama_model", "qwen3.5:2b-q4_K_M")
            self._ensure_local_ollama(base_url)
        # Hosted providers are used only when selected in config; credentials never override that choice.
        elif self.provider == "omniroute":
            base_url = self.config.get("llm", {}).get("omniroute_base_url", "http://127.0.0.1:20128/v1")
            api_key = omniroute_key
            default_model = "groq/qwen/qwen3.8-27b"
            self.openai_model = self.config.get("llm", {}).get("omniroute_model", default_model)
            if not api_key:
                logger.error("OMNIROUTE_API_KEY is missing; refusing to start or call the gateway.")
                return False
            # Lightweight health check: probe if OmniRoute is reachable
            try:
                import urllib.request
                req = urllib.request.Request(f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {api_key}"})
                with urllib.request.urlopen(req, timeout=1.5):
                    pass
                logger.info(f"Jarvis OmniRoute link verified at {base_url}")
                self._adopt_local_service("OmniRoute", base_url, ("omniroute",))
            except Exception:
                logger.info(f"OmniRoute gateway at {base_url} is offline. Attempting to start a managed service...")
                try:
                    import time
                    service_process = subprocess.Popen(
                        ["omniroute", "serve", "--no-open", "--no-tray"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        close_fds=True,
                    )
                    self.service_manager.register("OmniRoute", service_process)
                    time.sleep(2.0)
                    req = urllib.request.Request(f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {api_key}"})
                    with urllib.request.urlopen(req, timeout=2.0):
                        pass
                    logger.info("Managed OmniRoute service started successfully.")
                except Exception as e:
                    logger.warning("OmniRoute gateway at %s is not responding (%s); no provider fallback will be attempted.", base_url, e)
                    return False
        elif self.provider == "groq" or (groq_key and groq_key.startswith("gsk_")):
            api_key = groq_key
            base_url = "https://api.groq.com/openai/v1"
            self.provider = "groq"
            default_model = "qwen/qwen3.8-27b"
            self.openai_model = self.config.get("llm", {}).get("groq_model", default_model)
        elif self.provider == "grok" or (xai_key and xai_key.startswith("xai-")):
            api_key = xai_key
            base_url = "https://api.x.ai/v1"
            self.provider = "grok"
            default_model = "grok-4.6"
            self.openai_model = self.config.get("llm", {}).get("grok_model", default_model)
        elif groq_key:
            api_key = groq_key
            base_url = "https://api.groq.com/openai/v1"
            self.provider = "groq"
            self.openai_model = "qwen/qwen3.8-27b"

        if not api_key:
            logger.error("No API key is configured for provider %s; refusing to switch providers.", self.provider)
            return False

        try:
            from openai import OpenAI
            self.openai_client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=0,
                timeout=float(self.config.get("llm", {}).get("ollama_timeout_seconds", 180))
                if self.provider == "ollama" else 12.0,
            )
            self.openai_messages = [
                {"role": "system", "content": self._get_system_instruction()}
            ]
            # Restore recent interactions from persistent SQLite memory
            recent_turns = self.memory.get_recent_interactions(limit=6)
            for turn in recent_turns:
                u_text = turn.get("user_input", "").strip()
                j_text = turn.get("jarvis_reply", "").strip()
                if u_text and j_text:
                    if any(bad in j_text.lower() for bad in ("limit for tool steps", "cannot launch applications", "not a web browser or an os")):
                        continue
                    self.openai_messages.append({"role": "user", "content": u_text})
                    self.openai_messages.append({"role": "assistant", "content": j_text})
            logger.info(f"Jarvis {self.provider.upper()} link initialized with model: {self.openai_model} ({len(recent_turns)} prior turns restored).")
            return True
        except Exception as e:
            if self.provider == "ollama":
                logger.error("Could not initialize the local Ollama client: %s. Cloud fallback is disabled.", e)
            elif self.provider == "groq":
                logger.error(f"Failed to initialize Groq client: {e}. Refusing to switch providers.")
            else:
                logger.error("Failed to initialize %s client: %s. Refusing to switch providers.", self.provider, e)
            return False

    def _ensure_local_ollama(self, base_url: str) -> None:
        """Start the installed Ollama server in the background if it is not reachable."""
        server_url = base_url.split("/v1", 1)[0].rstrip("/")
        try:
            import urllib.request
            with urllib.request.urlopen(f"{server_url}/api/tags", timeout=1.0):
                logger.info("Local Ollama server is already running.")
                self._adopt_local_service("Ollama", server_url, ("ollama",))
                return
        except Exception:
            pass

        ollama_exe = shutil.which("ollama")
        if not ollama_exe:
            local_app_data = os.getenv("LOCALAPPDATA", "")
            candidate = os.path.join(local_app_data, "Programs", "Ollama", "ollama.exe")
            if os.path.isfile(candidate):
                ollama_exe = candidate
        if not ollama_exe:
            logger.warning("Ollama is not reachable and its executable was not found.")
            return

        try:
            service_process = subprocess.Popen(
                [ollama_exe, "serve"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
            self.service_manager.register("Ollama", service_process)
            import urllib.request
            for _ in range(10):
                time.sleep(0.5)
                try:
                    with urllib.request.urlopen(f"{server_url}/api/tags", timeout=1.0):
                        logger.info("Started the local Ollama server.")
                        return
                except Exception:
                    continue
            logger.warning("Started Ollama, but its local API is not responding yet.")
        except Exception as e:
            logger.warning("Could not start the local Ollama server: %s", e)

    def _adopt_local_service(self, name: str, service_url: str, identity_tokens: tuple[str, ...]) -> None:
        """Track a verified local dependency so it also stops when Jarvis exits."""
        llm_cfg = self.config.get("llm", {})
        if not llm_cfg.get("stop_existing_local_services_on_exit", True):
            return
        parsed = urlsplit(service_url)
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or not parsed.port:
            return
        self.service_manager.adopt_local_listener(name, parsed.port, identity_tokens)

    def _init_gemini(self, model_name: str | None = None):
        """Initializes Google Gemini client and chat session."""
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            logger.warning("No GEMINI_API_KEY provided. Gemini LLM calls will fail until configured.")
            return

        self.gemini_client = genai.Client(api_key=api_key)
        if model_name:
            self.gemini_model = model_name

        all_tools = {
            name: tool for name, tool in SkillRegistry.get_all_tools().items()
            if name in self.active_tool_names
        }
        all_tools = list(all_tools.values())
        system_instruction = self._get_system_instruction()

        # Restore recent interactions for Gemini history
        history = []
        recent_turns = self.memory.get_recent_interactions(limit=6)
        for turn in recent_turns:
            u_text = turn.get("user_input", "").strip()
            j_text = turn.get("jarvis_reply", "").strip()
            if u_text and j_text:
                history.append(types.Content(role="user", parts=[types.Part.from_text(text=u_text)]))
                history.append(types.Content(role="model", parts=[types.Part.from_text(text=j_text)]))

        self.gemini_chat = self.gemini_client.chats.create(
            model=self.gemini_model,
            history=history if history else None,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=self.temperature,
                tools=all_tools,
            )
        )
        self.gemini_task_mode = self.active_task_mode
        self.gemini_tool_names = self.active_tool_names
        logger.info(f"Jarvis Gemini session initialized with model: {self.gemini_model} ({len(recent_turns)} prior turns restored).")

    def _init_antigravity(self) -> bool:
        """Initializes the local Antigravity CLI bridge using the user's Google AI Pro subscription."""
        if not self.antigravity_bridge.is_available():
            logger.error("Antigravity CLI ('agy') is not available on this machine.")
            return False
        logger.info(f"Jarvis Antigravity Core initialized with model: {self.antigravity_model}")
        return True

    def _init_github(self) -> bool:
        """Initializes GitHub Copilot CLI using the restricted Jarvis agent profile."""
        if not self.github_bridge.is_available():
            logger.error("GitHub Copilot CLI is unavailable or its restricted Jarvis agent profile is missing.")
            return False
        logger.info("Jarvis GitHub Copilot CLI bridge initialized with tools disabled.")
        return True

    def _init_openrouter(self) -> bool:
        """Initializes the OpenRouter provider with configured free model candidates."""
        from jarvis.core.providers.openrouter import OpenRouterProvider

        or_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not or_key:
            logger.error(
                "OPENROUTER_API_KEY is not set. Add it to .env to use the OpenRouter free fleet, Sir."
            )
            return False

        llm_cfg = self.config.get("llm", {})
        primary_model = llm_cfg.get("openrouter_model", self.OPENROUTER_FREE_MODELS[0])
        max_tokens = int(llm_cfg.get("max_output_tokens", 512))

        self.openrouter_provider = OpenRouterProvider(
            model=primary_model,
            api_key=or_key,
            temperature=self.temperature,
            candidate_models=list(self.OPENROUTER_FREE_MODELS),
            config={"max_output_tokens": max_tokens},
        )
        self.openai_messages = [
            {"role": "system", "content": self._get_system_instruction()}
        ]
        # Restore recent interactions from SQLite memory
        recent_turns = self.memory.get_recent_interactions(limit=6)
        for turn in recent_turns:
            u_text = turn.get("user_input", "").strip()
            j_text = turn.get("jarvis_reply", "").strip()
            if u_text and j_text:
                if any(bad in j_text.lower() for bad in ("limit for tool steps", "cannot launch applications")):
                    continue
                self.openai_messages.append({"role": "user", "content": u_text})
                self.openai_messages.append({"role": "assistant", "content": j_text})

        logger.info(
            "Jarvis OpenRouter fleet initialized — primary: '%s' | %d free candidates ready | %d prior turns restored.",
            primary_model, len(self.OPENROUTER_FREE_MODELS), len(recent_turns),
        )
        return True

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

    def _try_fast_reflex(self, norm_text: str, dry_run: bool = False) -> Optional[str | bool]:
        """Executes unambiguous local OS, desktop, media, and browser commands in <10ms.
        Bypasses slow cloud round-trips for instant physical and spoken execution.
        If dry_run=True, tests for a reflex hit without executing any skills.
        """
        return self._get_reflex_dispatcher().try_fast_reflex(norm_text, dry_run=dry_run)

    @staticmethod
    def _narrate_tool_action(fn_name: str, fn_args: Dict[str, Any]) -> str:
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
    ) -> Tuple[str, str, Dict[str, Any]]:
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

    def _process_github_query(self, user_text: str, sentence_callback: Any = None) -> str:
        """Processes user input using the restricted GitHub Copilot CLI bridge."""
        if not self.github_bridge.is_available():
            logger.error("GitHub Copilot CLI bridge is unavailable.")
            return "GitHub Copilot CLI is not connected. Install it and authenticate with `copilot login`, Sir."

        logger.info("Routing query to GitHub Copilot CLI with tools disabled.")

        result = self.github_bridge.generate_response(
            prompt=user_text,
            system_instruction=self._get_system_instruction(user_text),
        )
        if result.get("status") != "SUCCESS":
            error_text = (result.get("response") or "The CLI returned no response.").strip()
            logger.error("GitHub Copilot CLI request failed: %s", error_text[:300])
            return f"My GitHub Copilot CLI link could not complete that request: {error_text}"

        response_text = (result.get("response") or "").strip()
        if not response_text:
            logger.error("GitHub Copilot CLI marked an empty response as successful.")
            return "GitHub Copilot CLI returned an empty response, Sir."

        if sentence_callback:
            speech_buffer = response_text
            while speech_buffer:
                chunk, remaining = self._extract_next_speech_chunk(speech_buffer)
                if chunk is None:
                    chunk, remaining = speech_buffer, ""
                spoken = self._clean_speech_clause(chunk)
                if spoken:
                    sentence_callback(spoken)
                speech_buffer = remaining
        self.memory.log_interaction(user_text, response_text)
        self.last_latencies["github"] = float(result.get("duration_seconds", 0.0)) * 1000.0
        return response_text

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

    def specialist_status(self) -> Dict[str, str]:
        """Return safe, non-secret availability info for the HUD."""
        llm_cfg = self.config.get("llm", {})
        specialists = llm_cfg.get("specialists", {})
        enabled = specialists.get("enabled", True)
        metered_enabled = specialists.get("allow_metered_api_calls", False)
        ag_available = getattr(self, "antigravity_bridge", None) and self.antigravity_bridge.is_available()
        return {
            "hybrid": "ACTIVE" if self.provider in ("hybrid", "auto") else "STANDBY",
            "antigravity": f"ACTIVE ({self.antigravity_model.upper()})" if self.provider in ("antigravity", "agy") else ("READY (PRO TIER)" if ag_available else "UNAVAILABLE"),
            "github": (
                "ACTIVE (GITHUB COPILOT CLI)"
                if self.provider in ("github", "github_copilot", "github-copilot", "copilot", "gh")
                and self.github_bridge.is_available()
                else ("CLI FOUND (AUTH NOT VERIFIED)" if self.github_bridge.is_available() else "UNAVAILABLE")
            ),
            "ollama": "ACTIVE" if (getattr(self, "ollama_client", None) or (self.provider == "ollama" and self.openai_client)) else "LOCAL PRIMARY",
            "gemini": "ACTIVE" if self.gemini_client else ("READY (ANTIGRAVITY)" if ag_available else ("KEY SET" if os.getenv("GEMINI_API_KEY") else "ADD KEY")),
            "groq": "ACTIVE" if (getattr(self, "groq_client", None) or (self.provider == "groq" and self.openai_client)) else ("KEY SET" if os.getenv("GROQ_API_KEY") else "ADD KEY"),
            "grok": "READY" if enabled and metered_enabled and (os.getenv("XAI_API_KEY", "").strip() or os.getenv("GROK_API_KEY", "").strip()) else "METERED / OFF",
            "claude": "READY (ANTIGRAVITY)" if ag_available else ("READY" if enabled and metered_enabled and os.getenv("ANTHROPIC_API_KEY", "").strip() else "METERED / OFF"),
            "openrouter": (
                f"ACTIVE ({getattr(self.openrouter_provider, 'model', '?')})"
                if self.provider in ("openrouter", "open_router", "or") and getattr(self, "openrouter_provider", None) and self.openrouter_provider.is_available()
                else ("KEY SET (FREE FLEET)" if os.getenv("OPENROUTER_API_KEY") else "ADD KEY")
            ),
        }

    def get_latencies(self) -> Dict[str, float]:
        """Returns the most recent measured round-trip inference latencies in milliseconds."""
        return dict(self.last_latencies)

    def get_latency_profiler(self) -> PipelineLatencyProfiler:
        """Returns the nanosecond stage-by-stage PipelineLatencyProfiler instance."""
        return self.latency_profiler

    def _consult_specialist(self, provider: str, prompt: str) -> str:
        """Run only a user-requested secondary consultation; never changes the primary model."""
        llm_cfg = self.config.get("llm", {})
        specialists = llm_cfg.get("specialists", {})
        if not specialists.get("enabled", True):
            return "Specialist consultations are disabled in config, Sir."

        # OpenRouter Free Fleet Consultation
        if provider in {"openrouter", "open_router", "or", "nemotron", "cohere", "free"}:
            or_provider = getattr(self, "openrouter_provider", None)
            if not or_provider:
                from jarvis.core.providers.openrouter import OpenRouterProvider
                or_key = os.getenv("OPENROUTER_API_KEY", "").strip()
                if not or_key:
                    return "OpenRouter is not connected. Add OPENROUTER_API_KEY to .env to use the free fleet, Sir."
                or_provider = OpenRouterProvider(
                    api_key=or_key,
                    candidate_models=list(self.OPENROUTER_FREE_MODELS),
                    config={"max_output_tokens": int(llm_cfg.get("max_output_tokens", 512))},
                )
            from jarvis.core.providers.base import UnifiedRunner
            runner = UnifiedRunner(safety_gate=self.safety, max_iterations=1, per_turn_timeout=30.0)
            reply, _ = runner.execute_session(
                provider=or_provider,
                messages=[{"role": "user", "content": prompt}],
                system_instruction="You are a specialist consultant for Jarvis. Answer the request clearly and concisely.",
            )
            return reply or f"OpenRouter returned no text, Sir."

        # Antigravity CLI Consultation (Google AI Pro & Claude Sonnet integration)

        ag_models_map = {
            "pro": "gemini-3.8-flash-high",
            "gemini-pro": "gemini-3.8-flash-high",
            "flash": "gemini-3.8-flash-low",
            "gemini-flash": "gemini-3.8-flash-low",
            "flash-low": "gemini-3.8-flash-low",
            "flash-medium": "gemini-3.8-flash-medium",
            "flash-high": "gemini-3.8-flash-high",
            "argon": "gemini-4-argon-high",
            "claude": "claude-sonnet-4-6",
            "sonnet": "claude-sonnet-4-6",
            "opus": "claude-opus-4-6-thinking",
            "gpt-oss": "gpt-oss-120b-medium",
            "security": "claude-sonnet-4-6",
            "architect": "claude-sonnet-4-6",
            "code_review": "claude-sonnet-4-6",
            "antigravity": self.antigravity_model,
            "agy": self.antigravity_model,
        }
        if (provider in ag_models_map or (provider == "gemini" and not os.getenv("GEMINI_API_KEY"))) and getattr(self, "antigravity_bridge", None) and self.antigravity_bridge.is_available():
            target_model = ag_models_map.get(provider, self.antigravity_model)
            logger.info("Routing specialist '%s' consultation to ANTIGRAVITY BRIDGE (%s)...", provider, target_model)
            specialist_instruction = "You are a specialist consultant for Jarvis. Answer the user's specific request clearly and concisely."
            if provider == "security":
                custom_p = self.load_curated_prompt("whitebox_security_auditor")
                if custom_p:
                    specialist_instruction = custom_p
            elif provider in {"architect", "code_review"}:
                custom_p = self.load_curated_prompt("phased_ast_code_reviewer")
                if custom_p:
                    specialist_instruction = custom_p
            res = self.antigravity_bridge.generate_response(
                prompt=prompt,
                model=target_model,
                system_instruction=specialist_instruction,
                timeout=60.0
            )
            return (res.get("response") or f"{provider.title()} returned no text, Sir.").strip()

        if provider in {"github", "github-copilot", "copilot", "gh"} and getattr(self, "github_bridge", None) and self.github_bridge.is_available():
            logger.info("Routing specialist '%s' consultation to GitHub Copilot CLI with tools disabled.", provider)
            res = self.github_bridge.generate_response(
                prompt=prompt,
                system_instruction="You are a specialist consultant for Jarvis. Answer the user's specific request clearly and concisely.",
            )
            if res.get("status") == "SUCCESS" and (res.get("response") or "").strip():
                return res["response"].strip()
            error_text = (res.get("response") or "The CLI returned no response.").strip()
            logger.error("GitHub Copilot specialist consultation failed: %s", error_text[:300])
            return f"GitHub Copilot CLI could not complete the consultation: {error_text}"

        if provider in {"grok", "claude"} and not specialists.get("allow_metered_api_calls", False):
            return (
                f"{provider.title()} API consultations are disabled to protect your no-spend setting, Sir. "
                "Their developer APIs are usage-billed; enable allow_metered_api_calls only if you accept that."
            )

        models = specialists.get("models", {})
        if provider == "groq":
            api_key = os.getenv("GROQ_API_KEY", "").strip()
            if not api_key:
                return "Groq is not connected. Add GROQ_API_KEY to use an explicitly requested consultation, Sir."
            try:
                from openai import OpenAI
                client = OpenAI(
                    api_key=api_key,
                    base_url="https://api.groq.com/openai/v1",
                    max_retries=0,
                    timeout=45.0,
                )
                response = client.chat.completions.create(
                    model=models.get("groq", "qwen/qwen3.8-27b"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=self.temperature,
                    max_tokens=int(llm_cfg.get("max_output_tokens", 1024)),
                )
                return (response.choices[0].message.content or "Groq returned no text, Sir.").strip()
            except Exception as exc:
                logger.warning("Explicit Groq consultation failed: %s", exc)
                return "Groq could not complete that consultation. Your local Ollama conversation is still available, Sir."

        if provider == "gemini":
            api_key = os.getenv("GEMINI_API_KEY", "").strip()
            if not api_key:
                return "Gemini is not connected. Add GEMINI_API_KEY to use an explicitly requested consultation, Sir."
            try:
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(
                    model=models.get("gemini", "gemini-3.8-flash"),
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=(
                            "You are a specialist consultant for Jarvis. Answer the user's specific request "
                            "clearly and concisely. Do not claim actions were performed."
                        ),
                        temperature=self.temperature,
                        max_output_tokens=int(llm_cfg.get("max_output_tokens", 1024)),
                    ),
                )
                return (response.text or "Gemini returned no text, Sir.").strip()
            except Exception as exc:
                logger.warning("Explicit Gemini consultation failed: %s", exc)
                return "Gemini could not complete that consultation. Your local Ollama conversation is still available, Sir."

        if provider == "grok":
            api_key = os.getenv("XAI_API_KEY", "").strip() or os.getenv("GROK_API_KEY", "").strip()
            if not api_key:
                return "Grok is not connected. Add XAI_API_KEY to use an explicitly requested consultation, Sir."
            model = models.get("grok", "grok-4.6")
            base_url = "https://api.x.ai/v1"
            messages = [{"role": "user", "content": prompt}]
            try:
                from openai import OpenAI
                client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=45.0)
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=int(llm_cfg.get("max_output_tokens", 1024)),
                )
                return (response.choices[0].message.content or "Grok returned no text, Sir.").strip()
            except Exception as exc:
                logger.warning("Explicit Grok consultation failed: %s", exc)
                return "Grok could not complete that consultation. Your local Ollama conversation is still available, Sir."

        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return "Claude is not connected. Add ANTHROPIC_API_KEY to use an explicitly requested consultation, Sir."
        payload = json.dumps({
            "model": models.get("claude", "claude-sonnet-4-6"),
            "max_tokens": int(llm_cfg.get("max_output_tokens", 1024)),
            "system": "You are a specialist consultant for Jarvis. Answer clearly and concisely. Do not claim actions were performed.",
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        request = Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=45.0) as response:
                body = json.loads(response.read().decode("utf-8"))
            answer = "".join(block.get("text", "") for block in body.get("content", []) if block.get("type") == "text")
            return answer.strip() or "Claude returned no text, Sir."
        except Exception as exc:
            err_msg = str(exc)
            logger.warning("Explicit Claude consultation failed: %s", exc)
            if hasattr(self, "antigravity_bridge") and self.antigravity_bridge.is_available() and self.antigravity_bridge._is_token_exhaustion_error(err_msg):
                self.antigravity_bridge.mark_claude_exhausted(reason=err_msg)
                is_complex = self.antigravity_bridge.is_task_complex(prompt)
                fallback_tier = "gemini-3.8-flash-high" if is_complex else "gemini-3.8-flash-medium"
                logger.warning(
                    "Claude consultation token limit reached. Seamlessly falling back to Antigravity %s (complexity: %s)...",
                    fallback_tier, "high" if is_complex else "moderate"
                )
                res = self.antigravity_bridge.generate_response(
                    prompt=prompt,
                    model=fallback_tier,
                    system_instruction="You are a specialist consultant for Jarvis. Answer clearly and concisely. Do not claim actions were performed.",
                )
                if res.get("status") == "SUCCESS":
                    return res.get("response", "")
            return "Claude could not complete that consultation. Your local Ollama conversation is still available, Sir."

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
    def _extract_next_speech_chunk(buffer: str) -> tuple[Optional[str], str]:
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

    def _process_openai_compatible_query(self, user_text: str, sentence_callback: Any = None) -> str:
        """Processes user input using OmniRoute, Groq, or xAI Grok with real-time streaming TTS and dynamic failover."""
        t_start = time.perf_counter()
        # Determine model candidates with Smart Dynamic Tiering
        if (self.provider == "groq" or self.openai_client == getattr(self, "groq_client", None)) and hasattr(self, "groq_candidates"):
            candidate_models = list(self.groq_candidates)
        else:
            candidate_models = [self.openai_model]

        # Keep system instruction up-to-date with latest dynamic memory facts and past episodic lessons
        if self.openai_messages and self.openai_messages[0]["role"] == "system":
            self.openai_messages[0]["content"] = self._get_system_instruction(user_text)
        
        # Prepare user payload with vision if requested
        include_vision = self.should_capture_screen(user_text)
        if include_vision:
            from jarvis.skills.screen_vision import _get_active_window_details, check_visual_privacy
            w_title, w_proc = _get_active_window_details()
            privacy_alert = check_visual_privacy(w_title, w_proc)
            if privacy_alert:
                logger.warning("Visual Privacy Guard blocked capture during streaming: %s", privacy_alert)
                user_msg = {"role": "user", "content": f"{user_text}\n[SYSTEM NOTE: Visual screen capture was blocked by Visual Privacy Guard because active window '{w_title}' contains sensitive credentials.]"}
                include_vision = False
            else:
                img_bytes = ScreenPerception.capture_screen_bytes(max_width=1024, quality=70)
                if img_bytes:
                    b64 = base64.b64encode(img_bytes).decode("utf-8")
                    user_msg = {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_text},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                        ]
                    }
                else:
                    user_msg = {"role": "user", "content": user_text}
                    include_vision = False
        else:
            user_msg = {"role": "user", "content": user_text}

        self.openai_messages.append(user_msg)
        tools = SkillRegistry.get_openai_tools(self.active_tool_names)
        max_output_tokens = int(self.config.get("llm", {}).get("max_output_tokens", 1024))

        last_error = None
        for model_candidate in candidate_models:
            logger.info(f"Routing query to {self.provider.upper()} ({model_candidate}) with real-time streaming...")
            try:
                # Update active model
                self.openai_model = model_candidate

                request_options = {}
                if self.provider == "ollama" and model_candidate.startswith("qwen3.5"):
                    # Keep everyday voice turns fast; allow extra deliberation for repository work.
                    if self.active_task_mode == "code":
                        request_options["reasoning_effort"] = self.config.get("llm", {}).get(
                            "ollama_code_reasoning_effort", "low"
                        )
                    else:
                        request_options["reasoning_effort"] = "none"
                elif self.provider == "groq" and model_candidate.startswith("qwen/"):
                    llm_cfg = self.config.get("llm", {})
                    tier = self.classify_query_tier(user_text) if llm_cfg.get("dynamic_tiering", True) else "deep"
                    reasoning_effort = llm_cfg.get(
                        "groq_fast_reasoning_effort" if tier == "fast" else "groq_deep_reasoning_effort",
                        "none" if tier == "fast" else "low",
                    )
                    request_options["reasoning_effort"] = reasoning_effort
                    logger.info("Groq Qwen reasoning effort selected: %s (%s tier)", reasoning_effort, tier)

                tool_calls_map = {}
                full_content_chunks = []
                text_buffer = ""

                # Step 1: Stream completion from provider
                stream = self.openai_client.chat.completions.create(
                    model=model_candidate,
                    messages=self.openai_messages,
                    tools=tools if tools else None,
                    temperature=0.0 if tools else self.temperature,
                    max_tokens=max_output_tokens,
                    stream=True,
                    **request_options,
                )

                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    # Accumulate streaming tool call deltas
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_map:
                                tool_calls_map[idx] = {
                                    "id": tc.id or "",
                                    "name": tc.function.name or "" if tc.function else "",
                                    "arguments": tc.function.arguments or "" if tc.function else "",
                                }
                            else:
                                if tc.id:
                                    tool_calls_map[idx]["id"] += tc.id
                                if tc.function and tc.function.name:
                                    tool_calls_map[idx]["name"] += tc.function.name
                                if tc.function and tc.function.arguments:
                                    tool_calls_map[idx]["arguments"] += tc.function.arguments

                    # Accumulate text content & stream to TTS immediately if no tool calls are pending
                    if delta.content:
                        full_content_chunks.append(delta.content)
                        if sentence_callback and not tool_calls_map:
                            text_buffer += delta.content
                            # Hold speech dispatch if accumulating inline XML tool calls
                            if "<tool_call" not in text_buffer and "<function=" not in text_buffer:
                                s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)
                                while s_clause:
                                    clean_clause = self._clean_speech_clause(s_clause)
                                    if clean_clause:
                                        sentence_callback(clean_clause)
                                    s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)

                had_tool_call = False
                last_tool_result = ""
                tool_iterations = 0
                max_tool_iterations = 16 if self.active_task_mode == "code" else 2
                executed_tool_signatures = set()

                action_tools = {
                    "launch_application", "open_url_in_desktop_browser", "close_application",
                    "adjust_volume", "control_media", "minimize_all_windows", "lock_workstation",
                    "take_desktop_screenshot", "shutdown_jarvis", "start_timer", "cancel_timer",
                    "execute_protocol", "get_daily_briefing", "inspect_ram_hogs", "optimize_workstation",
                    "play_youtube_media", "open_project", "open_user_folder",
                    "take_quick_note", "read_recent_notes", "search_notes",
                    "check_hardware_health", "start_hardware_watchdog", "stop_hardware_watchdog",
                    "deep_research"
                }

                # Step 2: Handle tool calls if invoked
                while tool_calls_map and tool_iterations < max_tool_iterations:
                    had_tool_call = True
                    tool_iterations += 1

                    tool_calls_list = []
                    for idx in sorted(tool_calls_map.keys()):
                        tc_data = tool_calls_map[idx]
                        call_id = tc_data["id"] or f"call_{idx}_{int(time.time())}"
                        tool_calls_list.append({
                            "id": call_id,
                            "type": "function",
                            "function": {"name": tc_data["name"], "arguments": tc_data["arguments"]}
                        })

                    # Cycle detection: check if any tool call in this batch has already been executed
                    has_repeated_call = False
                    for tc in tool_calls_list:
                        sig = (tc["function"]["name"], tc["function"]["arguments"].strip())
                        if sig in executed_tool_signatures:
                            logger.warning(f"Detected repeated tool call loop: {sig}. Halting tool iterations.")
                            has_repeated_call = True
                            break
                        executed_tool_signatures.add(sig)

                    if has_repeated_call:
                        tool_calls_map = {}
                        break

                    assistant_msg = {
                        "role": "assistant",
                        "content": "".join(full_content_chunks) or None,
                        "tool_calls": tool_calls_list
                    }
                    self.openai_messages.append(assistant_msg)

                    has_action_completed = False
                    for step_idx, tc in enumerate(tool_calls_list, start=1):
                        fn_name = tc["function"]["name"]
                        try:
                            fn_args = json.loads(tc["function"]["arguments"] or "{}")
                        except Exception:
                            fn_args = {}

                        logger.info(f"{self.provider.upper()} Tool call: {fn_name}({fn_args})")
                        if sentence_callback:
                            narrative = self._narrate_tool_action(fn_name, fn_args)
                            sentence_callback(narrative)

                        is_safe, warning = self.safety.check_tool_call(fn_name, fn_args)
                        if not is_safe:
                            tool_result = f"Action halted: {warning}"
                            if sentence_callback:
                                sentence_callback(f"Action halted: {warning}")
                        else:
                            if self._active_cancel_token and hasattr(self._active_cancel_token, "raise_if_cancelled"):
                                self._active_cancel_token.raise_if_cancelled()
                            tool_result = self._execute_tool_with_orchestrator(fn_name, fn_args, step_index=step_idx)
                            self._record_tool_execution(fn_name, fn_args, tool_result)
                            last_tool_result = str(tool_result)
                            if fn_name in action_tools:
                                has_action_completed = True

                        logger.info(f"{self.provider.upper()} Tool result: {tool_result}")
                        self.openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": str(tool_result),
                        })

                    # Reset buffers for conversational follow-up response
                    tool_calls_map = {}
                    full_content_chunks = []
                    text_buffer = ""

                    # For routine voice queries or when an OS action has completed, disable further tools
                    # on the follow-up request so small models (Qwen 2B) respond with conversational speech.
                    allow_further_tools = (
                        self.active_task_mode == "code"
                        or (not has_action_completed and tool_iterations < 2)
                    )
                    followup_tools = tools if (allow_further_tools and tools) else None

                    try:
                        followup_stream = self.openai_client.chat.completions.create(
                            model=self.openai_model,
                            messages=self.openai_messages,
                            tools=followup_tools,
                            temperature=self.temperature,
                            max_tokens=max_output_tokens,
                            stream=True,
                            **request_options,
                        )
                    except Exception as fe:
                        logger.warning(f"Post-tool follow-up with {self.openai_model} failed ({fe}). Attempting fallback synthesis...")
                        followup_stream = None
                        # 1. Try other candidate models if on Groq
                        if (self.provider in ("groq", "hybrid", "auto") or self.openai_client == getattr(self, "groq_client", None)) and hasattr(self, "groq_candidates"):
                            for alt_model in self.groq_candidates:
                                if alt_model != self.openai_model:
                                    try:
                                        logger.info(f"Attempting post-tool synthesis on fallback Groq candidate '{alt_model}'...")
                                        followup_stream = self.openai_client.chat.completions.create(
                                            model=alt_model,
                                            messages=self.openai_messages,
                                            tools=None,
                                            temperature=self.temperature,
                                            max_tokens=max_output_tokens,
                                            stream=True,
                                        )
                                        self.openai_model = alt_model
                                        break
                                    except Exception as alt_err:
                                        logger.warning(f"Fallback Groq model '{alt_model}' failed: {alt_err}")
                        # 2. Try local Ollama if Groq failed or was exhausted
                        if followup_stream is None and getattr(self, "ollama_client", None):
                            try:
                                logger.info(f"Attempting post-tool synthesis on LOCAL OLLAMA ({self.ollama_model})...")
                                followup_stream = self.ollama_client.chat.completions.create(
                                    model=self.ollama_model,
                                    messages=self.openai_messages,
                                    tools=None,
                                    temperature=self.temperature,
                                    max_tokens=max_output_tokens,
                                    stream=True,
                                    reasoning_effort="none",
                                )
                            except Exception as ollama_err:
                                logger.warning(f"Local Ollama synthesis fallback failed: {ollama_err}")
                        if followup_stream is None:
                            raise fe

                    for chunk in followup_stream:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        if delta.tool_calls and allow_further_tools:
                            for tc in delta.tool_calls:
                                idx = tc.index
                                if idx not in tool_calls_map:
                                    tool_calls_map[idx] = {
                                        "id": tc.id or "",
                                        "name": tc.function.name or "" if tc.function else "",
                                        "arguments": tc.function.arguments or "" if tc.function else "",
                                    }
                                else:
                                    if tc.id:
                                        tool_calls_map[idx]["id"] += tc.id
                                    if tc.function and tc.function.name:
                                        tool_calls_map[idx]["name"] += tc.function.name
                                    if tc.function and tc.function.arguments:
                                        tool_calls_map[idx]["arguments"] += tc.function.arguments
                        if delta.content:
                            full_content_chunks.append(delta.content)
                            if sentence_callback:
                                text_buffer += delta.content
                                s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)
                                while s_clause:
                                    clean_clause = self._clean_speech_clause(s_clause)
                                    if clean_clause:
                                        sentence_callback(clean_clause)
                                    s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)

                # Fallback if loop was halted while the model still generated pending tool calls:
                # Request a final conversational spoken reply with tools=None
                if tool_calls_map and not full_content_chunks:
                    tool_calls_map = {}
                    followup_stream = self.openai_client.chat.completions.create(
                        model=self.openai_model,
                        messages=self.openai_messages,
                        tools=None,
                        temperature=self.temperature,
                        max_tokens=max_output_tokens,
                        stream=True,
                        **request_options,
                    )
                    for chunk in followup_stream:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        if delta.content:
                            full_content_chunks.append(delta.content)
                            if sentence_callback:
                                text_buffer += delta.content
                                s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)
                                while s_clause:
                                    clean_clause = self._clean_speech_clause(s_clause)
                                    if clean_clause:
                                        sentence_callback(clean_clause)
                                    s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)

                raw_content = "".join(full_content_chunks).strip()
                if not had_tool_call and "<tool_call>" in raw_content:
                    fn_match = re.search(r'<function=([a-zA-Z0-9_]+)>', raw_content)
                    if fn_match:
                        had_tool_call = True
                        fn_name = fn_match.group(1)
                        fn_args = {}
                        for p_match in re.finditer(r'<parameter=([a-zA-Z0-9_]+)>\s*(.*?)\s*</parameter>', raw_content, re.DOTALL):
                            fn_args[p_match.group(1)] = p_match.group(2).strip()

                        logger.info(f"{self.provider.upper()} Inline tool call: {fn_name}({fn_args})")
                        is_safe, warning = self.safety.check_tool_call(fn_name, fn_args)
                        if not is_safe:
                            tool_result = f"Action halted: {warning}"
                        else:
                            if self._active_cancel_token and hasattr(self._active_cancel_token, "raise_if_cancelled"):
                                self._active_cancel_token.raise_if_cancelled()
                            tool_result = self._execute_tool_with_orchestrator(fn_name, fn_args, step_index=1)
                            self._record_tool_execution(fn_name, fn_args, tool_result)
                        last_tool_result = str(tool_result)
                        logger.info(f"{self.provider.upper()} Inline tool result: {tool_result}")

                        self.openai_messages.append({"role": "assistant", "content": raw_content})
                        self.openai_messages.append({
                            "role": "user",
                            "content": f"[System Tool Result: {tool_result}. Briefly acknowledge to Sir that this was completed.]"
                        })
                        try:
                            followup_stream = self.openai_client.chat.completions.create(
                                model=self.openai_model,
                                messages=self.openai_messages,
                                tools=None,
                                temperature=self.temperature,
                                max_tokens=max_output_tokens,
                                stream=True,
                                **request_options,
                            )
                        except Exception as fe:
                            followup_stream = None
                            if getattr(self, "ollama_client", None):
                                try:
                                    followup_stream = self.ollama_client.chat.completions.create(
                                        model=self.ollama_model,
                                        messages=self.openai_messages,
                                        tools=None,
                                        temperature=self.temperature,
                                        max_tokens=max_output_tokens,
                                        stream=True,
                                        reasoning_effort="none",
                                    )
                                except Exception:
                                    pass
                            if followup_stream is None:
                                raise fe

                        full_content_chunks = []
                        text_buffer = ""
                        for chunk in followup_stream:
                            if not chunk.choices:
                                continue
                            delta = chunk.choices[0].delta
                            if delta.content:
                                full_content_chunks.append(delta.content)
                                if sentence_callback:
                                    text_buffer += delta.content
                                    s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)
                                    while s_clause:
                                        clean_clause = self._clean_speech_clause(s_clause)
                                        if clean_clause:
                                            sentence_callback(clean_clause)
                                        s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)

                # Flush any remaining text in the buffer to TTS
                if sentence_callback and text_buffer.strip():
                    clean_clause = self._clean_speech_clause(text_buffer.strip())
                    if clean_clause:
                        sentence_callback(clean_clause)

                reply_text = "".join(full_content_chunks).strip() if full_content_chunks else "At your service, Sir."
                reply_text = re.sub(r"<tool_call>.*?</tool_call>", "", reply_text, flags=re.DOTALL).strip()
                if not reply_text:
                    reply_text = "Updated, Sir."

                self.openai_messages.append({"role": "assistant", "content": reply_text})
                self.memory.log_interaction(user_text, reply_text)
                if not had_tool_call:
                    self.memory.auto_learn_from_interaction(user_text, reply_text, provider=self.provider, extractor_fn=self._extract_memory_insights)

                # Bound memory window
                if len(self.openai_messages) > 25:
                    self.openai_messages = [self.openai_messages[0]] + self.openai_messages[-20:]

                elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
                prov_key = "ollama" if (getattr(self, "ollama_client", None) and self.openai_client == self.ollama_client) else "groq"
                self.last_latencies[prov_key] = elapsed_ms
                logger.info(f"Inference latency ({prov_key}): {elapsed_ms}ms")

                return reply_text

            except Exception as e:
                last_error = e
                if include_vision and user_msg.get("role") == "user" and isinstance(user_msg.get("content"), list):
                    logger.warning(
                        f"{self.provider.upper()} vision stream encountered error: {e}. Gracefully retrying with text query..."
                    )
                    user_msg["content"] = user_text
                    include_vision = False
                    continue
                if model_candidate != candidate_models[-1]:
                    next_model = candidate_models[candidate_models.index(model_candidate) + 1]
                    logger.warning(
                        f"{self.provider.upper()} model '{model_candidate}' unavailable or quota limit reached ({e}). "
                        f"Seamlessly switching to next fallback model: '{next_model}'..."
                    )
                    continue
                else:
                    logger.warning(f"{self.provider.upper()} request failed on final candidate '{model_candidate}': {e}.")
                    break

        # If tools executed successfully, NEVER return a failure message; deliver deterministic verbal acknowledgment
        if had_tool_call and last_tool_result:
            logger.info("Tool execution succeeded prior to synthesis failure. Delivering deterministic verbal acknowledgment...")
            reply_text = f"Action completed, Sir: {last_tool_result}"
            if sentence_callback:
                clean_s = self._clean_speech_clause(reply_text)
                if clean_s:
                    sentence_callback(clean_s)
            self.openai_messages.append({"role": "assistant", "content": reply_text})
            self.memory.log_interaction(user_text, reply_text)
            return reply_text

        # In hybrid mode, if Groq candidates are exhausted before any tool ran, cascade to Local Ollama or Gemini:
        if self.provider in ("hybrid", "auto") and self.openai_client == getattr(self, "groq_client", None):
            if getattr(self, "ollama_client", None):
                logger.warning(f"All Groq candidates exhausted ({last_error}). Seamlessly falling back to LOCAL OLLAMA...")
                self.openai_client = self.ollama_client
                self.openai_model = self.ollama_model
                return self._process_openai_compatible_query(user_text, sentence_callback)
            elif self.gemini_client:
                logger.warning(f"All Groq candidates exhausted ({last_error}). Seamlessly falling back to GEMINI...")
                return self._process_gemini_query(user_text, sentence_callback)

        logger.warning(f"All candidate models exhausted on {self.provider.upper()} (last error: {last_error}).")
        return (
            f"My {self.provider} model could not complete that request. "
            "I did not send it to another provider; please try again, Sir."
        )

    def _process_openrouter_query(self, user_text: str, sentence_callback: Any = None) -> str:
        """Processes user input via the OpenRouter free AI fleet with streaming TTS and dynamic failover."""
        t_start = time.perf_counter()

        if not self.openrouter_provider or not self.openrouter_provider.is_available():
            return "OpenRouter is not connected. Please set OPENROUTER_API_KEY in .env, Sir."

        # Keep system instruction up-to-date
        if self.openai_messages and self.openai_messages[0]["role"] == "system":
            self.openai_messages[0]["content"] = self._get_system_instruction(user_text)

        self.openai_messages.append({"role": "user", "content": user_text})
        tools = SkillRegistry.get_openai_tools(self.active_tool_names)

        turn_response = self.openrouter_provider.generate_turn(
            messages=self.openai_messages[1:],  # skip the system message; it's injected separately
            tools=tools if tools else None,
            system_instruction=self.openai_messages[0]["content"] if self.openai_messages else None,
            sentence_callback=sentence_callback,
            timeout=35.0,
        )

        reply_text = turn_response.content or ""

        # Handle tool calls returned by the model
        if turn_response.tool_calls:
            assistant_entry: Dict[str, Any] = {
                "role": "assistant",
                "content": reply_text or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in turn_response.tool_calls
                ],
            }
            self.openai_messages.append(assistant_entry)

            for step_idx, tc in enumerate(turn_response.tool_calls, start=1):
                fn_name, fn_args = tc.name, tc.arguments
                logger.info("OpenRouter tool dispatch: %s(%s)", fn_name, fn_args)
                if sentence_callback:
                    sentence_callback(self._narrate_tool_action(fn_name, fn_args))
                is_safe, warning = self.safety.check_tool_call(fn_name, fn_args)
                if not is_safe:
                    tool_result = f"Action halted: {warning}"
                    if sentence_callback:
                        sentence_callback(f"Action halted: {warning}")
                else:
                    tool_result = self._execute_tool_with_orchestrator(fn_name, fn_args, step_index=step_idx)
                    self._record_tool_execution(fn_name, fn_args, tool_result)
                self.openai_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(tool_result),
                })

            # Follow-up generation turn after tools
            followup = self.openrouter_provider.generate_turn(
                messages=self.openai_messages[1:],
                system_instruction=self.openai_messages[0]["content"] if self.openai_messages else None,
                sentence_callback=sentence_callback,
                timeout=30.0,
            )
            reply_text = followup.content or "Action completed, Sir."

        if not reply_text:
            reply_text = "At your service, Sir."

        reply_text = re.sub(r"<tool_call>.*?</tool_call>", "", reply_text, flags=re.DOTALL).strip()

        self.openai_messages.append({"role": "assistant", "content": reply_text})
        self.memory.log_interaction(user_text, reply_text)
        self.memory.auto_learn_from_interaction(
            user_text, reply_text, provider="openrouter", extractor_fn=self._extract_memory_insights
        )

        # Bound memory window
        if len(self.openai_messages) > 25:
            self.openai_messages = [self.openai_messages[0]] + self.openai_messages[-20:]

        elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
        self.last_latencies["openrouter"] = elapsed_ms
        logger.info(
            "OpenRouter inference latency: %sms | model: %s",
            elapsed_ms, turn_response.model,
        )
        return reply_text

    def _process_gemini_query(self, user_text: str, sentence_callback: Any = None) -> str:
        """Processes user input using Google Gemini with dynamic candidate failover."""

        t_start = time.perf_counter()
        if not self.gemini_client:
            api_key = os.getenv("GEMINI_API_KEY", "").strip()
            if api_key:
                self.gemini_client = genai.Client(api_key=api_key)
                self._init_gemini()
            else:
                return "My apologies, Sir. My cognitive link requires an API key in the environment or .env file."

        attempts = len(self.gemini_candidates)
        for attempt in range(attempts):
            active_model = self.gemini_candidates[self.gemini_model_idx]
            if (
                not self.gemini_chat
                or self.gemini_model != active_model
                or self.gemini_task_mode != self.active_task_mode
                or self.gemini_tool_names != self.active_tool_names
            ):
                self._init_gemini(active_model)

            try:
                contents = [user_text]

                if self.should_capture_screen(user_text):
                    from jarvis.skills.screen_vision import _get_active_window_details, check_visual_privacy
                    w_title, w_proc = _get_active_window_details()
                    privacy_alert = check_visual_privacy(w_title, w_proc)
                    if privacy_alert:
                        logger.warning("Visual Privacy Guard blocked capture during Gemini stream: %s", privacy_alert)
                        contents.append(f"[SYSTEM NOTE: Visual screen capture was blocked by Visual Privacy Guard because active window '{w_title}' contains sensitive credentials.]")
                    else:
                        screenshot = ScreenPerception.capture_screen()
                        if screenshot:
                            contents.append(screenshot)

                is_deep = (self.classify_query_tier(user_text) == "deep" or self.active_task_mode == "code")
                call_config = None if is_deep else types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_budget=0))

                text_buffer = ""
                full_content_chunks = []
                function_calls_accumulated = []

                # Stream tokens directly to Kokoro-82M speech callback in real time
                response_stream = self.gemini_chat.send_message_stream(contents, config=call_config)
                for chunk in response_stream:
                    if getattr(chunk, "function_calls", None):
                        function_calls_accumulated.extend(chunk.function_calls)
                    chunk_text = ""
                    try:
                        if hasattr(chunk, "candidates") and chunk.candidates:
                            cand = chunk.candidates[0]
                            if hasattr(cand, "content") and cand.content and cand.content.parts:
                                text_parts = [p.text for p in cand.content.parts if getattr(p, "text", None)]
                                chunk_text = "".join(text_parts)
                    except Exception:
                        chunk_text = getattr(chunk, "text", "") or ""

                    if chunk_text:
                        full_content_chunks.append(chunk_text)
                        if sentence_callback:
                            text_buffer += chunk_text
                            s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)
                            while s_clause:
                                clean_clause = self._clean_speech_clause(s_clause)
                                if clean_clause:
                                    sentence_callback(clean_clause)
                                s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)

                gemini_tool_iterations = 0
                max_gemini_tool_iterations = 16 if self.active_task_mode == "code" else 8
                while function_calls_accumulated and gemini_tool_iterations < max_gemini_tool_iterations:
                    gemini_tool_iterations += 1
                    tool_responses = []
                    for call in function_calls_accumulated:
                        tool_name = call.name
                        tool_args = dict(call.args) if call.args else {}
                        logger.info(f"Tool call: {tool_name}({tool_args})")
                        if sentence_callback:
                            narrative = self._narrate_tool_action(tool_name, tool_args)
                            sentence_callback(narrative)

                        is_safe, warning = self.safety.check_tool_call(tool_name, tool_args)
                        if not is_safe:
                            tool_result = f"Action halted: {warning}"
                            if sentence_callback:
                                sentence_callback(f"Action halted: {warning}")
                        else:
                            tool_result = self._execute_tool_with_orchestrator(tool_name, tool_args, step_index=gemini_tool_iterations)
                            self._record_tool_execution(tool_name, tool_args, tool_result)

                        logger.info(f"Tool result: {tool_result}")
                        tool_responses.append(types.Part.from_function_response(
                            name=tool_name,
                            response={"result": str(tool_result)}
                        ))

                    function_calls_accumulated = []
                    followup_stream = self.gemini_chat.send_message_stream(tool_responses, config=call_config)
                    for chunk in followup_stream:
                        if getattr(chunk, "function_calls", None):
                            function_calls_accumulated.extend(chunk.function_calls)
                        chunk_text = ""
                        try:
                            if hasattr(chunk, "candidates") and chunk.candidates:
                                cand = chunk.candidates[0]
                                if hasattr(cand, "content") and cand.content and cand.content.parts:
                                    text_parts = [p.text for p in cand.content.parts if getattr(p, "text", None)]
                                    chunk_text = "".join(text_parts)
                        except Exception:
                            chunk_text = getattr(chunk, "text", "") or ""

                        if chunk_text:
                            full_content_chunks.append(chunk_text)
                            if sentence_callback:
                                text_buffer += chunk_text
                                s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)
                                while s_clause:
                                    clean_clause = self._clean_speech_clause(s_clause)
                                    if clean_clause:
                                        sentence_callback(clean_clause)
                                    s_clause, text_buffer = self._extract_next_speech_chunk(text_buffer)

                # Flush any remaining text in speech buffer to TTS
                if sentence_callback and text_buffer.strip():
                    clean_clause = self._clean_speech_clause(text_buffer.strip())
                    if clean_clause:
                        sentence_callback(clean_clause)

                if function_calls_accumulated:
                    reply_text = (
                        "I reached the limit for tool steps on this request, "
                        "so I stopped before completing the remaining actions."
                    )
                else:
                    reply_text = "".join(full_content_chunks).strip() if full_content_chunks else "At your service, Sir."
                self.memory.log_interaction(user_text, reply_text)
                self.memory.auto_learn_from_interaction(user_text, reply_text, provider=self.provider, extractor_fn=self._extract_memory_insights)

                elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
                self.last_latencies["gemini"] = elapsed_ms
                logger.info(f"Inference latency (gemini streaming): {elapsed_ms}ms")

                return reply_text

            except Exception as e:
                err_str = str(e)
                is_quota_or_congestion = any(
                    k in err_str.lower() for k in ("503", "unavailable", "high demand", "429", "resource_exhausted", "quota", "limit", "rate")
                )
                if is_quota_or_congestion and attempt < attempts - 1:
                    time.sleep(0.5)
                    next_idx = (self.gemini_model_idx + 1) % len(self.gemini_candidates)
                    next_model = self.gemini_candidates[next_idx]
                    logger.warning(f"Model '{active_model}' quota/rate limit reached or busy ({err_str[:80]}). Seamlessly switching cognitive link to '{next_model}'...")
                    self.gemini_model_idx = next_idx
                    self.gemini_model = next_model
                    self._init_gemini(next_model)
                    continue

                logger.error(f"Error in Gemini processing: {e}")
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    return "My apologies, Sir. My neural uplink is currently experiencing high network congestion. Please give me a brief moment."
                return "I encountered a slight anomaly in my cognitive circuits, Sir. Please try again in a moment."

        return "My cognitive systems are temporarily congested, Sir. Please try again in a few moments."

    def _process_antigravity_query(self, user_text: str, sentence_callback: Any = None) -> str:
        """Processes queries using the local Antigravity CLI (Gemini 3.8 Flash High/Low, Claude Sonnet, Claude Opus)."""
        t_start = time.perf_counter()
        tools = SkillRegistry.get_openai_tools(self.active_tool_names)
        system_instruction = self._get_system_instruction(user_text)

        # If tools are relevant to this turn, embed tool schemas into the instruction
        prompt = user_text
        has_tools = bool(tools and self.active_tool_names)
        if has_tools:
            tool_signatures = []
            for t in tools:
                fn = t.get("function", {})
                params = fn.get("parameters", {}).get("properties", {})
                tool_signatures.append(f"- {fn.get('name')}: {fn.get('description')} (params: {list(params.keys())})")
            tools_block = (
                "Native OS & System Tools Available:\n" + "\n".join(tool_signatures) + "\n\n"
                "If you need to perform an action, retrieve system/calendar data, or build/implement/add a feature, respond with a JSON tool call:\n"
                '{"tool": "<name>", "arguments": {<args>}}\n'
                "NEVER claim or describe in conversational text that you created, modified, or added features unless you output a tool call to actually do it.\n"
                "Otherwise, respond with your direct conversational answer to Sir.\n\n"
            )
            prompt = tools_block + f"Sir asks: {user_text}"

        # Determine target model: honor explicit user prompt directives, active user model selection, and self-build rules
        model_override_match = re.search(
            r"\b(?:use|using|with)\s+(pro|flash|claude|sonnet|opus|argon|3\.8|3\.1|4)(?:\s+(low|medium|med|high))?\b",
            user_text,
            re.IGNORECASE,
        )
        if model_override_match:
            base = model_override_match.group(1).lower().strip()
            var = (model_override_match.group(2) or "").lower().strip()
            if base in ("pro", "3.1"):
                target_model = "gemini-3.8-flash-high"
            elif base in ("argon", "4"):
                eff = "medium" if var in ("medium", "med") else ("low" if var == "low" else "high")
                target_model = f"gemini-4-argon-{eff}"
            elif base in ("flash", "3.8"):
                self.antigravity_bridge.reset_gemini_flash_exhaustion()
                if var == "high":
                    target_model = "gemini-3.8-flash-high"
                elif var in ("medium", "med"):
                    target_model = "gemini-3.8-flash-medium"
                else:
                    target_model = "gemini-3.8-flash-low"
            elif base in ("claude", "sonnet"):
                self.antigravity_bridge.reset_claude_exhaustion()
                target_model = "claude-sonnet-4-6"
            elif base == "opus":
                target_model = "claude-opus-4-6-thinking"
            else:
                target_model = self.antigravity_bridge.resolve_model(f"{base} {var}".strip())
        elif not getattr(self, "antigravity_auto_tiering", True):
            # User has explicitly chosen/locked a model (e.g. "switch to pro", "switch to flash", etc.)
            target_model = self.antigravity_model
        else:
            target_model = self.cognitive_router.resolve_target_model(
                user_text=user_text,
                active_task_mode=self.active_task_mode,
                active_configured_model=self.antigravity_model,
                antigravity_bridge=self.antigravity_bridge,
            )

        # Multi-turn tool execution loop
        max_tool_iterations = 12 if self.active_task_mode == "code" else 4
        iterations = 0
        current_prompt = prompt
        last_tool_result = ""
        had_tool_call = False
        reply = ""
        final_reply_streamed = False
        response_provider = "antigravity"

        while iterations < max_tool_iterations:
            iterations += 1
            if self._active_cancel_token and getattr(self._active_cancel_token, "is_cancelled", False):
                logger.info("Antigravity turn cancelled before iteration %d.", iterations)
                return "Command cancelled, Sir."
            is_tool_chunk = False
            final_reply_streamed = False

            def stream_filter_cb(chunk: str):
                nonlocal is_tool_chunk, final_reply_streamed
                if self._active_cancel_token and getattr(self._active_cancel_token, "is_cancelled", False):
                    return
                if is_tool_chunk:
                    return
                # Look for tool call JSON boundary in chunk
                tool_marker_idx = -1
                for marker in ('{"tool"', '{"tool', '{ "tool"', '{ "tool'):
                    pos = chunk.find(marker)
                    if pos != -1:
                        tool_marker_idx = pos
                        break
                if tool_marker_idx == -1 and chunk.strip().startswith("{"):
                    tool_marker_idx = chunk.find("{")

                if tool_marker_idx != -1:
                    is_tool_chunk = True
                    # If model provided conversational thought or explanation before tool call, stream it!
                    spoken_prefix = chunk[:tool_marker_idx].strip()
                    if spoken_prefix and len(spoken_prefix.split()) >= 2 and sentence_callback:
                        sentence_callback(spoken_prefix)
                    return

                if "```" in chunk:
                    is_tool_chunk = True
                    return

                final_reply_streamed = True
                if sentence_callback:
                    sentence_callback(chunk)

            stream_cb = stream_filter_cb if sentence_callback else None
            res = self.antigravity_bridge.generate_response(
                prompt=current_prompt,
                model=target_model,
                system_instruction=system_instruction,
                sentence_callback=stream_cb,
                timeout=90.0,
            )
            reply_text = (res.get("response") or "").strip()
            self.last_latencies["antigravity"] = float(res.get("duration_seconds", 0.0)) * 1000.0

            error_text = " ".join(
                str(res.get(field) or "") for field in ("response", "error")
            )
            if (
                res.get("status") != "SUCCESS"
                and self.antigravity_bridge._is_token_exhaustion_error(error_text)
                and self.github_bridge.is_available()
            ):
                logger.warning("Antigravity quota exhausted; handing this turn to GitHub Copilot CLI.")
                copilot_result = self.github_bridge.generate_response(
                    prompt=current_prompt,
                    system_instruction=system_instruction,
                    timeout=90.0,
                )
                copilot_text = (copilot_result.get("response") or "").strip()
                self.last_latencies["github"] = float(
                    copilot_result.get("duration_seconds", 0.0)
                ) * 1000.0
                if copilot_result.get("status") == "SUCCESS" and copilot_text:
                    response_provider = "github"
                    reply = copilot_text
                    if sentence_callback:
                        speech_buffer = reply
                        while speech_buffer:
                            speech_chunk, speech_buffer = self._extract_next_speech_chunk(speech_buffer)
                            if speech_chunk is None:
                                speech_chunk, speech_buffer = speech_buffer, ""
                            spoken = self._clean_speech_clause(speech_chunk)
                            if spoken:
                                sentence_callback(spoken)
                        streamed_to_tts = True
                else:
                    copilot_error = (
                        copilot_text or "GitHub Copilot CLI returned no usable response."
                    )
                    logger.error("GitHub Copilot CLI quota fallback failed: %s", copilot_error[:300])
                    reply = (
                        "Antigravity has reached its quota, and the GitHub Copilot CLI "
                        f"fallback failed: {copilot_error}"
                    )
                    if sentence_callback:
                        sentence_callback(reply)
                    streamed_to_tts = True
                break

            if self._active_cancel_token and getattr(self._active_cancel_token, "is_cancelled", False):
                logger.info("Antigravity turn cancelled by user interrupt after generation.")
                return "Command cancelled, Sir."

            # Check for tool call in output
            tool_call_match = re.search(
                r'\{[\s\r\n]*"tool"[\s\r\n]*:[\s\r\n]*"([^"]+)"[\s\r\n]*,[\s\r\n]*"arguments"[\s\r\n]*:[\s\r\n]*(\{.*?\})[\s\r\n]*\}',
                reply_text,
                re.DOTALL
            )
            if not tool_call_match:
                # No further tool calls: we have reached the final answer!
                reply = reply_text
                break

            had_tool_call = True
            fn_name = tool_call_match.group(1).strip()
            try:
                fn_args = json.loads(tool_call_match.group(2))
            except Exception:
                fn_args = {}

            logger.info("ANTIGRAVITY Tool call [%d/%d]: %s(%s)", iterations, max_tool_iterations, fn_name, fn_args)
            if sentence_callback:
                narrative = self._narrate_tool_action(fn_name, fn_args)
                sentence_callback(narrative)

            is_safe, warning = self.safety.check_tool_call(fn_name, fn_args)
            if not is_safe:
                tool_result = f"Action halted: {warning}"
                if sentence_callback:
                    sentence_callback(f"Action halted: {warning}")
            else:
                try:
                    if self._active_cancel_token and hasattr(self._active_cancel_token, "raise_if_cancelled"):
                        self._active_cancel_token.raise_if_cancelled()
                    tool_result = self._execute_tool_with_orchestrator(fn_name, fn_args, step_index=iterations)
                    self._record_tool_execution(fn_name, fn_args, tool_result)
                except RuntimeError as err:
                    if "preempted" in str(err).lower() or "cancelled" in str(err).lower():
                        logger.info("Antigravity tool execution aborted by user interrupt: %s", err)
                        return "Command cancelled, Sir."
                    raise
            last_tool_result = str(tool_result)
            logger.info("ANTIGRAVITY Tool result: %s", last_tool_result[:250])

            # Refresh available tools in case a new dynamic skill was hot-registered!
            if fn_name == "create_or_update_custom_skill":
                tools = SkillRegistry.get_openai_tools()
                tool_signatures = []
                for t in tools:
                    fn = t.get("function", {})
                    params = fn.get("parameters", {}).get("properties", {})
                    tool_signatures.append(f"- {fn.get('name')}: {fn.get('description')} (params: {list(params.keys())})")
                tools_block = "\n\n[System Update]: Your newly created skill has been hot-registered into the live runtime! Available tools now include:\n" + "\n".join(tool_signatures) + "\n\n"
            else:
                tools_block = ""

            # Prepare next turn in the conversation
            current_prompt += (
                f"\n\n[Previous Action Executed]: {fn_name}\n"
                f"[Action Output]:\n{last_tool_result}\n"
                f"{tools_block}"
                f"Next instruction: If additional tool calls are needed (e.g. to execute or test the newly created skill), output the next JSON tool call: "
                f'{{"tool": "<name>", "arguments": {{<args>}}}}.\n'
                f"If the requested task is complete, deliver your clear, concise final verbal response to Sir as JARVIS."
            )

        if not reply:
            reply = reply_text or (f"Action completed, Sir: {last_tool_result}" if had_tool_call else "At your service, Sir.")

        # Deliver spoken response if sentence_callback provided and content wasn't already streamed
        if sentence_callback and not final_reply_streamed and reply:
            sentences = [s.strip() for s in re.split(r"(?<!\d)(?<=[.!?])(?!\d)\s+", reply) if s.strip()]
            for s in sentences:
                clean_s = self._clean_speech_clause(s)
                if clean_s:
                    sentence_callback(clean_s)

        self.memory.log_interaction(user_text, reply, None)
        self.memory.auto_learn_from_interaction(user_text, reply, provider=response_provider, extractor_fn=self._extract_memory_insights)
        elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
        logger.info("Inference latency (%s - %s): %sms", response_provider, target_model, elapsed_ms)
        return reply

    def _extract_memory_insights(self, user_text: str, reply_text: str) -> List[Dict[str, Any]]:
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
            import urllib.request
            import json
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

    def get_cancellation_token(self) -> Optional[Any]:
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
