"""配置加载：.env（密钥）+ config.yaml（行为策略）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 默认联网搜索触发关键词（命中即认为该问题可能需要实时信息）
DEFAULT_SEARCH_KEYWORDS: tuple[str, ...] = (
    "天气", "气温", "下雨", "下雪", "台风", "地震", "新闻", "头条", "热搜",
    "实时", "最新", "股价", "股票", "行情", "汇率", "油价", "金价", "比分", "排名",
)


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
    match_mode: str = "exact"  # exact（精确）| contains（包含）
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
class PersonaConfig:
    name: str = "小助手"
    style: str = "自然活泼"
    emoji: bool = True
    length: str = "auto"  # auto | short | detailed


@dataclass
class SearchConfig:
    enabled: bool = True
    provider: str = "tavily"
    api_key: str = ""
    top_k: int = 4
    timeout_seconds: float = 10.0
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_SEARCH_KEYWORDS))


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    search: SearchConfig = field(default_factory=SearchConfig)

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
        cfg.reply.match_mode = str(reply_raw.get("match_mode", cfg.reply.match_mode))
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

        persona_raw = raw.get("persona") or {}
        cfg.persona.name = str(persona_raw.get("name", cfg.persona.name))
        cfg.persona.style = str(persona_raw.get("style", cfg.persona.style))
        cfg.persona.emoji = bool(persona_raw.get("emoji", cfg.persona.emoji))
        cfg.persona.length = str(persona_raw.get("length", cfg.persona.length))

        search_raw = raw.get("search") or {}
        cfg.search.enabled = bool(search_raw.get("enabled", cfg.search.enabled))
        cfg.search.provider = str(search_raw.get("provider", cfg.search.provider))
        cfg.search.api_key = str(os.getenv("SEARCH_API_KEY") or "")
        cfg.search.top_k = int(search_raw.get("top_k", cfg.search.top_k))
        cfg.search.timeout_seconds = float(search_raw.get("timeout_seconds", cfg.search.timeout_seconds))
        keywords = _as_str_list(search_raw.get("keywords"))
        cfg.search.keywords = keywords if keywords else list(DEFAULT_SEARCH_KEYWORDS)

        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.reply.max_reply_per_minute < 0:
            raise ValueError("config: reply.max_reply_per_minute 不能为负")
        if self.reply.min_interval_seconds < 0:
            raise ValueError("config: reply.min_interval_seconds 不能为负")
        if self.reply.match_mode not in ("exact", "contains"):
            raise ValueError("config: reply.match_mode 只能是 exact / contains")
        if self.session.max_turns < 1:
            raise ValueError("config: session.max_turns 至少为 1")
        _parse_hhmm(self.reply.night_silence_start)
        _parse_hhmm(self.reply.night_silence_end)
        if self.search.top_k < 0:
            raise ValueError("config: search.top_k 不能为负")
        if self.persona.length not in ("auto", "short", "detailed"):
            raise ValueError("config: persona.length 只能是 auto / short / detailed")


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    s = str(value).strip()
    return [s] if s else []


def _parse_hhmm(value: str) -> tuple[int, int]:
    """解析并校验 HH:MM 时间，非法则抛 ValueError。"""
    parts = [p.strip() for p in str(value).split(":")]
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"config: 时间格式应为 HH:MM，当前为 {value!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"config: 时间超出范围，当前为 {value!r}")
    return h, m