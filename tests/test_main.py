"""对会话级消息合并（_coalesce，现已迁至 controller）做单元测试（不依赖真实微信）。"""
from __future__ import annotations

from src.controller import _coalesce
from src.wechat_client import IncomingMessage


def test_coalesce_merges_same_chat_keeps_order():
    msgs = [
        IncomingMessage(content="在吗", chat="小明", is_group=False),
        IncomingMessage(content="有个急事", chat="小明", is_group=False),
        IncomingMessage(content="hi", chat="小红", is_group=False),
    ]
    out = _coalesce(msgs)
    assert len(out) == 2
    assert out[0].chat == "小明" and out[0].content == "在吗\n有个急事"
    assert out[1].chat == "小红" and out[1].content == "hi"


def test_coalesce_keeps_group_and_friend_distinct():
    msgs = [
        IncomingMessage(content="a", chat="小明", is_group=False),
        IncomingMessage(content="b", chat="某群", is_group=True, sender="张三"),
    ]
    out = _coalesce(msgs)
    assert len(out) == 2


def test_coalesce_single_message_unchanged():
    msgs = [IncomingMessage(content="你好", chat="小明", is_group=False)]
    out = _coalesce(msgs)
    assert len(out) == 1
    assert out[0].content == "你好"