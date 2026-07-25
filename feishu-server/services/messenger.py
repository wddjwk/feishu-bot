from __future__ import annotations

import json
import mimetypes
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from config import Config
from utils.lark_cli_wrapper import LarkCliError, LarkCliWrapper
import logger


class FeishuApiError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class TenantTokenProvider:
    def __init__(self, app_id: str, app_secret: str, base_url: str) -> None:
        if not app_id or not app_secret:
            raise FeishuApiError("missing FEISHU_APP_ID or FEISHU_APP_SECRET")
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self._token = ""
        self._expires_at = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires_at:
            return self._token
        url = f"{self.base_url}/auth/v3/tenant_access_token/internal"
        payload = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8")
        request = Request(url, data=payload, method="POST", headers={"Content-Type": "application/json; charset=utf-8"})
        data = _read_json_response(request)
        code = data.get("code", -1)
        if code != 0:
            logger.error("获取 tenant_access_token 失败：code=%s msg=%s", code, data.get("msg"))
            raise FeishuApiError(f"failed to get tenant_access_token: {data.get('msg', data)}", code=code)
        self._token = data["tenant_access_token"]
        self._expires_at = time.time() + max(int(data.get("expire", 7200)) - 60, 60)
        logger.debug("已刷新 tenant_access_token：有效期=%ss", data.get("expire"))
        return self._token


