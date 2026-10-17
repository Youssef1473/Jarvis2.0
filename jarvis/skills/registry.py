"""Re-exports for SkillRegistry and speculative metadata types."""
from jarvis.skills.base import (
    SkillRegistry,
    SkillMetadata,
    SpeculationForbiddenError,
    ResourceScope,
)

__all__ = [
    "SkillRegistry",
    "SkillMetadata",
    "SpeculationForbiddenError",
    "ResourceScope",
]
