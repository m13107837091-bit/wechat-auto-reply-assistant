"""app 日志轮转（src/log_rotate.py）的回归测试。

背景：`app_YYYYMMDD.log` 是第三方库 wechatauto 建的**不轮转**的文件处理器，
无人值守跑久了会一直涨。我们在连上微信后把它换成 RotatingFileHandler。
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest

from src.log_rotate import enable_app_log_rotation


@pytest.fixture(autouse=True)
def _isolate_root_logger():
    """前后都把根 logger 的处理器恢复原样，别污染 pytest 自己的日志。"""
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()


def _add_file_handler(path: Path, level: int = logging.DEBUG) -> logging.FileHandler:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(level)
    logging.getLogger().addHandler(handler)
    return handler


def test_replaces_plain_app_log_handler_with_rotating(tmp_path):
    app_log = tmp_path / "app_20261005.log"
    _add_file_handler(app_log)

    assert enable_app_log_rotation(max_bytes=1024, backups=2) == str(app_log)

    rotating = [h for h in logging.getLogger().handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1
    # 文件、格式、级别都保持原样，只是换了类
    assert rotating[0].baseFilename == str(app_log)
    assert (rotating[0].maxBytes, rotating[0].backupCount) == (1024, 2)
    assert rotating[0].level == logging.DEBUG
    assert rotating[0].formatter._fmt == "%(message)s"


def test_rotation_actually_rotates(tmp_path):
    app_log = tmp_path / "app_20261005.log"
    _add_file_handler(app_log)
    enable_app_log_rotation(max_bytes=200, backups=1)

    log = logging.getLogger("rotate-test")
    for _ in range(20):
        log.warning("x" * 40)

    assert (tmp_path / "app_20261005.log.1").exists()   # 转存出来了
    assert app_log.stat().st_size < 200 + 200           # 当前文件被重置过


def test_is_idempotent(tmp_path):
    _add_file_handler(tmp_path / "app_20261005.log")
    assert enable_app_log_rotation() != ""
    assert enable_app_log_rotation() == ""              # 第二次没得换（已经是轮转版）


def test_ignores_other_log_files(tmp_path):
    _add_file_handler(tmp_path / "backend.log")
    assert enable_app_log_rotation() == ""
    # 别人的文件处理器原封不动
    assert any(isinstance(h, logging.FileHandler)
               and not isinstance(h, logging.handlers.RotatingFileHandler)
               for h in logging.getLogger().handlers)


def test_no_file_handler_is_fine():
    assert enable_app_log_rotation() == ""
