import json
import tempfile
import unittest
from pathlib import Path

from services.messenger import FeishuApiError
from services.prompt_builder import (
    build_prompt,
    extract_files,
    extract_text,
    is_standalone_file_message,
    normalize_message,
)


class FakeConfig:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, dotted: str, default=None):
        values = {
            "prompt.group_thread_context_limit": 50,
        }
        return values.get(dotted, default)

    def resolve_path(self, dotted: str):
        if dotted != "prompt.work_root":
            raise AssertionError(dotted)
        return self.root / "workfolder"


class FakeMessenger:
    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}
        self.downloaded: list[dict] = []

    def get_message(self, message_id: str):
        return self.messages.get(message_id)

    def list_messages(self, *_args, **_kwargs):
        return []

    def download_message_resource(self, message_id: str, key: str, resource_type: str, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.suffix:
            dest = dest.with_suffix(".png" if resource_type == "image" else ".bin")
        dest.write_bytes(b"resource")
        self.downloaded.append({"message_id": message_id, "key": key, "type": resource_type, "path": dest})
        return dest


class PromptBuilderTests(unittest.TestCase):
    @staticmethod
    def _payload(result):
        return json.loads(result.prompt)

    def test_post_preserves_links_code_markdown_and_media_references(self):
        message = {
            "message_id": "m-post",
            "message_type": "post",
            "content": json.dumps(
                {
                    "zh_cn": {
                        "title": "标题",
                        "content_v2": [
                            [
                                {"tag": "md", "text": "**正文** [链接](https://example.test) ![图片](img-key)"},
                                {"tag": "a", "text": "文档", "href": "https://docs.example.test"},
                                {"tag": "at", "user_id": "ou_user", "user_name": "用户"},
                            ],
                            [{"tag": "code_block", "language": "PYTHON", "text": "print('ok')"}],
                            [{"tag": "media", "file_key": "file-key", "image_key": "img-key"}],
                        ],
                    }
                },
                ensure_ascii=False,
            ),
        }
        references = {"m-post": {"img-key": "@/tmp/image.png", "file-key": "@/tmp/video.mp4"}}

        text = extract_text(message, resource_references=references)

        self.assertIn("[链接](https://example.test)", text)
        self.assertIn("[文档](https://docs.example.test)", text)
        self.assertIn("@用户", text)
        self.assertIn("```python\nprint('ok')\n```", text)
        self.assertIn("@/tmp/image.png", text)
        self.assertIn("@/tmp/video.mp4", text)

    def test_text_only_prompt_does_not_create_workfolder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            messenger = FakeMessenger()
            config = FakeConfig(root)
            message = {
                "message_id": "m-text",
                "message_type": "text",
                "content": json.dumps({"text": "无需附件"}),
            }

            result = build_prompt(message, messenger, config)

            self.assertFalse((root / "workfolder" / "m-text").exists())

        self.assertEqual(self._payload(result)["workfolder"], "workfolder/m-text")

    def test_markdown_only_image_key_is_downloaded_and_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            messenger = FakeMessenger()
            config = FakeConfig(Path(tmp))
            message = {
                "message_id": "m-markdown",
                "message_type": "post",
                "content": json.dumps(
                    {
                        "zh_cn": {
                            "content_v2": [
                                [{"tag": "md", "text": "![截图](img_v3_markdown_only)"}],
                            ]
                        }
                    }
                ),
            }

            result = build_prompt(message, messenger, config)

        self.assertEqual(result.attached_files[0]["key"], "img_v3_markdown_only")
        self.assertIn("@", self._payload(result)["user_input"])

    def test_failed_attachment_download_hides_resource_key_from_prompt(self):
        class FailingMessenger(FakeMessenger):
            def download_message_resource(self, *_args, **_kwargs):
                raise FeishuApiError("download failed")

        with tempfile.TemporaryDirectory() as tmp:
            messenger = FailingMessenger()
            config = FakeConfig(Path(tmp))
            message = {
                "message_id": "m-failed-image",
                "message_type": "image",
                "content": json.dumps({"image_key": "img_server_resource"}),
            }

            result = build_prompt(message, messenger, config)

        self.assertIn("[附件下载失败]", result.prompt)
        self.assertNotIn("img_server_resource", result.prompt)

    def test_build_prompt_downloads_rich_resources_without_server_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            messenger = FakeMessenger()
            config = FakeConfig(Path(tmp))
            message = {
                "message_id": "m-rich",
                "message_type": "post",
                "content": json.dumps(
                    {
                        "zh_cn": {
                            "content": [
                                [{"tag": "img", "image_key": "img-key"}],
                                [{"tag": "media", "file_key": "file-key", "image_key": "cover-key"}],
                            ]
                        }
                    }
                ),
            }

            result = build_prompt(message, messenger, config)

        self.assertEqual({item["key"] for item in result.attached_files}, {"img-key", "file-key", "cover-key"})
        payload = self._payload(result)
        self.assertIn("@", payload["user_input"])
        self.assertEqual(set(payload), {"workfolder", "user_id", "user_input", "context"})
        self.assertEqual(payload["workfolder"], "workfolder/m-rich")

    def test_reply_to_file_downloads_parent_file_but_standalone_file_is_identified(self):
        with tempfile.TemporaryDirectory() as tmp:
            messenger = FakeMessenger()
            config = FakeConfig(Path(tmp))
            standalone = {
                "message_id": "m-file",
                "message_type": "file",
                "content": json.dumps({"file_key": "file-key", "file_name": "report.pdf"}),
            }
            messenger.messages["m-file"] = standalone
            reply = {
                "message_id": "m-reply",
                "message_type": "text",
                "parent_id": "m-file",
                "content": json.dumps({"text": "请分析这个文件"}),
            }

            result = build_prompt(reply, messenger, config)

        self.assertTrue(is_standalone_file_message(standalone))
        self.assertFalse(is_standalone_file_message(reply))
        self.assertEqual(result.attached_files[0]["name"], "resource_01_report.pdf")
        payload = self._payload(result)
        self.assertIn("@", payload["user_input"])
        self.assertTrue(payload["context"])
        self.assertEqual(payload["workfolder"], "workfolder/m-file")

    def test_extract_files_handles_direct_audio_and_video(self):
        audio = normalize_message(
            {
                "message_id": "m-audio",
                "message_type": "audio",
                "content": json.dumps({"file_key": "audio-key", "duration": 1000}),
            }
        )
        video = normalize_message(
            {
                "message_id": "m-video",
                "message_type": "media",
                "content": json.dumps({"file_key": "video-key", "image_key": "cover-key", "file_name": "demo.mp4"}),
            }
        )

        self.assertEqual(extract_files(audio), [{"key": "audio-key", "type": "file", "name": "audio"}])
        self.assertEqual(
            extract_files(video),
            [
                {"key": "cover-key", "type": "image", "name": "demo.mp4"},
                {"key": "video-key", "type": "file", "name": "demo.mp4"},
            ],
        )

    def test_group_thread_context_keeps_current_message_and_non_bot_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = FakeConfig(Path(tmp))

            class ThreadMessenger(FakeMessenger):
                def list_messages(self, *_args, **_kwargs):
                    self.list_kwargs = _kwargs
                    return [
                        {
                            "message_id": f"old-{index}",
                            "message_type": "text",
                            "content": json.dumps({"text": f"old {index}"}),
                            "create_time": str(index),
                            "sender": {"id": f"ou_old_{index}"},
                        }
                        for index in range(50)
                    ]

            messenger = ThreadMessenger()
            message = {
                "message_id": "current",
                "message_type": "image",
                "thread_id": "thread-1",
                "chat_type": "group",
                "sender_id": "ou_current",
                "content": json.dumps({"image_key": "current-image"}),
                "create_time": "999",
            }

            result = build_prompt(message, messenger, config)

        self.assertEqual(result.attached_files[-1]["key"], "current-image")
        payload = self._payload(result)
        self.assertIn("@", payload["user_input"])
        self.assertTrue(any("[ou_old_1]: old 1" in line for line in payload["context"]))
        self.assertNotIn("current-image", "\n".join(payload["context"]))
        self.assertEqual(messenger.list_kwargs["sort_type"], "ByCreateTimeDesc")

    def test_private_topic_keeps_replied_file_reference_without_repeating_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            messenger = FakeMessenger()
            config = FakeConfig(Path(tmp))
            messenger.messages["file-parent"] = {
                "message_id": "file-parent",
                "message_type": "file",
                "content": json.dumps({"file_key": "file-key", "file_name": "notes.txt"}),
                "create_time": "1",
                "sender": {"id": "ou_other"},
            }
            message = {
                "message_id": "topic-current",
                "message_type": "text",
                "thread_id": "thread-1",
                "chat_type": "p2p",
                "parent_id": "file-parent",
                "sender_id": "ou_user",
                "content": json.dumps({"text": "继续"}),
            }

            result = build_prompt(message, messenger, config)

        payload = self._payload(result)
        self.assertEqual(payload["user_id"], "ou_user")
        self.assertIn("@", payload["user_input"])
        self.assertEqual(payload["context"], [])
        self.assertEqual(payload["workfolder"], "workfolder/file-parent")

    def test_prompt_never_contains_server_message_ids_or_raw_unknown_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            messenger = FakeMessenger()
            config = FakeConfig(Path(tmp))
            message = {
                "message_id": "om_server_id",
                "message_type": "post",
                "sender_id": "ou_sender",
                "content": json.dumps(
                    {
                        "zh_cn": {
                            "content": [
                                [{"tag": "at", "user_id": "ou_hidden", "user_name": "张三"}],
                                [{"tag": "unsupported", "sensitive_id": "om_hidden"}],
                            ]
                        }
                    }
                ),
            }

            result = build_prompt(message, messenger, config)

        payload = self._payload(result)
        self.assertIn("@张三", payload["user_input"])
        self.assertIn("[飞书组件：unsupported]", payload["user_input"])
        self.assertEqual(payload["workfolder"], "workfolder/om_server_id")
        self.assertNotIn("om_hidden", result.prompt)
        self.assertNotIn("ou_hidden", result.prompt)

    def test_attachment_cache_reuses_opaque_path_without_message_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            messenger = FakeMessenger()
            config = FakeConfig(Path(tmp))
            message = {
                "message_id": "om_server_id",
                "message_type": "image",
                "content": json.dumps({"image_key": "img_key"}),
            }

            first = build_prompt(message, messenger, config)
            second = build_prompt(message, messenger, config)

        self.assertEqual(len(messenger.downloaded), 1)
        self.assertIn("@", self._payload(first)["user_input"])
        self.assertEqual(first.prompt, second.prompt)
        self.assertEqual(self._payload(first)["workfolder"], "workfolder/om_server_id")


if __name__ == "__main__":
    unittest.main()
