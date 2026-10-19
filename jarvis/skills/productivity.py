"""Productivity, reminders, notes, and long-term memory tools for Jarvis."""
import datetime
from jarvis.skills.base import SkillRegistry
from jarvis.core.memory import MemoryManager
from jarvis.core.config_loader import load_config

_memory_instance: MemoryManager | None = None


def set_memory_manager(mgr: MemoryManager):
    """Sets the shared singleton MemoryManager instance."""
    global _memory_instance
    _memory_instance = mgr


def _get_memory() -> MemoryManager:
    global _memory_instance
    if _memory_instance is not None:
        import os
        db_path = getattr(_memory_instance, "db_path", None)
        if db_path and not os.path.exists(db_path):
            _memory_instance = None
    if _memory_instance is None:
        cfg = load_config()
        _memory_instance = MemoryManager(cfg)
    return _memory_instance


@SkillRegistry.register(
    name="get_current_time_and_date",
    description="Returns the exact current local date, day of the week, and time."
)
def get_current_time_and_date() -> str:
    """Returns formatted current timestamp."""
    now = datetime.datetime.now()
    return now.strftime("Today is %A, %B %d, %Y. The current time is %I:%M:%S %p.")


@SkillRegistry.register(
    name="add_task_or_reminder",
    description="Adds a new task, reminder, or to-do item to the persistent task list."
)
def add_task_or_reminder(description: str, due_date: str | None = None) -> str:
    """Stores a task in SQLite."""
    mem = _get_memory()
    return mem.add_task(description, due_date)


@SkillRegistry.register(
    name="list_pending_tasks",
    description="Retrieves all currently pending tasks and reminders."
)
def list_pending_tasks() -> str:
    """Lists incomplete tasks."""
    mem = _get_memory()
    tasks = mem.get_pending_tasks()
    if not tasks:
        return "You have no pending tasks, Sir."
    formatted = [f"#{t['id']}: {t['description']}" + (f" (Due: {t['due_date']})" if t['due_date'] else "") for t in tasks]
    return "\n".join(formatted)


@SkillRegistry.register(
    name="complete_task",
    description="Marks a task as completed by its numerical ID."
)
def complete_task(task_id: int) -> str:
    """Completes a task."""
    mem = _get_memory()
    return mem.complete_task(task_id)


@SkillRegistry.register(
    name="remember_fact",
    description="Stores a permanent personal fact, preference, or piece of knowledge about the user or projects in long-term memory."
)
def remember_fact(key: str, value: str, category: str = "general") -> str:
    """Stores a fact in SQLite."""
    mem = _get_memory()
    return mem.remember_fact(key, value, category)


@SkillRegistry.register(
    name="recall_fact",
    description="Recalls a stored fact, user preference, or personal knowledge by key or semantic topic description."
)
def recall_fact(key: str) -> str:
    """Recalls a stored fact using exact key match with semantic vector fallback."""
    mem = _get_memory()
    val = mem.get_fact(key)
    if val:
        return f"Stored fact for '{key}': {val}"

    # Hybrid FTS5 + Vector recall fallback via RRF
    matches = mem.hybrid_search_facts(key, top_k=3)
    if matches:
        formatted = []
        for m in matches:
            formatted.append(f"• [{m['category']}] {m['key']}: {m['value']}")
        return f"Hybrid memory recall for '{key}':\n" + "\n".join(formatted)

    return f"No stored fact or semantic match found for '{key}'."


@SkillRegistry.register(
    name="update_user_preference",
    description="Updates or sets a permanent personal preference, habit, or behavioral directive for Sir in Core Memory (e.g. 'preferred_browser: brave', 'voice_speed: 1.15', 'call_me: Boss', 'primary_project: Jarvis')."
)
def update_user_preference(key: str, value: str) -> str:
    """Updates a core user preference in SQLite Memory."""
    mem = _get_memory()
    if hasattr(mem, "update_user_preference"):
        return mem.update_user_preference(key, value)
    return mem.remember_fact(key, value, category="user_preference")


