"""Intent acknowledgment, shutdown directive classification, and topic extraction heuristics."""
from __future__ import annotations

import re


def is_shutdown_command(query: str) -> bool:
    """Determines if query is a departure or shutdown request."""
    clean = "".join(c.lower() for c in (query or "") if c.isalnum() or c.isspace())
    normalized = " ".join(clean.split())
    if normalized in {
        "stop",
        "stop please",
        "please stop",
        "can you stop",
        "could you stop",
        "exit",
        "quit",
        "goodbye",
        "good bye",
        "bye",
    }:
        return True

    # Questions or explanations asking 'how', 'why', 'what', 'who' must never trigger shutdown
    if any(
        normalized.startswith(q)
        for q in ("how ", "why ", "what ", "who ", "where ", "when ", "can you explain", "explain ")
    ):
        return False

    # Programming instructions and technical directives must never trigger shutdown
    code_markers = {
        "def",
        "class",
        "import",
        "python",
        "code",
        "script",
        "step",
        "execute",
        "directive",
        "permission",
        "server",
        "vim",
    }
    query_words = set(normalized.split())
    if query_words & code_markers:
        return False

    # Guard against long technical sentences accidentally triggering departure
    if len(normalized.split()) > 7:
        return False

    triggers = [
        r"\bgoodbye\b",
        r"\bgood bye\b",
        r"\bbye\b",
        r"\bshutdown\b",
        r"\bshut down\b",
        r"\bexit\b",
        r"\bquit\b",
        r"\bthats it for now\b",
        r"\bthat is it for now\b",
        r"\bthats all for now\b",
        r"\bthat is all for now\b",
        r"\bpower down\b",
        r"\bdeactivate\b",
        r"\bgo to sleep\b",
        r"\bsleep now\b",
        r"\bpower off\b",
        r"\bgo offline\b",
        r"\bterminate\b",
        r"\bclose jarvis\b",
        r"\bstop jarvis\b",
        r"\bstop listening\b",
        r"\bstop working\b",
        r"\bstop all services\b",
        r"\bstop all the services\b",
        r"\bstop every service\b",
        r"\bshut yourself down\b",
        r"\bshut down every tool\b",
        r"\bshut down all tools\b",
        r"\bkill jarvis\b",
        r"\bkill every tool\b",
        r"\bkill all tools\b",
        r"\bkill everything\b",
        r"\bturn off\b",
        r"\bclose down\b",
        r"\bstop all tools\b",
    ]
    return any(re.search(t, normalized) for t in triggers)


def get_immediate_intent_acknowledgment(
    norm_query: str,
    is_fast_reflex: bool = False,
    is_model_switch: bool = False,
) -> str | None:
    """Returns an immediate conversational acoustic acknowledgement (<150ms) for complex cognitive turns."""
    if is_fast_reflex or is_model_switch:
        return None

    lower = (norm_query or "").lower().strip()
    is_conversational = any(
        lower.startswith(k) or k in lower
        for k in [
            "it's okay",
            "its okay",
            "forget about",
            "never mind",
            "hello",
            "hi",
            "hey",
            "how are you",
            "who are you",
            "thank you",
            "thanks",
            "good morning",
            "good evening",
        ]
    )
    if is_conversational:
        return None

    if any(
        k in lower
        for k in [
            "teach yourself",
            "add a feature",
            "create a feature",
            "create a skill",
            "build a skill",
            "learn how",
            "learn to",
        ]
    ):
        return "On it, Sir. Conceptualizing the architecture and writing the capability now."
    if any(lower.startswith(k) for k in ["search", "find", "locate", "where is", "where are", "lookup", "look up", "scan"]):
        return "Scanning through the codebase now, Sir."
    if any(k in lower for k in ["fix this", "patch this", "write code", "implement", "refactor", "run tests"]):
        return "Analyzing the implementation and preparing the changes, Sir."
    if any(k in lower for k in ["read that", "process that pdf", "ingest", "parse that"]):
        return "Parsing the document details now, Sir."
    if any(lower.startswith(k) for k in ["why", "explain", "analyze", "compare", "break down"]):
        return "Analyzing that now, Sir. Here is my assessment."

    return None


def extract_topic_label(query: str) -> str:
    """Derives a concise 2-4 word topic phrase from the query for bookmark bridging."""
    q = (query or "").strip()
    pattern = re.compile(
        r"^(?:jarvis,?\s*|hey jarvis,?\s*|can you\s+|could you\s+|please\s+|tell me about\s+|explain\s+|what is\s+|how does\s+|why does\s+|what are\s+|how do\s+)",
        flags=re.IGNORECASE,
    )
    while True:
        new_q = pattern.sub("", q).strip()
        if new_q == q:
            break
        q = new_q
    q_clean = re.sub(r"[?!.,;:]+$", "", q).strip()
    words = q_clean.split()
    if not words:
        return "the prior topic"
    topic = " ".join(words[:4]).strip().lower()
    return topic or "the prior topic"
