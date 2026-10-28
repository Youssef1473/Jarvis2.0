"""Transcript Distillation & Trait Inscription Engine for Jarvis 2.0.

Harvests unprocessed interaction dialogue from SQLite during workstation sleep phase,
scrubs sensitive credentials, distills long-term user traits, coding conventions,
and bi-temporal knowledge graph edges via LLM, and crystallizes them into persistent
memory with zero-contention per-item interruption yields (<2ms).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("Jarvis.Memory.TranscriptDistiller")

# Canonical slots for Tier-1 core user profile to keep <core_user_preferences> strictly under 1KB
CANONICAL_PREFERENCE_KEYS: frozenset[str] = frozenset({
    "preferred_languages",
    "coding_conventions",
    "communication_style",
    "primary_ide",
    "current_focus",
    "workflow_preferences",
    "speech_speed",
    "verbosity",
    "active_compiler",
    "package_manager",
    "test_framework",
    "os_environment",
    "hardware_specs",
    "daily_schedule",
})

# Hard ceiling on total user preference entries
MAX_CORE_PREFERENCES_CEILING: int = 20

# Trivial greetings and simple reflexes to skip LLM distillation
_TRIVIAL_UTTERANCES: frozenset[str] = frozenset({
    "hey jarvis", "jarvis", "hello", "hi", "good morning", "good evening",
    "yes", "no", "ok", "okay", "stop", "cancel", "thanks", "thank you",
    "what time is it", "what is the date", "system online", "status",
    "exit", "quit", "clear", "ping", "test", "ready",
})

# Precise secret regexes targeting actual key-value assignments and known token signatures.
# Bare words like 'auth' or 'token' are NOT matched to avoid discarding developer discussions.
_SENSITIVE_PATTERNS: list[re.Pattern] = [
    # Key-value assignments: api_key = "...", token: '...', password = "..."
    re.compile(
        r"(?:api[_-]?key|secret|password|token|bearer)\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.~+/]{8,}['\"]?",
        re.IGNORECASE,
    ),
    # GitHub personal access tokens
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b", re.IGNORECASE),
    # OpenAI API keys
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b", re.IGNORECASE),
    # Google API keys
    re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}\b", re.IGNORECASE),
    # JWT tokens: eyJ...
    re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b",
        re.IGNORECASE,
    ),
    # Bearer authentication header
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{15,}\b", re.IGNORECASE),
]


def scrub_sensitive_credentials(text: str) -> str:
    """Replaces sensitive API keys, tokens, and passwords with [REDACTED_CREDENTIAL]."""
    if not text:
        return ""
    scrubbed = text
    for pat in _SENSITIVE_PATTERNS:
        scrubbed = pat.sub("[REDACTED_CREDENTIAL]", scrubbed)
    return scrubbed


def contains_unredacted_credentials(text: str) -> bool:
    """Returns True if the text contains recognizable credential tokens or secret assignments."""
    if not text:
        return False
    return any(pat.search(text) for pat in _SENSITIVE_PATTERNS)


class TranscriptDistiller:
    """Autonomous idle-phase conversation harvester and trait distillation engine."""

    def __init__(
        self,
        db_path: str,
        vector_engine: Any = None,
        llm_config: dict[str, Any] | None = None,
        memory_manager: Any = None,
    ):
        self.db_path = str(db_path)
        self.vector_engine = vector_engine
        self.llm_config = llm_config or {}
        self.memory_manager = memory_manager

    def _connect(self, read_only: bool = False) -> sqlite3.Connection:
        """Opens a SQLite connection with WAL mode and sqlite-vec loaded."""
        uri = f"file:{self.db_path}{'?mode=ro' if read_only else ''}"
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=5.0 if read_only else 0.05,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=50;")
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
        except Exception as e:
            logger.debug("sqlite-vec load notice in TranscriptDistiller: %s", e)
        return conn

    def get_last_distilled_id(self) -> int:
        """Retrieves the highest interaction ID already distilled from schema_meta."""
        conn = self._connect(read_only=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM schema_meta WHERE key = 'last_distilled_interaction_id'")
            row = cursor.fetchone()
            if row and str(row["value"]).isdigit():
                return int(row["value"])
            return 0
        finally:
            conn.close()

    def set_last_distilled_id(self, last_id: int) -> None:
        """Updates the highest distilled interaction ID in schema_meta."""
        conn = self._connect(read_only=False)
        try:
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('last_distilled_interaction_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(last_id),),
            )
        finally:
            conn.close()

    def get_unprocessed_interactions(
        self, limit: int = 25
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Fetches new interaction records beyond the last distilled ID.

        Returns:
            (meaningful_interactions, max_fetched_id, all_trivial)
        """
        last_id = self.get_last_distilled_id()
        conn = self._connect(read_only=True)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, user_input, jarvis_reply, tool_calls, created_at "
                "FROM interactions WHERE id > ? ORDER BY id ASC LIMIT ?",
                (last_id, limit),
            )
            rows = cursor.fetchall()
            if not rows:
                return [], last_id, False

            max_id = max(int(r["id"]) for r in rows)
            meaningful: list[dict[str, Any]] = []

            for r in rows:
                u_text = (r["user_input"] or "").strip()
                u_lower = u_text.lower()
                # Filter out trivial utterances or short reflex commands
                if len(u_lower) < 6 or u_lower in _TRIVIAL_UTTERANCES:
                    continue
                meaningful.append({
                    "id": int(r["id"]),
                    "user_input": u_text,
                    "jarvis_reply": (r["jarvis_reply"] or "").strip(),
                    "tool_calls": (r["tool_calls"] or "").strip(),
                    "created_at": str(r["created_at"]),
                })

            all_trivial = (len(meaningful) == 0 and len(rows) > 0)
            return meaningful, max_id, all_trivial
        finally:
            conn.close()

    def format_transcript_block(self, interactions: list[dict[str, Any]]) -> str:
        """Formats interactions into a chronological dialogue script."""
        turns = []
        for i, turn in enumerate(interactions, start=1):
            tools_part = f" [Tools: {turn['tool_calls']}]" if turn.get("tool_calls") else ""
            turns.append(
                f"Turn #{i} (Interaction #{turn['id']} at {turn['created_at']})\n"
                f"Sir: {turn['user_input']}\n"
                f"JARVIS: {turn['jarvis_reply']}{tools_part}"
            )
        return "\n\n".join(turns)

    def extract_traits_and_facts(
        self,
        transcript_text: str,
        is_user_active_fn: Callable[[], bool] | None = None,
        extractor_fn: Callable[[str], dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Distills multi-turn dialogue into structured traits with pre- and post-LLM secret scrubbing."""
        if not transcript_text.strip():
            return {"core_preferences": [], "knowledge_edges": [], "atomic_facts": []}

        # Adjustment 2: Scrub sensitive credentials BEFORE sending to LLM
        scrubbed_prompt_text = scrub_sensitive_credentials(transcript_text)

        # Adjustment 5: Check physical user activity before LLM call
        if is_user_active_fn and is_user_active_fn():
            logger.info("User activity detected prior to LLM trait extraction; yielding immediately.")
            return None

        # 1. Injectable or Mock Extractor Fast Path (used in testing)
        if extractor_fn is not None:
            raw_output = extractor_fn(scrubbed_prompt_text)
            # Adjustment 5: Check physical activity immediately after LLM call
            if is_user_active_fn and is_user_active_fn():
                logger.info("User activity detected after extractor execution; yielding immediately.")
                return None
            return self._validate_and_sanitize_distilled_data(raw_output)

        # 2. Production LLM Dispatch
        system_instruction = (
            "You are the Sleep-Phase Memory & Trait Distillation Engine for Jarvis 2.0.\n"
            "Analyze the conversation transcripts between Sir and JARVIS.\n"
            "Extract permanent personal traits, coding preferences, behavioral habits,\n"
            "or factual knowledge about projects, tools, deadlines, or people mentioned by Sir.\n\n"
            "Output a strict JSON object with three arrays:\n"
            "1. 'core_preferences': [ {'key': 'canonical_key', 'value': 'concise directive'} ]\n"
            "   - Allowed canonical keys ONLY: preferred_languages, coding_conventions, communication_style,\n"
            "     primary_ide, current_focus, workflow_preferences, speech_speed, verbosity, active_compiler,\n"
            "     package_manager, test_framework, os_environment, hardware_specs, daily_schedule.\n"
            "   - Do NOT invent arbitrary keys for core_preferences.\n"
            "2. 'knowledge_edges': [\n"
            "     {\n"
            "       'subject': 'Sir or entity name',\n"
            "       'predicate': 'relationship or attribute',\n"
            "       'object': 'value or target entity',\n"
            "       'is_functional': true/false (true if singular current state, false if additive),\n"
            "       'confidence': 0.85 to 1.0,\n"
            "       'decay_lambda': 0.01\n"
            "     }\n"
            "   ]\n"
            "3. 'atomic_facts': [ {'category': 'category_name', 'key': 'unique_key', 'value': 'fact statement'} ]\n\n"
            "CRITICAL RULES:\n"
            "- NEVER extract temporary commands or questions ('what is the weather', 'run tests').\n"
            "- NEVER extract conversational filler or polite banter.\n"
            "- NEVER output API keys, passwords, or secrets.\n"
            "- If no permanent traits or facts are present, output:\n"
            "  {'core_preferences': [], 'knowledge_edges': [], 'atomic_facts': []}\n"
            "Output valid JSON without markdown formatting."
        )

        extracted_data: dict[str, Any] | None = None

        # Try Antigravity Bridge first
        try:
            from jarvis.core.antigravity_bridge import AntigravityBridge
            bridge = AntigravityBridge()
            if bridge.is_available():
                res = bridge.generate_response(
                    prompt=f"[Conversation Transcript]\n{scrubbed_prompt_text}",
                    model="gemini-3.8-flash-medium",
                    system_instruction=system_instruction,
                    timeout=30.0,
                )
                raw_reply = (res.get("response") or "").strip()
                if raw_reply:
                    clean_json = re.sub(r"^```(?:json)?\s*", "", raw_reply)
                    clean_json = re.sub(r"\s*```$", "", clean_json)
                    m = re.search(r"(\{.*\})", clean_json, re.DOTALL)
                    if m:
                        extracted_data = json.loads(m.group(1))
        except Exception as ag_err:
            logger.debug("AntigravityBridge trait distillation notice: %s", ag_err)

        # Adjustment 5: Check physical activity immediately after LLM invocation
        if is_user_active_fn and is_user_active_fn():
            logger.info("User activity detected after LLM call; yielding immediately.")
            return None

        # Fallback to local Ollama if configured and Antigravity unavailable
        if not extracted_data:
            ollama_url = self.llm_config.get("ollama_base_url", "http://127.0.0.1:11434/v1")
            ollama_model = self.llm_config.get("ollama_model", "qwen3.5:2b-q4_K_M")
            try:
                from openai import OpenAI
                client = OpenAI(base_url=ollama_url, api_key="ollama", timeout=25.0)
                res = client.chat.completions.create(
                    model=ollama_model,
                    messages=[
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": scrubbed_prompt_text},
                    ],
                    temperature=0.0,
                )
                content = res.choices[0].message.content.strip()
                clean_json = re.sub(r"^```(?:json)?\s*", "", content)
                clean_json = re.sub(r"\s*```$", "", clean_json)
                m = re.search(r"(\{.*\})", clean_json, re.DOTALL)
                if m:
                    extracted_data = json.loads(m.group(1))
            except Exception as ol_err:
                logger.debug("Local Ollama trait distillation notice: %s", ol_err)

        if not extracted_data:
            return None

        return self._validate_and_sanitize_distilled_data(extracted_data)

    def _validate_and_sanitize_distilled_data(
        self, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Adjustment 2 & 3: Post-extraction validation, credential scrubbing, and canonical key enforcement."""
        sanitized: dict[str, Any] = {
            "core_preferences": [],
            "knowledge_edges": [],
            "atomic_facts": [],
        }

        # 1. Core Preferences
        for pref in data.get("core_preferences", []):
            if not isinstance(pref, dict):
                continue
            k = str(pref.get("key", "")).strip().lower().replace(" ", "_")
            v = str(pref.get("value", "")).strip()
            if not k or not v or contains_unredacted_credentials(k) or contains_unredacted_credentials(v):
                continue

            v_clean = scrub_sensitive_credentials(v)
            # Adjustment 3: Cap and route non-canonical keys to atomic_facts
            if k in CANONICAL_PREFERENCE_KEYS:
                sanitized["core_preferences"].append({"key": k, "value": v_clean})
            else:
                sanitized["atomic_facts"].append({
                    "category": "general",
                    "key": k,
                    "value": v_clean,
                })

        # 2. Knowledge Edges
        for edge in data.get("knowledge_edges", []):
            if not isinstance(edge, dict):
                continue
            s = str(edge.get("subject", "")).strip()
            p = str(edge.get("predicate", "")).strip().lower().replace(" ", "_")
            o = str(edge.get("object", "")).strip()
            if not s or not p or not o:
                continue
            if (
                contains_unredacted_credentials(s)
                or contains_unredacted_credentials(p)
                or contains_unredacted_credentials(o)
            ):
                continue

            sanitized["knowledge_edges"].append({
                "subject": s,
                "predicate": p,
                "object": scrub_sensitive_credentials(o),
                "is_functional": bool(edge.get("is_functional", True)),
                "confidence": max(0.1, min(1.0, float(edge.get("confidence", 0.95)))),
                "decay_lambda": max(0.0001, float(edge.get("decay_lambda", 0.01))),
            })

        # 3. Atomic Facts
        for fact in data.get("atomic_facts", []):
            if not isinstance(fact, dict):
                continue
            cat = str(fact.get("category", "general")).strip().lower()
            k = str(fact.get("key", "")).strip().lower().replace(" ", "_")
            v = str(fact.get("value", "")).strip()
            if not k or not v or contains_unredacted_credentials(k) or contains_unredacted_credentials(v):
                continue

            sanitized["atomic_facts"].append({
                "category": cat,
                "key": k,
                "value": scrub_sensitive_credentials(v),
            })

        return sanitized

    def commit_distilled_knowledge(
        self,
        distilled_data: dict[str, Any],
        max_interaction_id: int,
        is_user_active_fn: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Adjustment 4: Per-item interruption yield (<2ms) and cursor safety during inscription."""
        prefs_inscribed = 0
        edges_inscribed = 0
        facts_inscribed = 0
        yielded_early = False

        if self.memory_manager is not None:
            memory = self.memory_manager
        else:
            from jarvis.core.memory import MemoryManager
            base_dir = Path(self.db_path).parent
            memory = MemoryManager({
                "_base_dir": str(base_dir),
                "memory": {"db_path": str(Path(self.db_path).name), "background_learning": False},
            })

        # 1. Inscribe Core Preferences with 20-row ceiling guard
        active_prefs = memory.get_user_preferences()
        for p in distilled_data.get("core_preferences", []):
            # Adjustment 4: Per-item interruption check before DB write / embedding
            if is_user_active_fn and is_user_active_fn():
                logger.info("User activity detected during preference inscription; yielding in <2ms.")
                yielded_early = True
                break

            k = p["key"]
            v = p["value"]
            # Enforce 20-slot ceiling: if full and key is new, route to atomic_facts
            if len(active_prefs) >= MAX_CORE_PREFERENCES_CEILING and k not in active_prefs:
                distilled_data.setdefault("atomic_facts", []).append({
                    "category": "general",
                    "key": k,
                    "value": v,
                })
                continue

            memory.update_user_preference(k, v)
            active_prefs[k] = v
            prefs_inscribed += 1

        # 2. Inscribe Knowledge Edges
        if not yielded_early:
            for e in distilled_data.get("knowledge_edges", []):
                # Adjustment 4: Per-item interruption check before embedding
                if is_user_active_fn and is_user_active_fn():
                    logger.info("User activity detected during knowledge edge inscription; yielding in <2ms.")
                    yielded_early = True
                    break

                memory.remember_edge(
                    subject=e["subject"],
                    predicate=e["predicate"],
                    object_val=e["object"],
                    is_functional=e["is_functional"],
                    confidence=e["confidence"],
                    decay_lambda=e["decay_lambda"],
                    source_context=f"distilled_from_interaction_up_to_{max_interaction_id}",
                )
                edges_inscribed += 1

        # 3. Inscribe Atomic Facts
        if not yielded_early:
            for f in distilled_data.get("atomic_facts", []):
                # Adjustment 4: Per-item interruption check before vector embedding
                if is_user_active_fn and is_user_active_fn():
                    logger.info("User activity detected during fact inscription; yielding in <2ms.")
                    yielded_early = True
                    break

                memory.remember_fact(
                    key=f["key"],
                    value=f["value"],
                    category=f["category"],
                )
                facts_inscribed += 1

        # Adjustment 4: Cursor Safety — Only update last_distilled_interaction_id if we completed without yielding
        if not yielded_early and max_interaction_id > 0:
            self.set_last_distilled_id(max_interaction_id)

        return {
            "status": "yielded" if yielded_early else "success",
            "preferences_inscribed": prefs_inscribed,
            "edges_inscribed": edges_inscribed,
            "facts_inscribed": facts_inscribed,
            "traits_extracted": prefs_inscribed + edges_inscribed + facts_inscribed,
            "last_interaction_id": max_interaction_id if not yielded_early else self.get_last_distilled_id(),
        }

    def distill_unprocessed_interactions(
        self,
        is_user_active_fn: Callable[[], bool] | None = None,
        max_turns: int = 25,
        extractor_fn: Callable[[str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Orchestrates harvest, secret scrubbing, trait distillation, and dual-crystallization."""
        # 1. Early user activity check
        if is_user_active_fn and is_user_active_fn():
            return {"status": "yielded_idle", "turns_processed": 0, "traits_extracted": 0}

        # 2. Fetch unprocessed interaction batch
        interactions, max_id, all_trivial = self.get_unprocessed_interactions(limit=max_turns)

        # 3. Handle zero new records
        if not interactions and not all_trivial:
            return {"status": "no_new_interactions", "turns_processed": 0, "traits_extracted": 0}

        # 4. Adjustment 4: If all turns in batch were trivial, advance cursor safely without LLM call
        if all_trivial:
            self.set_last_distilled_id(max_id)
            logger.info("All %d unprocessed interactions were trivial greetings; advanced cursor to ID %d.", max_turns, max_id)
            return {
                "status": "all_trivial",
                "turns_processed": max_turns,
                "traits_extracted": 0,
                "last_interaction_id": max_id,
            }

        # 5. Format transcript chunk
        transcript_text = self.format_transcript_block(interactions)

        # 6. Extract traits via LLM (or mock extractor)
        try:
            distilled = self.extract_traits_and_facts(
                transcript_text=transcript_text,
                is_user_active_fn=is_user_active_fn,
                extractor_fn=extractor_fn,
            )
        except Exception as ex:
            # Adjustment 4: Never advance cursor on an LLM exception
            logger.warning("Transcript distillation LLM error; cursor NOT advanced: %s", ex)
            return {"status": "extraction_error", "error": str(ex), "turns_processed": 0, "traits_extracted": 0}

        if distilled is None:
            # Interrupted / yielded
            return {"status": "yielded_during_extraction", "turns_processed": 0, "traits_extracted": 0}

        # 7. Inscribe into memory with per-item interruption yields
        commit_res = self.commit_distilled_knowledge(
            distilled_data=distilled,
            max_interaction_id=max_id,
            is_user_active_fn=is_user_active_fn,
        )
        commit_res["turns_processed"] = len(interactions)
        return commit_res
