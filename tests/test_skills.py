"""Tests for native skills registry and implementations."""
import sys
import unittest.mock
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

    # Test weather tool (Open-Meteo API with deterministic mock fallback)
    with unittest.mock.patch("requests.get") as mock_get:
        mock_geo = unittest.mock.MagicMock()
        mock_geo.json.return_value = {
            "results": [{"latitude": 51.5074, "longitude": -0.1278, "name": "London", "country": "United Kingdom"}]
        }
        mock_weather = unittest.mock.MagicMock()
        mock_weather.json.return_value = {
            "current_weather": {"temperature": 15.2, "windspeed": 12.0}
        }
        mock_get.side_effect = [mock_geo, mock_weather]
        weather_res = SkillRegistry.execute("get_weather", city="London")
        print(f"Weather result: {weather_res}")
        assert "Weather in London" in weather_res

    print("All skills tests passed successfully!")


if __name__ == "__main__":
    test_skills_execution()
