"""Unit tests for Zero-VRAM Native Photo Archival Skill."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from PIL import Image

import jarvis.skills.photo_manager as pm
from jarvis.skills.photo_manager import (
    save_photo,
    save_clipboard_photo,
    list_photos,
    describe_photo,
)


class TestPhotoManager(unittest.TestCase):
    """Verifies image ingestion, metadata scrubbing, validation, and async SQLite indexing."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_photos_dir = Path(self.temp_dir.name) / "photos"
        self.temp_photos_dir.mkdir(parents=True, exist_ok=True)
        self.temp_db_path = Path(self.temp_dir.name) / "photos.db"

        self.orig_photos_dir = pm.PHOTOS_DIR
        self.orig_db_path = pm.DB_PATH
        pm.PHOTOS_DIR = self.temp_photos_dir
        pm.DB_PATH = self.temp_db_path

        self.test_img_path = Path(self.temp_dir.name) / "test_sample.png"
        img = Image.new("RGB", (120, 80), color="blue")
        img.save(str(self.test_img_path), format="PNG")

    def tearDown(self):
        pm._db_queue.join()
        pm.PHOTOS_DIR = self.orig_photos_dir
        pm.DB_PATH = self.orig_db_path
        import gc
        gc.collect()
        self.temp_dir.cleanup()

    def test_save_valid_photo(self):
        """Valid image must be verified, sanitized, and saved into photos dir."""
        res = save_photo(str(self.test_img_path))
        self.assertIn("Successfully archived photo", res)
        self.assertIn("EXIF metadata stripped", res)

    def test_reject_malformed_fake_image(self):
        """Fake payload disguised as image must be rejected by magic-byte verification."""
        fake_path = Path(self.temp_dir.name) / "fake.jpg"
        with open(fake_path, "wb") as f:
            f.write(b"NOT_A_REAL_IMAGE_FILE_PAYLOAD_DATA")

        res = save_photo(str(fake_path))
        self.assertIn("Failed to save photo", res)

    def test_list_photos_query(self):
        """list_photos must return archived photo records from SQLite."""
        # Ensure at least one photo is saved
        save_photo(str(self.test_img_path))
        pm._db_queue.join()  # Deterministic wait for async worker commit
        listing = list_photos(limit=5)
        self.assertIn("Archived Photo Gallery:", listing)

    def test_clipboard_empty_graceful(self):
        """save_clipboard_photo must handle empty/text clipboard gracefully without throwing."""
        res = save_clipboard_photo()
        self.assertIsInstance(res, str)

    def test_describe_nonexistent_photo(self):
        """describe_photo must cleanly handle missing photo ID."""
        res = describe_photo("nonexistent_id_99999")
        self.assertIn("was not found", res)


if __name__ == "__main__":
    unittest.main()
