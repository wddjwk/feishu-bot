import tempfile
import unittest
from pathlib import Path

from utils.file_delivery import extract_file_deliveries, is_image_file


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

    def _dirs(self, tmp: str):
        workspace = Path(tmp) / "workspace"
        work_dir = workspace / "workfolder" / "msg-1"
        work_dir.mkdir(parents=True)
        return workspace, work_dir

    def test_absolute_path_anywhere_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, work_dir = self._dirs(tmp)
            outside = Path(tmp) / "elsewhere" / "note.txt"
            outside.parent.mkdir(parents=True)
            outside.write_text("hi", encoding="utf-8")

            delivery = extract_file_deliveries(f"[[FEISHU_FILE:{outside}]]", work_dir, workspace)

            self.assertEqual(delivery.paths, [outside.resolve()])
            self.assertEqual(delivery.text, "")

    def test_relative_path_resolves_against_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, work_dir = self._dirs(tmp)
            report = work_dir / "report.pdf"
            report.write_text("x", encoding="utf-8")

            delivery = extract_file_deliveries(
                "[[FEISHU_FILE:workfolder/msg-1/report.pdf]]", work_dir, workspace
            )

            self.assertEqual(delivery.paths, [report.resolve()])

    def test_bare_filename_resolves_against_work_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, work_dir = self._dirs(tmp)
            report = work_dir / "report.pdf"
            report.write_text("x", encoding="utf-8")

            delivery = extract_file_deliveries("[[FEISHU_FILE:report.pdf]]", work_dir, workspace)

            self.assertEqual(delivery.paths, [report.resolve()])

    def test_missing_file_is_reported_as_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, work_dir = self._dirs(tmp)

            delivery = extract_file_deliveries("[[FEISHU_FILE:missing.pdf]]", work_dir, workspace)

            self.assertEqual(delivery.paths, [])
            self.assertEqual(len(delivery.errors), 1)


if __name__ == "__main__":
    unittest.main()
