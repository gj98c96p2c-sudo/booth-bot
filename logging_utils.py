"""構造化ログ。

すべてのログを `[LEVEL] module: message` 形式に統一する。
Render のログビューアで grep しやすくするのが目的。
"""

import datetime
import sys


def _timestamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _emit(level: str, module: str, message: str, *, stream=None) -> None:
    stream = stream or sys.stdout
    print(f"{_timestamp()} [{level}] {module}: {message}", file=stream, flush=True)


def debug(module: str, message: str) -> None:
    _emit("DEBUG", module, message)


def info(module: str, message: str) -> None:
    _emit("INFO", module, message)


def warn(module: str, message: str) -> None:
    _emit("WARN", module, message)


def error(module: str, message: str) -> None:
    _emit("ERROR", module, message, stream=sys.stderr)
