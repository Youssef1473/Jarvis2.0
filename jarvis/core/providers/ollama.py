"""Local Ollama Provider Adapter for Jarvis 2.0.

Provides single-turn generation using local Ollama models (e.g. Qwen 2B/7B).
Recursion is stripped and delegated to UnifiedRunner.
"""
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

from jarvis.audio.chunker import StreamingSentenceChunker
from jarvis.core.providers.base import BaseProvider, LLMResponse, ToolCall

logger = logging.getLogger("Jarvis.Provider.Ollama")


class OllamaProvider(BaseProvider):
    """Adapter for local Ollama instances via OpenAI-compatible endpoints."""

    def __init__(
        self,
        model: str = "qwen3.5:2b-q4_K_M",
        base_url: str = "http://127.0.0.1:11434/v1",
        temperature: float = 0.2,
        config: dict[str, Any] | None = None,
    ):
        super().__init__(name="ollama", model=model, config=config)
        self.base_url = base_url
        self.temperature = temperature
        self.client = OpenAI(base_url=self.base_url, api_key="ollama")

    def generate_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system_instruction: str | None = None,
        sentence_callback: Callable[[str], None] | None = None,
        timeout: float = 35.0,
    ) -> LLMResponse:
        """Executes a single generation turn against the local Ollama instance."""
        t_start = time.perf_counter()

        formatted_messages = []
        if system_instruction:
            formatted_messages.append({"role": "system", "content": system_instruction})

        for m in messages:
            formatted_messages.append({
                "role": m.get("role", "user"),
                "content": m.get("content") or "",
                **({"tool_call_id": m["tool_call_id"]} if "tool_call_id" in m else {}),
                **({"tool_calls": m["tool_calls"]} if "tool_calls" in m else {}),
            })

        tool_calls_map: dict[int, dict[str, Any]] = {}
        content_chunks: list[str] = []
        chunker = StreamingSentenceChunker() if sentence_callback else None

        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=formatted_messages,
                tools=tools if tools else None,
                temperature=self.temperature,
                stream=True,
                timeout=timeout,
            )

            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # Accumulate structured tool calls
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": tc.id or f"ollama_tc_{idx}_{int(time.time()*1000)}",
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

                # Stream content text
                if delta.content:
                    content_chunks.append(delta.content)
                    if chunker:
                        for clause in chunker.feed(delta.content):
                            sentence_callback(clause)

            if chunker:
                for clause in chunker.flush():
                    sentence_callback(clause)

        except Exception as e:
            logger.error("Ollama generation turn failed: %s", e)
            return LLMResponse(
                content=f"Local Ollama error: {e}",
                latency_ms=(time.perf_counter() - t_start) * 1000.0,
                model=self.model,
            )

        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        full_text = "".join(content_chunks).strip()

        # Parse structured tool calls
        final_tool_calls: list[ToolCall] = []
        for tc_data in tool_calls_map.values():
            fn_name = tc_data["name"]
            try:
                fn_args = json.loads(tc_data["arguments"] or "{}")
            except Exception:
                fn_args = {}
            final_tool_calls.append(ToolCall(
                id=tc_data["id"],
                name=fn_name,
                arguments=fn_args,
            ))

        # Handle local models that emit raw XML tags (<tool_call><function=name>...)
        if not final_tool_calls and "<tool_call>" in full_text:
            fn_match = re.search(r'<function=([a-zA-Z0-9_]+)>', full_text)
            args_match = re.search(r'<arguments=({.*?})>', full_text, re.DOTALL)
            if fn_match:
                fn_name = fn_match.group(1).strip()
                try:
                    fn_args = json.loads(args_match.group(1)) if args_match else {}
                except Exception:
                    fn_args = {}
                final_tool_calls.append(ToolCall(
                    id=f"ollama_xml_{int(time.time()*1000)}",
                    name=fn_name,
                    arguments=fn_args,
                ))

        return LLMResponse(
            content=full_text,
            tool_calls=final_tool_calls,
            latency_ms=elapsed_ms,
            model=self.model,
        )
