"""Calendar, schedule management, and Stark executive morning briefing protocol."""
import datetime
import re
from typing import Any

from jarvis.core.config_loader import load_config
from jarvis.core.memory import MemoryManager
from jarvis.skills.base import SkillRegistry
from jarvis.skills.web_search import get_weather

_memory_instance = None


def _get_memory() -> MemoryManager:
    global _memory_instance
    if _memory_instance is None:
        cfg = load_config()
        _memory_instance = MemoryManager(cfg)
    return _memory_instance


@SkillRegistry.register(
    name="schedule_calendar_event",
    description="Schedules a meeting, appointment, or calendar event with title, date/time, and optional location."
)
def schedule_calendar_event(title: str, start_time: str, end_time: str = "", location: str = "") -> str:
    """Adds a calendar event to the schedule."""
    mem = _get_memory()
    clean_time = start_time.strip()
    for fmt in ("%H:%M", "%I:%M %p", "%I %p"):
        try:
            datetime.datetime.strptime(clean_time, fmt)
            clean_time = f"{datetime.date.today().isoformat()} {clean_time}"
            break
        except ValueError:
            pass

    return mem.add_calendar_event(title, clean_time, end_time or None, location or None)


@SkillRegistry.register(
    name="list_calendar_events",
    description="Lists all upcoming scheduled meetings, calendar appointments, classes, timetable entries, and agenda events. Always call this when Sir asks about his calendar, schedule, upcoming classes, or agenda."
)
def list_calendar_events(include_all: bool = False) -> str:
    """Returns scheduled events grouped chronologically."""
    mem = _get_memory()
    events = mem.get_calendar_events(include_cancelled=include_all)
    if not events:
        return "You have no scheduled meetings or appointments on your calendar, Sir."

    from collections import defaultdict
    grouped = defaultdict(list)
    ungrouped = []

    for e in events:
        st = e.get("start_time", "")
        date_match = re.match(r"^(\d{4}-\d{2}-\d{2})", st)
        if date_match:
            d_str = date_match.group(1)
            grouped[d_str].append(e)
        else:
            ungrouped.append(e)

    lines = []
    for d_str in sorted(grouped.keys()):
        try:
            dt = datetime.datetime.strptime(d_str, "%Y-%m-%d")
            day_header = dt.strftime("%A, %B %d, %Y")
        except Exception:
            day_header = d_str
        lines.append(f"• {day_header}:")
        for e in grouped[d_str]:
            time_part = e["start_time"].replace(d_str, "").strip()
            loc = f" at {e['location']}" if e.get("location") else ""
            end_val = e.get("end_time") or ""
            end_part = end_val.replace(d_str, "").strip() if end_val else ""
            end_str = f" to {end_part}" if end_part else ""
            lines.append(f"   - #{e['id']}: '{e['title']}' at {time_part or e['start_time']}{end_str}{loc}")

    if ungrouped:
        lines.append("• Other Appointments:")
        for e in ungrouped:
            loc = f" at {e['location']}" if e.get("location") else ""
            end = f" to {e['end_time']}" if e.get("end_time") else ""
            lines.append(f"   - #{e['id']}: '{e['title']}' scheduled for {e['start_time']}{end}{loc}")

    return "\n".join(lines)


@SkillRegistry.register(
    name="cancel_calendar_event",
    description="Cancels, removes, or dismisses a scheduled meeting or calendar event by its numerical ID or by matching title (e.g. 'Stark Board Meeting'). Call this when Sir asks to cancel or dismiss a meeting, or says they do not have that meeting."
)
def cancel_calendar_event(event_id: Any = None, title: str = "") -> str:
    """Cancels a scheduled event by ID or title."""
    mem = _get_memory()
    return mem.cancel_calendar_event(event_id=event_id, title=title)


@SkillRegistry.register(
    name="delete_calendar",
    description="Deletes or removes a saved timetable or calendar document by title or ID, or clears the active calendar schedule. Call this when Sir asks to delete a calendar or clear all calendar events."
)
def delete_calendar(calendar_name: str = "", calendar_id: Any = None, clear_events: bool = False) -> str:
    """Deletes a saved calendar document or clears the schedule."""
    mem = _get_memory()
    cals = mem.get_saved_calendars()
    target_id = None
    target_title = calendar_name.strip()

    if calendar_id is not None:
        try:
            target_id = int(calendar_id)
        except Exception:
            pass

    if target_id is None and target_title:
        for c in cals:
            if target_title.lower() in c["title"].lower():
                target_id = c["id"]
                target_title = c["title"]
                break

    if target_id is not None:
        mem.delete_saved_calendar(target_id)
        cleared_msg = ""
        if clear_events:
            n = mem.clear_calendar_events()
            cleared_msg = f" and cleared {n} associated event(s)"
        return f"Calendar '{target_title}' (#{target_id}) has been deleted{cleared_msg}, Sir."

    if clear_events or "clear" in target_title.lower():
        n = mem.clear_calendar_events()
        return f"All active calendar events ({n} events) have been cleared, Sir."

    return f"I could not locate a saved calendar matching '{calendar_name}', Sir."


@SkillRegistry.register(
    name="morning_protocol",
    description="Executes the full executive morning briefing protocol: date/time, weather report, today's schedule, pending tasks, and global headlines."
)
def morning_protocol(city: str = "") -> str:
    """Executes Jarvis executive morning briefing."""
    mem = _get_memory()
    now = datetime.datetime.now()
    greeting_time = now.strftime("%A, %B %d, %Y, %I:%M %p")

    # Determine city: check passed arg, or saved preference, or default London
    target_city = city.strip() if city else (mem.get_fact("city") or "London")

    # 1. Weather
    try:
        weather_info = get_weather(target_city)
    except Exception:
        weather_info = "Weather telemetry unavailable."

    # 2. Schedule
    events = mem.get_calendar_events()
    if events:
        schedule_summary = f"You have {len(events)} item(s) on your schedule today: " + "; ".join(
            [f"'{e['title']}' at {e['start_time']}" for e in events[:3]]
        )
    else:
        schedule_summary = "Your calendar is completely clear today, Sir."

    # 3. Tasks
    tasks = mem.get_pending_tasks()
    if tasks:
        task_summary = f"You have {len(tasks)} pending task(s), including: " + "; ".join(
            [t["description"] for t in tasks[:3]]
        )
    else:
        task_summary = "All pending action items are up to date."

    # 4. Email check (if configured)
    email_section = ""
    try:
        from jarvis.skills.email_manager import check_unread_emails
        email_info = check_unread_emails()
        if "Neither Gmail nor Outlook" not in email_info:
            email_section = f"\nInbox Telemetry: {email_info}"
    except Exception:
        pass

    briefing = (
        f"Good morning, Sir. Today is {greeting_time}.\n"
        f"Atmospheric conditions in {target_city}: {weather_info}\n"
        f"Agenda: {schedule_summary}\n"
        f"Action Items: {task_summary}"
        f"{email_section}\n"
        f"All core systems and telemetry remain at optimal efficiency. Standing by for your instructions."
    )
    return briefing
