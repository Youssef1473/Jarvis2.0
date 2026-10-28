"""Specialist cloud consultation routing and status inspection for Jarvis 2.0."""
from __future__ import annotations

import json
import logging
import os
from urllib.request import Request, urlopen

from google import genai
from google.genai import types

logger = logging.getLogger("Jarvis.Brain.Specialist")


class SpecialistConsultationMixin:
    """Provides user-requested secondary specialist consultations and availability inspection."""

    def specialist_status(self) -> dict[str, str]:
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
            return reply or "OpenRouter returned no text, Sir."

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
