"""Test suite for browser agent and calendar manager skills."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.skills.base import SkillRegistry
import jarvis.skills.calendar_manager
import jarvis.skills.browser_agent


def test_new_skills():
    tools = SkillRegistry.get_all_tools()
    print(f"Total registered tools count: {len(tools)}")

    # Verify tool presence
    assert "browse_and_read_webpage" in tools
    assert "get_tech_news_headlines" in tools
    assert "schedule_calendar_event" in tools
    assert "morning_protocol" in tools

    # 1. Test Calendar
    print("\n1. Testing Calendar...")
    sched_res = SkillRegistry.execute(
        "schedule_calendar_event",
        title="Stark Board Meeting",
        start_time="2:00 PM",
        location="Conference Room A"
    )
    print(f"Scheduled: {sched_res}")
    assert "Stark Board Meeting" in sched_res

    list_res = SkillRegistry.execute("list_calendar_events")
    print(f"Calendar events:\n{list_res}")
    assert "Stark Board Meeting" in list_res

    # Clean up test event
    cancel_res = SkillRegistry.execute("cancel_calendar_event", title="Stark Board Meeting")
    print(f"Cleanup: {cancel_res}")
    assert "Cancelled" in cancel_res

    # 2. Test Morning Protocol
    print("\n2. Testing Morning Protocol...")
    briefing = SkillRegistry.execute("morning_protocol", city="London")
    print(f"Morning Briefing excerpt:\n{briefing[:200]}...")
    assert "Good morning, Sir" in briefing

    # 3. Test Tech News Headlines
    print("\n3. Testing Tech News Headlines...")
    news = SkillRegistry.execute("get_tech_news_headlines", count=3)
    print(f"Headlines:\n{news}")
    assert "Top Trending Tech Headlines" in news

    # 4. Test Autonomous Webpage Reader
    print("\n4. Testing Autonomous Webpage Browsing...")
    web_res = SkillRegistry.execute("browse_and_read_webpage", url="https://example.com")
    print(f"Webpage content excerpt:\n{web_res[:200]}...")
    assert "Example Domain" in web_res

    print("\nAll Option 3 & Option 4 skill tests passed successfully!")


if __name__ == "__main__":
    test_new_skills()
