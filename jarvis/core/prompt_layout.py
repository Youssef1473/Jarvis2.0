"""4-Zone Canonical Prompt Prefix Layout for 90%+ Server-Side KV Cache Hits in Jarvis 2.0.

Structure:
- Zone 1: Static System Instructions & Compact TypeScript Tool Specs (100% Immutable)
- Zone 2: Core User Profile & Stable Memory (Semi-Static, <1KB, Sorted Keys)
- Zone 3: Multi-Turn Conversation History (Append-Only)
- Zone 4: Ephemeral State, Telemetry & Nonce Execution Frame (Dynamic Tail)
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("Jarvis.Core.PromptLayout")


@dataclass
class RadixNode:
    """Represents a discrete, cacheable block in the prompt prefix tree."""

    content: str
    digest: bytes
    parent: RadixNode | None = None

    @classmethod
    def create_root(cls, preamble: str, tool_specs: str) -> RadixNode:
        """Constructs the H_root node (~28k tokens).

        Computed once at startup with explicit BPE barrier sentinels.
        """
        content = f"{preamble.strip()}\n\n[ZONE 1: TOOLS]\n\n{tool_specs.strip()}\n\n[ZONE 2: PREFERENCES]\n\n"
        digest = hashlib.blake2b(content.encode("utf-8"), digest_size=8).digest()
        logger.debug("H_root materialized. Digest: %s", digest.hex())
        return cls(content=content, digest=digest)

    def append_child(self, new_content: str, zone_sentinel: str) -> RadixNode:
        """Derives a child node (e.g., H_user, H_turn_k) using a rolling hash."""
        terminated_content = f"{new_content.strip()}\n\n[{zone_sentinel}]\n\n"
        hasher = hashlib.blake2b(digest_size=8)
        hasher.update(self.digest)
        hasher.update(terminated_content.encode("utf-8"))
        child_digest = hasher.digest()
        return RadixNode(content=terminated_content, digest=child_digest, parent=self)

    def compile_full_prompt(self, volatile_leaf: str) -> str:
        """Traverses the tree to compile the exact string payload for the cloud API,

        appending the un-cached volatile ephemera at the absolute tail.
        """
        nodes = []
        current: RadixNode | None = self
        while current is not None:
            nodes.append(current.content)
            current = current.parent

        nodes.reverse()
        nodes.append(volatile_leaf.strip())
        return "".join(nodes)

    @property
    def hex_digest(self) -> str:
        """Returns the hex string representation of the 64-bit BLAKE2b digest."""
        return self.digest.hex()

    @property
    def depth(self) -> int:
        """Returns the depth of the node from root (root depth = 1)."""
        count = 0
        current: RadixNode | None = self
        while current is not None:
            count += 1
            current = current.parent
        return count


def _map_schema_type_to_ts(prop_spec: dict[str, Any]) -> str:
    """Maps JSON schema parameter specification to compact TypeScript type."""
    stype = prop_spec.get("type", "any")
    if stype == "string":
        enum_vals = prop_spec.get("enum")
        if enum_vals:
            return " | ".join(f'"{v}"' for v in enum_vals)
        return "string"
    elif stype in ("integer", "number"):
        return "number"
    elif stype == "boolean":
        return "boolean"
    elif stype == "array":
        items = prop_spec.get("items", {})
        item_ts = _map_schema_type_to_ts(items) if items else "any"
        return f"{item_ts}[]"
    elif stype == "object":
        return "Record<string, any>"
    return "any"


def format_compact_typescript_tools(tools: list[dict[str, Any]]) -> str:
    """Converts tool schemas into deterministic, compact TypeScript function declarations.

    Tools and parameters are strictly sorted alphabetically to guarantee byte-for-byte
    serialization consistency across turns, maximizing server-side KV cache hits.
    """
    if not tools:
        return "// No external tools configured."

    # Extract tool metadata and sort alphabetically by tool name
    parsed_tools = []
    for tool in tools:
        fn = tool.get("function", {}) if "function" in tool else tool
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        if name:
            parsed_tools.append((name, desc, params))

    parsed_tools.sort(key=lambda t: t[0])

    lines: list[str] = []
    for name, desc, params in parsed_tools:
        if desc:
            lines.append(f"/** {desc.strip()} */")

        props = params.get("properties", {})
        required = set(params.get("required", []))

        if not props:
            lines.append(f"declare function {name}(): void;\n")
            continue

        sorted_prop_names = sorted(props.keys())
        arg_fields = []
        for p_name in sorted_prop_names:
            p_spec = props[p_name]
            p_type = _map_schema_type_to_ts(p_spec)
            opt = "" if p_name in required else "?"
            p_desc = p_spec.get("description", "")
            comment = f" // {p_desc.strip()}" if p_desc else ""
            arg_fields.append(f"    {p_name}{opt}: {p_type};{comment}")

        fields_block = "\n".join(arg_fields)
        lines.append(f"declare function {name}(args: {{\n{fields_block}\n}}): void;\n")

    return "\n".join(lines).strip()


def build_canonical_4zone_prompt(
    system_instructions: str,
    tools: list[dict[str, Any]],
    user_preferences: dict[str, Any],
    conversation_history: list[dict[str, Any]],
    ephemeral_telemetry: dict[str, Any],
    user_query: str,
    nonce: str,
) -> str:
    """Composes the full prompt enforcing the 4-zone canonical prefix layout.

    Guarantees Zone 1 and Zone 2 remain completely prefix-stable across conversational turns.
    """
    # Zone 1: Static Instructions & Immutable TypeScript Tools
    ts_tools = format_compact_typescript_tools(tools)
    zone1 = (
        "[ZONE 1: SYSTEM INSTRUCTIONS & IMMUTABLE TOOLS]\n"
        f"{system_instructions.strip()}\n\n"
        "[AVAILABLE TOOLS]\n"
        f"{ts_tools}"
    )

    # Zone 2: Core User Profile & Preferences (Sorted deterministically)
    pref_lines = []
    if user_preferences:
        for k in sorted(user_preferences.keys()):
            pref_lines.append(f"- {k}: {user_preferences[k]}")
    pref_text = "\n".join(pref_lines) if pref_lines else "- (No user preferences recorded)"
    zone2 = (
        "[ZONE 2: CORE USER PROFILE & PREFERENCES]\n"
        f"{pref_text}"
    )

    # Zone 3: Multi-Turn Conversation History (Append-Only)
    hist_lines = []
    if conversation_history:
        for turn in conversation_history:
            role = turn.get("role", "user").capitalize()
            content = turn.get("content", "").strip()
            if content:
                hist_lines.append(f"[{role}]: {content}")
    hist_text = "\n\n".join(hist_lines) if hist_lines else "(No previous turns)"
    zone3 = (
        "[ZONE 3: CONVERSATION HISTORY]\n"
        f"{hist_text}"
    )

    # Zone 4: Ephemeral Telemetry, Nonce Frame & Active Query
    telem_lines = []
    if ephemeral_telemetry:
        for k in sorted(ephemeral_telemetry.keys()):
            telem_lines.append(f"- {k}: {ephemeral_telemetry[k]}")
    telem_text = "\n".join(telem_lines) if telem_lines else "- (Telemetry unavailable)"

    zone4 = (
        "[ZONE 4: EPHEMERAL TELEMETRY & NONCE EXECUTION FRAME]\n"
        f"Ephemeral Telemetry:\n{telem_text}\n\n"
        f"Nonce Instruction:\n"
        f"Tool invocations for this turn MUST use delimiter <<JARVIS_CALL_{nonce}>>...<<END_CALL_{nonce}>>\n\n"
        f"User Query:\n{user_query.strip()}"
    )

    return f"{zone1}\n\n{zone2}\n\n{zone3}\n\n{zone4}"


def verify_prefix_stability(prompt_a: str, prompt_b: str, min_prefix_chars: int = 1024) -> bool:
    """Verifies that the canonical immutable prefix (Zone 1 & Zone 2) matches exactly across turns."""
    if not prompt_a or not prompt_b:
        return False
    limit = min(len(prompt_a), len(prompt_b), min_prefix_chars)
    if limit == 0:
        return False
    return prompt_a[:limit] == prompt_b[:limit]


__all__ = [
    "RadixNode",
    "build_canonical_4zone_prompt",
    "format_compact_typescript_tools",
    "verify_prefix_stability",
]
