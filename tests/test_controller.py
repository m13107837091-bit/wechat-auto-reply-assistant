"""对 BotController 的启停状态机与改人设做单元测试（注入假依赖，不碰真实微信/LLM）。"""
from __future__ import annotations

import json
import threading
import time

import src.controller as controller_mod
from src.bot import Decision
from src.config import AppConfig
from src.controller import BotController
from src.wechat_client import IncomingMessage, WeChatNotReadyError


class _FakeLLM:
    pass


class _FakeBot:
    """只实现控制器真正调用的 respond()。

    decision 给定时所有消息都返回它 —— 用来造「跳过」场景，不必碰真实策略。
    """

    def __init__(self, decision=None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.decision = decision

    def respond(self, chat, content, is_group, sender, at_list):
        if self.decision is not None:
            return self.decision
        return Decision(reply=f"回复:{content}")


class _BoomBot:
    """一被调用就炸，用来验证「循环里出错」会不会留下带人名的错误记录。"""

    def respond(self, chat, content, is_group, sender, at_list):
        raise RuntimeError("boom")


class _FakeWeChat:
    """模拟 WeChatClient 的最小接口：prepare/get_new_messages/send/discard_pending 等。"""

    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []
        self.sent: list[tuple[str, str]] = []
        self.discarded = 0
        self.prepared = 0
        self.only_person: bool | None = None   # 记录"只回个人"开关有没有热传到读取侧
        self.prepare_error: Exception | None = None
        self.fail_times = 0                          # 前 N 次 prepare 故意失败
        self.prepare_gate: threading.Event | None = None  # 给定时卡住 prepare，用来模拟"连接中"
        self.stopped = 0                             # 被拆掉几次（重连时要拆旧连接）
        self.group_whitelist = None                  # 白名单有没有前移到读取侧
        self.poll_alive_value = True                  # 读取线程心跳（批次 6.4）
        self.stats_calls = 0

    def prepare(self) -> None:
        self.prepared += 1
        if self.prepare_gate is not None:
            self.prepare_gate.wait(5)   # 模拟整库解密那几十秒
        if self.fail_times > 0:
            self.fail_times -= 1
            raise WeChatNotReadyError("微信没开")
        if self.prepare_error is not None:
            raise self.prepare_error

    def get_new_messages(self) -> list[IncomingMessage]:
        out = self.messages
        self.messages = []
        return out

    def send(self, chat: str, text: str, **kw) -> None:
        self.sent.append((chat, text))
        self.last_send_kwargs = kw

    def stop(self) -> None:
        self.stopped += 1

    def discard_pending(self) -> None:
        self.discarded += 1

    def set_only_person(self, value: bool) -> None:
        self.only_person = bool(value)

    def set_include_group(self, value: bool) -> None:
        self.include_group = bool(value)
    def set_group_whitelist(self, names, match_mode="exact") -> None:
        self.group_whitelist = (list(names or []), match_mode)

    def poll_alive(self) -> bool:
        return bool(self.poll_alive_value)

    def stats(self) -> dict:
        self.stats_calls += 1
        return {
            "listening": True,
            "poll_alive": self.poll_alive(),
            "last_poll_ago": 0.4,
            "last_message_ago": 12.0,
            "last_poll_error": "",
            "seen_chats": 3,
            "waiting_in_queue": 0,
            "counts": {"poll_rounds": 9, "read": 0, "drop_type": 0},
        }


def _make_controller(wechat: _FakeWeChat, persona_path=None, bot=None, lists_path=None,
                     wechat_factory=None) -> tuple[BotController, _FakeWeChat]:
    c = BotController(
        config=AppConfig(),
        llm=_FakeLLM(),
        bot=bot or _FakeBot(),
        wechat=wechat,
        persona_path=persona_path,
        lists_path=lists_path,
        wechat_factory=wechat_factory,
    )
    return c, wechat


def _wait_until(pred, timeout: float = 2.0) -> bool:
    """等条件成立。循环线程是异步的，别用固定 sleep 赌时序。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


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


# ---------- 最近动态（批次 2） ----------


def test_activity_records_replied():
    wc = _FakeWeChat()
    c, _ = _make_controller(wc)
    c.start()
    wc.messages.append(IncomingMessage(content="你好", chat="小明", is_group=False, sender="小明"))
    assert _wait_until(lambda: any(i["action"] == "replied" for i in c.activity.recent(10)))
    c.stop()

    item = [i for i in c.activity.recent(10) if i["action"] == "replied"][0]
    assert item["chat"] == "小明"
    assert item["incoming"] == "你好"
    assert item["outgoing"] == "回复:你好"
    assert item["reason"] == ""
    assert item["gen_seconds"] is not None and item["send_seconds"] is not None
    assert c.activity.counts()["today_replied"] == 1


def test_activity_records_skip_with_reason():
    wc = _FakeWeChat()
    c, _ = _make_controller(wc, bot=_FakeBot(Decision(None, "night_silence")))
    c.start()
    wc.messages.append(IncomingMessage(content="在吗", chat="小红", is_group=False, sender="小红"))
    assert _wait_until(lambda: any(i["action"] == "skipped" for i in c.activity.recent(10)))
    c.stop()

    item = [i for i in c.activity.recent(10) if i["action"] == "skipped"][0]
    assert item["reason"] == "night_silence"
    assert item["outgoing"] is None
    assert wc.sent == []  # 跳过就是真的没发出去
    assert c.activity.counts()["today_skipped"] == 1


def test_activity_records_start_and_stop():
    c, _ = _make_controller(_FakeWeChat())
    c.start()
    c.stop()
    reasons = [i["reason"] for i in c.activity.recent(10) if i["action"] == "system"]
    assert "started" in reasons
    assert "stopped" in reasons


def test_activity_records_error_with_sender():
    wc = _FakeWeChat()
    c, _ = _make_controller(wc, bot=_BoomBot())
    c.start()
    wc.messages.append(IncomingMessage(content="嗨", chat="小刚", is_group=False, sender="小刚"))
    assert _wait_until(lambda: any(i["action"] == "error" for i in c.activity.recent(10)))
    c.stop()

    item = [i for i in c.activity.recent(10) if i["action"] == "error"][0]
    assert item["reason"] == "exception"
    assert item["chat"] == "小刚"  # 错误要归到具体的人头上，否则面板只能说"出错了"
    assert "boom" in item["detail"]


def test_connect_does_not_touch_activity():
    wc = _FakeWeChat()
    c, _ = _make_controller(wc)
    assert c.connect() is True
    # 「连接微信」不是「打开开关」，不该在动态里留下任何痕迹 ——
    # 否则每次开机都会显示一对「已开启 / 已暂停」，看着像有人按过按钮。
    assert c.activity.recent(10) == []
    assert c.snapshot()["running"] is False
    assert wc.discarded == 1  # 连上时顺手清掉暂停期间积压的旧消息


def test_connect_failure_is_recorded():
    wc = _FakeWeChat()
    wc.prepare_error = WeChatNotReadyError("微信没开")
    c, _ = _make_controller(wc)
    assert c.connect() is False
    item = c.activity.recent(1)[0]
    assert item["action"] == "error"
    assert item["reason"] == "prepare_failed"
    assert "微信没开" in item["detail"]


# ---------- 预热（后台连微信 + 失败重试） ----------


def test_warm_up_reports_ready_without_starting():
    wc = _FakeWeChat()
    c, _ = _make_controller(wc)
    c.warm_up()
    assert _wait_until(lambda: c.snapshot()["warm"]["state"] == "ready")

    snap = c.snapshot()
    assert snap["running"] is False      # 预热不等于开回复
    assert snap["starting"] is False
    assert snap["warm"]["attempts"] == 1
    reasons = [i["reason"] for i in c.activity.recent(10)]
    assert "warm_ready" in reasons
    assert "started" not in reasons      # 预热不该在动态里留下「已开启」


def test_warm_up_retries_until_connected(monkeypatch):
    monkeypatch.setattr(controller_mod, "WARM_BACKOFF", (0.05, 0.05))
    wc = _FakeWeChat()
    wc.fail_times = 2                    # 前两次连不上（微信还没启动），第三次成功
    c, _ = _make_controller(wc)
    c.warm_up()
    assert _wait_until(lambda: c.snapshot()["warm"]["state"] == "ready", timeout=5)

    snap = c.snapshot()
    assert snap["warm"]["attempts"] == 3
    assert wc.prepared == 3
    assert snap["running"] is False


def test_warm_up_can_auto_start_when_connected(monkeypatch):
    monkeypatch.setattr(controller_mod, "WARM_BACKOFF", (0.05,))
    wc = _FakeWeChat()
    wc.fail_times = 1
    c, _ = _make_controller(wc)
    c.warm_up(auto_start=True)
    assert _wait_until(lambda: c.snapshot()["running"] is True, timeout=5)
    c.stop()


def test_start_pressed_during_connect_is_not_lost():
    """连接途中按「开始回复」必须记住这个意图。

    旧行为：start() 看到 _starting 为真就 return True 却不做任何事 ——
    界面上像是点成功了，实际上永远等不到它开始回复。
    """
    wc = _FakeWeChat()
    gate = threading.Event()
    wc.prepare_gate = gate
    c, _ = _make_controller(wc)
    c.warm_up()
    assert _wait_until(lambda: c.snapshot()["starting"] is True)  # 预热正在连

    assert c.start() is True                  # 就在这当口按下「开始回复」
    assert c.snapshot()["running"] is False   # 还没开始（连接还没回来）

    gate.set()                                # 让那 76 秒的连接完成
    assert _wait_until(lambda: c.snapshot()["running"] is True, timeout=5)
    c.stop()


def test_stop_cancels_pending_start_intent():
    wc = _FakeWeChat()
    gate = threading.Event()
    wc.prepare_gate = gate
    c, _ = _make_controller(wc)
    c.warm_up()
    assert _wait_until(lambda: c.snapshot()["starting"] is True)
    c.start()
    c.stop()                                  # 又反悔了
    gate.set()
    time.sleep(0.3)
    assert c.snapshot()["running"] is False   # 不该偷偷开始回


# ---------- 黑白名单：面板改完立即生效 ----------


def test_get_lists_reports_open_to_all_when_whitelist_empty():
    c, _ = _make_controller(_FakeWeChat())
    snap = c.get_lists()
    assert snap["whitelist"] == []
    assert snap["open_to_all"] is True   # 空名单 = 谁都回，面板要拿它报警


def test_update_lists_takes_effect_and_persists(tmp_path):
    p = tmp_path / "lists.local.json"
    c, _ = _make_controller(_FakeWeChat(), lists_path=p)
    snap = c.update_lists({"whitelist": "老冯\n小明", "blacklist": ["广告号"]})
    assert snap["whitelist"] == ["老冯", "小明"]
    assert snap["blacklist"] == ["广告号"]
    assert snap["open_to_all"] is False
    assert c.config.reply.whitelist == ["老冯", "小明"]  # 原地改 → 下一句即生效
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "whitelist": ["老冯", "小明"], "blacklist": ["广告号"], "only_person": True,
        "group_enabled": True, "group_whitelist": [], "group_trigger_keywords": [],
    }


def test_update_lists_accepts_comma_separated_and_dedupes(tmp_path):
    c, _ = _make_controller(_FakeWeChat(), lists_path=tmp_path / "lists.local.json")
    snap = c.update_lists({"whitelist": "老冯，小明、老冯, 小红"})
    assert snap["whitelist"] == ["老冯", "小明", "小红"]


def test_update_lists_only_touches_given_key(tmp_path):
    p = tmp_path / "lists.local.json"
    c, _ = _make_controller(_FakeWeChat(), lists_path=p)
    c.update_lists({"blacklist": ["广告号"]})
    c.update_lists({"whitelist": ["老冯"]})
    assert c.config.reply.whitelist == ["老冯"]
    assert c.config.reply.blacklist == ["广告号"]   # 第二次没传 → 保持
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["blacklist"] == ["广告号"]


def test_update_lists_immediately_blocks_non_whitelisted(tmp_path):
    """热生效的实证：真 Bot 每次都现读同一个 config 对象，不用重启。"""
    from src.bot import Bot
    from src.session import SessionStore

    c, _ = _make_controller(_FakeWeChat(), lists_path=tmp_path / "lists.local.json")
    bot = Bot(c.config, _FakeLLM(), SessionStore())
    assert bot.reject_reason("小明", "你好", False) is None      # 白名单空 = 谁都回
    c.update_lists({"whitelist": ["老冯"]})
    assert bot.reject_reason("小明", "你好", False) == "not_in_whitelist"
    assert bot.reject_reason("老冯", "你好", False) is None


def test_get_lists_defaults_to_only_person():
    c, _ = _make_controller(_FakeWeChat())
    assert c.get_lists()["only_person"] is True   # 默认只回个人，屏蔽服务号


def test_update_lists_can_turn_only_person_off(tmp_path):
    c, wc = _make_controller(_FakeWeChat(), lists_path=tmp_path / "lists.local.json")
    snap = c.update_lists({"only_person": False})
    assert snap["only_person"] is False
    assert c.config.reply.friend_only_person is False   # 立即生效
    assert wc.only_person is False                      # 并且热传到读取侧，不用重启
    saved = json.loads((tmp_path / "lists.local.json").read_text(encoding="utf-8"))
    assert saved["only_person"] is False

def test_reconnect_swaps_wechat_and_resumes_running():
    """重连 = 真的把微信对象换掉，而不是"什么都没做"。

    面板上那个「重连」按钮是手机上的唯一自救手段：微信掉线/接口卡死时，
    以前只能回家按键盘。这里钉住三件事：旧连接被拆、新对象连过、原来在跑就恢复。
    """
    old = _FakeWeChat()
    made: list[_FakeWeChat] = []

    def factory() -> _FakeWeChat:
        new = _FakeWeChat()
        made.append(new)
        return new

    c, _ = _make_controller(old, wechat_factory=factory)
    assert c.start() is True
    assert c.reconnect() is True
    # 顺序是：换对象 → start() → 标 ready。所以三件事一起等，别抢在中间断言
    assert _wait_until(
        lambda: bool(made) and c._wechat is made[0] and c._running is True
        and c.snapshot()['warm']['state'] == 'ready'
    )
    assert old.stopped == 1                   # 旧连接确实被拆了
    assert made[0].prepared == 1              # 新对象连过一次
    assert made[0].discarded == 1             # 重连后清掉积压，不回放旧消息
    assert c.snapshot()['warm']['state'] == 'ready'
    assert 'reconnected' in [i['reason'] for i in c.activity.recent(10)]
    c.stop()


def test_reconnect_while_paused_stays_paused():
    """暂停状态下点重连，不该顺手把自动回复打开。"""
    old = _FakeWeChat()
    made: list[_FakeWeChat] = []

    def factory() -> _FakeWeChat:
        new = _FakeWeChat()
        made.append(new)
        return new

    c, _ = _make_controller(old, wechat_factory=factory)
    assert c.reconnect() is True
    assert _wait_until(lambda: bool(made) and c._wechat is made[0])
    assert c._running is False
    assert old.stopped == 1
    c.stop()


def test_reconnect_refused_while_connecting():
    """正在连接（整库解密那几十秒）时拒绝重连：否则会变成两份解密抢微信。"""
    wc = _FakeWeChat()
    gate = threading.Event()
    wc.prepare_gate = gate
    c, _ = _make_controller(wc, wechat_factory=_FakeWeChat)
    c.warm_up()
    assert _wait_until(lambda: c.snapshot()['starting'] is True)
    assert c.reconnect() is False
    gate.set()


def test_reconnect_failure_hands_over_to_warm_retry(monkeypatch):
    """重连连不上就交给预热退避重试，别让一次失败之后没人管。"""
    monkeypatch.setattr(controller_mod, 'WARM_BACKOFF', (0.05,))
    old = _FakeWeChat()
    made: list[_FakeWeChat] = []

    def factory() -> _FakeWeChat:
        new = _FakeWeChat()
        new.fail_times = 1 if not made else 0
        made.append(new)
        return new

    c, _ = _make_controller(old, wechat_factory=factory)
    assert c.reconnect() is True
    assert _wait_until(lambda: c.snapshot()['warm']['state'] == 'ready', timeout=5)
    assert 'prepare_failed' in [i['reason'] for i in c.activity.recent(20)]


# ---------- 批次 6：心跳上报与守夜人自救 ----------


def test_snapshot_includes_receive_heartbeat():
    """批次 6.1：面板要能一眼分清「没人找它」和「它聋了」。"""
    wc = _FakeWeChat()
    c, _ = _make_controller(wc)
    assert c.connect() is True
    rec = c.snapshot()["receive"]
    assert rec["available"] is True
    assert rec["poll_alive"] is True
    assert rec["counts"]["read"] == 0
    assert rec["last_poll_ago"] == 0.4


def test_snapshot_receive_survives_client_without_stats():
    """老/假实现没有 stats() 时也不能让面板 500：退化成 available=False。"""

    class _Bare:
        def prepare(self):
            return None

        def stop(self):
            return None

        def discard_pending(self):
            return None

        def get_new_messages(self):
            return []

        def send(self, *a, **kw):
            return None

    c, _ = _make_controller(_Bare())
    assert c.connect() is True
    assert c.snapshot()["receive"]["available"] is False


def test_group_whitelist_pushed_to_read_side():
    """批次 6.3：白名单前移到读取侧，面板改完立刻生效。"""
    wc = _FakeWeChat()
    c, _ = _make_controller(wc)
    c.config.reply.group_whitelist = ["闲适老爸小屋"]
    c.config.reply.group_enabled = True
    assert c.connect() is True
    assert wc.group_whitelist == (["闲适老爸小屋"], "exact")
    c.update_lists({"group_whitelist": ["新群"]})
    assert wc.group_whitelist == (["新群"], "exact")


def test_guard_heals_when_poll_thread_dead():
    """批次 6.4：读取线程卡住 → 守夜人自动重连，并留下原因。

    这是 2026-10-05 那晚的教训：全家都说它不回，面板却写着「运行中」——
    状态来自标志位，标志位不会因为线程死掉而变。
    """
    old = _FakeWeChat()
    old.poll_alive_value = False          # 假装轮询线程死了
    made: list[_FakeWeChat] = []

    def factory() -> _FakeWeChat:
        new = _FakeWeChat()
        made.append(new)
        return new

    c, _ = _make_controller(old, wechat_factory=factory)
    c._warm_state = "ready"
    c._guard_once()
    assert _wait_until(lambda: bool(made) and c._wechat is made[0])
    assert old.stopped == 1                       # 旧连接被拆（真的重连了）
    # 注意顺序：先记 stuck_recovered，再记 reconnecting（重连自己也会留痕）
    assert "stuck_recovered" in [i["reason"] for i in c.activity.recent(10)]
    c.stop()


def test_guard_does_nothing_until_warm_ready():
    """还没连上就别自愈：那时候「卡住」是假象，打断退避重试只会更糟。"""
    wc = _FakeWeChat()
    wc.poll_alive_value = False
    c, _ = _make_controller(wc)
    c._warm_state = "waiting"
    c._guard_once()
    assert wc.stopped == 0


def test_guard_debounces_repeats():
    """卡住只自愈一次：10 分钟内不重复，否则重连会变成死循环。"""
    wc = _FakeWeChat()
    wc.poll_alive_value = False
    c, _ = _make_controller(wc, wechat_factory=_FakeWeChat)
    c._warm_state = "ready"
    c._guard_once()
    first = c._last_heal_at
    assert first > 0
    c._guard_once()                               # 立刻再来一次
    assert c._last_heal_at == first               # 没有被再次触发