@SkillRegistry.register(
    name="get_user_preferences",
    description="Inspects all stored core user preferences, behavioral rules, and personal habits recorded in Sir's Core Memory profile."
)
def get_user_preferences() -> str:
    """Retrieves all core user preferences."""
    mem = _get_memory()
    if hasattr(mem, "get_user_preferences"):
        prefs = mem.get_user_preferences()
        if not prefs:
            return "No core user preferences are currently registered in your profile, Sir."
        lines = [f"• {k}: {v}" for k, v in sorted(prefs.items())]
        return "Sir's Active Core Preferences:\n" + "\n".join(lines)

    matches = mem.get_facts_by_category("user_preference") if hasattr(mem, "get_facts_by_category") else {}
    if matches:
        return "Sir's Core Preferences:\n" + "\n".join(f"• {k}: {v}" for k, v in matches.items())
    return "No core user preferences are currently registered in your profile, Sir."


@SkillRegistry.register(
    name="delete_user_preference",
    description="Deletes or purges a personal preference, habit, or behavioral directive from Sir's Core Memory profile by key."
)
def delete_user_preference(key: str) -> str:
    """Deletes a core user preference from SQLite Memory."""
    mem = _get_memory()
    if hasattr(mem, "delete_user_preference"):
        success = mem.delete_user_preference(key)
        if success:
            return f"Preference '{key}' has been purged from core memory, Sir."
        return f"No preference found matching '{key}' to purge, Sir."
    return "Memory manager does not support preference pruning, Sir."


@SkillRegistry.register(
    name="delete_stored_fact",
    description="Deletes or prunes a stored atomic fact from persistent memory by key or ID."
)
def delete_stored_fact(key_or_id: str) -> str:
    """Deletes a stored fact from SQLite Memory."""
    mem = _get_memory()
    if hasattr(mem, "delete_fact"):
        success = mem.delete_fact(key_or_id)
        if success:
            return f"Fact '{key_or_id}' has been purged from memory, Sir."
        return f"No fact found matching '{key_or_id}' to purge, Sir."
    return "Memory manager does not support fact pruning, Sir."


@SkillRegistry.register(
    name="recall_past_conversation",
    description="Searches previous conversation logs and dialogues with Sir by topic, question, or keyword. Use this whenever Sir asks what you previously discussed, past decisions, earlier commands, or past topics."
)
def recall_past_conversation(query: str, limit: int = 5) -> str:
    """Searches past conversation logs in SQLite memory."""
    mem = _get_memory()
    results = mem.search_interactions(query, limit=limit)
    if not results:
        # Fallback to semantic vector dialogue search
        results = mem.semantic_search_interactions(query, top_k=limit, min_score=0.10)
    if not results:
        return f"No past conversation logs found matching '{query}', Sir."

    formatted = []
    for r in results:
        ts = r.get("created_at", "Unknown time")
        u_msg = r.get("user_input", "")
        j_msg = r.get("jarvis_reply", "")
        score_str = f" [Match: {r['relevance_score']:.0%}]" if "relevance_score" in r else ""
        formatted.append(f"[{ts}]{score_str}\nSir: {u_msg}\nJarvis: {j_msg}")

    return "Past Dialogue Logs Found:\n\n" + "\n\n---\n\n".join(formatted)


@SkillRegistry.register(
    name="semantic_search_memory",
    description="Searches long-term factual memory, conversation archives, and ingested documents using semantic vector similarity."
)
def semantic_search_memory(query: str, search_type: str = "all") -> str:
    """Performs semantic vector search across facts, document vaults, and past conversations."""
    mem = _get_memory()
    outputs = []

    if search_type in ("all", "facts"):
        facts = mem.semantic_search_facts(query, top_k=3)
        if facts:
            f_lines = [f"• [{f['category']}] {f['key']}: {f['value']} (Score: {f['relevance_score']:.0%})" for f in facts]
            outputs.append("Matching Facts & Knowledge:\n" + "\n".join(f_lines))

    if search_type in ("all", "documents", "docs", "papers", "pdf"):
        docs = mem.hybrid_search_documents(query, limit=3)
        if docs:
            doc_block = mem.format_document_reference_block(docs)
            outputs.append("Matching Document Knowledge:\n" + doc_block)

    if search_type in ("all", "conversations", "dialogue"):
        dialogues = mem.semantic_search_interactions(query, top_k=3)
        if dialogues:
            d_lines = []
            for d in dialogues:
                d_lines.append(f"[{d.get('created_at', 'Past')}] (Relevance: {d['relevance_score']:.0%})\nSir: {d['user_input']}\nJarvis: {d['jarvis_reply']}")
            outputs.append("Matching Conversation Logs:\n" + "\n---\n".join(d_lines))

    if not outputs:
        return f"No semantic memories, documents, or dialogues found matching '{query}', Sir."
    return "\n\n".join(outputs)

