"""可选联网搜索：按需触发，检索结果作为参考资料增强回复。

保持轻量：用标准库 urllib 直连搜索 API，不引入额外依赖。
未配置 / 失败时静默退化，不影响主回复流程。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Sequence

from .config import SearchConfig


class SearchError(Exception):
    """搜索调用失败。"""


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def should_search(text: str, keywords: Sequence[str]) -> bool:
    """命中关键词即认为该问题可能需要实时/事实信息，需检索。"""
    return any(k in text for k in keywords)


class SearchProvider:
    """搜索后端抽象。"""

    def search(self, query: str, top_k: int = 4) -> list[SearchResult]:
        raise NotImplementedError


class TavilySearch(SearchProvider):
    """Tavily 搜索（专为 AI/LLM 设计，key 走 .env 的 SEARCH_API_KEY）。"""

    ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, api_key: str, timeout_seconds: float = 10.0) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, top_k: int = 4) -> list[SearchResult]:
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max(1, top_k),
            "search_depth": "basic",
        }
        req = urllib.request.Request(
            self.ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            raise SearchError(f"搜索失败: {exc}") from exc

        results: list[SearchResult] = []
        for item in data.get("results", [])[:top_k]:
            results.append(
                SearchResult(
                    title=str(item.get("title", "") or ""),
                    url=str(item.get("url", "") or ""),
                    snippet=str(item.get("content", "") or ""),
                )
            )
        return results


def build_search(cfg: SearchConfig) -> SearchProvider | None:
    """按配置构建搜索后端；未启用 / 未配 key / 未知 provider 时返回 None（不搜索）。"""
    if not cfg.enabled:
        return None
    if cfg.provider == "tavily":
        if not cfg.api_key:
            return None
        return TavilySearch(cfg.api_key, cfg.timeout_seconds)
    return None