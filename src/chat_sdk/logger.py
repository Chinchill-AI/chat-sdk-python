"""Logger types and implementations for chat-sdk."""

from __future__ import annotations

import logging
import sys
from typing import Any, Literal, Protocol

LogLevel = Literal["debug", "info", "warn", "error", "silent"]

_LOG_LEVEL_ORDER: list[LogLevel] = ["debug", "info", "warn", "error", "silent"]


class Logger(Protocol):
    """Logger interface for chat-sdk."""

    def child(self, prefix: str) -> Logger: ...
    def debug(self, message: str, *args: Any) -> None: ...
    def info(self, message: str, *args: Any) -> None: ...
    def warn(self, message: str, *args: Any) -> None: ...
    def error(self, message: str, *args: Any) -> None: ...


class ConsoleLogger:
    """Default console logger implementation."""

    def __init__(self, level: LogLevel = "info", prefix: str = "chat-sdk") -> None:
        self._level = level
        self._prefix = prefix
        self._logger = logging.getLogger(prefix)

    def _should_log(self, level: LogLevel) -> bool:
        return _LOG_LEVEL_ORDER.index(level) >= _LOG_LEVEL_ORDER.index(self._level)

    def child(self, prefix: str) -> ConsoleLogger:
        return ConsoleLogger(self._level, f"{self._prefix}:{prefix}")

    def debug(self, message: str, *args: Any) -> None:
        if self._should_log("debug"):
            print(f"[{self._prefix}] {message}", *args, file=sys.stderr)

    def info(self, message: str, *args: Any) -> None:
        if self._should_log("info"):
            print(f"[{self._prefix}] {message}", *args, file=sys.stderr)

    def warn(self, message: str, *args: Any) -> None:
        if self._should_log("warn"):
            print(f"[{self._prefix}] {message}", *args, file=sys.stderr)

    def error(self, message: str, *args: Any) -> None:
        if self._should_log("error"):
            print(f"[{self._prefix}] {message}", *args, file=sys.stderr)
