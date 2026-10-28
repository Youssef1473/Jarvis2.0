"""Spoken Note-Taker & Thought Capture skill for Jarvis 2.0."""
import datetime
import logging
import re
import threading
from pathlib import Path

from jarvis.skills.base import SkillRegistry

logger = logging.getLogger("Jarvis.Notes")

_notes_lock = threading.Lock()
NOTES_DIR = Path("data/notes")
NOTES_FILE = NOTES_DIR / "notes.md"


def _ensure_notes_storage():
    """Ensures notes directory and markdown file exist."""
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    if not NOTES_FILE.exists():
        NOTES_FILE.write_text("# Jarvis 2.0 Notes Archive\n\n", encoding="utf-8")


def _sanitize_note_input(text: str) -> str:
    """Strips speech command prefixes to store clean note substance."""
    cleaned = text.strip()
    prefixes = [
        r"^take\s+(?:a\s+)?note\s*(?:that|to|:)?\s*",
        r"^note\s+down\s*(?:that|to|:)?\s*",
        r"^write\s+down\s*(?:that|to|:)?\s*",
        r"^jot\s+down\s*(?:that|to|:)?\s*",
        r"^save\s+(?:a\s+)?note\s*(?:that|to|:)?\s*",
        r"^record\s+(?:a\s+)?note\s*(?:that|to|:)?\s*",
    ]
    for p in prefixes:
        cleaned = re.sub(p, "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned if cleaned else text.strip()


@SkillRegistry.register(
    name="take_quick_note",
    description="Captures and saves a spoken note or thought from Sir to persistent notes storage (e.g. 'take a note that we need to deploy the update')."
)
def take_quick_note(content: str, category: str = "General") -> str:
    """Appends a timestamped note entry to the persistent Markdown notes file."""
    if not content or not content.strip():
        return "Note content cannot be empty, Sir."

    clean_content = _sanitize_note_input(content)
    clean_cat = re.sub(r"[^a-zA-Z0-9_\-\s]", "", category).strip() or "General"
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    entry = f"### {now_str} | {clean_cat}\n- {clean_content}\n\n"

    with _notes_lock:
        _ensure_notes_storage()
        with open(NOTES_FILE, "a", encoding="utf-8") as f:
            f.write(entry)

    preview = clean_content[:80] + ("..." if len(clean_content) > 80 else "")
    logger.info(f"Captured note under '{clean_cat}': {preview}")
    return f"Noted, Sir: '{preview}' has been saved to your notes."


@SkillRegistry.register(
    name="read_recent_notes",
    description="Reads Sir's most recently captured notes back to them (e.g. 'read my notes', 'what are my recent notes')."
)
def read_recent_notes(count: int = 5) -> str:
    """Reads back the last N captured notes."""
    with _notes_lock:
        _ensure_notes_storage()
        try:
            raw = NOTES_FILE.read_text(encoding="utf-8")
        except Exception as e:
            return f"Unable to read notes archive: {e}"

    # Parse entries by ### header
    blocks = [b.strip() for b in raw.split("### ") if b.strip() and not b.startswith("# Jarvis")]
    if not blocks:
        return "You have no saved notes in your archive, Sir."

    recent = blocks[-max(1, count):]
    formatted = []
    for idx, block in enumerate(reversed(recent), start=1):
        lines = block.splitlines()
        header = lines[0].strip() if lines else "Note"
        body = " ".join([l.lstrip("-•* ").strip() for l in lines[1:] if l.strip()])
        formatted.append(f"{idx}. [{header}]: {body}")

    return f"Your Recent Notes, Sir ({len(recent)}):\n" + "\n".join(formatted)


@SkillRegistry.register(
    name="search_notes",
    description="Searches saved notes for specific keywords or topics (e.g. 'search notes for deployment')."
)
def search_notes(keyword: str) -> str:
    """Searches stored notes for keyword matches."""
    if not keyword or not keyword.strip():
        return "Please specify a keyword to search, Sir."

    kw = keyword.lower().strip()
    with _notes_lock:
        _ensure_notes_storage()
        try:
            raw = NOTES_FILE.read_text(encoding="utf-8")
        except Exception as e:
            return f"Unable to read notes archive: {e}"

    blocks = [b.strip() for b in raw.split("### ") if b.strip() and not b.startswith("# Jarvis")]
    matches = []
    for block in blocks:
        if kw in block.lower():
            lines = block.splitlines()
            header = lines[0].strip()
            body = " ".join([l.lstrip("-•* ").strip() for l in lines[1:] if l.strip()])
            matches.append(f"• [{header}]: {body}")

    if not matches:
        return f"No notes found matching '{keyword}', Sir."

    return f"Found {len(matches)} note(s) matching '{keyword}', Sir:\n" + "\n".join(matches[:5])
