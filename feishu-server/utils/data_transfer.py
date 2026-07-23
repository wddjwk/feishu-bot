from __future__ import annotations

import fnmatch
import zipfile
from datetime import datetime
from pathlib import Path

import logger

TIMESTAMP_FMT = "%Y%m%d-%H%M%S"
_GLOB_MAGIC = frozenset("*?[")


class DataTransferError(RuntimeError):
    pass


def _is_glob(pattern: str) -> bool:
    return bool(_GLOB_MAGIC & set(pattern))


def resolve_export_files(config_paths: list[str], project_root: Path) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for raw in config_paths:
        if _is_glob(raw):
            matched = sorted(project_root.glob(raw))
            if not matched:
                raise DataTransferError(f"导出路径无匹配：{raw}")
            for p in matched:
                if p.is_file():
                    resolved = p.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        files.append(resolved)
                elif p.is_dir():
                    for f in sorted(p.rglob("*")):
                        if f.is_file():
                            resolved = f.resolve()
                            if resolved not in seen:
                                seen.add(resolved)
                                files.append(resolved)
        else:
            path = (project_root / raw).resolve()
            if not path.exists():
                raise DataTransferError(f"导出路径不存在：{raw}")
            if path.is_file():
                if path not in seen:
                    seen.add(path)
                    files.append(path)
            else:
                for f in sorted(path.rglob("*")):
                    if f.is_file():
                        resolved = f.resolve()
                        if resolved not in seen:
                            seen.add(resolved)
                            files.append(resolved)
    return files


def create_export_zip(
    config_paths: list[str],
    project_root: Path,
    dest_dir: Path,
    *,
    prefix: str = "",
) -> Path:
    files = resolve_export_files(config_paths, project_root)
    timestamp = datetime.now().strftime(TIMESTAMP_FMT)
    zip_name = f"{prefix}feishubot-data-{timestamp}.zip"
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            arcname = f.relative_to(project_root).as_posix()
            zf.write(f, arcname)
    logger.info("已创建导出压缩包：%s 文件数=%s", zip_path.name, len(files))
    return zip_path


def _entry_allowed(name: str, config_paths: list[str]) -> bool:
    for raw in config_paths:
        if _is_glob(raw):
            if fnmatch.fnmatch(name, raw):
                return True
            if fnmatch.fnmatch(name, raw.rstrip("/") + "/*"):
                return True
        elif name == raw:
            return True
        elif name.startswith(raw.rstrip("/") + "/"):
            return True
    return False


def validate_zip_entries(zip_path: Path, config_paths: list[str]) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            if name.startswith("/"):
                raise DataTransferError(f"压缩包包含绝对路径：{name}")
            if ".." in Path(name).parts:
                raise DataTransferError(f"压缩包包含路径穿越：{name}")
            if not _entry_allowed(name, config_paths):
                raise DataTransferError(f"压缩包包含不允许的路径：{name}")


def extract_zip(zip_path: Path, project_root: Path) -> int:
    count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.filename.endswith("/"):
                continue
            target = project_root / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            count += 1
    logger.info("已解压导入压缩包：%s 文件数=%s", zip_path.name, count)
    return count
