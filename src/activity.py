"""最近动态：一份进程内的环形缓冲，记录「它替你做了什么」。

为什么需要它：``/api/status`` 只回答「开没开」，不回答「干了啥」。而人在外面
（图书馆、地铁上）打开面板时最想知道的恰恰是后者 —— 回了谁、回了什么、
跳过了谁、为什么跳过。所以这里单独记一路结构化事件，给面板的「最近动态」用。

几个刻意的取舍：

- **只放内存，不落盘**。这类「最近发生了什么」重启后本来就没意义；真要长期留痕，
  `wechatauto_logs/app_*.log` 已经是全量记录，不在这里做第二份。
- **有上限**。`deque(maxlen=...)`，内存有界，不会因为跑了好几天而一直涨。
- **线程安全**。写方是控制器的回复线程，读方是 webui 的 HTTP 线程
  （ThreadingHTTPServer 每个请求一个线程），所以必须加锁。
- **计数按本地日期分桶**。跨天自动归零，面板上的「今日」才是真的今天。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import date
from typing import Any

# action 取值。前三项是 docs/plan-desktop-app.md `8.1 约定的；
# system 是额外加的：面板上「13:20 自动回复已开启」这类信息同样值得看见。
ACTION_REPLIED = "replied"
ACTION_SKIPPED = "skipped"
ACTION_ERROR = "error"
ACTION_SYSTEM = "system"

# 单条正文最多存这么多字符。够显示全文，又不至于被一条超长消息把内存撑起来。
MAX_TEXT = 2000
DEFAULT_MAXLEN = 200


class ActivityLog:
    """线程安全的「最近动态」环形缓冲 + 今日计数。"""

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._day = date.today()
        self._counts = {"replied": 0, "skipped": 0, "errors": 0}

    # ---------- 写 ----------

    @staticmethod
    def _clip(text: str | None) -> str | None:
        """超长正文截断并注明原文长度 —— 静默截断会让人误以为对方只说了半句。"""
        if text is None:
            return None
        if len(text) <= MAX_TEXT:
            return text
        return text[:MAX_TEXT] + "…（原文共 %d 字，已截断）" % len(text)

    def _roll_day_locked(self) -> None:
        """跨天把今日计数归零。调用方必须已持锁。"""
        today = date.today()
        if today != self._day:
            self._day = today
            self._counts = {"replied": 0, "skipped": 0, "errors": 0}

    def record(
        self,
        *,
        action: str,
        chat: str = "",
        is_group: bool = False,
        sender: str = "",
        incoming: str = "",
        outgoing: str | None = None,
        reason: str = "",
        detail: str = "",
        gen_seconds: float | None = None,
        send_seconds: float | None = None,
        ts: float | None = None,
    ) -> dict[str, Any]:
        """记一条事件并返回它（返回是为了让单测能直接断言）。"""
        item: dict[str, Any] = {
            "ts": time.time() if ts is None else ts,
            "chat": chat,
            "is_group": bool(is_group),
            "sender": sender or chat,
            "incoming": self._clip(incoming) or "",
            "outgoing": self._clip(outgoing),
            "action": action,
            "reason": reason,
            "detail": detail,
            "gen_seconds": None if gen_seconds is None else round(gen_seconds, 2),
            "send_seconds": None if send_seconds is None else round(send_seconds, 2),
        }
        with self._lock:
            self._roll_day_locked()
            self._items.append(item)
            if action == ACTION_REPLIED:
                self._counts["replied"] += 1
            elif action == ACTION_SKIPPED:
                self._counts["skipped"] += 1
            elif action == ACTION_ERROR:
                self._counts["errors"] += 1
        return item

    # ---------- 读 ----------

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """最近的若干条，**新的在前**（面板从上往下就是从新到旧）。"""
        if limit <= 0:
            return []
        with self._lock:
            items = list(self._items)
        items.reverse()
        return items[:limit]

    def counts(self) -> dict[str, int]:
        """今日计数。"""
        with self._lock:
            self._roll_day_locked()
            replied = self._counts["replied"]
            skipped = self._counts["skipped"]
            errors = self._counts["errors"]
        return {
            "today_replied": replied,
            "today_skipped": skipped,
            "today_errors": errors,
            "today_total": replied + skipped + errors,
        }

    def snapshot(self, limit: int = 50) -> dict[str, Any]:
        """`GET /api/recent` 的完整响应体（契约见 docs/plan-desktop-app.md `8.1）。"""
        return {"items": self.recent(limit), "counts": self.counts()}
