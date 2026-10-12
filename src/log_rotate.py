"""把第三方库 wechatauto 的日志文件换成带轮转的。

why：`wechatauto/logger.py` 在根 logger 上挂了一个**普通** `FileHandler`，写
`wechatauto_logs/app_YYYYMMDD.log`（不轮转）。无人值守一直开着，日志就一直涨
（本机实测约 0.7MB/天）。它属于 site-packages，直接改会在升级 wechatauto 时被覆盖，
所以这里用标准库 API 把它**替换**掉 —— 文件名、格式、级别全部保持原样，只把类换成
`RotatingFileHandler`（阈值 1MB，留 5 份：app_20261005.log.1 … .5）。

看门狗脚本里 `backend-loop.log` / `desktop-shell.log` 是同样的思路（超过 1MB 就转存一份），
三条日志的行为因此一致。
"""
from __future__ import annotations

import logging
import logging.handlers
import os

APP_LOG_MAX_BYTES = 1024 * 1024   # 单个日志文件到 1MB 就转存
APP_LOG_BACKUPS = 5               # 最多保留 5 份历史


def enable_app_log_rotation(max_bytes: int = APP_LOG_MAX_BYTES,
                            backups: int = APP_LOG_BACKUPS) -> str:
    """把 `app_*.log` 的处理器换成轮转版；返回被换掉的完整路径，没得换就返回空串。

    幂等：已经换过、或第三方库还没建、或那个 handler 指向别的文件时，都返回空串，
    可以放心重复调用。只认文件名为 `app_*.log` 的处理器，不碰别人的日志。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if not isinstance(handler, logging.FileHandler):
            continue
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            continue                                  # 已经是轮转版，不用再换
        path = getattr(handler, "baseFilename", "") or ""
        name = os.path.basename(path)
        if not (name.startswith("app_") and name.endswith(".log")):
            continue                                  # 不是 wechatauto 那个文件
        root.removeHandler(handler)
        handler.close()                               # 先放开文件句柄，Windows 才让重开
        rotating = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        rotating.setFormatter(handler.formatter)
        rotating.setLevel(handler.level)
        root.addHandler(rotating)
        return path
    return ""
