from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


FILE_MARKER = re.compile(r"(?m)^[ \t]*\[\[FEISHU_FILE:(?P<path>[^\]\r\n]+)\]\][ \t]*$")
IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class FileDelivery:
    text: str
    paths: list[Path]
    errors: list[str]


def extract_file_deliveries(result: str, workspace: Path) -> FileDelivery:
    workspace = workspace.resolve()
    paths: list[Path] = []
    errors: list[str] = []
    seen: set[Path] = set()
    for match in FILE_MARKER.finditer(result):
        raw_path = match.group("path").strip()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            errors.append(f"文件标记必须使用绝对路径：{raw_path}")
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError:
            errors.append(f"文件不在 agent-workspace 内：{raw_path}")
            continue
        if not resolved.is_file():
            errors.append(f"文件不存在：{raw_path}")
            continue
        if resolved not in seen:
            seen.add(resolved)
            paths.append(resolved)
    text = FILE_MARKER.sub("", result).strip()
    return FileDelivery(text=text, paths=paths, errors=errors)


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES and path.stat().st_size <= MAX_IMAGE_BYTES
