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
        if not api_key or api_key == "your_gemini_api_key_here":
            logger.warning("No valid GEMINI_API_KEY provided. Gemini LLM calls will fail until configured.")
            return

        http_opts = types.HttpOptions(timeout=15000)
        self.gemini_client = genai.Client(api_key=api_key, http_options=http_opts)
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
        """Processes user input using Google Gemini with dynamic candidate failover.

        The 25-second turn deadline is a bounded, best-effort target rather than a
        hard physical guarantee. Because Google Gemini API enforces a minimum client
        deadline of 10.0s (10,000ms), subsequent candidate retry attempts are aborted
        if the remaining time budget falls below 10.0s.
        """
        t_start = time.perf_counter()
        total_turn_deadline_seconds = 25.0
        turn_deadline = t_start + total_turn_deadline_seconds

        if not self.gemini_client:
            api_key = os.getenv("GEMINI_API_KEY", "").strip()
            if api_key and api_key != "your_gemini_api_key_here":
                http_opts = types.HttpOptions(timeout=10000)
                self.gemini_client = genai.Client(api_key=api_key, http_options=http_opts)
                self._init_gemini()
            else:
                return "My apologies, Sir. My cognitive link requires a valid GEMINI_API_KEY in the environment or .env file."

        attempts = min(3, len(self.gemini_candidates))
        for attempt in range(attempts):
            remaining_budget = turn_deadline - time.perf_counter()
            # Initial attempt needs minimal buffer; retries require Google API's min 10.0s deadline
            min_required = 10.0 if attempt > 0 else 2.0
            if remaining_budget < min_required:
                logger.warning(
                    "Turn deadline budget exhausted (%.2fs remaining, min %.1fs required). Aborting candidate retries.",
                    remaining_budget,
                    min_required,
                )
                return "My apologies, Sir. The cognitive request timed out before receiving a response from Gemini."

            if attempt > 0:
                self._is_turn_retry = True

            active_model = self.gemini_candidates[self.gemini_model_idx]
            if (
                not self.gemini_chat
                or self.gemini_model != active_model
                or self.gemini_task_mode != self.active_task_mode
                or self.gemini_tool_names != self.active_tool_names
            ):
                self._init_gemini(active_model)

            try:
                user_envelope = (
                    self.build_ephemeral_user_envelope(user_text)
                    if hasattr(self, "build_ephemeral_user_envelope")
                    else user_text
                )
                contents = [user_envelope]

                # Tool deduplication notice: avoid executing tools twice when failing over mid-turn
                executed_tools = getattr(self, "_current_turn_executed_tools", [])
                if executed_tools:
                    def _fmt_tool(t: Any) -> str:
                        if isinstance(t, tuple) and len(t) >= 2:
                            return f"{t[0]}({t[1]})"
                        if isinstance(t, dict):
                            return f"{t.get('tool', 'tool')}({t.get('args', {})})"
                        return str(t)

                    executed_summary = ", ".join(_fmt_tool(t) for t in executed_tools)
                    contents.append(
                        f"[SYSTEM NOTE: The following tools were ALREADY executed in this turn: {executed_summary}. "
                        f"DO NOT call these tools again. Provide your conversational response to the user based on these results.]"
                    )

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
                had_stream_chunks = False
                has_candidates = False
                safety_blocked = False
                is_malformed = False

                # Stream tokens directly to Kokoro-82M speech callback in real time
                response_stream = self.gemini_chat.send_message_stream(contents, config=call_config)
                for chunk in response_stream:
                    had_stream_chunks = True
                    try:
                        prompt_feedback = getattr(chunk, "prompt_feedback", None)
                        if prompt_feedback and not hasattr(prompt_feedback, "_mock_return_value"):
                            block_reason = getattr(prompt_feedback, "block_reason", None)
                            if block_reason and not hasattr(block_reason, "_mock_return_value"):
                                b_name = str(getattr(block_reason, "name", block_reason)).upper()
                                if b_name in ("SAFETY", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT"):
                                    safety_blocked = True

                        fn_calls = getattr(chunk, "function_calls", None)
                        if fn_calls and isinstance(fn_calls, (list, tuple)):
                            function_calls_accumulated.extend(fn_calls)

                        chunk_text = ""
                        candidates = getattr(chunk, "candidates", None)
                        if candidates and isinstance(candidates, (list, tuple)) and len(candidates) > 0:
                            has_candidates = True
                            cand = candidates[0]
                            finish_reason = getattr(cand, "finish_reason", None)
                            if finish_reason and not hasattr(finish_reason, "_mock_return_value"):
                                f_name = str(getattr(finish_reason, "name", finish_reason)).upper()
                                if f_name in ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
                                    safety_blocked = True
                            content = getattr(cand, "content", None)
                            parts = getattr(content, "parts", None) if content else None
                            if parts and isinstance(parts, (list, tuple)):
                                text_parts = [
                                    str(p.text) for p in parts
                                    if getattr(p, "text", None) and isinstance(p.text, str) and not getattr(p, "thought", False)
                                ]
                                chunk_text = "".join(text_parts)
                        else:
                            fallback_text = getattr(chunk, "text", None)
                            if isinstance(fallback_text, str) and fallback_text:
                                chunk_text = fallback_text
                    except (AttributeError, TypeError, ValueError, KeyError, IndexError) as chunk_err:
                        logger.warning("Malformed Gemini chunk encountered: %s", chunk_err)
                        is_malformed = True
                        chunk_text = ""

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
                        try:
                            tool_result, _ = self._dispatch_tool_pipeline(
                                tool_name, tool_args, step_index=gemini_tool_iterations, sentence_callback=sentence_callback
                            )
                        except RuntimeError as err:
                            if "preempted" in str(err).lower() or "cancelled" in str(err).lower():
                                logger.info("Gemini tool execution aborted by user interrupt: %s", err)
                                self.gemini_chat = None
                                return "Command cancelled, Sir."
                            raise
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
                        had_stream_chunks = True
                        try:
                            prompt_feedback = getattr(chunk, "prompt_feedback", None)
                            if prompt_feedback and not hasattr(prompt_feedback, "_mock_return_value"):
                                block_reason = getattr(prompt_feedback, "block_reason", None)
                                if block_reason and not hasattr(block_reason, "_mock_return_value"):
                                    b_name = str(getattr(block_reason, "name", block_reason)).upper()
                                    if b_name in ("SAFETY", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT"):
                                        safety_blocked = True

                            fn_calls = getattr(chunk, "function_calls", None)
                            if fn_calls and isinstance(fn_calls, (list, tuple)):
                                function_calls_accumulated.extend(fn_calls)

                            chunk_text = ""
                            candidates = getattr(chunk, "candidates", None)
                            if candidates and isinstance(candidates, (list, tuple)) and len(candidates) > 0:
                                has_candidates = True
                                cand = candidates[0]
                                finish_reason = getattr(cand, "finish_reason", None)
                                if finish_reason and not hasattr(finish_reason, "_mock_return_value"):
                                    f_name = str(getattr(finish_reason, "name", finish_reason)).upper()
                                    if f_name in ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
                                        safety_blocked = True
                                content = getattr(cand, "content", None)
                                parts = getattr(content, "parts", None) if content else None
                                if parts and isinstance(parts, (list, tuple)):
                                    text_parts = [
                                        str(p.text) for p in parts
                                        if getattr(p, "text", None) and isinstance(p.text, str) and not getattr(p, "thought", False)
                                    ]
                                    chunk_text = "".join(text_parts)
                            else:
                                fallback_text = getattr(chunk, "text", None)
                                if isinstance(fallback_text, str) and fallback_text:
                                    chunk_text = fallback_text
                        except (AttributeError, TypeError, ValueError, KeyError, IndexError) as chunk_err:
                            logger.warning("Malformed Gemini followup chunk encountered: %s", chunk_err)
                            is_malformed = True
                            chunk_text = ""

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

                is_failed_or_blocked = False
                if function_calls_accumulated:
                    reply_text = (
                        "I reached the limit for tool steps on this request, "
                        "so I stopped before completing the remaining actions."
                    )
                    # Unresolved function calls leave the SDK chat history ending in a model function_call.
                    # Invalidate session so the next request reconstructs a clean chat from memory.
                    self.gemini_chat = None
                elif full_content_chunks:
                    reply_text = "".join(full_content_chunks).strip()
                elif safety_blocked:
                    logger.warning("Gemini response was blocked by content safety policies.")
                    reply_text = "My apologies, Sir. That request was blocked by content safety policies."
                    self.gemini_chat = None
                    is_failed_or_blocked = True
                elif is_malformed:
                    logger.error("Gemini returned a malformed response.")
                    reply_text = "I encountered a slight anomaly in my cognitive circuits, Sir. Please try again in a moment."
                    self.gemini_chat = None
                    is_failed_or_blocked = True
                elif not had_stream_chunks:
                    logger.error("Gemini returned an empty response stream without chunks.")
                    reply_text = "My apologies, Sir. Google Gemini returned an empty response stream."
                    self.gemini_chat = None
                    is_failed_or_blocked = True
                elif not has_candidates:
                    logger.error("Gemini returned a response without candidates.")
                    reply_text = "My apologies, Sir. Google Gemini returned no response candidates."
                    self.gemini_chat = None
                    is_failed_or_blocked = True
                else:
                    logger.error("Gemini response contained candidates but no usable text or function calls.")
                    reply_text = "My apologies, Sir. Google Gemini returned an empty response without usable content."
                    self.gemini_chat = None
                    is_failed_or_blocked = True

                if sentence_callback and is_failed_or_blocked and reply_text and not text_buffer.strip():
                    clean_s = self._clean_speech_clause(reply_text)
                    if clean_s:
                        sentence_callback(clean_s)

                try:
                    self.memory.log_interaction(user_text, reply_text)
                    if not is_failed_or_blocked:
                        self.memory.auto_learn_from_interaction(
                            user_text, reply_text, provider=self.provider, extractor_fn=self._extract_memory_insights
                        )
                except Exception as log_err:  # noqa: BLE001
                    logger.debug("Gemini interaction logging notice: %s", log_err)

                elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
                self.last_latencies["gemini"] = elapsed_ms
                self._gemini_last_verified_timestamp = time.time()
                self._gemini_last_error = None
                logger.info(f"Inference latency (gemini streaming): {elapsed_ms}ms")

                return reply_text

            except Exception as e:  # noqa: BLE001
                # Streaming or tool execution failed mid-flight: chat history is uncertain/corrupted.
                # Invalidate chat session to avoid reusing an inconsistent turn sequence.
                self.gemini_chat = None

                err_str = str(e)
                err_lower = err_str.lower()
                self._gemini_last_error = err_str[:120]

                # User cancellation or preemption
                if isinstance(e, RuntimeError) and any(k in err_lower for k in ("preempted", "cancelled")):
                    logger.info("Gemini turn aborted by user interrupt: %s", e)
                    return "Command cancelled, Sir."

                # 1. Authentication failure (permanent, do not retry other models)
                if any(k in err_lower for k in ("401", "403", "api_key_invalid", "unauthenticated", "permission_denied", "forbidden")):
                    logger.error("Gemini API authentication failed: %s", e)
                    return "My apologies, Sir. My Gemini API credentials failed authentication. Please verify your GEMINI_API_KEY."

                # 2. Rate limit / quota exhaustion or transient server timeout
                is_quota_or_congestion = any(
                    k in err_lower for k in (
                        "503", "unavailable", "high demand", "429", "resource_exhausted",
                        "quota", "limit", "rate", "504", "deadline_exceeded", "timed out", "timeout"
                    )
                )
                if is_quota_or_congestion and attempt < attempts - 1:
                    time_remaining = turn_deadline - time.perf_counter()
                    # Google Gemini API enforces a minimum client deadline of 10.0s (10000ms)
                    if time_remaining < 10.0:
                        logger.warning(
                            "Turn deadline budget exhausted (%.2fs remaining, min 10.0s required for Gemini retry). Aborting candidate retries.",
                            time_remaining,
                        )
                        return "My apologies, Sir. The cognitive request timed out before receiving a response from Gemini."

                    time.sleep(0.5)
                    self._is_turn_retry = True
                    next_idx = (self.gemini_model_idx + 1) % len(self.gemini_candidates)
                    next_model = self.gemini_candidates[next_idx]
                    logger.warning(
                        "Model '%s' quota/rate limit/timeout reached (%s). Seamlessly switching cognitive link to '%s'...",
                        active_model, err_str[:80], next_model,
                    )
                    self.gemini_model_idx = next_idx
                    self.gemini_model = next_model
                    self._init_gemini(next_model)
                    continue

                # 3. Connection / Network failure
                if any(k in err_lower for k in ("getaddrinfo failed", "connecterror", "networkerror", "connection refused", "winerror 10061")):
                    logger.error("Gemini network connectivity failure: %s", e)
                    return "My apologies, Sir. I am unable to connect to Google Gemini due to a network connection failure."

                # 4. Timeout exhausted
                if any(k in err_lower for k in ("504", "deadline_exceeded", "timed out", "timeout")):
                    logger.error("Gemini request timed out: %s", e)
                    return "My apologies, Sir. The cognitive request timed out before receiving a response from Gemini."

                logger.error(f"Error in Gemini processing: {e}")
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    return "My apologies, Sir. My neural uplink is currently experiencing high network congestion. Please give me a brief moment."
                return "I encountered a slight anomaly in my cognitive circuits, Sir. Please try again in a moment."

        return "My cognitive systems are temporarily congested, Sir. Please try again in a few moments."
