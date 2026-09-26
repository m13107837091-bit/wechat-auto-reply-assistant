"""对 BotController 的启停状态机与改人设做单元测试（注入假依赖，不碰真实微信/LLM）。"""
from __future__ import annotations

import json
import time

from src.config import AppConfig
from src.controller import BotController
from src.wechat_client import IncomingMessage


class _FakeLLM:
    pass


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def on_message(self, chat, content, is_group, sender, at_list):
        return f"回复:{content}"


class _FakeWeChat:
    """模拟 WeChatClient 的最小接口：prepare/get_new_messages/send/discard_pending。"""

    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []
        self.sent: list[tuple[str, str]] = []
        self.discarded = 0
        self.prepared = 0

    def prepare(self) -> None:
        self.prepared += 1

    def get_new_messages(self) -> list[IncomingMessage]:
        out = self.messages
        self.messages = []
        return out

    def send(self, chat: str, text: str) -> None:
        self.sent.append((chat, text))

    def discard_pending(self) -> None:
        self.discarded += 1


def _make_controller(wechat: _FakeWeChat, persona_path=None) -> tuple[BotController, _FakeWeChat]:
    c = BotController(
        config=AppConfig(),
        llm=_FakeLLM(),
        bot=_FakeBot(),
        wechat=wechat,
        persona_path=persona_path,
    )
    return c, wechat


def test_start_sets_running_and_prepares():
    wc = _FakeWeChat()
    c, wc = _make_controller(wc)
    assert c.start() is True
    assert c._running is True
    assert c.snapshot()["running"] is True
    assert wc.prepared == 1
    assert wc.discarded == 1  # start 时清一次积压
    c.stop()


def test_start_idempotent_does_not_re_discard():
    wc = _FakeWeChat()
    c, wc = _make_controller(wc)
    c.start()
    assert c.start() is True  # 已运行，直接返回
    assert wc.discarded == 1
    c.stop()


def test_stop_pauses_and_resume_discards():
    wc = _FakeWeChat()
    c, wc = _make_controller(wc)
    c.start()
    c.stop()
    assert c._running is False
    assert c.snapshot()["running"] is False
    c.start()  # 恢复：再清一次积压
    assert wc.discarded == 2
    assert c._running is True
    c.stop()


def test_message_is_processed_while_running():
    wc = _FakeWeChat()
    c, _ = _make_controller(wc)
    c.start()
    wc.messages.append(IncomingMessage(content="你好", chat="小明", is_group=False))
    deadline = time.time() + 2
    while time.time() < deadline and not wc.sent:
        time.sleep(0.02)
    c.stop()
    assert ("小明", "回复:你好") in wc.sent


def test_update_persona_mutates_in_place_and_persists(tmp_path):
    p = tmp_path / "persona.json"
    c, _ = _make_controller(_FakeWeChat(), persona_path=p)
    updated = c.update_persona({"name": "小刚", "emoji": False, "system_prompt": "你好呀"})
    assert updated["name"] == "小刚"
    assert updated["emoji"] is False
    assert updated["system_prompt"] == "你好呀"
    assert c.config.persona.name == "小刚"  # 原地改，下一句即生效
    assert c.config.llm.system_prompt == "你好呀"
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["name"] == "小刚"
    assert saved["system_prompt"] == "你好呀"


def test_update_persona_ignores_bad_length(tmp_path):
    p = tmp_path / "persona.json"
    c, _ = _make_controller(_FakeWeChat(), persona_path=p)
    c.update_persona({"length": "不存在的档位"})
    assert c.config.persona.length == "auto"  # 非法值不生效，保持默认


def test_snapshot_reports_last_error(tmp_path):
    p = tmp_path / "persona.json"
    c, _ = _make_controller(_FakeWeChat(), persona_path=p)
    c.last_error = "未连接微信"
    assert c.snapshot()["last_error"] == "未连接微信"