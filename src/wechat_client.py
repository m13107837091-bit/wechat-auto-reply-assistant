"""微信接入：wechatauto 后端（Windows PC 微信 4.x）。

wechatauto-replica（``import wechatauto``）是专为微信 4.x 设计的接入库：
读取新消息走本地消息库（SQLCipher 解密读取），发送走 UIA + 坐标/OCR 混合，
不再依赖微信 3.9.x 的 UIA 控件树（微信 4.x 已移除，导致旧 wxauto 失效）。

全局监听由 ``WeChat.AddListenAll`` 挂回调，回调跑在库内的独立工作线程里；
本模块把回调产出的消息统一映射为 :class:`IncomingMessage` 放入线程安全队列，
``main`` 主循环轮询队列即可。所有收发逻辑都做了懒加载 + 防御式取值，
便于在无微信环境对其它模块做单测。
"""
from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

# 不参与自动回复的消息属性：自己发的、系统提示（在接收侧过滤，杜绝“自己回自己”的死循环）。
SKIPPED_ATTRS = frozenset({"self", "system"})
# 只回文本（含引用）；图片 / 语音 / 视频 / 文件 / 链接 / 表情 / 位置 / 名片等一律忽略。
REPLYABLE_TYPES = frozenset({"text", "quote"})


class WeChatNotReadyError(RuntimeError):
    """PC 微信未登录 / 未运行 / 未装 wechatauto-replica / 微信版本不匹配。"""


@dataclass
class IncomingMessage:
    content: str
    chat: str
    is_group: bool
    sender: str = ""
    at_list: Sequence[str] = field(default_factory=list)
    received_at: float = 0.0  # 监听线程入队时刻，用于观测「消息到达 → 被处理」的读取延迟


class WeChatClient:
    """极薄 wechatauto 封装：准备、拉新消息、发消息。"""

    def __init__(self) -> None:
        self._wx: Any = None
        self._queue: "queue.Queue[IncomingMessage]" = queue.Queue()
        self._listening = False

    def _get_wx(self) -> Any:
        if self._wx is None:
            try:
                import wechatauto  # 懒加载
            except ImportError as exc:
                raise WeChatNotReadyError(
                    "未安装 wechatauto-replica，请先执行 pip install -r requirements.txt"
                ) from exc
            try:
                self._wx = wechatauto.WeChat()
            except Exception as exc:  # noqa: BLE001
                raise WeChatNotReadyError(
                    f"连接 PC 微信失败（请确认微信已登录、窗口可用、版本与 wechatauto-replica 匹配）: {exc}"
                ) from exc
        return self._wx

    def prepare(self) -> None:
        """校验微信可连接，并开启全局监听。"""
        self._get_wx()
        self._listen_all()

    def _listen_all(self) -> None:
        if self._listening:
            return
        wx = self._wx

        # 监听轮询间隔默认 1s；拉低到 0.5s，让新消息更快被拾取、回复更及时。
        # 需在 AddListenAll 内部创建 db.Listener 之前设置才生效。
        try:
            from wechatauto.param import WxParam

            WxParam.LISTEN_INTERVAL = 0.5
        except Exception:  # noqa: BLE001 - 版本差异时回退默认值，不影响启动
            pass

        def _on_message(msg: Any, chat: Any) -> None:
            incoming = self._to_incoming(chat, msg)
            if incoming is not None:
                self._queue.put(incoming)

        try:
            wx.AddListenAll(_on_message, discover=True)
            wx.StartListening()
        except Exception:  # noqa: BLE001 - 监听失败不阻断启动
            return
        self._listening = True

    def get_new_messages(self) -> list[IncomingMessage]:
        self._get_wx()
        if not self._listening:
            self._listen_all()
        out: list[IncomingMessage] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    @staticmethod
    def _to_incoming(chat: Any, msg: Any) -> IncomingMessage | None:
        """把 wechatauto 的一条消息映射为 IncomingMessage；不可回复时返回 None。

        ``chat`` 为会话对象（全局监听回调里的 ``_AllMessageChat``），带
        ``.who``（会话 username，群聊以 ``@chatroom`` 结尾）与 ``.nickname``
        （显示名）。为便于单测，同样接受带这两个字段的任意对象。
        """
        attr = str(getattr(msg, "attr", "") or "").lower()
        if attr in SKIPPED_ATTRS:
            return None
        mtype = str(getattr(msg, "type", "") or "").lower()
        if mtype not in REPLYABLE_TYPES:
            return None
        content = str(getattr(msg, "content", "") or "")
        if not content.strip():
            return None
        username = str(getattr(chat, "who", None) or getattr(chat, "_wxid", "") or "")
        is_group = username.endswith("@chatroom")
        nickname = str(getattr(chat, "nickname", None) or username or "")
        sender = str(getattr(msg, "sender", "") or "")
        at_list = getattr(msg, "at_list", None) or []
        if isinstance(at_list, str):
            at_list = [at_list]
        return IncomingMessage(
            content=content,
            chat=nickname,
            is_group=is_group,
            sender=sender,
            at_list=list(at_list),
            received_at=time.time(),
        )

    def send(self, chat: str, text: str, at: str | None = None) -> None:
        wx = self._get_wx()
        try:
            resp = wx.SendMsg(msg=text, who=chat, at=at) if at else wx.SendMsg(msg=text, who=chat)
        except Exception as exc:  # noqa: BLE001
            raise WeChatNotReadyError(f"发送消息失败: {exc}") from exc
        # SendMsg 返回 WxResponse：异常之外的“发送失败”也一并抛出，避免静默丢失回复。
        if resp is not None and not resp:
            raise WeChatNotReadyError(f"发送消息失败: {resp}")