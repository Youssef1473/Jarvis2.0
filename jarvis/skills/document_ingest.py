"""Universal Context-Gated Document & PDF Ingestion Engine for Jarvis 2.0.
Supports multi-domain intake (calendar events, tasks, research papers, contracts, invoices).
Files remain staged until Sir provides conversational context and instructions.
"""
import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Dict, Any, List, Optional

from jarvis.skills.base import SkillRegistry
from jarvis.core.memory import MemoryManager
from jarvis.core.config_loader import load_config

logger = logging.getLogger("Jarvis.DocumentIngest")

_memory_instance: Optional[MemoryManager] = None


def _get_memory() -> MemoryManager:
    global _memory_instance
    if _memory_instance is None:
        cfg = load_config()
        _memory_instance = MemoryManager(cfg)
    return _memory_instance


def _extract_page_content(page) -> str:
    """Extracts page text preserving 2D spatial column alignments for timetables and schedules."""
    snippets = []
    def visitor(text, cm, tm, fontDict, fontSize):
        t = text.strip()
        if t:
            snippets.append((tm[4], tm[5], t))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
        snippets = []

    day_map = {
        "lundi": "Monday",
        "mardi": "Tuesday",
        "mercredi": "Wednesday",
        "jeudi": "Thursday",
        "vendredi": "Friday",
        "samedi": "Saturday",
        "dimanche": "Sunday",
        "monday": "Monday",
        "tuesday": "Tuesday",
        "wednesday": "Wednesday",
        "thursday": "Thursday",
        "friday": "Friday",
        "saturday": "Saturday",
        "sunday": "Sunday",
    }

    # Detect day column headers
    day_columns = []
    for x, y, text in snippets:
        lower = text.lower()
        if lower in day_map:
            day_columns.append({"day": day_map[lower], "raw_day": text, "x": x, "y": y, "date": ""})

    if len(day_columns) >= 2:
        # Sort columns left to right by x coordinate
        day_columns.sort(key=lambda d: d["x"])

        # Match dates near headers (e.g. 12/10, 12/10/2026)
        date_pattern = re.compile(r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?$")
        for x, y, text in snippets:
            if date_pattern.match(text):
                closest = min(day_columns, key=lambda c: abs(c["x"] - x))
                if abs(closest["x"] - x) < 70 and abs(closest["y"] - y) < 80:
                    closest["date"] = text

        col_width = (day_columns[-1]["x"] - day_columns[0]["x"]) / (len(day_columns) - 1)
        min_header_y = min(c["y"] for c in day_columns)

        # Content in the table grid is below the day header row and above footer legend
        grid_items = [it for it in snippets if 80 <= it[1] <= min_header_y - 15]

        # Bucket items by column
        col_buckets = {c["day"]: [] for c in day_columns}
        for x, y, text in grid_items:
            if any(text == c["raw_day"] or text == c["date"] for c in day_columns):
                continue
            lower_text = text.lower()
            if any(k in lower_text for k in ["emploi du temps", "page 1", "légende", "legende", "année", "annee"]):
                continue
            if re.match(r"^\d{1,2}h$", text):
                continue

            closest = min(day_columns, key=lambda c: abs(c["x"] - x))
            dist = abs(closest["x"] - x)
            if dist < col_width * 0.55:
                col_buckets[closest["day"]].append((x, y, text))

        lines = ["--- Timetable Grid Extracted By Day Column ---"]
        for col in day_columns:
            day_name = col["day"]
            date_str = f" ({col['date']})" if col["date"] else ""
            items = col_buckets[day_name]
            items.sort(key=lambda it: -it[1])
            lines.append(f"\n### Day: {day_name}{date_str}")
            if not items:
                lines.append("(No classes or events scheduled)")
            else:
                lines.append(" | ".join([it[2] for it in items]))

        return "\n".join(lines)

    # Fallback to layout mode
    try:
        layout_txt = page.extract_text(extraction_mode="layout")
        if layout_txt and len(layout_txt.strip()) > 30:
            return layout_txt.strip()
    except Exception:
        pass

    return page.extract_text() or ""


def extract_text_from_pdf(file_path: str, max_pages: int = 50) -> str:
    """Extracts text from a PDF file using pypdf with fallback to Gemini Multimodal."""
    p = Path(file_path)
    if not p.is_file():
        raise FileNotFoundError(f"Document file not found: {file_path}")

    extracted_text = ""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(p))
        pages_to_read = min(len(reader.pages), max_pages)
        page_texts = []
        for i in range(pages_to_read):
            t = _extract_page_content(reader.pages[i])
            if t.strip():
                page_texts.append(f"--- Page {i + 1} ---\n{t.strip()}")
        extracted_text = "\n\n".join(page_texts).strip()
    except Exception as e:
        logger.warning(f"pypdf extraction failed for {file_path}: {e}")

    # If extracted text is minimal (e.g. scanned document or image-only PDF), attempt Gemini Multimodal OCR
    if len(extracted_text) < 40:
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        if gemini_key:
            try:
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=gemini_key)
                pdf_bytes = p.read_bytes()
                cfg = load_config()
                gem_model = cfg.get("llm", {}).get("model", "gemini-3.8-flash")
                response = client.models.generate_content(
                    model=gem_model,
                    contents=[
                        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                        "Extract all readable text, tables, dates, and information from this document verbatim."
                    ]
                )
                if response.text and len(response.text.strip()) > len(extracted_text):
                    extracted_text = response.text.strip()
                    logger.info(f"Successfully extracted text via Gemini Multimodal OCR for {p.name}")
            except Exception as ocr_err:
                logger.debug(f"Gemini OCR fallback notice: {ocr_err}")

    return extracted_text


