"""对 Bot 的纯决策逻辑与 SessionStore 做单元测试（不依赖真实微信）。"""
from __future__ import annotations

import time
from datetime import datetime

from src.bot import Bot, RateLimiter
from src.config import AppConfig
from src.llm import LLMError
from src.session import SessionStore


class FakeLLM:
    def __init__(self, reply: str = "你好"):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def chat(self, messages, max_retries=3):
        self.calls.append(list(messages))
        return self.reply


class FailLLM:
    def chat(self, messages, max_retries=3):
        raise LLMError("boom")


def make_bot(**overrides) -> Bot:
    cfg = AppConfig()
    cfg.reply.night_silence_enabled = False  # 默认关闭，让测试与“当前时间”解耦
    cfg.reply.min_interval_seconds = 0.0     # 关闭最小间隔，避免相邻断言被限速
    for k, v in overrides.items():
        setattr(cfg.reply, k, v)
    return Bot(cfg, FakeLLM(), SessionStore(cfg.session.max_turns, cfg.session.ttl_seconds))


# ---------- 过滤决策 ----------


def test_empty_message_rejected():
    bot = make_bot()
    assert bot.reject_reason("小明", "   ", False) == "empty"


def test_friend_and_group_switch():
    bot = make_bot(friend_enabled=False)
    assert bot.reject_reason("小明", "hi", False) == "friend_disabled"
    bot = make_bot(group_enabled=False)
    assert bot.reject_reason("某群", "hi", True) == "group_disabled"


def test_whitelist():
    bot = make_bot(whitelist=["小明"])
    assert bot.reject_reason("小红", "hi", False) == "not_in_whitelist"
    assert bot.reject_reason("小明", "hi", False) is None


def test_blacklist_chat_and_sender():
    bot = make_bot(blacklist=["小明"])
    assert bot.reject_reason("小明", "hi", False) == "in_blacklist"
    assert bot.reject_reason("某群", "hi", True, sender="小明") == "in_blacklist"


def test_match_mode_exact():
    bot = make_bot(whitelist=["小明"], match_mode="exact")
    assert bot.reject_reason("小红", "hi", False) == "not_in_whitelist"
    assert bot.reject_reason("小明", "hi", False) is None
    # 精确模式下，带后缀的备注名不算命中
    assert bot.reject_reason("小明（同事）", "hi", False) == "not_in_whitelist"


def test_match_mode_contains():
    bot = make_bot(whitelist=["小明"], match_mode="contains")
    assert bot.reject_reason("小明（同事）", "hi", False) is None
    assert bot.reject_reason("小红", "hi", False) == "not_in_whitelist"


def test_blacklist_match_mode():
    bot = make_bot(blacklist=["老板"], match_mode="contains")
    assert bot.reject_reason("王老板", "hi", False) == "in_blacklist"
    assert bot.reject_reason("某群", "hi", True, sender="李老板") == "in_blacklist"

    bot2 = make_bot(blacklist=["老板"], match_mode="exact")
    assert bot2.reject_reason("王老板", "hi", False) is None


def test_group_only_when_mentioned():
    bot = make_bot(group_only_when_mentioned=True)
    assert bot.reject_reason("某群", "hi", True) == "not_mentioned"
    assert bot.reject_reason("某群", "hi", True, at_list=["我"]) is None

    bot2 = make_bot(group_only_when_mentioned=True, group_self_nickname="我")
    assert bot2.reject_reason("某群", "hi", True, at_list=["别人"]) == "not_mentioned"
    assert bot2.reject_reason("某群", "hi", True, at_list=["我"]) is None


def test_keyword_trigger():
    bot = make_bot(keyword_trigger=["机票"])
    assert bot.reject_reason("小明", "今天天气不错", False) == "keyword_not_matched"
    assert bot.reject_reason("小明", "帮我查机票", False) is None


def test_night_silence_spans_midnight():
    bot = make_bot(night_silence_enabled=True, night_silence_start="23:30", night_silence_end="07:00")
    assert bot.reject_reason("小明", "hi", False, now=datetime(2025, 1, 1, 23, 45)) == "night_silence"
    assert bot.reject_reason("小明", "hi", False, now=datetime(2025, 1, 1, 3, 0)) == "night_silence"
    assert bot.reject_reason("小明", "hi", False, now=datetime(2025, 1, 1, 12, 0)) is None


def test_night_silence_disabled():
    bot = make_bot(night_silence_enabled=False)
    assert bot.reject_reason("小明", "hi", False, now=datetime(2025, 1, 1, 3, 0)) is None


# ---------- 限速 ----------


def test_rate_limit_window():
    rl = RateLimiter(max_per_minute=1, min_interval=0)
    now = time.time()
    assert rl.allowed(now) is True
    rl.record(now)
    assert rl.allowed(now + 1) is False  # 一分钟内已达上限


def test_rate_limit_min_interval():
    rl = RateLimiter(max_per_minute=100, min_interval=1.0)
    now = time.time()
    assert rl.allowed(now) is True
    rl.record(now)
    assert rl.allowed(now + 0.5) is False
    assert rl.allowed(now + 1.5) is True


# ---------- 会话记忆 ----------


def test_session_truncation_keeps_latest():
    s = SessionStore(max_turns=2, ttl_seconds=3600)
    for i in range(5):
        s.append("k", "user" if i % 2 == 0 else "assistant", f"m{i}")
    msgs = s.get("k")
    assert len(msgs) == 4
    assert msgs[-1] == {"role": "user", "content": "m4"}


# ---------- 组装与回复 ----------


def test_on_message_reply_and_context():
    bot = make_bot()
    fake: FakeLLM = bot.llm  # type: ignore[assignment]

    reply = bot.on_message("小明", "你好", False)
    assert reply == "你好"
    assert fake.calls[0][0]["role"] == "system"
    assert fake.calls[0][-1] == {"role": "user", "content": "你好"}

    bot.on_message("小明", "再聊聊", False)
    # system + user + assistant + user
    assert len(fake.calls[1]) == 4
    assert fake.calls[1][-1] == {"role": "user", "content": "再聊聊"}


def test_on_message_fallback_on_llm_error():
    cfg = AppConfig()
    cfg.reply.night_silence_enabled = False
    cfg.reply.min_interval_seconds = 0.0
    bot = Bot(cfg, FailLLM(), SessionStore())
    assert bot.on_message("小明", "你好", False) == cfg.reply.fallback_reply


def test_on_message_rate_limited_skips_llm():
    cfg = AppConfig()
    cfg.reply.max_reply_per_minute = 0  # 永不回复
    fake = FakeLLM()
    bot = Bot(cfg, fake, SessionStore())
    assert bot.on_message("小明", "hi", False) is None
    assert fake.calls == []