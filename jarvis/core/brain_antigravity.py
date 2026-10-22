"""Antigravity CLI sidecar, GitHub Copilot bridge, and OpenRouter streaming provider mixin."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Brain.Antigravity")


class AntigravityProviderMixin:
    """Provides local Antigravity CLI pipe streaming, GitHub Copilot CLI, and OpenRouter fleet reasoning."""

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

    def _process_openrouter_query(self, user_text: str, sentence_callback: Any = None) -> str:
        """Processes user input via the OpenRouter free AI fleet with streaming TTS and dynamic failover."""
        t_start = time.perf_counter()

        if not self.openrouter_provider or not self.openrouter_provider.is_available():
            return "OpenRouter is not connected. Please set OPENROUTER_API_KEY in .env, Sir."

        if self.openai_messages and self.openai_messages[0]["role"] == "system":
            self.openai_messages[0]["content"] = self._get_system_instruction(user_text)

        self.openai_messages.append({"role": "user", "content": user_text})
        tools = SkillRegistry.get_openai_tools(self.active_tool_names)

        turn_response = self.openrouter_provider.generate_turn(
            messages=self.openai_messages[1:],
            tools=tools if tools else None,
            system_instruction=self.openai_messages[0]["content"] if self.openai_messages else None,
            sentence_callback=sentence_callback,
            timeout=35.0,
        )

        reply_text = turn_response.content or ""

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
                tool_result, _ = self._dispatch_tool_pipeline(
                    fn_name, fn_args, step_index=step_idx, sentence_callback=sentence_callback
                )
                self.openai_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(tool_result),
                })

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

        if len(self.openai_messages) > 25:
            self.openai_messages = [self.openai_messages[0]] + self.openai_messages[-20:]

        elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
        self.last_latencies["openrouter"] = elapsed_ms
        logger.info(
            "OpenRouter inference latency: %sms | model: %s",
            elapsed_ms, turn_response.model,
        )
        return reply_text

    def _process_antigravity_query(self, user_text: str, sentence_callback: Any = None) -> str:
        """Processes queries using the local Antigravity CLI (Gemini 3.8 Flash High/Low, Claude Sonnet, Claude Opus)."""
        t_start = time.perf_counter()
        tools = SkillRegistry.get_openai_tools(self.active_tool_names)
        system_instruction = self._get_system_instruction(user_text)

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
            target_model = self.antigravity_model
        else:
            target_model = self.cognitive_router.resolve_target_model(
                user_text=user_text,
                active_task_mode=self.active_task_mode,
                active_configured_model=self.antigravity_model,
                antigravity_bridge=self.antigravity_bridge,
            )

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
                break

            if self._active_cancel_token and getattr(self._active_cancel_token, "is_cancelled", False):
                logger.info("Antigravity turn cancelled by user interrupt after generation.")
                return "Command cancelled, Sir."

            tool_call_match = re.search(
                r'\{[\s\r\n]*"tool"[\s\r\n]*:[\s\r\n]*"([^"]+)"[\s\r\n]*,[\s\r\n]*"arguments"[\s\r\n]*:[\s\r\n]*(\{.*?\})[\s\r\n]*\}',
                reply_text,
                re.DOTALL
            )
            if not tool_call_match:
                reply = reply_text
                break

            had_tool_call = True
            fn_name = tool_call_match.group(1).strip()
            try:
                fn_args = json.loads(tool_call_match.group(2))
            except Exception:
                fn_args = {}

            logger.info("ANTIGRAVITY Tool call [%d/%d]: %s(%s)", iterations, max_tool_iterations, fn_name, fn_args)
            try:
                tool_result, _ = self._dispatch_tool_pipeline(
                    fn_name, fn_args, step_index=iterations, sentence_callback=sentence_callback
                )
            except RuntimeError as err:
                if "preempted" in str(err).lower() or "cancelled" in str(err).lower():
                    logger.info("Antigravity tool execution aborted by user interrupt: %s", err)
                    return "Command cancelled, Sir."
                raise
            last_tool_result = str(tool_result)
            logger.info("ANTIGRAVITY Tool result: %s", last_tool_result[:250])

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
