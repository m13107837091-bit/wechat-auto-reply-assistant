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

import logging
import os
import queue
import time
from dataclasses import dataclass, field
from typing import Any, Sequence, TypeGuard

log = logging.getLogger(__name__)

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

    def __init__(self, include_group: bool = True) -> None:
        self._wx: Any = None
        self._queue: "queue.Queue[IncomingMessage]" = queue.Queue()
        self._listening = False
        self._include_group = include_group  # 是否读取群聊消息；由 main 按 reply.group.enabled 决定

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

    @staticmethod
    def _clear_watermark(wx: Any) -> None:
        """删除监听器的「水位」文件，让每次启动都从当前最新消息开始。

        wechatauto 的监听器会把已处理到哪条消息（sort_seq 水位）落盘复用，
        于是上次退出后、本次启动前对方发来的旧消息，会被当成「新消息」重放，
        造成「一启动、对方明明没发消息，机器人却自动回一条」的现象。启动前删掉
        该文件（``workdir/listener_watermark.json``），注册监听时水位即回落到
        「最新一条」，历史消息一律不回放。删不掉（路径异常/无权限）就静默忽略，
        不影响启动。
        """
        try:
            wf = os.path.join(wx._db.workdir, "listener_watermark.json")
            if os.path.exists(wf):
                os.remove(wf)
        except Exception:  # noqa: BLE001 - 删除失败不阻断启动
            pass

    def _prune_group_sessions(self, wx: Any) -> None:
        """把群聊会话从监听器的回调表里摘掉，减轻每轮轮询的负担。

        wechatauto 的全局监听会把**所有**会话（好友 + 群 + 文件传输助手…）
        都注册进 ``DBListener._callbacks``，轮询线程每轮会逐个会话查一次新消息
        （打开 SQLCipher 连接 → 查询 → 关闭）。好友 + 群加在一起会话越多、
        每轮越久，新消息被拾取就越慢——这正是「排队等待小、但消息出现慢」的
        来源。机器人默认不回群，把群会话（username 以 ``@chatroom`` 结尾）
        从回调表摘掉，可显著缩短每轮轮询耗时，让好友消息更快被读到。
        ``discover=False`` 时这些会话不会被自动加回来。
        """
        if self._include_group:
            return
        listener = getattr(wx, "_listener", None)
        callbacks = getattr(listener, "_callbacks", None)
        if not isinstance(callbacks, dict):
            return
        removed = 0
        for user in list(callbacks.keys()):
            if user.endswith("@chatroom"):
                callbacks.pop(user, None)
                removed += 1
        if removed:
            log.info(
                "[读取] 已摘除 %d 个群聊会话的监听（只轮询好友），剩余 %d 个会话",
                removed, len(callbacks),
            )

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

        # 关键：先清水位再挂监听，否则会把退出期间的旧消息当成新消息回放。
        self._clear_watermark(wx)

        def _on_message(msg: Any, chat: Any) -> None:
            incoming = self._to_incoming(chat, msg)
            if self._accept(incoming):
                self._queue.put(incoming)

        try:
            wx.AddListenAll(_on_message, discover=False)
            self._prune_group_sessions(wx)
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

    def _accept(self, incoming: IncomingMessage | None) -> TypeGuard[IncomingMessage]:
        """决定一条消息是否进入主循环。

        群聊默认丢弃（除非开了 ``include_group``），因为机器人默认只回私聊；
        把群消息挡在读侧，就不会在主循环里刷出「读取群聊→无需回复」的无用日志，
        也避免海量群消息挤占队列、拖慢真正需要回的好友消息。
        """
        if incoming is None:
            return False
        if not self._include_group and incoming.is_group:
            return False
        return True

    def send(self, chat: str, text: str, at: str | None = None) -> None:
        wx = self._get_wx()
        try:
            resp = wx.SendMsg(msg=text, who=chat, at=at) if at else wx.SendMsg(msg=text, who=chat)
        except Exception as exc:  # noqa: BLE001
            raise WeChatNotReadyError(f"发送消息失败: {exc}") from exc
        # SendMsg 返回 WxResponse：异常之外的“发送失败”也一并抛出，避免静默丢失回复。
        if resp is not None and not resp:
            raise WeChatNotReadyError(f"发送消息失败: {resp}")