import warnings
import requests
warnings.filterwarnings("ignore", category=RuntimeWarning)
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS
from jarvis.skills.base import SkillRegistry


@SkillRegistry.register(
    name="web_search",
    description="Performs a real-time internet search using DuckDuckGo to find current information, news, or answers."
)
def web_search(query: str, max_results: int = 4) -> str:
    """Searches the web via DuckDuckGo."""
    try:
        results = []
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, max_results=max_results))
            if not raw_results:
                raw_results = list(ddgs.text(query, backend="html", max_results=max_results))
            for r in raw_results:
                title = r.get("title", "")
                body = r.get("body", "")
                link = r.get("href", "")
                results.append(f"Title: {title}\nSnippet: {body}\nURL: {link}")
        if not results:
            return f"No results found for query: '{query}'."
        return "\n\n".join(results)
    except Exception as e:
        return f"Web search error: {e}"


@SkillRegistry.register(
    name="get_weather",
    description="Retrieves current weather and temperature for any city worldwide using Open-Meteo (free, no API key)."
)
def get_weather(city: str) -> str:
    """Gets real-time weather from Open-Meteo."""
    try:
        # Step 1: Geocode city name to lat/lon
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json"
        geo_res = requests.get(geo_url, timeout=5).json()
        if not geo_res.get("results"):
            return f"Could not find coordinates for city: {city}."

        loc = geo_res["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        city_name = loc.get("name", city)
        country = loc.get("country", "")

        # Step 2: Fetch current weather
        weather_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}&current_weather=true&temperature_unit=celsius"
        )
        weather_res = requests.get(weather_url, timeout=5).json()
        current = weather_res.get("current_weather", {})
        temp = current.get("temperature", "N/A")
        wind = current.get("windspeed", "N/A")

        return f"Weather in {city_name}, {country}: {temp}°C, Wind Speed: {wind} km/h."
    except Exception as e:
        return f"Weather lookup failed: {e}"


@SkillRegistry.register(
    name="get_wikipedia_summary",
    description="Fetches an informative encyclopedic summary of a concept, person, or historical event from Wikipedia."
)
def get_wikipedia_summary(topic: str) -> str:
    """Fetches summary from Wikipedia API."""
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{topic.replace(' ', '_')}"
        res = requests.get(url, headers={"User-Agent": "JarvisAssistant/2.0"}, timeout=5)
        if res.status_code == 200:
            data = res.json()
            return data.get("extract", "No extract found.")
        return f"Could not find Wikipedia entry for '{topic}'."
    except Exception as e:
        return f"Wikipedia lookup error: {e}"
