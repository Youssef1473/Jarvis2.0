"""OpenAI-compatible LLM provider mixin (Ollama, Groq, xAI Grok, OmniRoute) for Jarvis 2.0."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from jarvis.skills.base import SkillRegistry
from jarvis.vision.screen_capture import ScreenPerception

logger = logging.getLogger("Jarvis.Brain.OpenAI")


class OpenAIProviderMixin:
    """Provides local Ollama, Groq LPU, xAI Grok, and OmniRoute streaming LLM execution."""

    def _init_hybrid_providers(self) -> None:
        """Initializes Tri-Tier Hybrid Providers: Local Ollama, Groq LPU, and Gemini Multimodal."""
        try:
            from openai import OpenAI
        except ImportError:
            OpenAI = None
            logger.warning("OpenAI package not installed. Hybrid OpenAI-compatible providers unavailable.")

        llm_cfg = self.config.get("llm", {})

        # 1. Local Ollama (Tier 1: private, offline, fast local OS actions)
        ollama_base = llm_cfg.get("ollama_base_url", "http://127.0.0.1:11434/v1")
        self._ensure_local_ollama(ollama_base)
        if OpenAI is not None:
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
        if groq_key and OpenAI is not None:
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
                req = urllib.request.Request(f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {api_key}"})
                with urllib.request.urlopen(req, timeout=1.5):
                    pass
                logger.info(f"Jarvis OmniRoute link verified at {base_url}")
                self._adopt_local_service("OmniRoute", base_url, ("omniroute",))
            except Exception:
                logger.info(f"OmniRoute gateway at {base_url} is offline. Attempting to start a managed service...")
                try:
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
            from jarvis.skills.screen_vision import (
                _get_active_window_details,
                check_visual_privacy,
            )
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
                        tool_result, ok = self._dispatch_tool_pipeline(
                            fn_name, fn_args, step_index=step_idx, sentence_callback=sentence_callback
                        )
                        last_tool_result = str(tool_result)
                        if ok and fn_name in action_tools:
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
                        tool_result, _ = self._dispatch_tool_pipeline(
                            fn_name, fn_args, step_index=1, sentence_callback=sentence_callback
                        )
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
