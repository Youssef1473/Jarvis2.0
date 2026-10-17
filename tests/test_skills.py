"""Tests for native skills registry and implementations."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.skills.base import SkillRegistry
import jarvis.skills.os_control
import jarvis.skills.web_search
import jarvis.skills.dev_shell
import jarvis.skills.productivity


def test_skills_execution():
    tools = SkillRegistry.get_all_tools()
    print(f"Registered tools count: {len(tools)}")
    assert "get_current_time_and_date" in tools
    assert "web_search" in tools
    assert "get_weather" in tools
    assert "execute_powershell" in tools
    assert "list_directory" in tools
    assert "read_file" in tools

    # Test time tool
    time_res = SkillRegistry.execute("get_current_time_and_date")
    print(f"Time result: {time_res}")
    assert "Today is" in time_res

    # Test directory listing
    dir_res = SkillRegistry.execute("list_directory", folder_path=".")
    print(f"Dir result excerpt:\n{dir_res[:100]}...")
    assert "run.py" in dir_res

    # Test weather tool (Open-Meteo free API)
    weather_res = SkillRegistry.execute("get_weather", city="London")
    print(f"Weather result: {weather_res}")
    assert "Weather in London" in weather_res

    print("All skills tests passed successfully!")


if __name__ == "__main__":
    test_skills_execution()
