"""Reflex dispatcher for Jarvis 2.0.

Decouples sub-15ms fast reflex regexes, habit graph dispatching,
and acoustic tool narration from the monolithic JarvisBrain orchestrator.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.ReflexDispatcher")

SERVICES_MAP: dict[str, str] = {
    "youtube": "https://youtube.com",
    "google": "https://google.com",
    "github": "https://github.com",
    "reddit": "https://reddit.com",
    "twitter": "https://x.com",
    "chatgpt": "https://chatgpt.com",
    "gmail": "https://mail.google.com",
    "netflix": "https://netflix.com",
    "spotify": "https://open.spotify.com",
    "twitch": "https://twitch.tv",
}


class ReflexDispatcher:
    """Dispatches unambiguous local OS, desktop, media, and browser commands in <15ms."""

    def __init__(self, reflex_engine: Any | None = None) -> None:
        self.reflex_engine = reflex_engine

    @staticmethod
    def normalize_conversational_text(norm_text: str) -> str:
        """Strips common leading fillers, polite preambles, and trailing punctuation."""
        lower = (norm_text or "").lower().strip()
        prev = None
        while prev != lower:
            prev = lower
            lower = re.sub(
                r"^(?:no[,.]?|hey jarvis[,.]?|jarvis[,.]?|okay jarvis[,.]?|ok jarvis[,.]?|please|i want you to|could you(?: please)?|can you(?: please)?|would you(?: please)?)\s+",
                "",
                lower,
            ).strip()
        return re.sub(r"[.!?,]+$", "", lower).strip()

    @staticmethod
    def is_disallowed_reflex(clean: str) -> bool:
        """Determines if a command is a question, debugging inquiry, or past/future directive."""
        # Explicit self-diagnostic commands are always permitted
        if re.search(r"^(?:run\s+)?(?:jarvis doctor|doctor|troubleshoot yourself|troubleshoot system|run system health check|system health check|health check|check your systems|diagnose system|troubleshoot jarvis|system health)$", clean):
            return False

        if (
            any(clean.startswith(w) for w in (
                "how ", "why ", "what is ", "what are ", "who is ", "can you explain", "explain ",
                "when ", "if ", "whenever ", "while ", "after ", "before ", "as soon as ",
                "could you explain", "would you explain", "tell me why", "tell me how",
            ))
            or re.search(r"\b(?:why did|why do|why does|why are|why is|why would|what happened|see what|look why|check why|find out why|debug|diagnose why|troubleshoot)\b", clean)
            or re.search(r"\b(?:wrong|bug|issue|problem|broken|twice|duplicate tabs?|two tabs?)\b", clean)
            or re.search(r"\b(?:asked|opened|closed|said|told|happened|did you|have you)\b", clean)
            or any(t in clean for t in (
                "add feature", "add a feature", "create feature", "create a feature",
                "build yourself", "create a skill", "create skill", "add a skill", "add skill",
                "create command", "create a command", "add command", "add a command",
                "teach yourself", "create tool", "create a tool", "add tool", "add a tool",
            ))
        ):
            return True
        return False

    def is_fast_reflex(self, norm_text: str) -> bool:
        """Determines if a query matches a fast reflex command without executing any tools."""
        return bool(self.try_fast_reflex(norm_text, dry_run=True))

    def try_fast_reflex(
        self,
        norm_text: str,
        dry_run: bool = False,
        execute_fn: Callable[..., Any] | None = None,
    ) -> str | bool | None:
        """Executes unambiguous local OS, desktop, media, and browser commands in <15ms.

        Bypasses slow cloud round-trips for instant physical and spoken execution.
        If dry_run=True, tests for a reflex hit without executing any skills.
        """
        def _exec(skill_name: str, **kwargs):
            if dry_run:
                return True
            if skill_name in ("jarvis_doctor", "run_system_troubleshooting", "profile_runtime", "audit_memory_heap"):
                try:
                    pass
                except Exception:
                    pass
            if execute_fn:
                return execute_fn(skill_name, **kwargs)
            return SkillRegistry.execute(skill_name, **kwargs)

        clean = self.normalize_conversational_text(norm_text)
        if not clean:
            return None

        # 1. Check Dynamic Learned Habit Cache (<0.2ms lookup) first for maximum reflex speed
        if self.reflex_engine:
            habit = self.reflex_engine.check_habit(clean)
            if habit:
                if dry_run:
                    return True
                t0 = time.perf_counter()
                fn_name = habit["skill_name"]
                fn_args = habit.get("skill_kwargs", {})

                # Compound multi-step pipeline execution
                if fn_name == "__compound__" and isinstance(fn_args, list):
                    try:
                        results = []
                        for step in fn_args:
                            step_name = step.get("skill_name")
                            step_kwargs = step.get("skill_kwargs", {})
                            step_res = _exec(step_name, **step_kwargs)
                            results.append(str(step_res))
                            if any(str(step_res).startswith(err) for err in ("Failed to", "Action halted", "Could not find", "Error:")):
                                logger.warning(f"Compound step '{step_name}' reported failure: {step_res}. Halting pipeline.")
                                break
                        refl_ms = (time.perf_counter() - t0) * 1000.0
                        self.reflex_engine.record_hit(habit["id"], refl_ms)
                        logger.info(f"Learned compound habit ({len(fn_args)} steps) executed in {refl_ms:.2f}ms")
                        return results[-1] if results else "Done, Sir."
                    except Exception as e:
                        logger.warning(f"Error executing compound habit: {e}")
                else:
                    try:
                        res = _exec(fn_name, **fn_args)
                        refl_ms = (time.perf_counter() - t0) * 1000.0
                        self.reflex_engine.record_hit(habit["id"], refl_ms)
                        logger.info(f"Learned habit executed in {refl_ms:.2f}ms: {fn_name}({fn_args})")
                        if isinstance(res, dict) and "summary" in res:
                            return res["summary"]
                        return str(res)
                    except Exception as e:
                        logger.warning(f"Error executing habit: {e}")

        # Habit Learning Audit & Verification
        if re.search(r"^(?:verify\s+(?:your\s+|my\s+)?learning|what\s+have\s+you\s+learned|list\s+learned\s+habits|audit\s+learned\s+habits|check\s+your\s+learning|show\s+(?:your\s+)?learned\s+habits)$", clean):
            return _exec("verify_learned_habits")

        if re.search(r"^(?:clear|reset|wipe|delete)\s+(?:all\s+)?(?:learned\s+)?habits$", clean):
            if dry_run:
                return True
            count = self.reflex_engine.clear_habits() if self.reflex_engine else 0
            return f"Cleared {count} learned habits from memory, Sir."

        # Disallow reflex execution if Sir is asking a question, debugging, reporting an issue, or speaking in past tense
        if self.is_disallowed_reflex(clean):
            return None

        # 1. Master Volume
        if re.search(r"^(?:mute|unmute)(?:\s+(?:the\s+)?(?:volume|audio|sound|workstation|pc))?$", clean):
            return _exec("adjust_volume", action="mute")
        if re.search(r"^(?:volume up|turn up the volume|louder|increase volume|raise volume)$", clean):
            return _exec("adjust_volume", action="up")
        if re.search(r"^(?:volume down|turn down the volume|quieter|decrease volume|lower volume)$", clean):
            return _exec("adjust_volume", action="down")
        vol_match = re.search(r"^(?:set volume to|volume to|set volume|volume)\s+(\d{1,3})(?:%)?$", clean)
        if vol_match:
            return _exec("adjust_volume", percent=int(vol_match.group(1)))

        # 2. Media Playback
        if re.search(r"^(?:play|pause|play pause|resume)(?:\s+(?:music|song|media|playback|spotify))?$", clean):
            return _exec("control_media", action="playpause")
        if re.search(r"^(?:next song|next track|skip song|skip track|skip)(?:\s+song|\s+track)?$", clean):
            return _exec("control_media", action="next")
        if re.search(r"^(?:previous song|previous track|prev song|prev track|back track)$", clean):
            return _exec("control_media", action="previous")

        # 3. Workstation & Window Management
        if re.search(r"^(?:show desktop|minimize all|minimize all windows|minimize windows|go to desktop)$", clean):
            return _exec("minimize_all_windows")
        if re.search(r"^(?:lock workstation|lock pc|lock computer|lock screen|lock the pc)$", clean):
            return _exec("lock_workstation")
        if re.search(r"^(?:take screenshot|take a screenshot|capture screenshot|capture screen)$", clean):
            return _exec("take_desktop_screenshot")
        if re.search(r"^(?:save|capture|store)?\s*(?:photo|image|picture|screenshot)\s+(?:from|on)?\s*clipboard$|^save\s+clipboard\s+(?:photo|image|picture)$", clean):
            return _exec("save_clipboard_photo")
        if re.search(r"^(?:list|show|view)\s+(?:saved\s+|my\s+)?photos$|^photo\s+gallery$", clean):
            return _exec("list_photos")
        if re.search(r"^(?:what is the\s+)?(?:active window|focused window|what window is this|what am i looking at)$", clean):
            return _exec("get_active_window")
        if re.search(r"^(?:list\s+)?(?:running applications|running apps|open windows|what apps are open|what is open)$", clean):
            return _exec("list_running_applications")
        if re.search(r"^(?:system vitals|workstation vitals|hardware vitals|system status|hardware status)$", clean):
            return _exec("get_system_vitals")
        if re.search(r"^(?:battery status|battery level|battery percentage|check battery|how much battery)$", clean):
            return _exec("get_battery_status")
        if re.search(r"^(?:what(?:'s|\s+is)?\s+(?:the\s+)?(?:day|date|day\s+today|time|current\s+time|time\s+is\s+it|date\s+today)|what\s+day\s+is\s+it(?:\s+today)?|what\s+time\s+is\s+it|tell\s+me\s+the\s+(?:time|date|day)|what\s+is\s+today(?:'s)?(?:\s+date)?)$", clean):
            return _exec("get_current_time_and_date")
        if re.search(r"^(?:run\s+)?(?:jarvis doctor|doctor|troubleshoot yourself|troubleshoot system|run system health check|system health check|health check|check your systems|diagnose system|troubleshoot jarvis|system health)$", clean):
            return _exec("jarvis_doctor", verbose=False)
        if re.search(r"^(?:run\s+)?(?:system diagnostics|runtime diagnostics|profile runtime|thread telemetry|diagnose performance)$", clean):
            return _exec("profile_runtime")
        if re.search(r"^(?:audit\s+)?(?:memory heap|heap allocations|memory leak check|check memory leaks)$", clean):
            return _exec("audit_memory_heap")
        if re.search(r"^(?:list\s+)?(?:open browser tabs|browser tabs|open tabs|what tabs are open)$", clean):
            return _exec("list_open_browser_tabs")
        if re.search(r"^(?:launch|start|open|run)?\s*(?:the\s+)?(?:isolated\s+)?(?:debug\s+browser(?:\s+session)?|browser\s+debug\s+session|cdp\s+browser)(?:\s+for\s+me|\s+please)?$", clean):
            return _exec("launch_debug_browser")

        # 4. Browser Tabs & URL Navigation (<15ms)
        service_target = None
        for svc_name, svc_url in SERVICES_MAP.items():
            if re.search(rf"\b{svc_name}\b", clean):
                service_target = svc_url
                break

        url_match = re.search(
            r"(https?://\S+|[a-zA-Z0-9_\-\.]+\.(?:com|org|net|io|edu|gov|dev|ai|app|co|uk|de|fr|ca|me|tv|xyz|gg)(?:/[^\s]*)?)",
            clean,
            re.IGNORECASE,
        )

        is_imperative_open = bool(
            re.search(r"^(?:open|launch|navigate|go to|visit|browse|enter|load|load up)\b", clean)
            or re.search(r"^(?:new\s+tab|tab\s+(?:for|to|with)|(?:a\s+)?(?:brave|chrome|edge)\s+tab)\b", clean)
            or re.search(r"\bin\s+(?:brave|chrome|edge|browser)$", clean)
            or re.search(r"^(?:open\s+)?(?:a\s+)?(?:youtube|google|github|reddit|twitter|chatgpt|gmail|netflix|spotify|twitch)(?:\.com|\.tv|\.org)?(?:\s+tab)?(?:\s+in\s+(?:brave|chrome|edge))?$", clean)
        )

        if (url_match or service_target) and is_imperative_open:
            target_url = url_match.group(1).rstrip(".,!?") if url_match else service_target
            b = "brave"
            if "chrome" in clean:
                b = "chrome"
            elif "edge" in clean or "msedge" in clean:
                b = "msedge"
            reuse_tab = any(k in clean for k in ("in that tab", "in that brave tab", "in the tab", "in this tab", "in that window", "in the active tab", "enter a link in that brave tab"))
            call_kwargs = {"url": target_url, "browser": b}
            if reuse_tab:
                call_kwargs["new_tab"] = False
            return _exec("open_browser_tab", **call_kwargs)

        # Brave variations
        if re.search(r"^(?:open|launch|start|new)?\s*(?:a\s+)?(?:new\s+)?tab\s+(?:in|and|on|for)\s+(?:my\s+|the\s+)?(?:current\s+)?brave(?:\s+tab|\s+window)?(?:\s+for\s+me|\s+please)?$|^(?:open|launch|start|open a|new)\s+(?:brave\s+tab|brave|brave browser)(?:\s+for\s+me|\s+please)?$|^brave\s+tab$", clean):
            return _exec("open_browser_tab", browser="brave")

        # Chrome variations
        if re.search(r"^(?:open|launch|start|new)?\s*(?:a\s+)?(?:new\s+)?tab\s+(?:in|and|on|for)\s+chrome(?:\s+tab|\s+window)?(?:\s+for\s+me|\s+please)?$|^(?:open|launch|start|open a|new)\s+(?:chrome\s+tab|chrome|chrome browser)(?:\s+for\s+me|\s+please)?$|^chrome\s+tab$", clean):
            return _exec("open_browser_tab", browser="chrome")

        # Edge variations
        if re.search(r"^(?:open|launch|start|new)?\s*(?:a\s+)?(?:new\s+)?tab\s+(?:in|and|on|for)\s+edge(?:\s+tab|\s+window)?(?:\s+for\s+me|\s+please)?$|^(?:open|launch|start|open a|new)\s+(?:edge\s+tab|edge|msedge)(?:\s+for\s+me|\s+please)?$|^edge\s+tab$", clean):
            return _exec("open_browser_tab", browser="msedge")

        # Generic new tab
        if re.search(r"^(?:open|launch|new)?\s*(?:a\s+)?(?:new\s+)?tab(?:\s+for\s+me|\s+please)?$", clean):
            return _exec("open_browser_tab")

        # 4b. Terminate / Close Browser Tabs (<15ms Reflex)
        if re.search(r"^(?:close|terminate|kill|shut)\s+(?:a\s+|the\s+|this\s+|current\s+)?tab\s+(?:in|and|on|for)\s+(?:my\s+|the\s+)?(?:current\s+)?brave(?:\s+tab|\s+window)?(?:\s+for\s+me|\s+please)?$|^(?:close|terminate|kill|shut)\s+(?:a\s+|the\s+)?brave\s+tab(?:\s+for\s+me|\s+please)?$", clean):
            return _exec("close_browser_tab", browser="brave")

        if re.search(r"^(?:close|terminate|kill|shut)\s+(?:a\s+|the\s+|this\s+|current\s+)?tab\s+(?:in|and|on|for)\s+chrome(?:\s+tab|\s+window)?(?:\s+for\s+me|\s+please)?$|^(?:close|terminate|kill|shut)\s+(?:a\s+|the\s+)?chrome\s+tab(?:\s+for\s+me|\s+please)?$", clean):
            return _exec("close_browser_tab", browser="chrome")

        if re.search(r"^(?:close|terminate|kill|shut)\s+(?:a\s+|the\s+|this\s+|current\s+)?tab\s+(?:in|and|on|for)\s+edge(?:\s+tab|\s+window)?(?:\s+for\s+me|\s+please)?$|^(?:close|terminate|kill|shut)\s+(?:a\s+|the\s+)?edge\s+tab(?:\s+for\s+me|\s+please)?$", clean):
            return _exec("close_browser_tab", browser="msedge")

        if re.search(r"^(?:close|terminate|kill|shut)\s+(?:a\s+|the\s+|this\s+|current\s+)?(?:browser\s+)?tab(?:\s+for\s+me|\s+please)?$|^(?:close|terminate)\s+tab$", clean):
            return _exec("close_browser_tab")

        # 5. Window Switching / Focus
        focus_match = re.search(r"^(?:switch to|focus|bring up)\s+(brave|chrome|discord|spotify|code|vs code|terminal|notepad|calculator|explorer|files)$", clean)
        if focus_match:
            return _exec("focus_window", title=focus_match.group(1))

        # 6. Direct Application Launching
        app_match = re.search(r"^(?:open|launch)\s+(calculator|calc|notepad|spotify|discord|terminal|task manager|settings)$", clean)
        if app_match:
            return _exec("launch_application", app_name=app_match.group(1))

        # 7. Voice Profile & Speech Rate Fast Reflex (<15ms)
        voice_match = re.search(r"^(?:switch to|change voice to|set voice to|activate)\s+(?:the\s+)?(classic|authoritative|crisp|friday|george|lewis|daniel|emma|alice)(?:\s+voice|\s+persona)?(?:\s+for\s+me|\s+please)?$", clean)
        if voice_match:
            v_name = voice_match.group(1).lower()
            if v_name in ("classic", "authoritative", "crisp"):
                v_name = f"jarvis_{v_name}"
            return _exec("set_voice_profile", profile_name=v_name)

        speed_faster = re.search(r"^(?:speak faster|speed up voice|talk faster)(?:\s+for\s+me|\s+please)?$", clean)
        if speed_faster:
            return _exec("set_speech_rate", speed=1.20)

        speed_slower = re.search(r"^(?:speak slower|slow down voice|talk slower)(?:\s+for\s+me|\s+please)?$", clean)
        if speed_slower:
            return _exec("set_speech_rate", speed=0.90)

        speed_reset = re.search(r"^(?:reset voice speed|normal voice speed|reset speech rate)(?:\s+for\s+me|\s+please)?$", clean)
        if speed_reset:
            return _exec("set_speech_rate", speed=1.05)

        return None

    def finalize_turn_habit(
        self,
        user_text: str,
        executed_tools: list[tuple[str, dict[str, Any]]],
        turn_start_time: float,
    ) -> None:
        """Promotes successful multi-step or single-step tool executions into persistent fast reflexes."""
        if not self.reflex_engine or not executed_tools:
            return

        elapsed_ms = (time.perf_counter() - turn_start_time) * 1000.0
        clean_text = (user_text or "").strip()

        if len(executed_tools) == 1:
            tool_name, tool_args = executed_tools[0]
            self.reflex_engine.record_habit(
                trigger_query=clean_text,
                skill_name=tool_name,
                skill_kwargs=tool_args,
                turn1_latency_ms=elapsed_ms,
            )
        elif 2 <= len(executed_tools) <= 5:
            steps = [{"skill_name": t_name, "skill_kwargs": t_args} for t_name, t_args in executed_tools]
            self.reflex_engine.record_compound_habit(
                trigger_query=clean_text,
                steps=steps,
                turn1_latency_ms=elapsed_ms,
            )

    @staticmethod
    def narrate_tool_action(fn_name: str, fn_args: dict[str, Any]) -> str:
        """Returns a natural, safe spoken phrase describing an action before execution."""
        name = (fn_name or "").lower().strip()

        # Browser
        if name == "open_browser_tab":
            b = fn_args.get("browser") or "your browser"
            return f"Opening a new tab in {b}, Sir."
        elif name == "focus_window":
            title = fn_args.get("title") or "the application"
            return f"Switching to {title}, Sir."
        elif name == "launch_application":
            app = fn_args.get("app_name") or "the requested application"
            return f"Launching {app}, Sir."
        elif name == "close_application":
            app = fn_args.get("app_name") or "the application"
            return f"Closing {app}, Sir."
        elif name == "take_desktop_screenshot":
            return "Capturing a screenshot of your screen now, Sir."
        elif name in ("inspect_screen", "see_screen"):
            return "Visually analyzing your screen display now, Sir."
        elif name == "adjust_volume":
            return "Adjusting the system volume, Sir."
        elif name == "control_media":
            return "Updating media playback, Sir."
        elif name == "minimize_all_windows":
            return "Minimizing windows to reveal your desktop, Sir."
        elif name == "lock_workstation":
            return "Locking the workstation, Sir."
        elif name == "get_system_vitals":
            return "Checking system vitals and hardware telemetry, Sir."
        elif name == "get_battery_status":
            return "Checking battery levels, Sir."
        elif name == "profile_runtime":
            return "Running diagnostic runtime telemetry on active threads, Sir."
        elif name == "audit_memory_heap":
            return "Auditing Python heap allocations and memory metrics, Sir."
        elif name == "list_open_browser_tabs":
            return "Inspecting open browser tabs via DevTools channel, Sir."
        elif name == "inspect_window_controls":
            return "Inspecting native window UI controls via Accessibility tree, Sir."

        # Self-Evolution & Code Building
        elif name == "create_or_update_custom_skill":
            skill = fn_args.get("skill_name", "").replace("_", " ").strip()
            return f"Engineering the '{skill}' capability now, Sir. Validating syntax and compiling it into memory."
        elif name == "list_custom_skills":
            return "Retrieving your custom engineered capabilities, Sir."
        elif name == "delete_custom_skill":
            skill = fn_args.get("skill_name", "").replace("_", " ").strip()
            return f"Removing custom skill '{skill}', Sir."

        # Dev & Codebase Stewardship
        elif name == "search_codebase":
            query = fn_args.get("query", "")
            return f"Searching the repository for {query}, Sir." if query else "Scanning the codebase, Sir."
        elif name == "search_everything":
            query = fn_args.get("query", "")
            return f"Querying Voidtools Everything for {query}, Sir." if query else "Performing instant system-wide file search across all drives, Sir."
        elif name == "read_file":
            fname = Path(fn_args.get("file_path", "the file")).name
            return f"Reading {fname}, Sir."
        elif name == "write_file":
            fname = Path(fn_args.get("file_path", "the file")).name
            return f"Writing updates to {fname}, Sir."
        elif name == "patch_file":
            fname = Path(fn_args.get("file_path", "the file")).name
            return f"Patching {fname}, Sir."
        elif name == "execute_powershell":
            return "Executing command in the terminal, Sir."
        elif name == "run_project_tests":
            return "Running the test suite to verify our changes, Sir."

        # Photos & Visual Perception
        elif name == "save_photo":
            return "Archiving and scrubbing photo metadata now, Sir."
        elif name == "save_clipboard_photo":
            return "Archiving screenshot from clipboard now, Sir."
        elif name == "list_photos":
            return "Accessing your archived photo gallery, Sir."
        elif name == "describe_photo":
            return "Analyzing visual features via cloud vision stream, Sir."

        # Calendar & Documents
        elif name == "list_calendar_events":
            return "Consulting your calendar schedule, Sir."
        elif name == "add_calendar_event":
            return "Adding the event to your schedule, Sir."
        elif name == "cancel_calendar_event":
            return "Updating your schedule and cancelling the event, Sir."
        elif name in ("process_staged_document", "list_staged_documents"):
            return "Parsing and extracting details from your document, Sir."
        elif name in ("search_archived_documents", "read_archived_document"):
            return "Consulting your archived document vaults, Sir."
        elif name == "recall_past_conversation":
            return "Consulting our past conversation records, Sir."

        # Generic / dynamic custom skill
        humanized = name.replace("_", " ").capitalize()
        return f"Executing {humanized}, Sir."
