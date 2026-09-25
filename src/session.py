"""会话上下文记忆：按会话维护最近 N 轮对话，LRU 淘汰 + TTL 过期。"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class SessionStore:
    """按 (类别, 会话) 键维护 OpenAI 格式的多轮上下文。"""

    def __init__(self, max_turns: int = 8, ttl_seconds: int = 3600) -> None:
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self._data: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    def _prune_expired(self, now: float) -> None:
        expired = [k for k, v in self._data.items() if now - v["last_active"] > self.ttl_seconds]
        for k in expired:
            del self._data[k]

    def get(self, key: str) -> list[dict[str, str]]:
        now = time.time()
        self._prune_expired(now)
        entry = self._data.get(key)
        if entry is None:
            return []
        entry["last_active"] = now
        self._data.move_to_end(key)
        return list(entry["messages"])

    def append(self, key: str, role: str, content: str) -> None:
        now = time.time()
        entry = self._data.get(key)
        if entry is None:
            entry = {"messages": [], "last_active": now}
            self._data[key] = entry
        entry["messages"].append({"role": role, "content": content})
        max_msgs = self.max_turns * 2  # N 轮 = N 条 user + N 条 assistant
        if len(entry["messages"]) > max_msgs:
            del entry["messages"][: len(entry["messages"]) - max_msgs]
        entry["last_active"] = now
        self._data.move_to_end(key)

    def clear(self, key: str) -> None:
        self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)