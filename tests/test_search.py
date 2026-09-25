"""对搜索意图判定、检索增强与失败回退做单元测试（不真实联网）。"""
from __future__ import annotations

from src.bot import Bot
from src.config import AppConfig, SearchConfig
from src.search import SearchResult, build_search, should_search
from src.session import SessionStore


class FakeSearch:
    def __init__(self, results=None):
        self.results = results or []
        self.queries: list[str] = []

    def search(self, query, top_k=4):
        self.queries.append(query)
        return self.results


class FailingSearch:
    def search(self, query, top_k=4):
        raise RuntimeError("network down")


class FakeLLM:
    def __init__(self, reply="你好"):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def chat(self, messages, max_retries=3):
        self.calls.append(list(messages))
        return self.reply


def _bot(search=None) -> Bot:
    cfg = AppConfig()
    cfg.reply.night_silence_enabled = False
    cfg.reply.min_interval_seconds = 0.0
    return Bot(cfg, FakeLLM(), SessionStore(), search=search)


# ---------- 意图判定 ----------


def test_should_search_keywords():
    keywords = ["天气", "新闻", "股价"]
    assert should_search("今天天气怎么样", keywords) is True
    assert should_search("最近有什么新闻", keywords) is True
    assert should_search("在吗", keywords) is False


# ---------- build_search 工厂 ----------


def test_build_search_gating():
    assert build_search(SearchConfig(enabled=False, provider="tavily", api_key="k")) is None
    assert build_search(SearchConfig(enabled=True, provider="tavily", api_key="")) is None
    assert build_search(SearchConfig(enabled=True, provider="unknown", api_key="k")) is None
    assert build_search(SearchConfig(enabled=True, provider="tavily", api_key="k")) is not None


# ---------- 检索增强（_search_context） ----------


def test_search_context_triggered():
    bot = _bot(search=FakeSearch([SearchResult("某地天气", "http://x", "今天晴")]))
    ctx = bot._search_context("今天天气怎么样")
    assert "某地天气" in ctx
    assert "今天晴" in ctx


def test_search_context_not_triggered_for_plain_chat():
    bot = _bot(search=FakeSearch([SearchResult("t", "u", "s")]))
    assert bot._search_context("你好") == ""


def test_search_context_disabled_when_no_search():
    bot = _bot(search=None)
    assert bot._search_context("今天天气") == ""


def test_search_context_returns_empty_on_failure():
    bot = _bot(search=FailingSearch())
    assert bot._search_context("今天天气") == ""


def test_search_context_returns_empty_on_no_results():
    bot = _bot(search=FakeSearch([]))
    assert bot._search_context("今天天气") == ""


# ---------- 端到端：搜索结果注入回复，但不污染会话记忆 ----------


def test_on_message_enriches_but_keeps_memory_clean():
    fake_search = FakeSearch([SearchResult("上海天气", "http://x", "晴 25度")])
    bot = _bot(search=fake_search)

    reply = bot.on_message("小明", "今天上海天气", False)

    assert reply == "你好"
    # 检索只对原始 content 触发一次
    assert fake_search.queries == ["今天上海天气"]
    # 发给 LLM 的用户消息带上了搜索结果
    assert "晴 25度" in bot.llm.calls[0][-1]["content"]
    # 会话记忆里存的是原始文本，不带搜索结果
    stored = bot.sessions.get("friend:小明")
    assert stored[-2] == {"role": "user", "content": "今天上海天气"}