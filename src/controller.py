"""可启停的机器人核心：把原先 main 的阻塞循环搬成后台线程，供控制面板 start/stop。

控制面板（webui）只跟这个类打交道：``start()``/``stop()`` 开关自动回复，
``snapshot()`` 看状态，``get_persona()``/``update_persona()`` 读改人设并持久化。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .bot import Bot
from .config import AppConfig, LLMConfig, mask_api_key, save_llm_settings, save_persona
from .llm import LLMClient, LLMError
from .search import build_search
from .session import SessionStore
from .wechat_client import IncomingMessage, WeChatClient, WeChatNotReadyError

log = logging.getLogger(__name__)


def _coalesce(messages: list[IncomingMessage]) -> list[IncomingMessage]:
    """同一会话在本次轮询里积压的多条消息合并为一条。

    对方连发多条时，逐条回复会串行跑多次 LLM + 多次发送，后面的消息要排很久；
    合并成一条一口气回，只回一次、也更及时。内容用换行拼接保留原意。
    """
    index: dict[tuple[str, bool], int] = {}
    out: list[IncomingMessage] = []
    for m in messages:
        key = (m.chat, m.is_group)
        if key in index:
            prev = out[index[key]]
            out[index[key]] = IncomingMessage(
                content=f"{prev.content}\n{m.content}".strip(),
                chat=m.chat,
                is_group=m.is_group,
                sender=m.sender or prev.sender,
                at_list=m.at_list or prev.at_list,
                received_at=prev.received_at,
            )
        else:
            index[key] = len(out)
            out.append(m)
    return out


class BotController:
    """把「连接微信 + 监听回复」封装成可反复 start/stop 的服务。

    依赖（llm/bot/wechat）允许在 ``__init__`` 注入，便于无微信环境下写单测；
    ``persona_path`` 允许注入，测试时写到临时目录而非项目根的 persona.json。
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        llm: LLMClient | None = None,
        bot: Bot | None = None,
        wechat: WeChatClient | None = None,
        persona_path: str | Path | None = None,
        settings_path: str | Path | None = None,
    ) -> None:
        self.config = config or AppConfig.load()
        self._llm = llm
        self._bot = bot
        self._wechat = wechat
        self._persona_path = Path(persona_path) if persona_path else None
        self._settings_path = Path(settings_path) if settings_path else None

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # 代数递增：stop() 让旧循环退出；start() 起新代，避免「旧线程没退干净、
        # 新线程又起」时两个循环并发。循环用自己那一代做退出判断。
        self._generation = 0
        self._running = False
        self.last_error = ""

    # ---------- 生命周期 ----------

    def _ensure_ready(self) -> bool:
        """懒加载 LLM/Bot/WeChat 并连接微信；失败写 last_error 并返回 False。"""
        try:
            if self._llm is None:
                self._llm = LLMClient(self.config.llm)
        except LLMError as exc:
            self.last_error = str(exc)
            return False

        if self._bot is None:
            sessions = SessionStore(self.config.session.max_turns, self.config.session.ttl_seconds)
            search = build_search(self.config.search)
            self._bot = Bot(self.config, self._llm, sessions, search)

        if self._wechat is None:
            self._wechat = WeChatClient(include_group=self.config.reply.group_enabled)

        try:
            self._wechat.prepare()
        except WeChatNotReadyError as exc:
            self.last_error = str(exc)
            return False
        return True

    def start(self) -> bool:
        """开始/恢复自动回复；已运行则直接返回 True，准备失败返回 False。"""
        if self._running:
            return True
        if not self._ensure_ready():
            return False

        self._wechat.discard_pending()  # 丢弃暂停期间积压的旧消息，恢复后只回新的
        self._generation += 1
        gen = self._generation
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, args=(gen,), name="bot-reply-loop", daemon=True
        )
        self._running = True
        self._thread.start()
        log.info("自动回复已开启。")
        return True

    def stop(self) -> None:
        """暂停自动回复（不拆微信连接/监听，随时可再 start 恢复）。"""
        if not self._running:
            return
        self._running = False
        self._generation += 1  # 让当前循环尽快退出
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)  # 最多等 2s；若正卡在 LLM 调用则让它在后台自行退出
        self._thread = None
        log.info("自动回复已暂停。")

    def _run_loop(self, generation: int) -> None:
        """监听回复循环（后台线程）。用 ``_stop_event.wait`` 代替 sleep，便于随时停下。"""
        while generation == self._generation and not self._stop_event.is_set():
            try:
                for msg in _coalesce(self._wechat.get_new_messages()):
                    # 读取阶段：消息从「监听线程入队」到「主循环取到」的等待时长。
                    if msg.received_at:
                        log.info("[读取] %s 的新消息，排队等待 %.2fs", msg.chat, time.time() - msg.received_at)
                    else:
                        log.info("[读取] %s 的新消息", msg.chat)

                    gen_start = time.time()
                    log.info("[生成] 正在给 %s 生成回复中……", msg.chat)
                    reply = self._bot.on_message(msg.chat, msg.content, msg.is_group, msg.sender, msg.at_list)
                    gen_cost = time.time() - gen_start

                    if reply:
                        send_start = time.time()
                        self._wechat.send(msg.chat, reply)
                        send_cost = time.time() - send_start
                        log.info(
                            "[发送] %s：生成 %.2fs / 发送 %.2fs | %s",
                            msg.chat, gen_cost, send_cost, reply[:40],
                        )
                    else:
                        log.info("[生成] %s 无需回复（耗时 %.2fs）", msg.chat, gen_cost)
            except WeChatNotReadyError as exc:
                log.error("%s；5 秒后重试……", exc)
                self._stop_event.wait(5)
            except Exception as exc:  # noqa: BLE001 - 循环保持存活
                log.exception("处理消息时出错: %s", exc)
                self._stop_event.wait(1)
            self._stop_event.wait(0.2)

    # ---------- 状态 / 人设 ----------

    def snapshot(self) -> dict[str, Any]:
        """控制面板需要的整体状态。"""
        return {
            "running": self._running,
            "last_error": self.last_error,
            "persona": self.get_persona(),
            "llm": self.get_llm_settings(),
        }

    def get_persona(self) -> dict[str, Any]:
        pc = self.config.persona
        return {
            "name": pc.name,
            "style": pc.style,
            "emoji": pc.emoji,
            "length": pc.length,
            "system_prompt": self.config.llm.system_prompt,
        }

    def update_persona(self, overrides: dict[str, Any]) -> dict[str, Any]:
        """原地改人设（下一句回复即生效）并持久化到 persona.json。"""
        pc = self.config.persona
        if "name" in overrides:
            pc.name = str(overrides["name"]).strip()
        if "style" in overrides:
            pc.style = str(overrides["style"]).strip()
        if "emoji" in overrides:
            pc.emoji = bool(overrides["emoji"])
        if "length" in overrides:
            length = str(overrides["length"]).strip()
            if length in ("auto", "short", "detailed"):
                pc.length = length
        if "system_prompt" in overrides:
            self.config.llm.system_prompt = str(overrides["system_prompt"]).strip()

        snapshot = self.get_persona()
        save_persona(snapshot, self._persona_path)
        return snapshot

    def get_llm_settings(self) -> dict[str, Any]:
        """面板需要的 LLM 连接状态（key 只回脱敏值，绝不回全文）。"""
        llm = self.config.llm
        return {
            "provider": llm.provider,
            "base_url": llm.base_url,
            "model": llm.model,
            "api_key_masked": mask_api_key(llm.api_key),
            "has_api_key": bool(llm.api_key),
        }

    def update_llm(self, overrides: dict[str, Any]) -> dict[str, Any]:
        """改 LLM 连接设置（api_key/base_url/model）：先验证、再持久化并热切换。

        用新配置构造一次 LLMClient 以验证完整性（openai 兼容需要 base_url + api_key），
        验证通过才写回 config 与 settings.local.json，并把已构建的客户端/机器人切到新实例，
        下一句回复即生效；验证失败则不动任何状态。
        """
        llm = self.config.llm
        candidate = LLMConfig(
            provider=str(overrides.get("provider", llm.provider)).strip() or "openai",
            base_url=str(overrides.get("base_url", llm.base_url)).strip(),
            model=str(overrides.get("model", llm.model)).strip(),
            api_key=str(overrides.get("api_key", llm.api_key)).strip(),
            temperature=llm.temperature,
            max_tokens=llm.max_tokens,
            timeout_seconds=llm.timeout_seconds,
            system_prompt=llm.system_prompt,
        )
        try:
            new_llm = LLMClient(candidate)
        except LLMError as exc:
            return {"ok": False, "error": str(exc)}

        llm.provider = candidate.provider
        llm.base_url = candidate.base_url
        llm.model = candidate.model
        llm.api_key = candidate.api_key
        save_llm_settings(
            {"provider": llm.provider, "base_url": llm.base_url,
             "model": llm.model, "api_key": llm.api_key},
            self._settings_path,
        )

        self._llm = new_llm
        if self._bot is not None:
            self._bot.llm = new_llm
        return {"ok": True, "llm": self.get_llm_settings()}

    def test_llm(self) -> dict[str, Any]:
        """发一个极小的真实请求验证当前 key/地址是否可用。"""
        if self._llm is None:
            try:
                self._llm = LLMClient(self.config.llm)
            except LLMError as exc:
                return {"ok": False, "error": str(exc)}
        err = self._llm.test()
        return {"ok": not err, "error": err}