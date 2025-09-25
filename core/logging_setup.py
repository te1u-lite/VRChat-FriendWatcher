from __future__ import annotations

import logging
import re
import os
import pathlib
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

DEFAULT_LOG_DIR = Path.cwd() / "logs"
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR/"VRChat-FriendWatcher.log"


class SecretsFilter(logging.Filter):
    PATS = [
        (re.compile(r"(authToken=)([^&\s]+)"), r"\1[REDACTED]"),
        (re.compile(r"(auth=)([^;]+)"), r"\1[REDACTED]"),
        (re.compile(r"(Authorization:\s*Bearer\s+)(\S+)"), r"\1[REDACTED]"),
        (re.compile(r"(X-API-Key:\s*)(\S+)"), r"\1[REDACTED]"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        for pat, repl in self.PATS:
            msg = pat.sub(repl, msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logging(
        log_file: str | Path = DEFAULT_LOG_FILE,
        level: int = logging.INFO,
        max_bytes: int = 1_000_000,  # ~1MB
        backup_count: int = 5,
        console: bool = True,
) -> Path:
    """
    ルートロガーをローテーション付きで初期化。
    - 重複初期化を避ける (多重呼び出しOK)
    - ファイルは ./logs/VRChat-FriendWatcher.log (デフォルト)
    - コンソール出力 (デフォルトON)

    Returns: 実際に使われたログファイルのPath
    """
    # 既に設定済みなら何もしない (重複ハンドラ防止)
    root = logging.getLogger()
    if getattr(root, "_vrcwatcher_initialized", False):
        return Path(log_file)

    # ディレクトリ作成
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # ルートロガー設定
    root.setLevel(level)
    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ファイル (ローテーション)
    file_handler = RotatingFileHandler(
        filename=log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # コンソール
    if console:
        stream_handler = logging.StreamHandler(stream=sys.stdout)
        stream_handler.setFormatter(fmt)
        root.addHandler(stream_handler)

    # 再初期化防止フラグ
    root._vrcwatcher_initialized = True  # type: ignore[attr-defined]

    logging.getLogger(__name__).info("Logging initialized: %s", log_path)

    filt = SecretsFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(filt)
    logging.getLogger().addFilter(filt)

    return log_path
