"""对 system prompt 组装逻辑做单元测试。"""
from __future__ import annotations

from datetime import datetime

from src.config import AppConfig
from src.prompt import build_system_prompt

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _cfg(**overrides) -> AppConfig:
    cfg = AppConfig()
    for k, v in overrides.items():
        setattr(cfg.persona, k, v)
    return cfg


def test_prompt_contains_persona_and_style():
    text = build_system_prompt(_cfg(name="小明助手", style="幽默"), now=datetime(2026, 9, 25, 15, 0))
    assert "小明助手" in text
    assert "幽默" in text


def test_prompt_injects_current_time():
    now = datetime(2026, 9, 25, 15, 0)
    text = build_system_prompt(AppConfig(), now=now)
    assert "2026年9月25日" in text
    assert "15:00" in text
    assert _WEEKDAYS[now.weekday()] in text


def test_prompt_emoji_on_off():
    assert "emoji" in build_system_prompt(_cfg(emoji=True), now=datetime(2026, 9, 25, 15, 0))
    assert "不要使用 emoji" in build_system_prompt(_cfg(emoji=False), now=datetime(2026, 9, 25, 15, 0))


def test_prompt_length_strategies():
    assert "简短" in build_system_prompt(_cfg(length="short"), now=datetime(2026, 9, 25, 15, 0))
    assert "详细" in build_system_prompt(_cfg(length="detailed"), now=datetime(2026, 9, 25, 15, 0))