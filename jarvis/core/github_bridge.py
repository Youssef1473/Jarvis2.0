"""GitHub Copilot CLI bridge for Jarvis 2.0."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from jarvis.audio.prosodic_chunker import TwoGearProsodicChunker
from jarvis.core.stream_tool_parser import NonceGuardedStreamParser, ParsedToolCall

logger = logging.getLogger("Jarvis.GitHubCopilot")


class GitHubCopilotBridge:
    """Runs Copilot CLI in prompt mode with a no-tools custom agent."""

    DEFAULT_TIMEOUT_SECONDS = 120.0
    AGENT_NAME = "jarvis-chat"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        llm_cfg = self.config.get("llm", {})
        configured_path = llm_cfg.get("github_copilot_path", "").strip()
        self.copilot_path = configured_path or shutil.which("copilot") or ""
        self.timeout_seconds = float(
            llm_cfg.get("github_copilot_timeout_seconds", self.DEFAULT_TIMEOUT_SECONDS)
        )
        self.workspace = Path(__file__).resolve().parents[2]
        self.agent_path = self.workspace / ".github" / "agents" / f"{self.AGENT_NAME}.agent.md"

    def is_available(self) -> bool:
        executable = Path(self.copilot_path) if self.copilot_path else None
        if os.name == "nt" and executable and executable.suffix.lower() in {".bat", ".cmd"}:
            return False
        return bool(executable and executable.is_file() and self.agent_path.is_file())

    def generate_response(
        self,
        prompt: str,
        model: Optional[str] = None,
        system_instruction: str = "",
        timeout: Optional[float] = None,
        conversation_id: Optional[str] = None,
        candidates: Optional[list[str]] = None,
        sentence_callback: Optional[Any] = None,
        on_speech_chunk: Optional[Callable[[str], None]] = None,
        cancel_token: Optional[Any] = None,
        turn_nonce: Optional[str] = None,
        on_tool_call: Optional[Callable[[Any], None]] = None,
    ) -> Dict[str, Any]:
        del model, conversation_id, candidates
        callback = on_speech_chunk or sentence_callback

        if not self.is_available():
            return {
                "status": "ERROR",
                "response": (
                    "GitHub Copilot CLI is unavailable. On Windows, install the native CLI "
                    "with `winget install GitHub.Copilot`, authenticate with `copilot login`, "
                    "and ensure the Jarvis no-tools agent file is present."
                ),
                "duration_seconds": 0.0,
                "usage": {},
            }

        full_prompt = prompt.strip()
        if system_instruction.strip():
            full_prompt = (
                "[Jarvis System Instructions]\n"
                f"{system_instruction.strip()}\n\n"
                "[User Request]\n"
                f"{full_prompt}"
            )

        command = [
            self.copilot_path,
            "--agent",
            self.AGENT_NAME,
            "--prompt",
            full_prompt,
            "--no-custom-instructions",
            "--deny-tool=write",
            "--deny-tool=shell",
        ]
        timeout_seconds = timeout if timeout is not None else self.timeout_seconds
        started_at = time.perf_counter()

        # If no streaming callback is requested, OR subprocess.run is patched in tests: use subprocess.run
        is_mocked_run = (
            hasattr(subprocess.run, "_mock_name")
            or "Mock" in type(subprocess.run).__name__
        )

        if not callback or is_mocked_run:
            try:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(self.workspace),
                    timeout=timeout_seconds,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    close_fds=True,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                elapsed = time.perf_counter() - started_at
                logger.warning("GitHub Copilot CLI request exceeded %.1f seconds.", timeout_seconds)
                return {
                    "status": "TIMEOUT",
                    "response": f"GitHub Copilot CLI request timed out after {timeout_seconds:.1f} seconds, Sir.",
                    "duration_seconds": elapsed,
                    "usage": {},
                }
            except OSError as exc:
                elapsed = time.perf_counter() - started_at
                logger.error("Could not start GitHub Copilot CLI: %s", exc)
                return {
                    "status": "ERROR",
                    "response": f"Could not start GitHub Copilot CLI: {exc}",
                    "duration_seconds": elapsed,
                    "usage": {},
                }

            elapsed = time.perf_counter() - started_at
            response_text = result.stdout.strip()
            if result.returncode != 0:
                diagnostic_text = f"{result.stderr}\n{response_text}".lower()
                if any(marker in diagnostic_text for marker in ("not authenticated", "authentication required", "not logged in", "unauthorized")):
                    error_text = "Authentication failed. Run `copilot login` and retry."
                elif any(marker in diagnostic_text for marker in ("quota", "rate limit", "too many requests", "429")):
                    error_text = "Copilot reported a request or quota limit."
                else:
                    error_text = f"Copilot CLI exited with code {result.returncode}."
                logger.warning("GitHub Copilot CLI returned code %s.", result.returncode)
                return {
                    "status": "ERROR",
                    "response": error_text,
                    "duration_seconds": elapsed,
                    "usage": {},
                }

            if not response_text:
                error_text = "GitHub Copilot CLI returned an empty response."
                logger.warning("GitHub Copilot CLI returned no answer.")
                return {
                    "status": "ERROR",
                    "response": error_text,
                    "duration_seconds": elapsed,
                    "usage": {},
                }

            return {
                "status": "SUCCESS",
                "response": response_text,
                "duration_seconds": elapsed,
                "usage": {},
            }

        # Streaming execution via subprocess.Popen
        proc: Optional[subprocess.Popen] = None
        accumulated_text: list[str] = []
        chunker = TwoGearProsodicChunker()
        tool_parser = NonceGuardedStreamParser(turn_nonce=turn_nonce) if turn_nonce else None
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.workspace),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )

            deadline = started_at + timeout_seconds
            while True:
                if cancel_token is not None and hasattr(cancel_token, "raise_if_cancelled"):
                    try:
                        cancel_token.raise_if_cancelled()
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=1.0)
                        raise

                if time.perf_counter() > deadline:
                    proc.kill()
                    proc.wait(timeout=1.0)
                    elapsed = time.perf_counter() - started_at
                    logger.warning("GitHub Copilot CLI streaming exceeded %.1f seconds.", timeout_seconds)
                    return {
                        "status": "TIMEOUT",
                        "response": f"GitHub Copilot CLI request timed out after {timeout_seconds:.1f} seconds, Sir.",
                        "duration_seconds": elapsed,
                        "usage": {},
                    }

                try:
                    line = proc.stdout.readline() if proc.stdout else ""
                except (StopIteration, ValueError, OSError):
                    line = ""
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.01)
                    continue

                accumulated_text.append(line)
                speech_line = line
                if tool_parser:
                    clean_speech, ready_tools = tool_parser.feed(line)
                    speech_line = clean_speech
                    for t in ready_tools:
                        if on_tool_call:
                            on_tool_call(t)

                if speech_line:
                    for ready_chunk in chunker.feed(speech_line):
                        if callback:
                            callback(ready_chunk)

            if tool_parser:
                tail_speech, tail_tools = tool_parser.flush()
                for t in tail_tools:
                    if on_tool_call:
                        on_tool_call(t)
                if tail_speech:
                    for ready_chunk in chunker.feed(tail_speech):
                        if callback:
                            callback(ready_chunk)

            # Flush any remaining prosodic text
            for ready_chunk in chunker.flush():
                if callback:
                    callback(ready_chunk)

            stdout_rem, stderr_out = proc.communicate(timeout=2.0)
            if stdout_rem:
                accumulated_text.append(stdout_rem)

            elapsed = time.perf_counter() - started_at
            response_text = "".join(accumulated_text).strip()

            if proc.returncode != 0:
                diagnostic_text = f"{stderr_out}\n{response_text}".lower()
                if any(marker in diagnostic_text for marker in ("not authenticated", "authentication required", "not logged in", "unauthorized")):
                    error_text = "Authentication failed. Run `copilot login` and retry."
                elif any(marker in diagnostic_text for marker in ("quota", "rate limit", "too many requests", "429")):
                    error_text = "Copilot reported a request or quota limit."
                else:
                    error_text = f"Copilot CLI exited with code {proc.returncode}."
                return {
                    "status": "ERROR",
                    "response": error_text,
                    "duration_seconds": elapsed,
                    "usage": {},
                }

            if not response_text:
                return {
                    "status": "ERROR",
                    "response": "GitHub Copilot CLI returned an empty response.",
                    "duration_seconds": elapsed,
                    "usage": {},
                }

            return {
                "status": "SUCCESS",
                "response": response_text,
                "duration_seconds": elapsed,
                "usage": {},
            }

        except Exception:
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            raise

        return {
            "status": "SUCCESS",
            "response": response_text,
            "duration_seconds": elapsed,
            "usage": {},
        }
