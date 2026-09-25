"""大模型客户端：任意 OpenAI 兼容接口（默认 DeepSeek，可接 Ollama/OpenAI 等）。"""
from __future__ import annotations

import time
from typing import Sequence

from openai import OpenAI

from .config import LLMConfig


class LLMError(Exception):
    """大模型调用失败。"""


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        base_url = config.base_url
        api_key = config.api_key
        if config.provider == "ollama":
            base_url = base_url or "http://localhost:11434/v1"
            api_key = api_key or "ollama"
        if not base_url or not api_key:
            raise LLMError(
                "缺少 LLM 配置：请在 .env 中设置 LLM_BASE_URL 与 LLM_API_KEY"
                "（Ollama 本地模式可只设 LLM_PROVIDER=ollama）。"
            )
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=config.timeout_seconds)

    def chat(self, messages: Sequence[dict[str, str]], max_retries: int = 3) -> str:
        last: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.config.model,
                    messages=list(messages),
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                )
                content = resp.choices[0].message.content
                return (content or "").strip()
            except Exception as exc:  # noqa: BLE001 - 统一包装后抛出
                last = exc
                if attempt < max_retries:
                    time.sleep(min(2 ** attempt, 8))
        raise LLMError(f"LLM 调用失败（已重试）: {last}")