"""OpenRouter Provider Adapter for Jarvis 2.0.

Provides single-turn generation against OpenRouter API with currently listed free
models, streaming token-by-token TTS narration via StreamingSentenceChunker,
candidate failover, and structured tool execution.
Recursion is delegated to UnifiedRunner.
"""
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

from openai import OpenAI

from jarvis.audio.chunker import StreamingSentenceChunker
from jarvis.core.providers.base import BaseProvider, LLMResponse, ToolCall

logger = logging.getLogger("Jarvis.Provider.OpenRouter")


# Free model candidates confirmed in OpenRouter's public model catalog.
OPENROUTER_FREE_MODELS: List[str] = [
    "nvidia/nemotron-3.5-super-vl:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "cohere/north-mini-code:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
]

BACKUP_FREE_MODELS: List[str] = [
    "liquid/lfm-2.5-2.6b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "google/gemma-4-26b-a4b-it:free",
]


class OpenRouterProvider(BaseProvider):
    """Adapter for OpenRouter free AI model fleet via OpenAI-compatible endpoints."""

    def __init__(
        self,
        model: str = "nvidia/nemotron-3.5-super-vl:free",
        api_key: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api/v1",
        temperature: float = 0.2,
        candidate_models: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(name="openrouter", model=model, config=config)
        self.api_key = (
            api_key if api_key is not None else os.getenv("OPENROUTER_API_KEY", "").strip()
        )
        self.base_url = base_url
        self.temperature = temperature

        # Prioritize the configured model, then try the remaining free candidates.
        base_candidates = list(candidate_models or OPENROUTER_FREE_MODELS)
        self.candidate_models = [model] + [m for m in base_candidates if m != model] + [
            m for m in BACKUP_FREE_MODELS if m not in base_candidates and m != model
        ]

        self.client: Optional[OpenAI] = None
        if self.api_key:
            client_kwargs: Dict[str, Any] = {
                "base_url": self.base_url,
                "api_key": self.api_key,
                "default_headers": {
                    "HTTP-Referer": "https://github.com/Youssef/Jarvis_2.0",
                    "X-Title": "Jarvis 2.0",
                },
                "max_retries": 0,
            }
            try:
                from jarvis.core.providers.http2_pool import get_warm_http2_client
                client_kwargs["http_client"] = get_warm_http2_client()
            except Exception as e:
                logger.debug("Warm HTTP client initialization notice: %s", e)

            self.client = OpenAI(**client_kwargs)
        else:
            logger.warning("OpenRouter API key is missing. Set OPENROUTER_API_KEY in .env.")

    def is_available(self) -> bool:
        """Checks if OpenRouter client is initialized with an API key."""
        return bool(self.client and self.api_key)

    def switch_model(self, model_name: str) -> bool:
        """Hot-switches the active primary model to any candidate in the free pool."""
        self.model = model_name
        if model_name not in self.candidate_models:
            self.candidate_models.insert(0, model_name)
        else:
            self.candidate_models.remove(model_name)
            self.candidate_models.insert(0, model_name)
        logger.info(f"OpenRouter active model switched to: {model_name}")
        return True

    def generate_turn(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_instruction: Optional[str] = None,
        sentence_callback: Optional[Callable[[str], None]] = None,
        timeout: float = 35.0,
    ) -> LLMResponse:
        """Executes one generation turn, trying later free candidates after a model error."""
        t_start = time.perf_counter()

        if not self.client:
            return LLMResponse(
                content="OpenRouter is not connected. Add OPENROUTER_API_KEY to your .env file, Sir.",
                latency_ms=(time.perf_counter() - t_start) * 1000.0,
                model=self.model,
            )

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

        max_tokens = int(self.config.get("max_output_tokens", 512)) if self.config else 512
        last_error = None

        for candidate in self.candidate_models:
            tool_calls_map: Dict[int, Dict[str, Any]] = {}
            content_chunks: List[str] = []
            chunker = StreamingSentenceChunker() if sentence_callback else None

            try:
                logger.info(f"OpenRouter dispatching generation turn on candidate model: '{candidate}'")
                stream = self.client.chat.completions.create(
                    model=candidate,
                    messages=formatted_messages,
                    tools=tools if tools else None,
                    temperature=0.0 if tools else self.temperature,
                    max_tokens=max_tokens,
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
                                    "id": tc.id or f"or_tc_{idx}_{int(time.time()*1000)}",
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

                    # Stream text tokens directly to TTS callback
                    if delta.content:
                        content_chunks.append(delta.content)
                        if chunker and not tool_calls_map:
                            for clause in chunker.feed(delta.content):
                                sentence_callback(clause)

                if chunker and not tool_calls_map:
                    for clause in chunker.flush():
                        sentence_callback(clause)

                elapsed_ms = (time.perf_counter() - t_start) * 1000.0
                full_text = "".join(content_chunks).strip()

                # Parse structured tool calls
                final_tool_calls: List[ToolCall] = []
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

                # Handle models emitting inline XML tool calls
                if not final_tool_calls and "<tool_call>" in full_text:
                    fn_match = re.search(r"<function=([a-zA-Z0-9_]+)>", full_text)
                    args_match = re.search(r"<arguments=({.*?})>", full_text, re.DOTALL)
                    if fn_match:
                        fn_name = fn_match.group(1).strip()
                        try:
                            fn_args = json.loads(args_match.group(1)) if args_match else {}
                        except Exception:
                            fn_args = {}
                        final_tool_calls.append(ToolCall(
                            id=f"or_xml_{int(time.time()*1000)}",
                            name=fn_name,
                            arguments=fn_args,
                        ))
                    full_text = re.sub(r"<tool_call>.*?</tool_call>", "", full_text, flags=re.DOTALL).strip()

                return LLMResponse(
                    content=full_text,
                    tool_calls=final_tool_calls,
                    latency_ms=elapsed_ms,
                    model=candidate,
                )

            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                is_rate_limit = any(k in err_str for k in ("429", "rate limit", "quota", "temporarily", "congested"))
                if candidate != self.candidate_models[-1]:
                    next_cand = self.candidate_models[self.candidate_models.index(candidate) + 1]
                    logger.warning(
                        f"OpenRouter candidate '{candidate}' encountered error (rate_limit={is_rate_limit}): {e}. "
                        f"Cascading to next free candidate '{next_cand}' in <10ms..."
                    )
                    continue
                else:
                    logger.error(f"All OpenRouter free candidates exhausted: {e}")
                    break

        return LLMResponse(
            content=f"OpenRouter could not complete that request ({last_error}), Sir.",
            latency_ms=(time.perf_counter() - t_start) * 1000.0,
            model=self.model,
        )

    def stream_with_hedge(
        self,
        messages: List[Dict[str, Any]],
        primary_model: Optional[str] = None,
        backup_model: Optional[str] = None,
        hedge_delay_s: float = 0.65,
        sentence_callback: Optional[Callable[[str], None]] = None,
        system_instruction: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        timeout: float = 35.0,
    ) -> LLMResponse:
        """Executes a speculative hedge race between primary_model and backup_model.
        If primary_model does not yield any token within hedge_delay_s (default 650ms),
        speculatively launches backup_model; whichever yields the first token wins the turn,
        and the losing stream is immediately cancelled.
        """
        import queue
        import threading
        from jarvis.audio.prosodic_chunker import TwoGearProsodicChunker

        t_start = time.perf_counter()
        if not self.client or not self.api_key:
            return LLMResponse(
                content="OpenRouter is not connected. Add OPENROUTER_API_KEY to your .env file, Sir.",
                latency_ms=(time.perf_counter() - t_start) * 1000.0,
                model=primary_model or self.model,
            )

        p_model = primary_model or self.model
        b_model = backup_model or (
            self.candidate_models[1] if len(self.candidate_models) > 1 else "liquid/lfm-2.5-2.6b:free"
        )
        if b_model == p_model:
            b_model = self.candidate_models[1] if len(self.candidate_models) > 1 else b_model

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

        max_tokens = int(self.config.get("max_output_tokens", 512)) if self.config else 512
        q: queue.Queue = queue.Queue()
        abort_primary = threading.Event()
        abort_backup = threading.Event()

        def _worker(model_name: str, tag: str, abort_ev: threading.Event):
            try:
                stream = self.client.chat.completions.create(
                    model=model_name,
                    messages=formatted_messages,
                    tools=tools if tools else None,
                    temperature=0.0 if tools else self.temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )
                for chunk in stream:
                    if abort_ev.is_set():
                        break
                    choices = getattr(chunk, "choices", []) or []
                    if not choices:
                        continue
                    delta = getattr(choices[0], "delta", None)
                    content = getattr(delta, "content", None) if delta else None
                    if content:
                        q.put((tag, content))
                q.put((f"{tag}_done", None))
            except Exception as exc:
                q.put((f"{tag}_error", exc))

        t_primary = threading.Thread(target=_worker, args=(p_model, "primary", abort_primary), daemon=True)
        t_primary.start()

        winner_tag: Optional[str] = None
        winner_model: str = p_model
        backup_started = False
        t_backup: Optional[threading.Thread] = None
        accumulated_chunks: List[str] = []
        chunker = TwoGearProsodicChunker() if sentence_callback else None

        # Wait for first token up to hedge_delay_s
        deadline = time.perf_counter() + timeout
        try:
            item = q.get(timeout=hedge_delay_s)
            tag, val = item
            if tag == "primary" and val:
                winner_tag = "primary"
                winner_model = p_model
                accumulated_chunks.append(val)
                if sentence_callback and chunker:
                    for chk in chunker.feed(val):
                        sentence_callback(chk)
            elif tag == "primary_error":
                backup_started = True
                t_backup = threading.Thread(target=_worker, args=(b_model, "backup", abort_backup), daemon=True)
                t_backup.start()
        except queue.Empty:
            # Speculative trigger: primary took longer than hedge_delay_s
            backup_started = True
            t_backup = threading.Thread(target=_worker, args=(b_model, "backup", abort_backup), daemon=True)
            t_backup.start()

        # If winner not decided yet, race until first non-empty token arrives
        while winner_tag is None and time.perf_counter() < deadline:
            try:
                rem_time = max(0.05, deadline - time.perf_counter())
                item = q.get(timeout=rem_time)
                tag, val = item
                if tag in ("primary", "backup") and val:
                    winner_tag = tag
                    winner_model = p_model if tag == "primary" else b_model
                    accumulated_chunks.append(val)
                    if sentence_callback and chunker:
                        for chk in chunker.feed(val):
                            sentence_callback(chk)
                    # Abort the loser
                    if tag == "primary":
                        abort_backup.set()
                    else:
                        abort_primary.set()
                    break
                elif tag == "primary_error" and not backup_started:
                    backup_started = True
                    t_backup = threading.Thread(target=_worker, args=(b_model, "backup", abort_backup), daemon=True)
                    t_backup.start()
            except queue.Empty:
                break

        # Consume remaining tokens from the winner stream
        if winner_tag:
            done_tag = f"{winner_tag}_done"
            err_tag = f"{winner_tag}_error"
            while time.perf_counter() < deadline:
                try:
                    rem_time = max(0.05, deadline - time.perf_counter())
                    item = q.get(timeout=rem_time)
                    tag, val = item
                    if tag == winner_tag and val:
                        accumulated_chunks.append(val)
                        if sentence_callback and chunker:
                            for chk in chunker.feed(val):
                                sentence_callback(chk)
                    elif tag in (done_tag, err_tag):
                        break
                except queue.Empty:
                    break

        if sentence_callback and chunker:
            for chk in chunker.flush():
                sentence_callback(chk)

        content = "".join(accumulated_chunks).strip()
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        return LLMResponse(
            content=content,
            tool_calls=[],
            latency_ms=elapsed_ms,
            model=winner_model,
        )
