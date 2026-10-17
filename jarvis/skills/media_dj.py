"""Voice-controlled media streaming and YouTube playback for Jarvis 2.0."""
import re
import urllib.parse
import webbrowser
import logging
from jarvis.skills.base import SkillRegistry
from jarvis.skills.os_control import launch_application

logger = logging.getLogger("Jarvis.MediaDJ")


@SkillRegistry.register(
    name="play_youtube_media",
    description="Searches and plays music, songs, videos, or soundtracks directly on YouTube in Sir's browser."
)
def play_youtube_media(query: str) -> str:
    """Opens direct YouTube playback for the requested media."""
    clean = re.sub(r"[;&|<>$`\"]", "", query).strip()
    # Strip common leading filler phrases
    clean = re.sub(r'^(?:play|search for|put on|stream)\s+', '', clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r'\s+on\s+youtube$', '', clean, flags=re.IGNORECASE).strip()

    if not clean:
        return "Please specify a song, soundtrack, or video to play, Sir."

    encoded = urllib.parse.quote_plus(clean)
    url = f"https://www.youtube.com/results?search_query={encoded}"

    try:
        webbrowser.open_new_tab(url)
        return f"Streaming '{clean}' on YouTube, Sir."
    except Exception as e:
        logger.error(f"Failed to launch YouTube media: {e}")
        return f"Could not stream media on YouTube: {e}"
