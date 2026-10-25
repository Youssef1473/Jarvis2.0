"""Google Gemini multimodal streaming and function-calling integration for Jarvis 2.0."""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from google import genai
from google.genai import types

from jarvis.skills.base import SkillRegistry
from jarvis.vision.screen_capture import ScreenPerception

logger = logging.getLogger("Jarvis.Brain.Gemini")


class GeminiProviderMixin:
    """Provides Google Gemini multimodal reasoning, real-time streaming, and tool execution."""

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

        self.gemini_tools = all_tools
        self.gemini_chat = self.gemini_client.chats.create(
            model=self.gemini_model,
            history=history if history else None,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=self.temperature,
                tools=all_tools if all_tools else None,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )
        )
        self.gemini_task_mode = self.active_task_mode
        self.gemini_tool_names = self.active_tool_names
        logger.info(f"Jarvis Gemini session initialized with model: {self.gemini_model} ({len(recent_turns)} prior turns restored).")

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
                    from jarvis.skills.screen_vision import (
                        _get_active_window_details,
                        check_visual_privacy,
                    )
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
                active_tools = getattr(self, "gemini_tools", None)
                if active_tools is None and hasattr(self, "active_tool_names"):
                    active_tools = [
                        tool for name, tool in SkillRegistry.get_all_tools().items()
                        if name in self.active_tool_names
                    ]
                afc_config = types.AutomaticFunctionCallingConfig(disable=True)
                call_config = (
                    None
                    if is_deep
                    else types.GenerateContentConfig(
                        system_instruction=self._get_system_instruction(),
                        temperature=self.temperature,
                        tools=active_tools if active_tools else None,
                        automatic_function_calling=afc_config,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                    )
                )

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
                    except Exception:  # noqa: BLE001
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
                        tool_result, _ = self._dispatch_tool_pipeline(
                            tool_name, tool_args, step_index=gemini_tool_iterations, sentence_callback=sentence_callback
                        )
                        logger.info(f"Tool result: {tool_result}")
                        call_id = getattr(call, "id", None)
                        if call_id:
                            tool_responses.append(types.Part(
                                function_response=types.FunctionResponse(
                                    name=tool_name,
                                    response={"result": str(tool_result)},
                                    id=str(call_id),
                                )
                            ))
                        else:
                            tool_responses.append(types.Part.from_function_response(
                                name=tool_name,
                                response={"result": str(tool_result)},
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
                        except Exception:  # noqa: BLE001
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
                    # Unresolved function calls leave the SDK chat history ending in a model function_call.
                    # Invalidate session so the next request reconstructs a clean chat from memory.
                    self.gemini_chat = None
                else:
                    reply_text = "".join(full_content_chunks).strip() if full_content_chunks else "At your service, Sir."

                try:
                    self.memory.log_interaction(user_text, reply_text)
                    self.memory.auto_learn_from_interaction(
                        user_text, reply_text, provider=self.provider, extractor_fn=self._extract_memory_insights
                    )
                except Exception as log_err:  # noqa: BLE001
                    logger.debug("Gemini interaction logging notice: %s", log_err)

                elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
                self.last_latencies["gemini"] = elapsed_ms
                logger.info(f"Inference latency (gemini streaming): {elapsed_ms}ms")

                return reply_text

            except Exception as e:  # noqa: BLE001
                # Streaming or tool execution failed mid-flight: chat history is uncertain/corrupted.
                # Invalidate chat session to avoid reusing an inconsistent turn sequence.
                self.gemini_chat = None

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
