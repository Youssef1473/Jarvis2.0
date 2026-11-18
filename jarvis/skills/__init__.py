"""Jarvis 2.0 Skill Modules and Central Registry.

Exports all core runtime skills and Track B frontier skills via module-level __getattr__,
ensuring zero side-effects and zero eager imports during sandbox verification.
"""
import importlib
from typing import Any

from jarvis.skills.base import SkillRegistry

_TRACK_B_MODULES = {
    "job_object_sandbox",
    "nvml_dxgi_vram_arbiter",
    "modern_web",
    "gemini_intelligence",
}

__all__ = [
    "SkillRegistry",
    "job_object_sandbox",
    "nvml_dxgi_vram_arbiter",
]


def __getattr__(name: str) -> Any:
    if name in _TRACK_B_MODULES:
        return importlib.import_module(f"jarvis.skills.{name}")
    raise AttributeError(f"module 'jarvis.skills' has no attribute '{name}'")