@SkillRegistry.register(
    name="list_staged_documents",
    description="Lists all staged PDF or document files that are currently awaiting Sir's context and instructions."
)
def list_staged_documents() -> str:
    """Lists staged documents pending user context."""
    mem = _get_memory()
    staged = mem.get_staged_documents(status="PENDING_CONTEXT")
    if not staged:
        return "There are currently no staged documents awaiting review, Sir."

    lines = ["Staged Documents Awaiting Your Context, Sir:"]
    for doc in staged:
        size_kb = round(doc.get("file_size", 0) / 1024, 1)
        lines.append(f"• #{doc['id']}: '{doc['filename']}' ({size_kb} KB) — Staged at {doc.get('staged_at', 'recently')}")
    return "\n".join(lines)


@SkillRegistry.register(
    name="process_staged_document",
    description="Processes a staged PDF/document using Sir's conversational briefing and instructions. Extracts and registers calendar events, tasks, or knowledge documents into the database."
)
def process_staged_document(identifier: str = "", user_context: str = "") -> str:
    """Processes a staged document guided by Sir's spoken context."""
    mem = _get_memory()
    
    # 1. Resolve target staged document
    target_doc = None
    clean_id = identifier.strip() if identifier else ""
    if clean_id.isdigit():
        target_doc = mem.get_staged_document(doc_id=int(clean_id))
    elif clean_id:
        target_doc = mem.get_staged_document(filename=clean_id)
    
    if not target_doc:
        # Fall back to the most recently staged pending document
        target_doc = mem.get_staged_document()

    if not target_doc:
        return "I could not locate any staged documents awaiting your context, Sir. Please import a document first."

    file_path = target_doc.get("file_path", "")
    filename = target_doc.get("filename", "document.pdf")
    doc_id = target_doc.get("id")

    if not os.path.exists(file_path):
        return f"The physical file for '{filename}' could not be found at '{file_path}', Sir."

    # 2. Extract Document Content
    try:
        raw_text = extract_text_from_pdf(file_path)
    except Exception as e:
        return f"Unable to extract text from '{filename}', Sir: {str(e)}"

    if not raw_text.strip():
        return f"Document '{filename}' appears to be empty or unreadable, Sir."

    # Truncate text if excessively long for LLM context window (keep first 15,000 chars)
    truncated_text = raw_text[:15000]

    # 3. LLM Entity & Context Extraction
    today_str = datetime.date.today().isoformat()
    system_prompt = (
        f"You are the document intelligence extraction engine for JARVIS.\n"
        f"Today's date is: {today_str}.\n"
        f"Sir has provided a document along with his verbal briefing and instructions.\n"
        f"Carefully analyze Sir's instructions and the document content.\n"
        f"Extract all relevant entities as requested by Sir:\n"
        f"- If Sir wants calendar events/dates scheduled (or if the document is a syllabus/schedule/itinerary/timetable): "
        f"extract calendar events with 'title', 'start_time' (normalized with date e.g. 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'), 'end_time', 'location'.\n"
        f"- CRITICAL FOR TIMETABLES / SCHEDULES: Process EVERY day of the week (Monday through Sunday). "
        f"If a day has classes (such as Friday), extract them with their exact date (e.g. 2026-10-16). "
        f"Do NOT stop after Monday-Wednesday; inspect the full week including Thursday, Friday, Saturday.\n"
        f"If a day has no classes scheduled, do NOT generate events for that day.\n"
        f"- CRITICAL FOR KNOWLEDGE DISTILLATION (TIER 2 ATOMIC FACTS): "
        f"Extract key entity assertions, parameters, deadlines, contact persons, server/port configurations, "
        f"and core definitions into 'atomic_facts'. Each atomic fact MUST be a single, high-density key/value pair with a category.\n"
        f"- Always create a knowledge summary of the document for permanent archiving: 'title', 'category' (schedule|syllabus|invoice|contract|paper|manual|note|general), 'summary'.\n\n"
        f"OUTPUT FORMAT:\n"
        f"Output ONLY valid JSON with this exact schema (no markdown fences, no explanation):\n"
        f"{{\n"
        f'  "calendar_events": [\n'
        f'    {{"title": "...", "start_time": "YYYY-MM-DD HH:MM", "end_time": "...", "location": "..."}}\n'
        f'  ],\n'
        f'  "tasks": [\n'
        f'    {{"description": "...", "due_date": "YYYY-MM-DD"}}\n'
        f'  ],\n'
        f'  "atomic_facts": [\n'
        f'    {{"category": "project|credentials|spec|contacts|policy|finance|general", "key": "...", "value": "..."}}\n'
        f'  ],\n'
        f'  "knowledge_doc": {{\n'
        f'    "title": "...",\n'
        f'    "category": "...",\n'
        f'    "summary": "..."\n'
        f'  }}\n'
        f"}}"
    )

    user_prompt = (
        f"Sir's Verbal Context & Instructions: \"{user_context or 'Process and archive this document according to its contents.'}\"\n\n"
        f"Filename: {filename}\n\n"
        f"<untrusted_document_payload filename=\"{filename}\">\n"
        f"{truncated_text}\n"
        f"</untrusted_document_payload>"
    )

    extracted_data = {}
    cfg = load_config()
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

    # 1. Attempt extraction via Groq LPU (Tier 2 fast intellect / test mock target)
    if groq_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1", max_retries=0, timeout=15.0)
            res = client.chat.completions.create(
                model=cfg.get("llm", {}).get("groq_model", "qwen/qwen3.8-27b"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1,
                max_tokens=2500,
            )
            content = res.choices[0].message.content.strip()
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            extracted_data = json.loads(content)
        except Exception as e:
            logger.debug(f"Groq document extraction notice: {e}")

    # 2. Attempt extraction via Antigravity Bridge (Google AI Pro Fleet / zero cost)
    if not extracted_data:
        try:
            from jarvis.core.antigravity_bridge import AntigravityBridge
            ag_bridge = AntigravityBridge()
            if ag_bridge.is_available():
                res = ag_bridge.generate_response(
                    prompt=f"{system_prompt}\n\n{user_prompt}",
                    model="gemini-3.8-flash-medium",
                    system_instruction="You are a document intelligence extraction engine for Jarvis. Output ONLY valid JSON matching the exact requested schema.",
                    timeout=45.0,
                )
                raw_reply = (res.get("response") or "").strip()
                if raw_reply:
                    clean_json = re.sub(r"^```(?:json)?\s*", "", raw_reply)
                    clean_json = re.sub(r"\s*```$", "", clean_json)
                    m = re.search(r"(\{.*\})", clean_json, re.DOTALL)
                    if m:
                        extracted_data = json.loads(m.group(1))
        except Exception as e:
            logger.debug(f"Antigravity document extraction notice: {e}")

    # 3. Fallback to direct Gemini API with candidate model cascade
    if not extracted_data and gemini_key:
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=gemini_key)
            candidates = [
                cfg.get("llm", {}).get("model", "gemini-3.8-flash"),
                "gemini-2.5-flash",
                "gemini-2.5-pro",
            ]
            for gem_model in candidates:
                try:
                    res = client.models.generate_content(
                        model=gem_model,
                        contents=f"{system_prompt}\n\n{user_prompt}",
                        config=types.GenerateContentConfig(temperature=0.1, response_mime_type="application/json")
                    )
                    if res.text:
                        extracted_data = json.loads(res.text.strip())
                        if extracted_data:
                            break
                except Exception as inner_err:
                    logger.debug(f"Gemini candidate '{gem_model}' notice: {inner_err}")
        except Exception as e:
            logger.debug(f"Gemini document extraction notice: {e}")

    # Fallback to Local Ollama (Tier 1 offline intellect)
    if not extracted_data:
        try:
            from openai import OpenAI
            ollama_base = cfg.get("llm", {}).get("ollama_base_url", "http://127.0.0.1:11434/v1")
            ollama_model = cfg.get("llm", {}).get("ollama_model", "qwen3.5:2b-q4_K_M")
            client = OpenAI(api_key="ollama", base_url=ollama_base, max_retries=0, timeout=60.0)
            res = client.chat.completions.create(
                model=ollama_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1,
                max_tokens=2500,
            )
            content = res.choices[0].message.content.strip()
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            extracted_data = json.loads(content)
        except Exception as e:
            logger.debug(f"Ollama document extraction notice: {e}")

    # If extraction returned nothing, build fallback knowledge archive
    if not extracted_data:
        extracted_data = {
            "calendar_events": [],
            "tasks": [],
            "atomic_facts": [
                {
                    "category": "document",
                    "key": f"{filename.lower().replace('.', '_')}_summary",
                    "value": f"Archived {filename} with context: {user_context or 'None'}"
                }
            ],
            "knowledge_doc": {
                "title": filename.replace(".pdf", "").replace("_", " ").title(),
                "category": "general",
                "summary": f"Archived document: {filename} with user context: {user_context}."
            }
        }

    # 4. Commit Extracted Data to SQLite
    calendar_events = extracted_data.get("calendar_events", [])
    tasks = extracted_data.get("tasks", [])
    atomic_facts = extracted_data.get("atomic_facts", [])
    knowledge_doc = extracted_data.get("knowledge_doc", {})

    scheduled_count = 0
    for evt in calendar_events:
        title = evt.get("title", "").strip()
        start = evt.get("start_time", "").strip()
        end = evt.get("end_time", "")
        loc = evt.get("location", "")
        if title and start:
            # Normalize start time if only clock time is given
            for fmt in ("%H:%M", "%I:%M %p", "%I %p"):
                try:
                    datetime.datetime.strptime(start, fmt)
                    start = f"{today_str} {start}"
                    break
                except ValueError:
                    pass
            mem.add_calendar_event(title, start, end or None, loc or None)
            scheduled_count += 1

    tasks_count = 0
    for t in tasks:
        desc = t.get("description", "").strip()
        due = t.get("due_date", "")
        if desc:
            mem.add_task(desc, due or None)
            tasks_count += 1

    facts_count = 0
    for fact in atomic_facts:
        k = str(fact.get("key", "")).strip()
        v = str(fact.get("value", "")).strip()
        cat = str(fact.get("category", "document")).strip() or "document"
        if k and v:
            mem.remember_fact(k, v, category=cat)
            facts_count += 1

    doc_title = knowledge_doc.get("title", filename)
    doc_cat = knowledge_doc.get("category", "general")
    doc_summary = knowledge_doc.get("summary", "")
    mem.save_document_knowledge(
        title=doc_title,
        category=doc_cat,
        summary=doc_summary,
        raw_content=truncated_text[:2000],
        file_path=file_path
    )

    # 5. Mark Document as Processed
    mem.mark_document_processed(doc_id, user_context=user_context)

    # 6. Synthesize Spoken Summary
    actions = []
    if scheduled_count > 0:
        actions.append(f"{scheduled_count} calendar event{'s' if scheduled_count > 1 else ''}")
    if tasks_count > 0:
        actions.append(f"{tasks_count} action item{'s' if tasks_count > 1 else ''}")
    if facts_count > 0:
        actions.append(f"{facts_count} atomic fact{'s' if facts_count > 1 else ''}")
    actions.append(f"archived '{doc_title}' under {doc_cat}")

    action_summary = ", ".join(actions)
    return (
        f"Processed '{filename}' according to your instructions, Sir: "
        f"successfully registered {action_summary} in your database."
    )


