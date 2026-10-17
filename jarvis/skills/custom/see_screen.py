"""Custom Real-Time Screen Vision Skill for Jarvis 2.0.

Provides visual perception of Sir's active desktop display, describing open
applications, code, browser tabs, or text using Gemini Multimodal Vision.
"""
from jarvis.skills.base import SkillRegistry
from jarvis.skills.screen_vision import inspect_screen


@SkillRegistry.register(
    name="see_screen",
    description="Visually perceives Sir's active workstation screen and accurately describes what is displayed in real-time."
)
def see_screen(focus: str = "Describe what is currently visible on the screen") -> str:
    """Visually inspects Sir's desktop display in real-time."""
    return inspect_screen(query=focus)
