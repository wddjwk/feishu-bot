import tempfile
import unittest
from pathlib import Path

from utils.file_delivery import is_image_file


class FileDeliveryTests(unittest.TestCase):
    def test_only_supported_small_images_use_image_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            png = root / "image.png"
            png.write_bytes(b"small")
            svg = root / "image.svg"
            svg.write_text("<svg/>", encoding="utf-8")
            large = root / "large.png"
            large.write_bytes(b"x" * (5 * 1024 * 1024 + 1))

            self.assertTrue(is_image_file(png))
            self.assertFalse(is_image_file(svg))
            self.assertFalse(is_image_file(large))


if __name__ == "__main__":
    unittest.main()
