"""对 wechat_client 的消息解析与类型过滤做单元测试（不依赖真实微信）。

注意：本模块接入库已由 wxauto（微信 3.9.x）切换为 wechatauto（微信 4.x）。
消息模型随之变化：``attr``（self/friend/system）表示方向，``type``
（text/quote/image/...）表示内容类型；群聊由会话 username 的 ``@chatroom``
后缀判定，不再由 ``type='group'`` 标识。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.wechat_client import REPLYABLE_TYPES, SKIPPED_ATTRS, IncomingMessage, WeChatClient


def _msg(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _chat(username: str, nickname: str = "") -> SimpleNamespace:
    return SimpleNamespace(who=username, nickname=nickname, _wxid=username)


def test_friend_message_parsed():
    chat = _chat("wxid_abc", "小明")
    m = WeChatClient._to_incoming(chat, _msg(content="你好", type="text", attr="friend", sender="小明"))
    assert m is not None
    assert m.chat == "小明"
    assert m.content == "你好"
    assert m.is_group is False
    assert m.sender == "小明"


def test_group_message_parsed():
    chat = _chat("1234567890@chatroom", "某群")
    m = WeChatClient._to_incoming(chat, _msg(content="hi", type="text", attr="friend", sender="张三"))
    assert m is not None
    assert m.is_group is True
    assert m.sender == "张三"


def test_quote_message_replyable():
    chat = _chat("wxid_abc", "小明")
    m = WeChatClient._to_incoming(chat, _msg(content="在吗", type="quote", attr="friend"))
    assert m is not None
    assert m.content == "在吗"


def test_self_message_ignored():
    chat = _chat("wxid_abc", "小明")
    assert WeChatClient._to_incoming(chat, _msg(content="hi", type="text", attr="self")) is None


def test_system_message_ignored():
    chat = _chat("wxid_abc", "小明")
    # 系统消息：attr='system'，或 DB 路径下被覆写为 friend 但 type='base'
    assert WeChatClient._to_incoming(chat, _msg(content="已添加对方", type="base", attr="system")) is None
    assert WeChatClient._to_incoming(chat, _msg(content="你已添加对方", type="base", attr="friend")) is None


def test_media_types_ignored():
    chat = _chat("wxid_abc", "小明")
    for t in ("voice", "image", "emotion", "video", "file", "link", "location", "personal_card", "other"):
        assert WeChatClient._to_incoming(chat, _msg(content="", type=t, attr="friend")) is None


def test_empty_content_ignored():
    chat = _chat("wxid_abc", "小明")
    assert WeChatClient._to_incoming(chat, _msg(content="   ", type="text", attr="friend")) is None


def test_attr_type_case_insensitive():
    chat = _chat("wxid_abc", "小明")
    assert WeChatClient._to_incoming(chat, _msg(content="hi", type="text", attr="Self")) is None
    assert WeChatClient._to_incoming(chat, _msg(content="hi", type="Text", attr="Friend")) is not None


def test_group_detection_by_chatroom_suffix():
    m = WeChatClient._to_incoming(_chat("wxid_abc", "小明"), _msg(content="hi", type="text", attr="friend"))
    assert m is not None and m.is_group is False
    g = WeChatClient._to_incoming(_chat("999@chatroom", "某群"), _msg(content="hi", type="text", attr="friend"))
    assert g is not None and g.is_group is True


def test_nickname_falls_back_to_username():
    chat = _chat("wxid_abc")  # 无昵称时用 username 兜底
    m = WeChatClient._to_incoming(chat, _msg(content="hi", type="text", attr="friend"))
    assert m is not None
    assert m.chat == "wxid_abc"


def test_group_filtered_when_include_group_false():
    c = WeChatClient(include_group=False)
    assert c._accept(IncomingMessage(content="hi", chat="某群", is_group=True)) is False
    assert c._accept(IncomingMessage(content="hi", chat="小明", is_group=False)) is True
    assert c._accept(None) is False


def test_group_kept_when_include_group_true():
    c = WeChatClient(include_group=True)
    assert c._accept(IncomingMessage(content="hi", chat="某群", is_group=True)) is True


def test_constants():
    assert "self" in SKIPPED_ATTRS
    assert "system" in SKIPPED_ATTRS
    assert "text" in REPLYABLE_TYPES
    assert "quote" in REPLYABLE_TYPES
    assert "image" not in REPLYABLE_TYPES