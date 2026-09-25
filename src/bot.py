"""核心编排：消息过滤、群聊@、关键词、限速、夜间静默、上下文切片与回复。"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Sequence

from .config import AppConfig
from .llm import LLMClient, LLMError
from .session import SessionStore


class RateLimiter:
    """滑动窗口限速 + 两次回复最小间隔。"""

    def __init__(self, max_per_minute: int = 20, min_interval: float = 1.0) -> None:
        self.max_per_minute = max_per_minute
        self.min_interval = min_interval
        self._timestamps: list[float] = []
        self._last: float = 0.0

    def allowed(self, now: float) -> bool:
        window = now - 60.0
        self._timestamps = [t for t in self._timestamps if t > window]
        if self.max_per_minute >= 0 and len(self._timestamps) >= self.max_per_minute:
            return False
        if now - self._last < self.min_interval:
            return False
        return True

    def record(self, now: float) -> None:
        self._timestamps.append(now)
        self._last = now


class Bot:
    """把“该不该回 / 回什么”的决策与 LLM、会话、限速串起来。"""

    def __init__(self, config: AppConfig, llm: LLMClient, sessions: SessionStore) -> None:
        self.config = config
        self.llm = llm
        self.sessions = sessions
        self.limiter = RateLimiter(config.reply.max_reply_per_minute, config.reply.min_interval_seconds)

    # ---------- 纯决策逻辑（可单测） ----------

    @staticmethod
    def _hhmm(value: str) -> int:
        h, m = (s.strip() for s in value.split(":"))
        return int(h) * 60 + int(m)

    def is_night_silence(self, now: datetime | None = None) -> bool:
        cfg = self.config.reply
        if not cfg.night_silence_enabled:
            return False
        now = now or datetime.now()
        cur = now.hour * 60 + now.minute
        start = self._hhmm(cfg.night_silence_start)
        end = self._hhmm(cfg.night_silence_end)
        if start == end:
            return False
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end  # 跨午夜

    @staticmethod
    def _is_mentioned(at_list: Sequence[str] | None, self_nickname: str) -> bool:
        if not at_list:
            return False
        if self_nickname:
            return self_nickname in [str(a) for a in at_list]
        return True  # 未配置昵称时退化为“群里有人被 @”

    def reject_reason(
        self,
        chat: str,
        content: str,
        is_group: bool,
        sender: str = "",
        at_list: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> str | None:
        """返回“不回复的原因”；返回 None 表示应当回复。"""
        cfg = self.config.reply

        if not content or not content.strip():
            return "empty"

        if is_group:
            if not cfg.group_enabled:
                return "group_disabled"
        else:
            if not cfg.friend_enabled:
                return "friend_disabled"

        if cfg.whitelist and chat not in cfg.whitelist:
            return "not_in_whitelist"
        if cfg.blacklist and (chat in cfg.blacklist or (sender and sender in cfg.blacklist)):
            return "in_blacklist"

        if is_group and cfg.group_only_when_mentioned and not self._is_mentioned(at_list, cfg.group_self_nickname):
            return "not_mentioned"

        if cfg.keyword_trigger and not any(k in content for k in cfg.keyword_trigger):
            return "keyword_not_matched"

        if self.is_night_silence(now):
            return "night_silence"

        if not self.limiter.allowed(time.time()):
            return "rate_limited"

        return None

    # ---------- 组装与回复 ----------

    def _session_key(self, chat: str, is_group: bool) -> str:
        return ("group:" if is_group else "friend:") + chat

    def _user_text(self, content: str, is_group: bool, sender: str) -> str:
        if is_group and sender:
            return f"{sender} 说：{content}"
        return content

    def on_message(
        self,
        chat: str,
        content: str,
        is_group: bool,
        sender: str = "",
        at_list: Sequence[str] | None = None,
    ) -> str | None:
        """处理一条消息；返回要发送的回复文本，或 None（不回复）。"""
        if self.reject_reason(chat, content, is_group, sender, at_list):
            return None

        key = self._session_key(chat, is_group)
        user_text = self._user_text(content, is_group, sender)
        messages = [
            {"role": "system", "content": self.config.llm.system_prompt},
            *self.sessions.get(key),
            {"role": "user", "content": user_text},
        ]

        try:
            reply = self.llm.chat(messages)
        except LLMError:
            reply = self.config.reply.fallback_reply
            if not reply:
                return None

        self.sessions.append(key, "user", user_text)
        self.sessions.append(key, "assistant", reply)
        self.limiter.record(time.time())
        return reply