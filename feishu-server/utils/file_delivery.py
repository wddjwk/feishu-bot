from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import logger

FILE_MARKER = re.compile(r"(?m)^[ \t]*\[\[FEISHU_FILE:(?P<path>[^\]\r\n]+)\]\][ \t]*$")
IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class FileDelivery:
    text: str
    paths: list[Path]
    errors: list[str]


def extract_file_deliveries(result: str, work_dir: Path, workspace: Path) -> FileDelivery:
    work_dir = work_dir.resolve()
    workspace = workspace.resolve()
    paths: list[Path] = []
    errors: list[str] = []
    seen: set[Path] = set()
    for match in FILE_MARKER.finditer(result):
        raw_path = match.group("path").strip()
        candidate = Path(raw_path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = None
            for base in (work_dir, workspace):
                option = (base / candidate).resolve()
                if option.is_file():
                    resolved = option
                    break
        if resolved is None or not resolved.is_file():
            errors.append(f"文件不存在：{raw_path}")
            logger.warning("文件交付标记无效（文件不存在）：%s", raw_path)
            continue
        if resolved not in seen:
            seen.add(resolved)
            paths.append(resolved)
    if paths:
        logger.info("提取到文件交付标记：数量=%s 路径=%s", len(paths), [str(p) for p in paths])
    text = FILE_MARKER.sub("", result).strip()
    return FileDelivery(text=text, paths=paths, errors=errors)


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES and path.stat().st_size <= MAX_IMAGE_BYTES
