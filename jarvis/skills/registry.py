"""Re-exports for SkillRegistry and speculative metadata types."""
from jarvis.skills.base import (
    ResourceScope,
    SkillMetadata,
    SkillRegistry,
    SpeculationForbiddenError,
)

__all__ = [
    "ResourceScope",
    "SkillMetadata",
    "SkillRegistry",
    "SpeculationForbiddenError",
]
