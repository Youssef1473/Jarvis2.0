"""Cinematic Daily & Morning Briefing protocol for Jarvis 2.0."""
import re
import logging
import concurrent.futures
from jarvis.skills.base import SkillRegistry
from jarvis.skills.productivity import get_current_time_and_date
from jarvis.skills.web_search import get_weather
from jarvis.skills.browser_agent import get_tech_news_headlines
from jarvis.skills.os_control import get_system_vitals

logger = logging.getLogger("Jarvis.Briefing")


@SkillRegistry.register(
    name="get_daily_briefing",
    description="Provides a complete, cinematic morning or daily briefing including time, weather, top news, and workstation vitals."
)
def get_daily_briefing() -> str:
    """Aggregates time, weather, headlines, and system vitals concurrently into a polished executive briefing."""
    results = {}

    def _fetch_time():
        return get_current_time_and_date()

    def _fetch_weather():
        return get_weather("London")

    def _fetch_vitals():
        return get_system_vitals()

    def _fetch_news():
        return get_tech_news_headlines(count=2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        f_time = executor.submit(_fetch_time)
        f_weather = executor.submit(_fetch_weather)
        f_vitals = executor.submit(_fetch_vitals)
        f_news = executor.submit(_fetch_news)

        for name, future in [("time", f_time), ("weather", f_weather), ("vitals", f_vitals), ("news", f_news)]:
            try:
                results[name] = future.result(timeout=2.5)
            except Exception as e:
                logger.debug(f"Briefing fetch error for {name}: {e}")
                results[name] = None

    sections = []

    # 1. Greeting & Time
    time_str = results.get("time")
    if time_str:
        # Extract friendly clause
        clean_time = time_str.replace("Today is ", "").strip()
        sections.append(f"Good day, Sir. {clean_time}.")
    else:
        sections.append("Good day, Sir.")

    # 2. Weather
    w_str = results.get("weather")
    if w_str:
        clean_w = re.sub(r"^Weather in\s+", "", w_str).strip()
        sections.append(f"Current conditions: {clean_w}.")

    # 3. Workstation Hardware Status
    v_str = results.get("vitals")
    if v_str:
        # Extract CPU and RAM lines
        v_lines = [l.strip().lstrip("• ") for l in v_str.splitlines() if "CPU" in l or "Memory" in l or "Power" in l]
        if v_lines:
            sections.append(f"Workstation vitals: {', '.join(v_lines)}.")

    # 4. Top Headlines
    n_str = results.get("news")
    if n_str and "No news" not in n_str:
        n_lines = [l.strip().lstrip("1234567890. -•") for l in n_str.splitlines() if l.strip() and not l.startswith("Top")]
        if n_lines:
            sections.append(f"Top headline: {n_lines[0]}.")

    sections.append("All primary systems are nominal. How may I assist you today, Sir?")
    return " ".join(sections)