class Messenger:
    def __init__(
        self,
        config: Config,
        token_provider: TenantTokenProvider,
        *,
        lark_cli: LarkCliWrapper | None = None,
    ) -> None:
        self.config = config
        self.token_provider = token_provider
        self.base_url = str(config.get("server.base_url")).rstrip("/")
        self.lark_cli = lark_cli or LarkCliWrapper.from_config(config)
        self._bot_open_id = ""
        self._bot_open_id_lock = threading.Lock()

    def reply_card(self, message_id: str, card: dict[str, Any], *, reply_in_thread: bool = False) -> dict[str, Any]:
        return self.reply_message(message_id, "interactive", card, reply_in_thread=reply_in_thread)

    def reply_text(self, message_id: str, text: str, *, reply_in_thread: bool = False) -> dict[str, Any]:
        return self.reply_message(message_id, "text", {"text": text}, reply_in_thread=reply_in_thread)

    def reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: dict[str, Any],
        *,
        reply_in_thread: bool = False,
    ) -> dict[str, Any]:
        body = {
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
            "reply_in_thread": reply_in_thread,
            "uuid": str(uuid.uuid4()),
        }
        return self._request_json("POST", f"/im/v1/messages/{quote(message_id)}/reply", body=body).get("data", {})

    def send_card(self, receive_id: str, receive_id_type: str, card: dict[str, Any]) -> dict[str, Any]:
        return self.send_message(receive_id, receive_id_type, "interactive", card)

    def send_text(self, receive_id: str, receive_id_type: str, text: str) -> dict[str, Any]:
        return self.send_message(receive_id, receive_id_type, "text", {"text": text})

    def send_message(self, receive_id: str, receive_id_type: str, msg_type: str, content: dict[str, Any]) -> dict[str, Any]:
        body = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
            "uuid": str(uuid.uuid4()),
        }
        query = {"receive_id_type": receive_id_type}
        return self._request_json("POST", "/im/v1/messages", query=query, body=body).get("data", {})

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        data = self._request_json(
            "GET",
            f"/im/v1/messages/{quote(message_id)}",
            query={"user_id_type": "open_id", "card_msg_content_type": "user_card_content"},
        ).get("data", {})
        items = data.get("items") or []
        return items[0] if items else None

    def list_messages(
        self,
        container_id_type: str,
        container_id: str,
        *,
        limit: int = 50,
        sort_type: str = "ByCreateTimeAsc",
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        while len(items) < limit:
            query = {
                "container_id_type": container_id_type,
                "container_id": container_id,
                "sort_type": sort_type,
                "page_size": min(50, limit - len(items)),
                "card_msg_content_type": "user_card_content",
            }
            if page_token:
                query["page_token"] = page_token
            data = self._request_json("GET", "/im/v1/messages", query=query).get("data", {})
            items.extend(data.get("items") or [])
            if not data.get("has_more") or not data.get("page_token"):
                break
            page_token = data["page_token"]
        return items

    def get_bot_open_id(self) -> str:
        with self._bot_open_id_lock:
            if self._bot_open_id:
                return self._bot_open_id
            response = self._request_json("GET", "/bot/v3/info")
            bot = response.get("bot")
            if not isinstance(bot, dict):
                data = response.get("data")
                bot = data.get("bot") if isinstance(data, dict) else None
            open_id = bot.get("open_id") if isinstance(bot, dict) else None
            if not isinstance(open_id, str) or not open_id:
                raise FeishuApiError("bot info response did not include an open_id")
            self._bot_open_id = open_id
            return open_id

    def add_reaction(self, message_id: str, emoji_type: str) -> str | None:
        data = self._request_json(
            "POST",
            f"/im/v1/messages/{quote(message_id)}/reactions",
            body={"reaction_type": {"emoji_type": emoji_type}},
        ).get("data", {})
        return data.get("reaction_id")

    def delete_reaction(self, message_id: str, reaction_id: str) -> None:
        self._request_json("DELETE", f"/im/v1/messages/{quote(message_id)}/reactions/{quote(reaction_id)}")

    def download_message_resource(self, message_id: str, file_key: str, resource_type: str, dest: Path) -> Path:
        try:
            return self.lark_cli.download_message_resource(message_id, file_key, resource_type, dest)
        except LarkCliError as exc:
            logger.warning(
                "lark-cli 下载消息资源失败，回退飞书 API：消息=%s 资源=%s 类型=%s 错误=%s",
                message_id,
                file_key,
                resource_type,
                exc,
            )
        return self._download_message_resource_via_api(message_id, file_key, resource_type, dest)

    def reply_file(self, message_id: str, path: Path, *, reply_in_thread: bool) -> dict[str, Any]:
        return self.lark_cli.reply_file(message_id, path, reply_in_thread=reply_in_thread)

    def reply_image(self, message_id: str, path: Path, *, reply_in_thread: bool) -> dict[str, Any]:
        return self.lark_cli.reply_image(message_id, path, reply_in_thread=reply_in_thread)

    def send_file(self, chat_id: str, path: Path) -> dict[str, Any]:
        return self.lark_cli.send_file(chat_id, path)

    def send_image(self, chat_id: str, path: Path) -> dict[str, Any]:
        return self.lark_cli.send_image(chat_id, path)

    def _download_message_resource_via_api(self, message_id: str, file_key: str, resource_type: str, dest: Path) -> Path:
        query = urlencode({"type": resource_type})
        url = f"{self.base_url}/im/v1/messages/{quote(message_id)}/resources/{quote(file_key)}?{query}"
        request = Request(url, method="GET", headers={"Authorization": f"Bearer {self.token_provider.token()}"})
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urlopen(request, timeout=60) as response:
                content_type = response.headers.get("Content-Type", "")
                suffix = mimetypes.guess_extension(content_type.partition(";")[0].strip()) or ""
                if suffix and not dest.suffix:
                    dest = dest.with_suffix(suffix)
                dest.write_bytes(response.read())
                return dest
        except HTTPError as exc:
            raise FeishuApiError(f"resource download failed: HTTP {exc.code}", status=exc.code) from exc
        except URLError as exc:
            raise FeishuApiError(f"resource download failed: {exc}") from exc

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query_string = f"?{urlencode(query)}" if query else ""
        url = f"{self.base_url}{path}{query_string}"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token_provider.token()}"}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(url, data=payload, method=method, headers=headers)
        data = _read_json_response(request)
        code = data.get("code", 0)
        if code != 0:
            logger.error("飞书 API 错误：方法=%s 路径=%s code=%s msg=%s", method, path, code, data.get("msg"))
            raise FeishuApiError(str(data.get("msg", data)), code=code)
        return data


def _read_json_response(request: Request) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"msg": raw}
        raise FeishuApiError(f"HTTP {exc.code}: {data.get('msg', raw)}", code=data.get("code"), status=exc.code) from exc
    except URLError as exc:
        raise FeishuApiError(f"request failed: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeishuApiError(f"invalid JSON response: {raw[:200]}") from exc
