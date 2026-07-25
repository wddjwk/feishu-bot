import tempfile
import unittest
import zipfile
from pathlib import Path

from utils.data_transfer import (
    DataTransferError,
    create_export_zip,
    extract_zip,
    resolve_export_files,
    validate_zip_entries,
)


class DataTransferTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "feishu-server" / "data"
        self.memory_dir = self.root / "agent-workspace" / "memory"
        self.data_dir.mkdir(parents=True)
        self.memory_dir.mkdir(parents=True)
        (self.data_dir / "session_map.json").write_text('{"a":1}')
        (self.data_dir / "tasks.json").write_text("[]")
        (self.memory_dir / "SOUL.md").write_text("# soul")
        (self.memory_dir / "LONG_TERM.md").write_text("# long")
        self.dest_dir = self.root / "agent-workspace" / "workfolder"
        self.dest_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolve_directory(self):
        files = resolve_export_files(["feishu-server/data"], self.root)
        names = {f.name for f in files}
        self.assertEqual(names, {"session_map.json", "tasks.json"})

    def test_resolve_single_file(self):
        files = resolve_export_files(["agent-workspace/memory/SOUL.md"], self.root)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "SOUL.md")

    def test_resolve_glob(self):
        files = resolve_export_files(["agent-workspace/memory/*.md"], self.root)
        names = {f.name for f in files}
        self.assertEqual(names, {"SOUL.md", "LONG_TERM.md"})

    def test_resolve_glob_no_match_raises(self):
        with self.assertRaises(DataTransferError):
            resolve_export_files(["nonexistent/*.xyz"], self.root)

    def test_resolve_missing_path_is_skipped(self):
        files = resolve_export_files(["nonexistent/dir"], self.root)
        self.assertEqual(files, [])

    def test_resolve_deduplicates(self):
        files = resolve_export_files(
            ["agent-workspace/memory", "agent-workspace/memory/SOUL.md"], self.root
        )
        names = [f.name for f in files]
        self.assertEqual(names.count("SOUL.md"), 1)

    def test_create_export_zip_entries_relative_to_root(self):
        zip_path = create_export_zip(
            ["feishu-server/data", "agent-workspace/memory"], self.root, self.dest_dir
        )
        self.assertTrue(zip_path.name.startswith("feishubot-data-"))
        self.assertTrue(zip_path.name.endswith(".zip"))
        with zipfile.ZipFile(zip_path) as zf:
            names = sorted(zf.namelist())
        self.assertEqual(names, [
            "agent-workspace/memory/LONG_TERM.md",
            "agent-workspace/memory/SOUL.md",
            "feishu-server/data/session_map.json",
            "feishu-server/data/tasks.json",
        ])

    def test_create_backup_zip_has_bak_prefix(self):
        zip_path = create_export_zip(["feishu-server/data"], self.root, self.dest_dir, prefix="bak-")
        self.assertTrue(zip_path.name.startswith("bak-feishubot-data-"))

    def test_validate_accepts_valid(self):
        zip_path = create_export_zip(["feishu-server/data"], self.root, self.dest_dir)
        validate_zip_entries(zip_path, ["feishu-server/data", "agent-workspace/memory"])

    def test_validate_accepts_glob_match(self):
        zip_path = create_export_zip(["agent-workspace/memory/*.md"], self.root, self.dest_dir)
        validate_zip_entries(zip_path, ["agent-workspace/memory/*.md"])

    def test_validate_rejects_traversal(self):
        bad_zip = self.dest_dir / "bad.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("../etc/passwd", "root:x:0:0")
        with self.assertRaises(DataTransferError):
            validate_zip_entries(bad_zip, ["feishu-server/data"])

    def test_validate_rejects_absolute(self):
        bad_zip = self.dest_dir / "bad.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("/etc/passwd", "root:x:0:0")
        with self.assertRaises(DataTransferError):
            validate_zip_entries(bad_zip, ["feishu-server/data"])

    def test_validate_rejects_outside_allowed(self):
        bad_zip = self.dest_dir / "bad.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("other-dir/file.txt", "hello")
        with self.assertRaises(DataTransferError):
            validate_zip_entries(bad_zip, ["feishu-server/data"])

    def test_extract_zip_replaces_files(self):
        import_zip = self.dest_dir / "import.zip"
        with zipfile.ZipFile(import_zip, "w") as zf:
            zf.writestr("feishu-server/data/session_map.json", '{"b":2}')
            zf.writestr("agent-workspace/memory/SOUL.md", "# new soul")
        count = extract_zip(import_zip, self.root)
        self.assertEqual(count, 2)
        self.assertEqual((self.data_dir / "session_map.json").read_text(), '{"b":2}')
        self.assertEqual((self.memory_dir / "SOUL.md").read_text(), "# new soul")


if __name__ == "__main__":
    unittest.main()