@SkillRegistry.register(
    name="search_archived_documents",
    description="Searches archived documents, PDFs, manuals, and papers using hybrid BM25 and vector search with chunk sandwiching."
)
def search_archived_documents(query: str, limit: int = 3) -> str:
    """Searches archived document library with chunk sandwiching to eliminate lost-in-the-middle degradation."""
    mem = _get_memory()
    clean_q = str(query or "").strip()
    if not clean_q:
        return "Please specify a search query or topic to locate archived documents, Sir."

    results = mem.hybrid_search_documents(clean_q, limit=limit)
    if not results:
        # Fallback to standard text search
        results = mem.search_documents(clean_q, limit=limit)

    if not results:
        return f"No archived documents found matching '{clean_q}', Sir."

    return mem.format_document_reference_block(results)


@SkillRegistry.register(
    name="read_archived_document",
    description="Reads the detailed content or summary of a specific archived document by numeric ID or title match."
)
def read_archived_document(identifier: str) -> str:
    """Retrieves an archived document by numeric ID or title search."""
    mem = _get_memory()
    clean_id = str(identifier or "").strip()
    if not clean_id:
        return "Please provide a document ID or title to read, Sir."

    doc = None
    if clean_id.isdigit():
        doc = mem.get_document_by_id(int(clean_id))
    else:
        results = mem.search_documents(clean_id, limit=1)
        if results:
            doc = mem.get_document_by_id(results[0]["id"])

    if not doc:
        return f"Could not locate archived document '{clean_id}', Sir."

    return mem.format_document_reference_block([doc])
