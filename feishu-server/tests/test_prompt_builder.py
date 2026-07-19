import json
import tempfile
import unittest
from pathlib import Path

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
            "prompt.reply_chain_limit": 20,
            "prompt.thread_message_limit": 50,
        }
        return values.get(dotted, default)

    def resolve_path(self, dotted: str):
        if dotted != "prompt.download_dir":
            raise AssertionError(dotted)
        return self.root / "download"


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
        self.assertIn("<at user_id=\"ou_user\">用户</at>", text)
        self.assertIn("```python\nprint('ok')\n```", text)
        self.assertIn("@/tmp/image.png", text)
        self.assertIn("@/tmp/video.mp4", text)

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
        self.assertIn("@", result.prompt["user_input"])

    def test_build_prompt_downloads_rich_resources_and_keeps_raw_content(self):
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
        self.assertIn("@", result.prompt["user_input"])
        raw = result.prompt["context"]["source_messages"][0]["content"]
        self.assertEqual(raw["zh_cn"]["content"][0][0]["image_key"], "img-key")

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
        self.assertEqual(result.attached_files[0]["name"], "01_report.pdf")
        self.assertIn("@", "\n".join(result.prompt["context"]["conversation_history"]))

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

    def test_thread_context_keeps_current_message_when_history_is_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = FakeConfig(Path(tmp))

            class ThreadMessenger(FakeMessenger):
                def list_messages(self, *_args, **_kwargs):
                    return [
                        {
                            "message_id": f"old-{index}",
                            "message_type": "text",
                            "content": json.dumps({"text": f"old {index}"}),
                            "create_time": str(index),
                        }
                        for index in range(50)
                    ]

            messenger = ThreadMessenger()
            message = {
                "message_id": "current",
                "message_type": "image",
                "thread_id": "thread-1",
                "content": json.dumps({"image_key": "current-image"}),
                "create_time": "999",
            }

            result = build_prompt(message, messenger, config)

        self.assertEqual(result.prompt["context"]["source_messages"][-1]["message_id"], "current")
        self.assertEqual(result.attached_files[-1]["key"], "current-image")
        self.assertIn("@", result.prompt["user_input"])


if __name__ == "__main__":
    unittest.main()
