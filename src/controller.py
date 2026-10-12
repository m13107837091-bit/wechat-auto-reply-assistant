"""可启停的机器人核心：把原先 main 的阻塞循环搬成后台线程，供控制面板 start/stop。

控制面板（webui）只跟这个类打交道：``start()``/``stop()`` 开关自动回复，
``snapshot()`` 看状态，``get_persona()``/``update_persona()`` 读改人设并持久化。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .activity import (
    ACTION_ERROR,
    ACTION_REPLIED,
    ACTION_SKIPPED,
    ACTION_SYSTEM,
    ActivityLog,
)
from .bot import Bot
from .config import (
    AppConfig,
    LLMConfig,
    mask_api_key,
    save_llm_settings,
    save_lists,
    save_persona,
)
from .llm import LLMClient, LLMError
from .search import build_search
from .session import SessionStore
from .wechat_client import IncomingMessage, WeChatClient, WeChatNotReadyError

log = logging.getLogger(__name__)

# 预热失败后的重试间隔（秒）：第一次失败等 15s，之后翻倍，封顶 5 分钟。
# 「微信还没登录」可能要等很久，没必要每隔几秒就白跑一次整库解密级别的重活。
WARM_BACKOFF = (15.0, 30.0, 60.0, 120.0, 300.0)


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
        lists_path: str | Path | None = None,
        wechat_factory: Callable[[], WeChatClient] | None = None,
    ) -> None:
        self.config = config or AppConfig.load()
        self._llm = llm
        self._bot = bot
        self._wechat = wechat
        # 重连要能把微信对象整个换掉；工厂每次都按最新配置造一个。
        # 默认实现读 self.config，所以面板改了「回复群聊 / 只回个人」也会跟着走。
        if wechat_factory is None:
            def wechat_factory() -> WeChatClient:
                return WeChatClient(
                    include_group=self.config.reply.group_enabled,
                    only_person=self.config.reply.friend_only_person,
                )
        self._wechat_factory = wechat_factory
        self._persona_path = Path(persona_path) if persona_path else None
        self._settings_path = Path(settings_path) if settings_path else None
        self._lists_path = Path(lists_path) if lists_path else None

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # 批次 6.4：守夜人。盯「轮询线程」和「回复循环」的心跳，卡住就自愈。
        # 2026-10-05 的教训：全家都说它不回，而面板写着「运行中」——
        # 状态来自标志位，标志位不会因为线程死掉而变。
        self._guard_thread: threading.Thread | None = None
        self._guard_stop = threading.Event()
        self._last_heal_at = 0.0
        self._last_loop_at = 0.0   # 回复循环每转一圈更新一次（心跳）
        # 代数递增：stop() 让旧循环退出；start() 起新代，避免「旧线程没退干净、
        # 新线程又起」时两个循环并发。循环用自己那一代做退出判断。
        self._generation = 0
        self._running = False
        # 正在连接微信（prepare 很慢：要整库解密，见 wechat_client.prepare）。
        # 面板靠它显示「连接中…」，而不是把这段时间误报成「已暂停」。
        self._starting = False
        self.last_error = ""
        # 面板「最近动态」的事件缓冲。放实例上而不是做成模块级单例：测试可以各造各的，
        # 而且它的生命周期本来就该跟控制器一致。
        self.activity = ActivityLog()

        # ---- 预热（后台连微信）----
        self._warm_lock = threading.Lock()
        self._warm_thread: threading.Thread | None = None
        self._warm_stop = threading.Event()
        self._warm_state = "off"    # off | connecting | ready | waiting
        self._warm_attempts = 0
        self._warm_error = ""
        self._warm_retry_at = 0.0   # 下一次重试的时刻
        self._warm_auto_start = False
        # 「用户按了开始、但当时正在连接」——这个意图不能丢，否则那一下就等于没点。
        self._want_running = False
        # 连接开始的时刻。面板显示「已等待 N 秒」，免得几十秒的连接看起来像卡死。
        self._starting_since = 0.0
        # 「重连微信」的后台线程（面板点一下就走它，HTTP 立刻返回）
        self._reconnect_thread: threading.Thread | None = None
        # 重连前是否在回复：stop() 会清掉 _want_running，所以单独存一份
        self._reconnect_auto_start = False

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
            self._wechat = self._wechat_factory()

        try:
            self._wechat.prepare()
        except WeChatNotReadyError as exc:
            self.last_error = str(exc)
            return False
        self._push_read_switches()
        self._ensure_guard()
        return True

    def _push_read_switches(self) -> None:
        """把「读什么」的开关推给读取侧：群总开关、只回个人、群白名单。

        白名单以前只在回复判断那一层生效 —— 名单外的群照样要开库读一遍再丢掉。
        批次 6.3 前移到读取侧：不在名单里的群连消息库都不打开。
        """
        if self._wechat is None:
            return
        cfg = self.config.reply
        # 三个都是「读取侧优化」：老实现/测试替身缺哪个都不该让连接失败。
        self._call_read_switch("set_include_group", cfg.group_enabled)
        self._call_read_switch("set_only_person", cfg.friend_only_person)
        self._call_read_switch("set_group_whitelist", cfg.group_whitelist, cfg.match_mode)

    def _call_read_switch(self, name: str, *args: Any) -> None:
        """调用读取侧开关；对方没有这个方法就记一笔，不抛出。"""
        fn = getattr(self._wechat, name, None)
        if not callable(fn):
            log.debug("[名单] 读取侧没有 %s（忽略）", name)
            return
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            log.warning("[名单] 同步 %s 给读取侧失败（忽略）: %s", name, exc)

    # ---------- 守夜人（批次 6.4） ----------

    def _ensure_guard(self) -> None:
        """起「守夜人」线程（幂等）。"""
        if self._guard_thread is not None and self._guard_thread.is_alive():
            return
        self._guard_stop.clear()
        self._guard_thread = threading.Thread(
            target=self._guard_loop, name="bot-guard", daemon=True
        )
        self._guard_thread.start()

    def _guard_loop(self) -> None:
        """每 30 秒看一眼心跳；卡住就自愈（最多 10 分钟一次）。"""
        while not self._guard_stop.is_set():
            self._guard_stop.wait(30)
            if self._guard_stop.is_set():
                return
            try:
                self._guard_once()
            except Exception as exc:  # noqa: BLE001 - 守夜人自己不能死
                log.warning("[守护] 自检出错（忽略这次）: %s", exc)

    def _guard_once(self) -> None:
        """一次自检：轮询线程或回复循环卡住 → 记录 + 自动重连。"""
        if self._wechat is None or self._warm_state != "ready":
            return   # 还没连上/正在连：没有「卡住」可言，别打断退避重试
        if self._starting:
            return
        problem = ""
        try:
            if not self._wechat.poll_alive():
                problem = "读取线程卡住（收不到消息）"
        except Exception:  # noqa: BLE001 - 老/假实现没有 poll_alive
            return
        if (not problem and self._running and self._last_loop_at
                and time.time() - self._last_loop_at > 180):
            problem = "回复循环卡住 %.0f 秒" % (time.time() - self._last_loop_at)
        if not problem:
            return
        now = time.time()
        if now - self._last_heal_at < 600:
            return   # 10 分钟内只自愈一次，别把重连打成死循环
        self._last_heal_at = now
        log.error("[守护] %s —— 自动重连一次。", problem)
        self.activity.record(action=ACTION_ERROR, reason="stuck_recovered", detail=problem)
        self.reconnect()

    def connect(self) -> bool:
        """只连微信、不开始回复；给预热流程用。

        以前 main 的启动流程是 start() 紧接 stop()：微信确实连上了，但会在
        「最近动态」里留下一对假的「已开启 / 已暂停」，看起来像有人动过开关。
        连接和开关本来就是两件事，这里把它们分开。
        """
        if self._running or self._starting:
            return True  # 已经连着 / 正在连，别把整库解密跑第二遍

        self._starting = True
        self._starting_since = time.time()
        try:
            ok = self._ensure_ready()
        finally:
            self._starting = False

        if not ok:
            self.activity.record(action=ACTION_ERROR, reason="prepare_failed", detail=self.last_error)
            return False
        self._wechat.discard_pending()
        return True

    def start(self) -> bool:
        """开始/恢复自动回复；已运行则直接返回 True，准备失败返回 False。"""
        if self._running:
            return True
        if self._starting:
            # 正在连接（多半是预热在跑）：把「要开」这个意图记下来，等连上就开。
            # 旧行为是直接 return True 却不记，于是你点了开始，它永远不会开始。
            self._want_running = True
            return True

        self._want_running = True
        # 连接微信要整库解密，可能几十秒；置位让面板显示「连接中…」。
        self._starting = True
        self._starting_since = time.time()
        try:
            ok = self._start_locked()
        finally:
            self._starting = False
        if not ok:
            self._want_running = False  # 连不上就别留着这个意图，等你再点
        return ok

    def _start_locked(self) -> bool:
        if not self._ensure_ready():
            # 连不上微信也要留痕：否则人在外面只看到"点了没反应"。
            self.activity.record(action=ACTION_ERROR, reason="prepare_failed", detail=self.last_error)
            return False

        self._wechat.discard_pending()  # 丢弃暂停期间积压的旧消息，恢复后只回新的
        self._generation += 1
        gen = self._generation
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, args=(gen,), name="bot-reply-loop", daemon=True
        )
        self._running = True
        self._last_loop_at = time.time()
        self._thread.start()
        log.info("自动回复已开启。")
        self.activity.record(action=ACTION_SYSTEM, reason="started", detail="自动回复已开启")
        return True

    def stop(self) -> None:
        """暂停自动回复（不拆微信连接/监听，随时可再 start 恢复）。"""
        self._want_running = False  # 明确暂停 = 取消「连上就开」的意图
        if not self._running:
            return
        self._running = False
        self._generation += 1  # 让当前循环尽快退出
        self._stop_event.set()
        if self._thread is not None:
            try:
                # 最多等 2s；若正卡在 LLM 调用则让它在后台自行退出
                self._thread.join(timeout=2)
            except RuntimeError:
                # stop() 恰好撞上 start() 的"线程对象已建好、还没 start()"那一瞬。
                # 那一代循环会因为 _generation 已经递增而立即自退，没什么可等的。
                # （2026-10-05 测试里真的撞出过一次：cannot join thread before it is started）
                pass
        self._thread = None
        log.info("自动回复已暂停。")
        self.activity.record(action=ACTION_SYSTEM, reason="stopped", detail="自动回复已暂停")


    def reconnect(self) -> bool:
        """断线自救：扔掉微信接口对象重连一遍（后台跑，HTTP 立刻返回）。

        为什么要有它：面板是手机上的唯一入口。微信掉线、接口卡死时，
        以前只能回家按键盘；现在在手机上点一下就行。
        先停回复循环 → 丢掉旧对象重新 `prepare()` → 成功且原来在跑就自动恢复。
        """
        if self._starting:
            return False  # 正在连接中，别插一脚：那会变成两份整库解密
        # stop() 会清掉 _want_running（那是「用户按下开始」的意图），所以意图要另存一份
        self._reconnect_auto_start = self._running or self._want_running
        self.stop()
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return True
        with self._warm_lock:
            self._warm_state = "connecting"
        self.activity.record(
            action=ACTION_SYSTEM, reason="reconnecting", detail="正在重连微信…"
        )
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_worker, name="bot-reconnect", daemon=True
        )
        self._reconnect_thread.start()
        return True

    def _reconnect_worker(self) -> None:
        """后台重连：拆旧连接、重建、按原意图恢复。"""
        auto_start = self._reconnect_auto_start
        # 取消还在退避等待的预热重试，免得两处同时连微信
        self._warm_stop.set()
        warm = self._warm_thread
        if warm is not None and warm.is_alive():
            warm.join(timeout=3)
        self._warm_thread = None
        if self._wechat is not None:
            try:
                self._wechat.stop()
            except Exception:  # noqa: BLE001 - 关旧连接失败也要继续重建
                log.exception("关闭旧微信连接时出错（继续重连）")
            self._wechat = None
        self.last_error = ""
        # 走 start()/connect()：它们内部会 prepare、清积压、失败留痕。
        # 千万别在这里再 _ensure_ready() 一遍 —— 那是第二次整库解密（几十秒）。
        ok = self.start() if auto_start else self.connect()
        if ok:
            with self._warm_lock:
                self._warm_state = "ready"
                self._warm_error = ""
            log.info("重连完成：微信已重新连接（自动回复%s）。", "已恢复" if auto_start else "仍暂停")
            self.activity.record(
                action=ACTION_SYSTEM, reason="reconnected",
                detail="已重连微信" + ("，自动回复已恢复" if auto_start else "，仍暂停"),
            )
            return
        with self._warm_lock:
            self._warm_state = "waiting"
            self._warm_error = self.last_error
        log.warning("重连失败：%s；交给预热退避重试。", self.last_error)
        self.warm_up(auto_start=auto_start)
    def warm_up(self, auto_start: bool = False) -> None:
        """后台把微信连起来，失败就退避重试 —— 让你到图书馆点一下就是秒开。

        为什么需要它：``prepare()`` 要整库解密，一次 55～76 秒（见 wechat_client）。
        这笔开销只要在开机后付一次，之后一直连着，所以应该在启动时就付掉，而不是
        等你按下「开始回复」才付。更要命的是微信可能开机时还没启动/没登录 —— 只试
        一次的旧写法会一路失败到你点按钮的那一刻，那一次点击就要干等一分多钟。
        """
        if self._running:
            return
        with self._warm_lock:
            if self._warm_thread is not None and self._warm_thread.is_alive():
                return
            self._warm_stop.clear()
            self._warm_auto_start = auto_start
            self._warm_state = "connecting"
            self._warm_attempts = 0
            self._warm_error = ""
            self._warm_retry_at = 0.0
            self._warm_thread = threading.Thread(
                target=self._warm_loop, name="bot-warm-up", daemon=True
            )
            self._warm_thread.start()

    def _warm_loop(self) -> None:
        delay = 0.0
        while not self._warm_stop.is_set():
            if self._running:
                with self._warm_lock:
                    self._warm_state = "ready"
                return
            if delay and self._warm_stop.wait(delay):
                return

            with self._warm_lock:
                self._warm_attempts += 1
                attempt = self._warm_attempts
                self._warm_state = "connecting"
                self._warm_retry_at = 0.0

            started = time.time()
            if self.connect():
                with self._warm_lock:
                    self._warm_state = "ready"
                    self._warm_error = ""
                log.info(
                    "预热完成：微信已连接（第 %d 次尝试，耗时 %.1fs）。自动回复仍暂停。",
                    attempt, time.time() - started,
                )
                self.activity.record(
                    action=ACTION_SYSTEM, reason="warm_ready",
                    detail="微信已连接，随时可开（自动回复仍暂停）",
                )
                if self._warm_auto_start or self._want_running:
                    self.start()
                return

            delay = WARM_BACKOFF[min(attempt - 1, len(WARM_BACKOFF) - 1)]
            with self._warm_lock:
                self._warm_state = "waiting"
                self._warm_error = self.last_error
                self._warm_retry_at = time.time() + delay
            log.warning(
                "预热失败（第 %d 次，耗时 %.1fs）：%s；%.0f 秒后重试",
                attempt, time.time() - started, self.last_error, delay,
            )
    def _run_loop(self, generation: int) -> None:
        """监听回复循环（后台线程）。用 `_stop_event.wait` 代替 sleep，便于随时停下。"""
        current = None  # 当前正在处理的消息；异常时用它把错误归到具体的人头上
        while generation == self._generation and not self._stop_event.is_set():
            self._last_loop_at = time.time()   # 心跳：循环还转着（守夜人据此判断卡死）
            try:
                for msg in _coalesce(self._wechat.get_new_messages()):
                    current = msg
                    # 读取阶段：消息从「监听线程入队」到「主循环取到」的等待时长。
                    # 发现延迟 = 对方发出 → 我们读到，直接反映轮询的灵敏度。
                    detect = 0.0
                    if msg.received_at and msg.sent_at:
                        detect = max(0.0, msg.received_at - msg.sent_at)
                    if msg.received_at:
                        log.info(
                            "[读取] %s 的新消息，发现延迟 %.1fs / 排队等待 %.2fs",
                            msg.chat, detect, time.time() - msg.received_at,
                        )
                    else:
                        log.info("[读取] %s 的新消息", msg.chat)
                    gen_start = time.time()
                    log.info("[生成] 正在给 %s 生成回复中……", msg.chat)
                    decision = self._bot.respond(
                        msg.chat, msg.content, msg.is_group, msg.sender, msg.at_list
                    )
                    gen_cost = time.time() - gen_start

                    if not decision.reply:
                        # 跳过同样要留痕并带上原因 ——「它为什么没回这个人」是面板要回答的核心问题之一。
                        log.info("[跳过] %s：%s（耗时 %.2fs）", msg.chat, decision.reason, gen_cost)
                        self.activity.record(
                            action=ACTION_SKIPPED, chat=msg.chat, is_group=msg.is_group,
                            sender=msg.sender, incoming=msg.content,
                            reason=decision.reason, gen_seconds=gen_cost,
                        )
                        continue

                    send_start = time.time()
                    # 带上 chat_id：发送库报错时靠它回头查库复核（见 WeChatClient.send）
                    self._wechat.send(msg.chat, decision.reply, chat_id=msg.chat_id)
                    send_cost = time.time() - send_start

                    # 打全文，不再截断到 40 字（docs 的 E2）。出问题时最想看的，
                    # 恰恰是被截掉的那半句。
                    log.info(
                        "[发送] %s：生成 %.2fs / 发送 %.2fs | %s",
                        msg.chat, gen_cost, send_cost, decision.reply,
                    )
                    self.activity.record(
                        action=ACTION_REPLIED, chat=msg.chat, is_group=msg.is_group,
                        sender=msg.sender, incoming=msg.content, outgoing=decision.reply,
                        reason=decision.reason, gen_seconds=gen_cost, send_seconds=send_cost,
                    )
            except WeChatNotReadyError as exc:
                log.error("%s；5 秒后重试……", exc)
                self.activity.record(
                    action=ACTION_ERROR, reason="wechat_not_ready",
                    chat=(current.chat if current else ""), detail=str(exc),
                )
                self._stop_event.wait(5)
            except Exception as exc:  # noqa: BLE001 - 循环保持存活
                log.exception("处理消息时出错: %s", exc)
                self.activity.record(
                    action=ACTION_ERROR, reason="exception",
                    chat=(current.chat if current else ""),
                    sender=(current.sender if current else ""),
                    incoming=(current.content if current else ""),
                    detail=str(exc),
                )
                self._stop_event.wait(1)
            self._stop_event.wait(0.2)

    # ---------- 状态 / 人设 ----------

    def snapshot(self) -> dict[str, Any]:
        """控制面板需要的整体状态。"""
        warm_retry = 0.0
        if self._warm_state == "waiting" and self._warm_retry_at:
            warm_retry = max(0.0, self._warm_retry_at - time.time())
        return {
            "running": self._running,
            "starting": self._starting,
            # 连接已持续多久：面板显示「已等待 N 秒」，几十秒的连接才不像卡死。
            "starting_seconds": round(time.time() - self._starting_since, 1) if self._starting else 0.0,
            "last_error": self.last_error,
            # 预热状态：面板据此提前告诉你「微信已就绪、点一下就回」还是「还没连上」。
            "warm": {
                "state": self._warm_state,
                "attempts": self._warm_attempts,
                "error": self._warm_error,
                "retry_in": round(warm_retry, 1),
            },
            "persona": self.get_persona(),
            "llm": self.get_llm_settings(),
            # 批次 6.1/6.2：读取侧心跳与丢弃计数 —— 面板据此分清「没人找它」和「它聋了」
            "receive": self._receive_snapshot(),
        }

    def _receive_snapshot(self) -> dict[str, Any]:
        """读取侧状态快照（拿不到就返回 available=False，绝不让面板 500）。"""
        if self._wechat is None:
            return {"available": False}
        try:
            data = dict(self._wechat.stats())
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": str(exc)}
        data["available"] = True
        data["loop_alive"] = bool(
            self._running and self._last_loop_at
            and time.time() - self._last_loop_at <= 180
        )
        data["last_loop_ago"] = (
            round(time.time() - self._last_loop_at, 1) if self._last_loop_at else None
        )
        return data
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
        # 只认字符串：以前统一 str(...) 会把 JSON null 变成字符串 "None"，
        # 于是一个字段都没填的请求能把人设写成"None"（2026-10-05 真踩到过）。
        if isinstance(overrides.get("name"), str):
            pc.name = overrides["name"].strip()
        if isinstance(overrides.get("style"), str):
            pc.style = overrides["style"].strip()
        if "emoji" in overrides:
            pc.emoji = bool(overrides["emoji"])
        if isinstance(overrides.get("length"), str):
            length = overrides["length"].strip()
            if length in ("auto", "short", "detailed"):
                pc.length = length
        if isinstance(overrides.get("system_prompt"), str):
            self.config.llm.system_prompt = overrides["system_prompt"].strip()

        snapshot = self.get_persona()
        save_persona(snapshot, self._persona_path)
        return snapshot

    # ---------- 黑白名单（面板可编辑，改完立即生效） ----------

    def get_lists(self) -> dict[str, Any]:
        """面板需要的黑白名单状态。

        ``open_to_all`` 是给面板的显眼提示：白名单为空 = **谁发消息都会触发回复**。
        这是隐私上最该被看见的一件事，所以单独给个字段让前端红字提醒。
        """
        cfg = self.config.reply
        return {
            "whitelist": list(cfg.whitelist),
            "blacklist": list(cfg.blacklist),
            "match_mode": cfg.match_mode,
            "open_to_all": not cfg.whitelist,
            # 只回"个人"：自动屏蔽群聊、公众号服务号、企业微信、微信系统号。
            # 默认开 —— 否则银行/美团/电信这些服务号发消息也会触发回复。
            "only_person": cfg.friend_only_person,
            # 群聊：总开关 + 只回这些群 + 群里命中关键词才回
            "group_enabled": cfg.group_enabled,
            "group_whitelist": list(cfg.group_whitelist),
            "group_trigger_keywords": list(cfg.group_trigger_keywords),
        }

    def update_lists(self, overrides: dict[str, Any]) -> dict[str, Any]:
        """改黑白名单并**立即生效**（bot 每次判断都现读 config，不用重启）。

        只处理传进来的键：没传的那份名单保持原样。空名单是合法输入
        （白名单为空即"不限制"，见 :meth:`get_lists` 的 ``open_to_all``）。
        """
        cfg = self.config.reply
        if "whitelist" in overrides:
            cfg.whitelist = _clean_names(overrides.get("whitelist"))
        if "blacklist" in overrides:
            cfg.blacklist = _clean_names(overrides.get("blacklist"))
        if "only_person" in overrides:
            cfg.friend_only_person = bool(overrides.get("only_person"))
        if "group_enabled" in overrides:
            cfg.group_enabled = bool(overrides.get("group_enabled"))
        if "group_whitelist" in overrides:
            cfg.group_whitelist = _clean_names(overrides.get("group_whitelist"))
        if "group_trigger_keywords" in overrides:
            cfg.group_trigger_keywords = _clean_names(overrides.get("group_trigger_keywords"))
        # 读取侧的热开关一次性同步：群的开关决定"要不要去读群消息库"，
        # 只回个人决定"要不要读服务号/系统号"。改完下一轮轮询就按新值走，不用重启。
        self._push_read_switches()   # 白名单也一起前移到读取侧（批次 6.3）
        snapshot = self.get_lists()
        save_lists(
            {
                "whitelist": snapshot["whitelist"],
                "blacklist": snapshot["blacklist"],
                "only_person": snapshot["only_person"],
                "group_enabled": snapshot["group_enabled"],
                "group_whitelist": snapshot["group_whitelist"],
                "group_trigger_keywords": snapshot["group_trigger_keywords"],
            },
            self._lists_path,
        )
        log.info(
            "[名单] 已生效：白名单 %d 个（%s）/ 黑名单 %d 个 / 只回个人=%s / 群聊=%s（只回 %s 个群，关键词 %d 个）",
            len(snapshot["whitelist"]),
            "不限制——谁都能触发回复" if snapshot["open_to_all"] else "、".join(snapshot["whitelist"][:5]),
            len(snapshot["blacklist"]),
            "是" if snapshot["only_person"] else "否",
            "开" if snapshot["group_enabled"] else "关",
            len(snapshot["group_whitelist"]),
            len(snapshot["group_trigger_keywords"]),
        )
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


# 面板传上来的名单最多认这么多个名字。防御性上限：这个接口在公网上可达，
# 一个 1MB 的名单会把每次回复的判断变成线性扫描。
MAX_LIST_NAMES = 500


def _clean_names(value: Any) -> list[str]:
    """把面板传来的名单清洗成去重后的有序列表。

    接受 list，也接受"一行一个 / 逗号/顿号/分号分隔"的字符串——手机上从通讯录
    里粘一串名字过来是最常见的用法。空值一律返回空列表（合法：白名单为空即不限制）。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value
        for sep in ("，", "、", ";", "；", ",", "\t"):
            text = text.replace(sep, "\n")
        raw: list[str] = text.split("\n")
    elif isinstance(value, (list, tuple, set)):
        raw = [str(x) for x in value]
    else:
        raw = [str(value)]
    out: list[str] = []
    for item in raw:
        name = str(item).strip()
        if name and name not in out:
            out.append(name)
    if len(out) > MAX_LIST_NAMES:
        log.warning("[名单] 一次收到 %d 个名字，只保留前 %d 个", len(out), MAX_LIST_NAMES)
        out = out[:MAX_LIST_NAMES]
    return out