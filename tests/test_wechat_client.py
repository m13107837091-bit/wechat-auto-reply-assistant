"""对 wechat_client 的消息解析与类型过滤做单元测试（不依赖真实微信）。"""
from __future__ import annotations

from types import SimpleNamespace

from src.wechat_client import IGNORED_TYPES, WeChatClient


def _msg(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def test_friend_message_parsed():
    m = WeChatClient._to_incoming("小明", _msg(content="你好", type="friend", sender="小明"))
    assert m is not None
    assert m.chat == "小明"
    assert m.content == "你好"
    assert m.is_group is False


def test_group_message_parsed():
    m = WeChatClient._to_incoming("某群", _msg(content="hi", type="group", sender="张三", at_list=["我"]))
    assert m is not None
    assert m.is_group is True
    assert m.sender == "张三"
    assert m.at_list == ["我"]


def test_self_message_ignored():
    assert WeChatClient._to_incoming("小明", _msg(content="hi", type="self")) is None


def test_system_message_ignored():
    assert WeChatClient._to_incoming("小明", _msg(content="你已添加对方", type="sys")) is None


def test_media_types_ignored():
    for t in ("voice", "pic", "image", "video", "file", "share", "sticker"):
        assert WeChatClient._to_incoming("小明", _msg(content="", type=t)) is None


def test_type_case_insensitive():
    # type 统一取小写判断，大小写不敏感
    assert WeChatClient._to_incoming("小明", _msg(content="hi", type="Self")) is None
    assert WeChatClient._to_incoming("小明", _msg(content="hi", type="Friend")) is not None


def test_ignored_types_constant():
    assert "self" in IGNORED_TYPES
    assert "group" not in IGNORED_TYPES
    assert "friend" not in IGNORED_TYPES