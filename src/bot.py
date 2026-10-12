"""核心编排：消息过滤、群聊@、关键词、限速、夜间静默、上下文切片与回复。"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from .config import AppConfig
from .llm import LLMClient, LLMError
from .prompt import build_system_prompt
from .search import SearchProvider, should_search
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


# 「因为 LLM 挂了而退到兜底话术」的原因码。它和别的 reason 不同：这条**确实发出去了**，
# 所以 action 仍然是 replied，只是要标出来这不是正常生成。
REASON_LLM_FAILED = "llm_failed"


@dataclass(frozen=True)
class Decision:
    """一条消息的处理结论。

    - `reply`      要发出去的文本；None 表示不回。
    - `reason`     为什么是这个结论。不回时是原因码（见 `Bot.reject_reason`）；
                   回复时为空，除非走了兜底话术（`llm_failed`）。
    - `llm_failed` 是否因为 LLM 调用失败而退到了兜底话术。

    为什么要这个类型：面板的「最近动态」必须回答"它为什么没回这个人"，
    只返回一个字符串的话，沉默就无从解释了（契约见 docs/plan-desktop-app.md `8.1）。
    """

    reply: str | None = None
    reason: str = ""
    llm_failed: bool = False


class Bot:
    """把“该不该回 / 回什么”的决策与 LLM、会话、限速串起来。"""

    def __init__(
        self,
        config: AppConfig,
        llm: LLMClient,
        sessions: SessionStore,
        search: SearchProvider | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.sessions = sessions
        self.search = search
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

    def _in_list(self, name: str, patterns: Sequence[str]) -> bool:
        """按 match_mode 判断名称是否命中名单（精确或包含）。"""
        if self.config.reply.match_mode == "contains":
            return any(p in name for p in patterns)
        return name in patterns

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
            # 群聊白名单：只回名单里的群（不看这个的话，开了群聊就会到处插话）
            if cfg.group_whitelist and not self._in_list(chat, cfg.group_whitelist):
                return "not_in_group_whitelist"
        else:
            if not cfg.friend_enabled:
                return "friend_disabled"

        if cfg.whitelist and not self._in_list(chat, cfg.whitelist):
            return "not_in_whitelist"
        if cfg.blacklist and (self._in_list(chat, cfg.blacklist) or (sender and self._in_list(sender, cfg.blacklist))):
            return "in_blacklist"

        if is_group:
            # 群里说话的门槛，两种玩法二选一：
            #   ① 配了关键词 → 消息里出现关键词（或 @ 到三哥）才回，比如「三哥/老三」被提到时插一句嘴；
            #   ② 没配关键词 → 沿用 only_when_mentioned（默认 true = 只有被 @ 才回）。
            mentioned = self._is_mentioned(at_list, cfg.group_self_nickname)
            if cfg.group_trigger_keywords:
                if not (mentioned or any(k in content for k in cfg.group_trigger_keywords)):
                    return "group_keyword_not_matched"
            elif cfg.group_only_when_mentioned and not mentioned:
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

    def _search_context(self, content: str) -> str:
        """按需检索实时信息；返回参考文本或空串（不检索/失败/无结果）。"""
        if self.search is None or not self.config.search.enabled:
            return ""
        if not should_search(content, self.config.search.keywords):
            return ""
        try:
            results = self.search.search(content, self.config.search.top_k)
        except Exception:  # noqa: BLE001 - 搜索失败不影响主回复
            return ""
        if not results:
            return ""
        refs = "\n".join(f"- {r.title}：{r.snippet}" for r in results)
        return f"[以下是与问题相关的实时搜索结果，回答时请参考、不要编造]：\n{refs}"

    def respond(
        self,
        chat: str,
        content: str,
        is_group: bool,
        sender: str = "",
        at_list: Sequence[str] | None = None,
    ) -> Decision:
        """处理一条消息，返回「回什么 + 为什么」。"""
        reason = self.reject_reason(chat, content, is_group, sender, at_list)
        if reason:
            return Decision(None, reason)

        key = self._session_key(chat, is_group)
        user_text = self._user_text(content, is_group, sender)
        search_context = self._search_context(content)
        prompt_user = user_text + ("\n\n" + search_context if search_context else "")

        messages = [
            {"role": "system", "content": build_system_prompt(self.config)},
            *self.sessions.get(key),
            {"role": "user", "content": prompt_user},
        ]

        llm_failed = False
        try:
            reply = self.llm.chat(messages)
        except LLMError:
            # 兜底话术仍然算"回了"（字确实发出去了），但要标出这是降级而非正常生成。
            reply = self._fallback_reply()
            if not reply:
                return Decision(None, REASON_LLM_FAILED)
            llm_failed = True

        self.sessions.append(key, "user", user_text)
        self.sessions.append(key, "assistant", reply)
        self.limiter.record(time.time())
        return Decision(reply, llm_failed=llm_failed)

    def _fallback_reply(self) -> str:
        """大模型挂了时的兜底话术。

        配了 fallback_replies 就随机挑一句 —— 每次都同一句，三哥的朋友很快就能
        看出来对面是台复读机；轮着说至少不刺眼。
        """
        options = [s.strip() for s in self.config.reply.fallback_replies if s and s.strip()]
        if options:
            return random.choice(options)
        return self.config.reply.fallback_reply

    def on_message(
        self,
        chat: str,
        content: str,
        is_group: bool,
        sender: str = "",
        at_list: Sequence[str] | None = None,
    ) -> str | None:
        """只要回复文本的旧入口，保留给单测与其它调用方。"""
        return self.respond(chat, content, is_group, sender, at_list).reply