import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.lark_cli_wrapper import LarkCliError, LarkCliWrapper


class LarkCliWrapperTests(unittest.TestCase):
    def _wrapper(self, root: Path) -> LarkCliWrapper:
        (root / ".env").write_text("FEISHU_APP_ID=cli_test\nFEISHU_APP_SECRET=test-secret\n", encoding="utf-8")
        workspace = root / "workspace"
        workspace.mkdir()
        return LarkCliWrapper(root, workspace)

    @staticmethod
    def _ready_status(app_id: str = "cli_test") -> str:
        return json.dumps(
            {
                "appId": app_id,
                "identities": {"bot": {"status": "ready", "verified": True}},
            }
        )

    def test_uses_existing_matching_bot_identity_without_reconfiguring(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = self._wrapper(Path(tmp))
            with patch.object(wrapper, "_run", return_value=self._ready_status()) as run:
                wrapper.ensure_bot_identity()

        run.assert_called_once_with(["auth", "status", "--json", "--verify"], cwd=wrapper.project_root)

    def test_reconfigures_bot_from_env_when_cli_profile_mismatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = self._wrapper(Path(tmp))
            with patch.object(
                wrapper,
                "_run",
                side_effect=[self._ready_status("cli_other"), "", self._ready_status()],
            ) as run:
                wrapper.ensure_bot_identity()

        init_call = run.call_args_list[1]
        self.assertIn("--app-secret-stdin", init_call.args[0])
        self.assertIn("--name", init_call.args[0])
        self.assertIn(wrapper.profile, init_call.args[0])
        self.assertEqual(init_call.kwargs["stdin"], "test-secret\n")
        self.assertFalse(init_call.kwargs["use_profile"])

    def test_download_uses_relative_output_under_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrapper = self._wrapper(root)
            destination = root / "workspace" / "download" / "m1" / "image"

            def run_json(args, *, cwd):
                output = args[args.index("--output") + 1]
                path = cwd / output.removeprefix("./")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"image")
                return {"ok": True, "data": {"local_path": output}}

            with patch.object(wrapper, "ensure_bot_identity"), patch.object(wrapper, "_run_json", side_effect=run_json):
                path = wrapper.download_message_resource("m1", "img-key", "image", destination)

        self.assertEqual(path.name, "image")
        self.assertIn("download", str(path))

    def test_rejects_outside_workspace_media_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrapper = self._wrapper(root)
            outside = root / "outside.txt"
            outside.write_text("no", encoding="utf-8")

            with self.assertRaises(LarkCliError):
                wrapper.reply_file("om_xxx", outside)


if __name__ == "__main__":
    unittest.main()
