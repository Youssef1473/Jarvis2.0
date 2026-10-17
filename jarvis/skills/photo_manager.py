"""Re-export photo_manager from jarvis.core.skills.photo_manager."""
from jarvis.core.skills.photo_manager import (
    save_photo,
    save_clipboard_photo,
    list_photos,
    describe_photo,
)

__all__ = ["save_photo", "save_clipboard_photo", "list_photos", "describe_photo"]
