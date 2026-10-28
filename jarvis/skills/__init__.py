"""Jarvis 2.0 Skill Modules and Central Registry.

Exports all core runtime skills and Track B frontier skills via module-level __getattr__,
ensuring zero side-effects and zero eager imports during sandbox verification.
"""
import importlib
from typing import Any

from jarvis.skills.base import SkillRegistry

_TRACK_B_MODULES = {
    "rstrtmgr_lock_surgeon",
    "job_object_sandbox",
    "nvml_dxgi_vram_arbiter",
    "usn_journal_delta_radar",
    "reflex_macro_compiler",
    "ast_code_surgeon",
    "modern_web",
    "gemini_intelligence",
    "chrome_devtools",
}

__all__ = [
    "SkillRegistry",
    "apply_and_verify_surgical_patch",
    "ast_code_surgeon",
    "diagnose_test_failure_ast",
    "job_object_sandbox",
    "nvml_dxgi_vram_arbiter",
    "reflex_macro_compiler",
    "rstrtmgr_lock_surgeon",
    "usn_journal_delta_radar",
]


def __getattr__(name: str) -> Any:
    if name in _TRACK_B_MODULES:
        return importlib.import_module(f"jarvis.skills.{name}")
    if name in ("diagnose_test_failure_ast", "apply_and_verify_surgical_patch"):
        mod = importlib.import_module("jarvis.skills.ast_code_surgeon")
        return getattr(mod, name)
    raise AttributeError(f"module 'jarvis.skills' has no attribute '{name}'")
