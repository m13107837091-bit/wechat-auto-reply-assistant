"""微信接入：wxauto 后端（Windows PC 微信）。

采用懒加载 + 防御式取值，便于在无微信环境对其它模块做单测。
wxauto 的 API 随微信版本演进较快，收发逻辑都做了容错；
若你的微信版本与 wxauto 不匹配，请按 README 调整微信或 wxauto 版本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


class WeChatNotReadyError(RuntimeError):
    """PC 微信未登录 / 未运行 / 未装 wxauto / 微信版本不匹配。"""


@dataclass
class IncomingMessage:
    content: str
    chat: str
    is_group: bool
    sender: str = ""
    at_list: Sequence[str] = field(default_factory=list)


class WeChatClient:
    """极薄 wxauto 封装：准备、拉新消息、发消息。"""

    def __init__(self) -> None:
        self._wx: Any = None

    def _get_wx(self) -> Any:
        if self._wx is None:
            try:
                from wxauto import WeChat  # 懒加载
            except ImportError as exc:
                raise WeChatNotReadyError("未安装 wxauto，请先执行 pip install -r requirements.txt") from exc
            try:
                self._wx = WeChat()
            except Exception as exc:  # noqa: BLE001
                raise WeChatNotReadyError(
                    f"连接 PC 微信失败（请确认微信已登录、窗口可用、版本与 wxauto 匹配）: {exc}"
                ) from exc
        return self._wx

    def prepare(self) -> None:
        """校验微信可连接，并尽可能监听所有会话。"""
        self._get_wx()
        self._listen_all()

    def _listen_all(self) -> None:
        wx = self._wx
        try:
            sessions = wx.GetSessionList() or []
        except Exception:  # noqa: BLE001 - 监听失败不阻断启动
            return
        if not hasattr(wx, "AddListenChat"):
            return
        for sess in sessions:
            name = getattr(sess, "nickname", None)
            if name is None:
                name = str(sess)
            if not name:
                continue
            try:
                wx.AddListenChat(who=str(name))
            except Exception:  # noqa: BLE001
                continue

    def get_new_messages(self) -> list[IncomingMessage]:
        wx = self._get_wx()
        try:
            raw = wx.GetListenMessage()
        except Exception as exc:  # noqa: BLE001
            raise WeChatNotReadyError(f"获取消息失败: {exc}") from exc

        out: list[IncomingMessage] = []
        if isinstance(raw, dict):
            for chat, msgs in raw.items():
                if isinstance(msgs, (list, tuple)):
                    for msg in msgs:
                        out.append(self._to_incoming(str(chat), msg))
        return out

    @staticmethod
    def _to_incoming(chat: str, msg: Any) -> IncomingMessage:
        content = str(getattr(msg, "content", "") or "")
        mtype = str(getattr(msg, "type", "") or "")
        is_group = mtype == "group"
        sender = str(getattr(msg, "sender", "") or "")
        at_list = getattr(msg, "at_list", None) or []
        if isinstance(at_list, str):
            at_list = [at_list]
        return IncomingMessage(
            content=content,
            chat=chat,
            is_group=is_group,
            sender=sender,
            at_list=list(at_list),
        )

    def send(self, chat: str, text: str, at: str | None = None) -> None:
        wx = self._get_wx()
        try:
            if at:
                wx.SendMsg(msg=text, who=chat, at=at)
            else:
                wx.SendMsg(msg=text, who=chat)
        except Exception as exc:  # noqa: BLE001
            raise WeChatNotReadyError(f"发送消息失败: {exc}") from exc