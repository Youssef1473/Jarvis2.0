"""Google Gemini Provider Adapter for Jarvis 2.0.

Provides single-turn generation via Google GenAI SDK and Antigravity CLI bridge.
All tool-loop recursion is stripped and delegated to UnifiedRunner.
"""
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from jarvis.core.antigravity_bridge import AntigravityBridge
from jarvis.core.providers.base import BaseProvider, LLMResponse, ToolCall

logger = logging.getLogger("Jarvis.Provider.Gemini")


class GeminiProvider(BaseProvider):
    """Adapter for Google Gemini models and Antigravity Google AI Pro fleet."""

    def __init__(
        self,
        model: str = "gemini-3.8-flash-high",
        api_key: str | None = None,
        use_antigravity: bool = True,
        config: dict[str, Any] | None = None,
    ):
        super().__init__(name="gemini", model=model, config=config)
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
        self.use_antigravity = use_antigravity
        bridge_config = config or {"llm": {"antigravity_model": model}}
        self.antigravity_bridge = AntigravityBridge(config=bridge_config)
        self.client = None

        if self.api_key:
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
            except Exception as e:
                logger.warning("Could not initialize google.genai client: %s", e)

    def generate_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_instruction: str | None = None,
        sentence_callback: Callable[[str], None] | None = None,
        timeout: float = 35.0,
    ) -> LLMResponse:
        """Executes a single generation turn, returning text and extracted ToolCalls."""
        t_start = time.perf_counter()

        # Route 1: Antigravity CLI Bridge (Google AI Pro Fleet) if enabled and available
        if self.use_antigravity and self.antigravity_bridge.is_available():
            return self._generate_antigravity_turn(
                messages=messages,
                tools=tools,
                system_instruction=system_instruction,
                sentence_callback=sentence_callback,
                timeout=timeout,
                t_start=t_start,
            )

        # Route 2: Official Google GenAI SDK
        if self.client:
            return self._generate_sdk_turn(
                messages=messages,
                tools=tools,
                system_instruction=system_instruction,
                sentence_callback=sentence_callback,
                timeout=timeout,
                t_start=t_start,
            )

        return LLMResponse(
            content="Error: Neither Antigravity CLI nor GEMINI_API_KEY is configured, Sir.",
            latency_ms=(time.perf_counter() - t_start) * 1000.0,
            model=self.model,
        )

    def _generate_antigravity_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system_instruction: str | None,
        sentence_callback: Callable[[str], None] | None,
        timeout: float,
        t_start: float,
    ) -> LLMResponse:
        """Executes a single turn through the Antigravity CLI."""
        # Convert dialogue messages to prompt format
        dialogue_parts = []
        for m in messages:
            role = m.get("role", "user").upper()
            content = m.get("content") or ""
            if role == "TOOL":
                dialogue_parts.append(f"[Tool Execution Result]:\n{content}")
            elif role == "ASSISTANT":
                dialogue_parts.append(f"JARVIS: {content}")
            else:
                dialogue_parts.append(f"Sir: {content}")

        dialogue_text = "\n\n".join(dialogue_parts)

        # Inject tool schemas into prompt if available
        tools_block = ""
        if tools:
            tool_signatures = []
            for t in tools:
                fn = t.get("function", {})
                params = fn.get("parameters", {}).get("properties", {})
                tool_signatures.append(f"- {fn.get('name')}: {fn.get('description')} (params: {list(params.keys())})")
            tools_block = (
                "Native OS & System Tools Available:\n" + "\n".join(tool_signatures) + "\n\n"
                "If you need to perform an action, retrieve system/calendar data, or build a feature, respond with a JSON tool call:\n"
                '{"tool": "<name>", "arguments": {<args>}}\n'
                "NEVER describe imaginary actions in text unless you output a tool call to perform them.\n\n"
            )

        full_prompt = tools_block + dialogue_text

        # Sentence streaming filter to prevent raw JSON leaking into TTS
        is_tool_chunk = False
        spoken_chunks = []

        def stream_filter_cb(chunk: str):
            nonlocal is_tool_chunk
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
                prefix = chunk[:tool_marker_idx].strip()
                if prefix and len(prefix.split()) >= 2 and sentence_callback:
                    spoken_chunks.append(prefix)
                    sentence_callback(prefix)
                return

            if "```" in chunk:
                is_tool_chunk = True
                return

            spoken_chunks.append(chunk)
            if sentence_callback:
                sentence_callback(chunk)

        stream_cb = stream_filter_cb if sentence_callback else None

        res = self.antigravity_bridge.generate_response(
            prompt=full_prompt,
            model=self.model,
            system_instruction=system_instruction,
            sentence_callback=stream_cb,
            timeout=timeout,
        )

        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        raw_text = (res.get("response") or "").strip()

        # Parse tool calls from output
        tool_calls: list[ToolCall] = []
        from jarvis.core.streaming_tool_parser import extract_tool_call_json

        tool_match_info = extract_tool_call_json(raw_text)
        if tool_match_info:
            fn_name, fn_args, start_idx, _end_idx = tool_match_info
            tool_calls.append(ToolCall(
                id=f"call_{int(time.time()*1000)}",
                name=fn_name,
                arguments=fn_args,
            ))
            # Clean JSON out of spoken text
            clean_text = raw_text[:start_idx].strip()
        else:
            clean_text = raw_text

        return LLMResponse(
            content=clean_text,
            tool_calls=tool_calls,
            latency_ms=elapsed_ms,
            model=self.model,
            raw_response=res,
        )

    def _generate_sdk_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system_instruction: str | None,
        sentence_callback: Callable[[str], None] | None,
        timeout: float,
        t_start: float,
    ) -> LLMResponse:
        """Executes a single turn through the Google GenAI SDK."""
        from google.genai import types

        contents = []
        for m in messages:
            role = "model" if m.get("role") == "assistant" else "user"
            text_val = m.get("content") or ""
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=text_val)]))

        sdk_tools = None
        if tools:
            # Pass registered skills as function declarations if available
            pass

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=sdk_tools,
        )

        response = self.client.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )

        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        content_text = response.text or ""

        tool_calls: list[ToolCall] = []
        if response.function_calls:
            for idx, fc in enumerate(response.function_calls):
                tool_calls.append(ToolCall(
                    id=f"sdk_call_{idx}_{int(time.time()*1000)}",
                    name=fc.name,
                    arguments=dict(fc.args) if fc.args else {},
                ))

        if sentence_callback and content_text:
            sentence_callback(content_text)

        return LLMResponse(
            content=content_text,
            tool_calls=tool_calls,
            latency_ms=elapsed_ms,
            model=self.model,
            raw_response=response,
        )
