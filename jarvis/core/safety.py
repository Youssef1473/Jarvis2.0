"""Safety gatekeeper for dangerous tool executions in Jarvis 2.0."""
import re
from typing import Any


class SafetyGate:
    """Verifies whether commands or tools carry destructive risk."""

    # This denylist catches common destructive PowerShell commands and aliases.
    # It is not a sandbox: arbitrary shell code can still cause side effects.
    DANGEROUS_COMMAND_PATTERNS = [
        r"(?<![\w-])(?:remove-item|ri|del|erase|rd|rmdir|remove-itemproperty)(?![\w-])",
        r"(?<![\w-])(?:clear-content|clc|set-content|sc|out-file)(?![\w-])",
        r"(?<![\w-])(?:format|format-volume|clear-disk)(?![\w-])",
        r"(?<![\w-])(?:shutdown|stop-computer|restart-computer)(?![\w-])",
        r"(?<![\w-])(?:stop-process|kill)(?![\w-])",
        r"(?<![\w-])(?:set-executionpolicy)(?![\w-])",
        r"\b(?:drop\s+table|delete\s+from|truncate\s+table)\b",
        r"\b(?:invoke-expression|iex|encodedcommand|enc)\b",
        r"\bdel\s+/[fsq]",
    ]

    def __init__(self, config: dict[str, Any] = None):
        cfg = config or {}
        self.confirm_destructive = cfg.get("safety", {}).get("confirm_destructive", True)

    def is_command_dangerous(self, command_str: str) -> bool:
        """Checks if a shell command contains potentially destructive verbs."""
        cmd_lower = command_str.lower()
        for pattern in self.DANGEROUS_COMMAND_PATTERNS:
            if re.search(pattern, cmd_lower):
                return True
        return False

    def check_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """
        Returns (is_safe, warning_message).
        If is_safe is False, Jarvis must ask for confirmation first.
        """
        if not self.confirm_destructive:
            return True, ""

        if tool_name in (
            "execute_powershell",
            "execute_sandboxed_powershell",
            "run_background_task",
            "run_job_sandboxed_process",
        ):
            cmd = arguments.get("command", "")
            if isinstance(cmd, list):
                cmd = " ".join(cmd)
            if self.is_command_dangerous(cmd):
                return False, (
                    "Blocked a command matching Jarvis's destructive-command rules. "
                    "This tool cannot request interactive approval, so the command was not run."
                )

        # Browser navigation policy: block dangerous URI schemes and destructive internal pages.
        if tool_name in ("open_browser_tab", "switch_to_browser_tab"):
            target_url = (
                arguments.get("url") or arguments.get("tab_name_or_url") or ""
            ).lower().strip()
            _dangerous_schemes = (
                "javascript:", "file:", "data:", "vbscript:",
                "ms-msdt:", "search-ms:",
            )
            _destructive_pages = (
                "chrome://restart", "chrome://quit", "chrome://crash",
                "chrome://kill", "chrome://settings/clearbrowserdata",
                "brave://restart", "brave://quit", "brave://crash",
                "edge://restart", "edge://quit",
            )
            if any(target_url.startswith(s) for s in _dangerous_schemes):
                return False, (
                    f"Blocked browser navigation to forbidden URI scheme: '{target_url[:50]}'. "
                    "Only http://, https://, and safe about: pages are permitted."
                )
            if any(p in target_url for p in _destructive_pages):
                return False, (
                    f"Blocked navigation to potentially destructive internal browser URL: '{target_url[:80]}'."
                )

        # JavaScript evaluation policy: block storage-wiping and privileged API abuse.
        if tool_name == "execute_browser_javascript":
            expr = (arguments.get("script") or "").lower()
            _dangerous_js = (
                "localstorage.clear()", "sessionstorage.clear()",
                "indexeddb.deletedatabase", "document.cookie",
                "window.location=", "window.location.href=",
            )
            if any(term in expr for term in _dangerous_js):
                return False, (
                    "Blocked potentially destructive or exfiltrating JavaScript expression "
                    "from browser evaluation."
                )

        return True, ""

