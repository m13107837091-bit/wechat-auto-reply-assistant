"""配置加载：.env（密钥）+ config.yaml（行为策略）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LLMConfig:
    provider: str = "openai"
    model: str = "deepseek-chat"
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.6
    max_tokens: int = 800
    timeout_seconds: float = 30.0
    system_prompt: str = "你是一个友好、耐心的微信聊天助手。回复简洁自然、用中文。"


@dataclass
class ReplyConfig:
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    keyword_trigger: list[str] = field(default_factory=list)
    max_reply_per_minute: int = 20
    min_interval_seconds: float = 1.0
    night_silence_enabled: bool = True
    night_silence_start: str = "23:30"
    night_silence_end: str = "07:00"
    fallback_reply: str = "（自动回复）我现在不方便回复，稍后联系。"
    group_enabled: bool = True
    group_only_when_mentioned: bool = True
    group_self_nickname: str = ""
    friend_enabled: bool = True


@dataclass
class SessionConfig:
    max_turns: int = 8
    ttl_seconds: int = 3600


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    session: SessionConfig = field(default_factory=SessionConfig)

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "AppConfig":
        load_dotenv(PROJECT_ROOT / ".env")
        path = Path(config_path) if config_path else (PROJECT_ROOT / "config.yaml")
        raw: dict[str, Any] = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        cfg = cls()

        llm_raw = raw.get("llm") or {}
        cfg.llm.provider = str(os.getenv("LLM_PROVIDER") or llm_raw.get("provider", cfg.llm.provider))
        cfg.llm.model = str(os.getenv("LLM_MODEL") or llm_raw.get("model", cfg.llm.model))
        cfg.llm.base_url = str(os.getenv("LLM_BASE_URL") or llm_raw.get("base_url", "") or "")
        cfg.llm.api_key = str(os.getenv("LLM_API_KEY") or "")
        cfg.llm.temperature = float(llm_raw.get("temperature", cfg.llm.temperature))
        cfg.llm.max_tokens = int(llm_raw.get("max_tokens", cfg.llm.max_tokens))
        cfg.llm.timeout_seconds = float(llm_raw.get("timeout_seconds", cfg.llm.timeout_seconds))
        cfg.llm.system_prompt = str(llm_raw.get("system_prompt", cfg.llm.system_prompt))

        reply_raw = raw.get("reply") or {}
        cfg.reply.whitelist = _as_str_list(reply_raw.get("whitelist"))
        cfg.reply.blacklist = _as_str_list(reply_raw.get("blacklist"))
        cfg.reply.keyword_trigger = _as_str_list(reply_raw.get("keyword_trigger"))
        cfg.reply.max_reply_per_minute = int(reply_raw.get("max_reply_per_minute", cfg.reply.max_reply_per_minute))
        cfg.reply.min_interval_seconds = float(reply_raw.get("min_interval_seconds", cfg.reply.min_interval_seconds))
        night = reply_raw.get("night_silence") or {}
        cfg.reply.night_silence_enabled = bool(night.get("enabled", cfg.reply.night_silence_enabled))
        cfg.reply.night_silence_start = str(night.get("start", cfg.reply.night_silence_start))
        cfg.reply.night_silence_end = str(night.get("end", cfg.reply.night_silence_end))
        cfg.reply.fallback_reply = str(reply_raw.get("fallback_reply", cfg.reply.fallback_reply))
        group = reply_raw.get("group") or {}
        cfg.reply.group_enabled = bool(group.get("enabled", cfg.reply.group_enabled))
        cfg.reply.group_only_when_mentioned = bool(group.get("only_when_mentioned", cfg.reply.group_only_when_mentioned))
        cfg.reply.group_self_nickname = str(group.get("self_nickname", "") or "")
        friend = reply_raw.get("friend") or {}
        cfg.reply.friend_enabled = bool(friend.get("enabled", cfg.reply.friend_enabled))

        session_raw = raw.get("session") or {}
        cfg.session.max_turns = int(session_raw.get("max_turns", cfg.session.max_turns))
        cfg.session.ttl_seconds = int(session_raw.get("ttl_seconds", cfg.session.ttl_seconds))

        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.reply.max_reply_per_minute < 0:
            raise ValueError("config: reply.max_reply_per_minute 不能为负")
        if self.reply.min_interval_seconds < 0:
            raise ValueError("config: reply.min_interval_seconds 不能为负")
        if self.session.max_turns < 1:
            raise ValueError("config: session.max_turns 至少为 1")


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    s = str(value).strip()
    return [s] if s else []