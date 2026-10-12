"""微信接入：wechatauto 后端（Windows PC 微信 4.x）。

wechatauto-replica（``import wechatauto``）是专为微信 4.x 设计的接入库：
读取新消息走本地消息库（SQLCipher 解密读取），发送走 UIA + 坐标/OCR 混合。

**读取这一侧刻意不用库自带的 ``AddListenAll``**。它会给每个会话注册一个监听对象，
轮询线程每轮逐个会话查一次新消息，而每次查询都要把每个消息分片库各打开一次
（实测 ~82ms/会话）。本机 500 个会话 → 一轮 40 秒上下，新消息的发现延迟就在
0～40 秒之间浮动：会话表按"最近有往来"排序，正在聊天的人排最前面、几秒就轮到，
好久没联系的人排几百名后、要等整轮走完。这就是"有时候很快、有时候很久"的来源。

这里改成**会话表差分**：每轮先用 ``get_sessions()``（一次查询 ~20ms）看哪些会话有
新动静（最新消息时间/未读数/摘要变了），只对这几个会话去读消息。一轮通常在
0.1～0.5 秒，发现延迟降到 1 秒内，同时省掉原来每轮 2000 次开库。

群聊默认在读取侧就丢弃（``reply.group.enabled=false``），连查都不查，省一次开库。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Sequence, TypeGuard

from src.log_rotate import APP_LOG_BACKUPS, enable_app_log_rotation

log = logging.getLogger(__name__)

# 只回文本（含引用）；图片 / 语音 / 视频 / 文件 / 链接 / 表情 / 位置 / 名片等一律忽略。
#
# ⚠️ 消息库里的 type 是 wechatauto 的**中文显示名**（「文本」「动画表情」「系统消息」…，
# 见 wechatauto.db.MSG_TYPE_NAMES），不是 text / quote 这种英文枚举。
# 2026-10-05 三哥报「群里、私聊都不回了」根因就在这里：拿英文名去比中文名，
# **每一条消息都在这一步被静默丢掉** —— 轮询正常、日志干净、面板却一条记录都没有。
# 所以中英文名都认，另外用数字 type_code 兜底（1 = 文本）。
REPLYABLE_TYPES = frozenset({"text", "quote", "文本", "引用", "引用回复"})
REPLYABLE_TYPE_CODES = frozenset({1, 37, 105})  # 1 = 文本；37 / 105 = 引用回复


def is_replyable_type(row: dict) -> bool:
    """这条消息的类型值不值得回（只看类型，不看内容）。"""
    name = str(row.get("type") or "").strip().lower()
    if name in REPLYABLE_TYPES:
        return True
    code = row.get("type_code")
    if isinstance(code, int) and code in REPLYABLE_TYPE_CODES:
        return True
    return False
# 自己发的消息在消息库里的 sender_id（wechatauto 约定：2 = 本机账号）。
SELF_SENDER_ID = 2
# 第一次见到某个会话时往回看几条：既要拿到水位锚点，又要兜住"连发好几条"。
# 只回放「本进程启动之后」到达的消息（见 self._started_at），更早的一律当历史。
BASELINE_LOOKBACK = 10

# 微信里"不是人"的固定会话 id。个人是 wxid_*，公众号服务号是 gh_*，群是 *@chatroom，
# 企业微信是 *@openim；剩下这些是微信自己的系统号（本机实测都出现过）：
SYSTEM_SESSION_IDS = frozenset({
    "filehelper",                 # 文件传输助手（自己给自己发东西）
    "notifymessage",              # 服务通知
    "brandservicesessionholder",  # 品牌服务会话（各类服务号的聚合入口）
    "brandsessionholder",
    "@placeholder_foldgroup",     # 折叠群聊的占位会话
    "weixin",                     # 微信团队
    "newsapp", "fmessage", "medianote", "floatbottle", "qmessage",
    "qqmail", "tmessage", "officialaccounts", "mphelper", "exmail_tool",
    "feedsapp", "voip", "weibo", "facebookapp",
})


def is_person_session(user: str) -> bool:
    """这个会话是不是"个人"（而不是群 / 公众号服务号 / 企业微信 / 微信系统号）。

    判断依据来自本机真实数据：``wxid_une88h78z73822`` 是个人，
    ``gh_a19f4d5e89e5`` 是公众号服务号（中国建设银行），
    ``54415042263@chatroom`` 是群，``25984983108031557@openim`` 是企业微信。
    这样"新冒出来的服务号"不用一个个加黑名单也不会被回。
    """
    if not user:
        return False
    if user.startswith("@"):                          # @placeholder_foldgroup 这类占位
        return False
    if user.endswith("@chatroom") or user.endswith("@openim"):
        return False
    if user.startswith("gh_"):                        # 公众号 / 服务号
        return False
    return user not in SYSTEM_SESSION_IDS


class WeChatNotReadyError(RuntimeError):
    """PC 微信未登录 / 未运行 / 未装 wechatauto-replica / 微信版本不匹配。"""


@dataclass
class IncomingMessage:
    content: str
    chat: str
    is_group: bool
    sender: str = ""
    at_list: Sequence[str] = field(default_factory=list)
    chat_id: str = ""         # 会话内部 id（wxid_* / *@chatroom / gh_*），排障与类型判断用
    received_at: float = 0.0  # 轮询线程入队时刻
    sent_at: float = 0.0      # 对方发出时刻（消息库 create_time），用来算「发现延迟」


class WeChatClient:
    """极薄 wechatauto 封装：准备、拉新消息、发消息。"""

    def __init__(
        self,
        include_group: bool = True,
        poll_interval: float = 0.5,
        session_limit: int = 500,
        only_person: bool = True,
    ) -> None:
        self._wx: Any = None
        self._queue: "queue.Queue[IncomingMessage]" = queue.Queue()
        self._listening = False
        self._include_group = include_group  # 是否读取群聊消息；由 main 按 reply.group.enabled 决定
        # 只回"个人"：公众号服务号 / 企业微信 / 微信系统号一律不回（reply.friend.only_person）
        self._only_person = only_person
        self._poll_interval = max(0.05, float(poll_interval))
        self._session_limit = int(session_limit)
        # 每个会话上一轮看到的「指纹」：最新消息时间 / 未读数 / 摘要
        self._seen: dict[str, tuple[Any, ...]] = {}
        # 每个会话已处理到哪条消息（sort_seq）。0 = 还没定基线（第一次见到时才定）
        self._seq: dict[str, int] = {}
        # 本客户端开始工作的时刻。第一次见到某个会话时，只认这之后到达的消息：
        # 启动前/上次退出期间积压的旧消息一律不回放（否则一启动就替人回一串旧账）。
        self._started_at = time.time()
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._announced = False  # 第一轮成功后打一条日志，证明"收消息"这一侧活着
        # 库报发送失败后，往消息库复核的等待时长（测试里会调小）
        self._send_confirm_timeout = 5.0
        # ---------- 批次 6：心跳与丢弃计数 ----------
        # 2026-10-05 的教训：机器人「失聪」了，而面板、日志、状态全都显示正常 ——
        # 因为读取侧每一次丢消息都是 `return None`，一句话都不留。
        # 所以：每一轮轮询打心跳，每一次丢弃按原因计数，面板才分得清
        # 「没人找它」和「它聋了」。
        self._counts: dict[str, int] = {
            "poll_rounds": 0,   # 成功的轮询轮数（心跳）
            "read": 0,          # 真正交给主循环的消息条数
            "drop_self": 0,     # 自己发的
            "drop_type": 0,     # 类型不支持（图片/语音/表情/系统消息…）
            "drop_empty": 0,    # 空内容
            "drop_group_off": 0,          # 群消息，但群回复总开关关着
            "drop_group_not_allowed": 0,  # 群不在白名单（读取侧就挡掉）
            "drop_not_person": 0,         # 公众号 / 企业微信 / 微信系统号
        }
        self._last_poll_at = 0.0     # 最近一次**成功**轮询的时刻（心跳）
        self._last_poll_error = ""   # 最近一次轮询异常（以前只进日志，面板看不到）
        self._last_message_at = 0.0  # 最近一条入队消息的对方发出时间
        # 群白名单前移：名单外的群连消息库都不打开（省掉整轮的无用开库）。
        # 语义必须与 bot.reject_reason 一致：空名单 = 不限制。
        self._group_whitelist: list[str] = []
        self._match_mode = "exact"
        self._group_names: dict[str, str] = {}   # chatroom id -> 群显示名（白名单是按名字写的）
        self._group_names_at = 0.0

    # ---------- 准备 ----------

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
        """校验微信可连接，并启动会话表轮询。

        这是「能不能马上开始回消息」的前置：连不上微信就抛
        :class:`WeChatNotReadyError`。注意启动时**不再**遍历全部会话的消息库
        ——那正是过去启动要干等 60 秒的根源；现在只打开会话表，
        大分片库留到真有新消息时按需打开。
        """
        t0 = time.perf_counter()
        self._get_wx()
        t1 = time.perf_counter()
        self._listen_all()
        t2 = time.perf_counter()
        log.info(
            "[启动] 连接微信窗口 %.1fs + 起轮询 %.1fs = 合计 %.1fs",
            t1 - t0, t2 - t1, t2 - t0,
        )
        # 此刻第三方库一定已经往根 logger 上挂了日志文件；顺手把它换成 1MB 轮转版，
        # 否则无人值守跑久了 app_YYYYMMDD.log 会一直涨（详见 src/log_rotate.py）。
        rotated = enable_app_log_rotation()
        if rotated:
            log.info("[日志] app 日志已启用 1MB 轮转（留 %d 份）：%s", APP_LOG_BACKUPS, rotated)

    def stop(self) -> None:
        """停轮询（进程退出/测试收尾用；日常暂停回复不需要停它）。"""
        self._poll_stop.set()
        thread = self._poll_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self._poll_thread = None
        self._listening = False

    # ---------- 读取：会话表差分轮询 ----------

    def _listen_all(self) -> None:
        """启动后台轮询线程（替代 wechatauto 的 AddListenAll）。"""
        if self._listening:
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="wx-session-poll", daemon=True
        )
        self._poll_thread.start()
        self._listening = True

    def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            try:
                self._poll_once()
                # 心跳：**成功**跑完一轮才更新（卡住时面板一眼看得出来）
                self._last_poll_at = time.time()
                self._counts["poll_rounds"] += 1
            except Exception as exc:  # noqa: BLE001 - 单轮出错不能弄死监听
                # 以前这里只写日志：连续失败十轮，面板也只会显示「运行中」（批次 6 补上）
                self._last_poll_error = str(exc)
                log.warning("[读取] 轮询出错（跳过本轮）: %s", exc)
            self._poll_stop.wait(self._poll_interval)

    def _poll_once(self) -> None:
        """一轮：看一次会话表，只读「有变化」的那几个会话。"""
        started = time.perf_counter()
        db = self._get_wx()._db
        sessions = db.get_sessions(limit=self._session_limit) or []
        for row in sessions:
            user = str(row.get("username") or "")
            if not user:
                continue
            sig = (row.get("last_time"), row.get("unread"), row.get("summary"))
            if self._seen.get(user) == sig:
                continue
            if user not in self._seen:
                # 第一次见到这个会话：**只登记指纹，不去读它的消息库**。
                # 500 个会话挨个读一遍就是原来那 40 秒（哪怕挪到后台也照样堵住新消息）；
                # 它一有新动静，指纹就会变，那时再读。
                self._seen[user] = sig
                continue
            self._seen[user] = sig
            # 不是"个人"的会话（群 / 公众号服务号 / 企业微信 / 系统号）在读取侧就丢：
            # 连消息库都不打开，省一次开库。
            if not self._should_read(user, db):
                continue
            self._read_new(db, user)

        if not self._announced and self._seen:
            # 只在第一轮成功后说一次：日志里能看到「收消息这一侧确实活着、一轮多快」。
            self._announced = True
            log.info(
                "[读取] 会话表轮询已启动：%d 个会话，一轮约 %.2fs（发现延迟 ≈ %.1fs）",
                len(self._seen), time.perf_counter() - started, self._poll_interval + 0.1,
            )

    def _should_read(self, user: str, db: Any = None) -> bool:
        """这个会话值不值得去读它的消息库。

        群聊：总开关 ``reply.group.enabled`` + 白名单（批次 6 前移到读取侧，
        名单外的群连消息库都不打开）；公众号服务号 / 企业微信 / 微信系统号
        一律不读 —— 机器人是替你回「人」的，这些不是人。

        ``db`` 可以为 None（老调用/单测）：那时只按总开关判断，不查群名。
        """
        if user.endswith("@chatroom"):
            if not self._include_group:
                self._drop("group_off")
                return False
            if db is not None and not self._group_allowed(db, user):
                self._drop("group_not_allowed")
                return False
            return True
        if not self._only_person:
            return True
        if not is_person_session(user):
            self._drop("not_person")
            return False
        return True

    def set_only_person(self, value: bool) -> None:
        """面板改了"只回个人"开关后立即生效（下一轮轮询就按新值走）。"""
        self._only_person = bool(value)

    def set_include_group(self, value: bool) -> None:
        """面板改了"回复群聊"总开关后立即生效（关掉就不再读群消息库）。"""
        self._include_group = bool(value)

    def set_group_whitelist(self, names: Sequence[str], match_mode: str = "exact") -> None:
        """面板改了群白名单后立即生效：下一轮轮询就跳过名单外的群。

        空名单 = 不限制（与 ``Bot.reject_reason`` 同一套语义，别在这里搞出第二套）。
        """
        self._group_whitelist = [str(n).strip() for n in (names or []) if str(n).strip()]
        self._match_mode = "contains" if match_mode == "contains" else "exact"

    def _group_allowed(self, db: Any, user: str) -> bool:
        """这个群值不值得读（空名单 = 全都读）。"""
        if not self._group_whitelist:
            return True
        name = self._group_name(db, user)
        if not name:
            # 拿不到群名就**放行**：宁可多读一个群，也不能因为查不到名字漏掉该回的群。
            return True
        if self._match_mode == "contains":
            return any(p in name for p in self._group_whitelist)
        return name in self._group_whitelist

    def _group_name(self, db: Any, user: str) -> str:
        """chatroom id → 群显示名（缓存 5 分钟）。

        用 ``db.get_groups()`` 拿全量映射：本机实测 91 个群。
        注意 ``db.group_id_to_name(chatroom_wxid)`` 是要单个 id 当参数的，
        拿不到全量 —— 这一点第一次写错时，过滤整个静默失效了（面板 count 全 0），
        所以 batch 6 把「过滤是否真的生效」也露到面板（见 stats.group_filter）。
        """
        now = time.time()
        if not self._group_names or now - self._group_names_at > 300:
            try:
                rows = db.get_groups() or []
            except Exception as exc:  # noqa: BLE001
                log.warning("[读取] 群名映射拿不到（本轮不过滤群）: %s", exc)
                return ""
            names: dict[str, str] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                uid = str(row.get("username") or "")
                if uid:
                    names[uid] = str(row.get("name") or "")
            if not names:
                return ""   # 一个群名都没拿到 → 放行（宁可多读）
            self._group_names = names
            self._group_names_at = now
        return self._group_names.get(user, "")

    def _drop(self, reason: str) -> None:
        """丢弃计数：读取侧每一个 ``return None`` 都要在这里留一笔。"""
        key = "drop_" + reason
        if key in self._counts:
            self._counts[key] += 1

    def poll_alive(self, stale_after: float = 30.0) -> bool:
        """轮询线程活着、且心跳不算旧（默认 30 秒没动静就算卡住）。"""
        thread = self._poll_thread
        if thread is None or not thread.is_alive() or not self._listening:
            return False
        if not self._last_poll_at:
            return False
        return (time.time() - self._last_poll_at) <= max(5.0, float(stale_after))

    def stats(self) -> dict[str, Any]:
        """读取侧心跳与丢弃计数（面板「收消息」那一行 + 卡死自救的判断依据）。"""
        now = time.time()
        return {
            "listening": self._listening,
            "poll_alive": self.poll_alive(),
            "last_poll_ago": round(now - self._last_poll_at, 1) if self._last_poll_at else None,
            "last_message_ago": (round(now - self._last_message_at, 1)
                                 if self._last_message_at else None),
            "last_poll_error": self._last_poll_error,
            "seen_chats": len(self._seen),
            # 群白名单前移是否**真的**在生效：名单数 + 已知群名数。
            # 第一次写错时它静默不生效（drop 计数全 0、别的群照样读），有这行才看得见。
            "group_filter": {"whitelist": len(self._group_whitelist),
                             "names_known": len(self._group_names)},
            "waiting_in_queue": self._queue.qsize(),
            "counts": dict(self._counts),
        }

    def _read_new(self, db: Any, user: str) -> None:
        """读某个会话的新消息并入队（第一次读它时先锚定水位）。"""
        if user not in self._seq:
            # 第一次读这个会话时**不猜水位**：猜小了会把历史消息当新消息回一遍。
            # 取最近几条当锚点，只接受"本进程启动之后"到达的那些。
            rows = self._safe(db.get_messages, user, limit=BASELINE_LOOKBACK) or []
            if not rows:
                return
            rows = sorted(rows, key=lambda r: r.get("sort_seq") or 0)
            self._seq[user] = int(rows[-1].get("sort_seq") or 0)
            # 减 1 秒容差：create_time 是整秒，边界上那条别误判成历史。
            cutoff = self._started_at - 1.0
            for row in rows:
                if float(row.get("create_time") or 0) >= cutoff:
                    self._emit(db, user, row)
            return

        since = self._seq[user]
        rows = self._safe(db.get_new_messages, user, since) or []
        for row in rows:
            seq = int(row.get("sort_seq") or 0)
            if seq <= since:
                continue
            self._emit(db, user, row)
            since = seq
        if rows:
            self._seq[user] = since

    @staticmethod
    def _safe(fn: Any, *args: Any, **kwargs: Any) -> Any:
        """单次查询失败（库损坏/正在 checkpoint）只跳过这一条，不弄死整轮。"""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("[读取] 查询失败（跳过）: %s", exc)
            return None

    def _emit(self, db: Any, user: str, row: dict) -> None:
        incoming = self._row_to_incoming(db, user, row)
        if self._accept(incoming):
            self._counts["read"] += 1
            self._last_message_at = max(
                self._last_message_at, float(row.get("create_time") or 0)
            )
            self._queue.put(incoming)

    def _row_to_incoming(self, db: Any, user: str, row: dict) -> IncomingMessage | None:
        """把消息库里的一条记录映射为 IncomingMessage；不可回复时返回 None。"""
        sender_id = row.get("sender_id")
        if sender_id in (SELF_SENDER_ID, str(SELF_SENDER_ID)):
            self._drop("self")
            return None  # 自己发的：绝不回自己（旧回调路径靠 attr=self 挡）
        if not is_replyable_type(row):
            self._drop("type")
            return None
        content = str(row.get("content") or "")
        if not content.strip():
            self._drop("empty")
            return None
        is_group = user.endswith("@chatroom")
        sender_username = str(row.get("sender_username") or "")
        if not sender_username and sender_id not in (None, ""):
            sender_username = str(sender_id)
        if is_group:
            # 群消息的 content 自带「<发送者 wxid>: 正文」前缀（wechatauto 的解析结果）。
            # 前缀要剥掉、wxid 要换成备注名 —— 大模型得知道是谁在说话，
            # 直接给它一串 wxid 它只会发懵，而且前缀会污染关键词判断。
            prefix = sender_username + ":"
            if sender_username and content.startswith(prefix):
                content = content[len(prefix):].lstrip()
            sender = self._display_name(db, sender_username) if sender_username else ""
        else:
            sender = sender_username
        if not content.strip():
            self._drop("empty")
            return None
        return IncomingMessage(
            content=content,
            chat=self._display_name(db, user),
            chat_id=user,
            is_group=is_group,
            sender=sender,
            # 消息库里没有 @ 列表；群聊靠 group_trigger_keywords / group_only_when_mentioned 控制。
            at_list=[],
            received_at=time.time(),
            sent_at=float(row.get("create_time") or 0),
        )

    @staticmethod
    def _display_name(db: Any, user: str) -> str:
        """会话 username → 显示名（备注/昵称）。查不到就退回 username。"""
        try:
            nicks = db.nickname_map()
            return str(nicks.get(user) or user)
        except Exception:  # noqa: BLE001
            return user

    # ---------- 消息队列 ----------

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

    def discard_pending(self) -> None:
        """清空已读到但尚未处理的消息队列。

        暂停自动回复期间轮询仍在跑、仍会入队；恢复时若不清掉，会把暂停期间
        积压的旧消息一批回完。这里直接清空，恢复后只回新的。
        （水位照常推进，所以不会因为清队列而重复读同一条。）
        """
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

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
        # 双保险：读取侧已经挡过一次；万一有人绕过 _should_read 直接把消息塞进队列，
        # 也不该让服务号/系统号触发回复。
        # 注意群聊不归这个开关管 —— 它只由 include_group 决定，否则两个开关会打架。
        # （chat_id 为空的老调用不受影响。）
        if (self._only_person and incoming.chat_id and not incoming.is_group
                and not is_person_session(incoming.chat_id)):
            return False
        return True

    # ---------- 发送 ----------

    def send(self, chat: str, text: str, at: str | None = None, chat_id: str | None = None) -> None:
        """给 chat（显示名）发一条消息；失败时抛 WeChatNotReadyError。

        chat_id 只用于**发送后复核**。踩过的坑：wechatauto 在 UIA 未就绪时会退化成
        「截图判断输入框有没有清空」的老路，微信窗口最小化/不在前台时它会把已发出的
        消息误判成失败并重发（一条变两条）。所以库报错时先回头查消息库：
        只要自己这句话已经落库，就当发送成功。
        """
        wx = self._get_wx()
        started = time.time()
        failed: str | None = None
        try:
            resp = wx.SendMsg(msg=text, who=chat, at=at) if at else wx.SendMsg(msg=text, who=chat)
        except Exception as exc:  # noqa: BLE001
            resp, failed = None, str(exc)
        else:
            # SendMsg 返回 WxResponse：异常之外的“发送失败”也要处理，避免静默丢失回复。
            if resp is not None and not resp:
                failed = str(resp)
        if failed is not None:
            if self._confirm_sent(chat_id, text, started):
                log.warning(
                    "[发送] %s：微信接口报「%s」，但消息库确认这句已发出（判定成功，不再重发）",
                    chat, failed,
                )
                return
            raise WeChatNotReadyError(f"发送消息失败: {failed}")

    def _confirm_sent(self, chat_id: str | None, text: str, since: float) -> bool:
        """复核：消息库里有没有我刚刚发出去的这句话。"""
        want = text.strip()
        if not chat_id or not want:
            return False
        try:
            db = self._get_wx()._db
        except Exception:  # noqa: BLE001 - 复核不了就按失败处理
            return False
        deadline = time.time() + self._send_confirm_timeout
        while True:
            try:
                rows = db.get_messages(chat_id, limit=5) or []
            except Exception:  # noqa: BLE001
                return False
            for row in rows:
                if str(row.get("sender_id")) != str(SELF_SENDER_ID):
                    continue
                if want not in str(row.get("content") or ""):
                    continue
                if float(row.get("create_time") or 0) >= since - 2:
                    return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)
