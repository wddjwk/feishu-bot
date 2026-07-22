from __future__ import annotations

import inspect
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class _SmartFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        message = record.getMessage()
        if record.exc_info and not message.endswith("\n"):
            message += "\n"
        if record.exc_info:
            message += self.formatException(record.exc_info)
        source = "Lark" if record.name == "Lark" else "server"
        module = getattr(record, "_module", "")
        module_part = f"[{module}]" if module else ""
        return f"[{record.levelname}][{timestamp}][{source}]{module_part} {message}"


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


_file_handler: _PrivateRotatingFileHandler | None = None
_logger: logging.Logger = logging.getLogger("feishu_server")
_logger.setLevel(logging.INFO)
_logger.addHandler(logging.NullHandler())
_logger.propagate = False
_level: int = logging.INFO


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    max_bytes: int = 100 * 1024 * 1024,
    backup_count: int = 5,
) -> Path:
    global _file_handler, _logger, _level

    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_dir.chmod(0o700)

    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_file = log_dir / f"feishu_server_{timestamp}.log"
    _level = getattr(logging, level.upper(), logging.INFO)
    formatter = _SmartFormatter()

    _file_handler = _PrivateRotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    _file_handler.setFormatter(formatter)

    _logger = logging.getLogger("feishu_server")
    _logger.setLevel(_level)
    _logger.propagate = False
    for h in _logger.handlers[:]:
        _logger.removeHandler(h)
    _logger.addHandler(_file_handler)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    _logger.addHandler(stdout_handler)

    _configure_lark()
    _logger.info(
        "日志已初始化：文件=%s 单文件上限=%s 字节 保留备份=%s",
        log_file,
        max_bytes,
        backup_count,
        extra={"_module": "main"},
    )
    return log_file


def _configure_lark() -> None:
    lark_logger = logging.getLogger("Lark")
    lark_logger.setLevel(_level)
    lark_logger.propagate = False
    for h in lark_logger.handlers[:]:
        lark_logger.removeHandler(h)
    if _file_handler:
        lark_logger.addHandler(_file_handler)
    lark_stdout = logging.StreamHandler(sys.stdout)
    lark_stdout.setFormatter(_SmartFormatter())
    lark_logger.addHandler(lark_stdout)


def reconfigure_lark() -> None:
    """Reconfigure lark SDK logger after lark_oapi is imported.

    The SDK adds its own StreamHandler on import; this removes it
    and restores our configured handlers.
    """
    _configure_lark()


def _caller_module() -> str:
    frame = inspect.currentframe()
    while frame is not None:
        frame = frame.f_back
        if frame is None:
            return ""
        if frame.f_globals.get("__name__") != __name__:
            break
    if frame is None:
        return ""
    name = frame.f_globals.get("__name__", "")
    if name == "__main__":
        return "main"
    return name.rsplit(".", 1)[-1] if "." in name else name


def log(level: str, msg: str, *args: object, exc_info: bool = False) -> None:
    level_value = getattr(logging, level.upper(), logging.INFO)
    _logger.log(level_value, msg, *args, exc_info=exc_info, extra={"_module": _caller_module()})


def debug(msg: str, *args: object, exc_info: bool = False) -> None:
    log("DEBUG", msg, *args, exc_info=exc_info)


def info(msg: str, *args: object, exc_info: bool = False) -> None:
    log("INFO", msg, *args, exc_info=exc_info)


def warning(msg: str, *args: object, exc_info: bool = False) -> None:
    log("WARNING", msg, *args, exc_info=exc_info)


def error(msg: str, *args: object, exc_info: bool = False) -> None:
    log("ERROR", msg, *args, exc_info=exc_info)


def exception(msg: str, *args: object) -> None:
    log("ERROR", msg, *args, exc_info=True)
