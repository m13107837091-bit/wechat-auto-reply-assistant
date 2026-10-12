"""wechat_client 的单元测试（不连微信）：消息映射、类型过滤、会话表差分轮询。

读取路径已从 wechatauto 的 ``AddListenAll``（每轮逐个会话查库，500 个会话一轮 40 秒）
换成**会话表差分**：先看 ``get_sessions()`` 里哪些会话有新动静，只读那几个。
下面用假消息库把这些行为钉住。
"""
from __future__ import annotations

import threading
import time

import pytest

from src.wechat_client import REPLYABLE_TYPES, IncomingMessage, WeChatClient, WeChatNotReadyError


class _FakeDB:
    """假消息库：喂会话表 + 每个会话的消息，用来验证差分与水位。"""

    def __init__(self, sessions=None, messages=None, nicknames=None, group_names=None,
                 groups_boom=False):
        self.sessions = list(sessions or [])
        self.messages = messages or {}      # username -> [row, ...]（升序）
        self.nicknames = nicknames or {}
        self.group_names = group_names or {}   # chatroom id -> 群名（白名单前置用）
        self.groups_boom = groups_boom         # 模拟 contact 库打不开
        self.new_queries = 0                # get_new_messages 调用次数
        self.baselines = 0                  # get_messages 调用次数
        self.boom = set()                   # 这些会话查询时抛错

    def get_sessions(self, limit=500):
        return self.sessions[:limit]

    def get_messages(self, user, limit=20, offset=0):
        self.baselines += 1
        if user in self.boom:
            raise RuntimeError("database disk image is malformed")
        # 真库是降序返回
        return list(reversed(self.messages.get(user, [])))[:limit]

    def get_new_messages(self, user, since_seq=0, limit=200):
        self.new_queries += 1
        if user in self.boom:
            raise RuntimeError("database disk image is malformed")
        return [r for r in self.messages.get(user, []) if r["sort_seq"] > since_seq][:limit]

    def nickname_map(self, refresh=False):
        return dict(self.nicknames)
    def get_groups(self):
        if self.groups_boom:
            raise RuntimeError("contact db 打不开")
        return [{"username": k, "name": v} for k, v in self.group_names.items()]

    def group_id_to_name(self, chatroom_wxid):
        return self.group_names.get(chatroom_wxid, "")


class _FakeWx:
    def __init__(self, db):
        self._db = db
        self.sent = []

    def SendMsg(self, msg=None, who=None, at=None):
        self.sent.append((who, msg))
        return True


def _client(db, **kw):
    """构造一个注入了假 wx 的客户端（不触发真 WeChat()，也不起轮询线程）。"""
    c = WeChatClient(include_group=kw.pop("include_group", False), **kw)
    c._wx = _FakeWx(db)
    c._listening = True          # 别让 get_new_messages() 顺手起线程
    return c


def _drain(c):
    out = []
    while not c._queue.empty():
        out.append(c._queue.get_nowait())
    return out


def _row(seq, content="你好", ttype="text", sender_id=7, user="wxid_a", age=0.0):
    return {
        "local_id": seq, "sort_seq": seq, "type": ttype, "sender_id": sender_id,
        "sender_username": user, "content": content, "create_time": time.time() + age,
    }


def _session(user, last_time=1, unread=0, summary="x"):
    return {"username": user, "last_time": last_time, "unread": unread, "summary": summary}


# ---------- 消息映射 ----------


def test_friend_text_row_parsed():
    db = _FakeDB(nicknames={"wxid_a": "小明"})
    c = _client(db)
    m = c._row_to_incoming(db, "wxid_a", _row(1, "在吗", user="wxid_a"))
    assert m is not None
    assert m.chat == "小明"          # 显示名（备注/昵称）
    assert m.content == "在吗"
    assert m.is_group is False
    assert m.sender == "wxid_a"
    assert m.sent_at > 0


def test_quote_row_parsed():
    db = _FakeDB()
    c = _client(db)
    m = c._row_to_incoming(db, "wxid_a", _row(1, "在吗", ttype="quote"))
    assert m is not None and m.content == "在吗"


def test_display_name_falls_back_to_username():
    db = _FakeDB()                    # nickname_map 里没有 → 用 username
    c = _client(db)
    m = c._row_to_incoming(db, "wxid_a", _row(1))
    assert m is not None and m.chat == "wxid_a"


def test_own_message_ignored():
    db = _FakeDB()
    c = _client(db)
    # sender_id=2 是自己发的（旧回调路径靠 attr='self' 挡）
    assert c._row_to_incoming(db, "wxid_a", _row(1, sender_id=2)) is None
    assert c._row_to_incoming(db, "wxid_a", _row(1, sender_id="2")) is None


def test_media_types_ignored():
    db = _FakeDB()
    c = _client(db)
    for t in ("voice", "image", "emotion", "video", "file", "link", "location", "personal_card", "other"):
        assert c._row_to_incoming(db, "wxid_a", _row(1, "", ttype=t)) is None


def test_empty_content_ignored():
    db = _FakeDB()
    c = _client(db)
    assert c._row_to_incoming(db, "wxid_a", _row(1, "   ")) is None


def test_group_detected_by_chatroom_suffix():
    db = _FakeDB()
    c = _client(db, include_group=True)
    m = c._row_to_incoming(db, "999@chatroom", _row(1, "hi", user="999@chatroom"))
    assert m is not None and m.is_group is True


def test_group_content_strips_sender_prefix_and_uses_nickname():
    """群消息库把正文写成「<发送者 wxid>: 正文」：要剥掉前缀、把 wxid 换成人名。"""
    db = _FakeDB(nicknames={"wxid_b": "老冯"})
    c = _client(db, include_group=True)
    m = c._row_to_incoming(db, "999@chatroom", _row(1, "wxid_b: 今晚吃啥", user="wxid_b"))
    assert m is not None
    assert m.content == "今晚吃啥"   # 前缀剥掉，模型看不到 wxid
    assert m.sender == "老冯"        # 换备注名，模型知道是谁在说话


def test_group_prefix_with_newline_is_stripped():
    db = _FakeDB(nicknames={"wxid_b": "老冯"})
    c = _client(db, include_group=True)
    m = c._row_to_incoming(db, "999@chatroom", _row(1, "wxid_b:" + chr(10) + "多行正文", user="wxid_b"))
    assert m is not None and m.content == "多行正文"


def test_friend_content_keeps_raw_text():
    """前缀剥离只针对群聊，好友消息原样保留。"""
    db = _FakeDB(nicknames={"wxid_b": "老冯"})
    c = _client(db)
    m = c._row_to_incoming(db, "wxid_b", _row(1, "wxid_b: 你好", user="wxid_b"))
    assert m is not None and m.content == "wxid_b: 你好"


# ---------- 会话表差分轮询 ----------


def test_first_round_does_not_read_any_session_db():
    """启动那一下不许挨个会话读消息库 —— 那正是原来一轮 40 秒的来源。"""
    db = _FakeDB(sessions=[_session("wxid_%d" % i) for i in range(50)])
    c = _client(db)
    c._poll_once()
    assert db.baselines == 0          # 一条消息库都没打开
    assert db.new_queries == 0
    assert len(c._seen) == 50         # 但 50 个会话的指纹都登记了


def test_first_read_takes_only_messages_after_start():
    db = _FakeDB(sessions=[_session("wxid_a", last_time=5)], nicknames={"wxid_a": "小明"})
    c = _client(db)
    c._poll_once()                    # 第一轮：只登记指纹
    assert db.baselines == 0

    db.messages["wxid_a"] = [
        _row(1, "老消息", user="wxid_a", age=-3600),
        _row(2, "刚到", user="wxid_a", age=1),
    ]
    db.sessions[0]["last_time"] = 6   # 动了
    c._poll_once()
    got = _drain(c)
    assert [m.content for m in got] == ["刚到"]   # 启动前的历史不回放
    assert got[0].chat == "小明"
    assert c._seq["wxid_a"] == 2      # 水位锚点定在最新一条
    assert db.baselines == 1


def test_no_history_replay_when_all_messages_are_old():
    db = _FakeDB(sessions=[_session("wxid_a")])
    c = _client(db)
    c._poll_once()
    db.messages["wxid_a"] = [_row(1, "很久以前", user="wxid_a", age=-7200)]
    db.sessions[0]["last_time"] = 9
    c._poll_once()
    assert _drain(c) == []            # 启动前的旧消息一条都不回
    assert c._seq["wxid_a"] == 1      # 但水位锚点定了


def test_poll_only_reads_changed_sessions():
    """核心：一轮只读"有变化"的会话，而不是全部。"""
    db = _FakeDB(sessions=[_session("wxid_a", 1), _session("wxid_b", 1), _session("wxid_c", 1)],
                 nicknames={"wxid_b": "小明"})
    c = _client(db)
    for u in ("wxid_a", "wxid_b", "wxid_c"):
        db.messages[u] = [_row(1, "hi", user=u, age=-3600)]

    c._poll_once()                       # 第一轮：只登记指纹
    assert db.baselines == 0 and db.new_queries == 0

    db.sessions[1]["last_time"] = 99     # 只有 wxid_b 有新动静
    db.messages["wxid_b"].append(_row(2, "新消息", user="wxid_b", age=0))
    c._poll_once()
    assert db.baselines == 1             # 只打开了它一个的库
    assert db.new_queries == 0           # 它还没水位，这次是"锚点 + 只收新的"
    got = _drain(c)
    assert len(got) == 1 and got[0].chat == "小明"
    assert got[0].content == "新消息"

    db.sessions[1]["last_time"] = 100    # 它又动了一次 → 走水位增量
    db.messages["wxid_b"].append(_row(3, "又一条", user="wxid_b", age=0))
    c._poll_once()
    assert db.new_queries == 1
    assert [m.content for m in _drain(c)] == ["又一条"]


def test_unchanged_sessions_are_not_read_again():
    db = _FakeDB(sessions=[_session("wxid_a", 1)])
    c = _client(db)
    db.messages["wxid_a"] = [_row(1, "hi", user="wxid_a", age=-3600)]
    c._poll_once()
    db.sessions[0]["last_time"] = 2
    db.messages["wxid_a"].append(_row(2, "第二条", user="wxid_a", age=0))
    c._poll_once()
    assert [m.content for m in _drain(c)] == ["第二条"]

    c._poll_once()                        # 会话表没再变 → 不重复读、不重复回
    assert db.baselines == 1 and db.new_queries == 0
    assert _drain(c) == []


def test_watermark_survives_multi_message_burst():
    db = _FakeDB(sessions=[_session("wxid_a", 1)])
    c = _client(db)
    db.messages["wxid_a"] = [_row(1, "旧", user="wxid_a", age=-3600)]
    c._poll_once()
    db.messages["wxid_a"] += [_row(2, "一", user="wxid_a", age=0),
                              _row(3, "二", user="wxid_a", age=0),
                              _row(4, "三", user="wxid_a", age=0)]
    db.sessions[0]["last_time"] = 2
    c._poll_once()
    assert [m.content for m in _drain(c)] == ["一", "二", "三"]
    assert c._seq["wxid_a"] == 4


def test_group_session_not_queried_when_include_group_false():
    db = _FakeDB(sessions=[_session("999@chatroom", 1)])
    c = _client(db, include_group=False)
    c._poll_once()
    db.sessions[0]["last_time"] = 2
    c._poll_once()
    assert db.baselines == 0 and db.new_queries == 0   # 群聊连查都不查


def test_group_session_read_when_include_group_true():
    db = _FakeDB(sessions=[_session("999@chatroom", 1)])
    c = _client(db, include_group=True)
    db.messages["999@chatroom"] = [_row(1, "群里说话", user="999@chatroom", age=-3600)]
    c._poll_once()
    db.messages["999@chatroom"].append(_row(2, "又说话", user="999@chatroom", age=0))
    db.sessions[0]["last_time"] = 2
    c._poll_once()
    assert [m.content for m in _drain(c)] == ["又说话"]


def test_one_broken_query_does_not_kill_the_round():
    db = _FakeDB(sessions=[_session("wxid_bad", 1), _session("wxid_ok", 1)], nicknames={"wxid_ok": "小明"})
    c = _client(db)
    db.boom = {"wxid_bad"}            # 这个会话的库是坏的
    db.messages["wxid_ok"] = [_row(1, "hi", user="wxid_ok", age=-3600)]
    c._poll_once()
    db.sessions[0]["last_time"] = 2
    db.sessions[1]["last_time"] = 2
    db.messages["wxid_ok"].append(_row(2, "第二条", user="wxid_ok", age=0))
    c._poll_once()                    # bad 抛错，但不能影响 ok
    assert [m.content for m in _drain(c)] == ["第二条"]


# ---------- 类型过滤 / 队列 ----------


def test_group_filtered_when_include_group_false():
    c = WeChatClient(include_group=False)
    assert c._accept(IncomingMessage(content="hi", chat="某群", is_group=True)) is False
    assert c._accept(IncomingMessage(content="hi", chat="小明", is_group=False)) is True
    assert c._accept(None) is False


def test_group_kept_when_include_group_true():
    c = WeChatClient(include_group=True)
    assert c._accept(IncomingMessage(content="hi", chat="某群", is_group=True)) is True


def test_discard_pending_clears_queue():
    c = WeChatClient(include_group=False)
    c._queue.put(IncomingMessage(content="旧消息", chat="小明", is_group=False))
    c.discard_pending()
    assert c._queue.empty()


def test_discard_pending_empty_queue_ok():
    WeChatClient().discard_pending()


def test_constants():
    assert "text" in REPLYABLE_TYPES
    assert "quote" in REPLYABLE_TYPES
    assert "image" not in REPLYABLE_TYPES


# ---------- 发送：库报失败时用消息库复核 ----------


class _FailingWx(_FakeWx):
    """模拟 wechatauto 的假失败：UIA 未就绪时它报失败，消息其实已发出。"""

    def SendMsg(self, msg=None, who=None, at=None):
        raise RuntimeError("发送失败：多次重试未完成")


def _self_row(seq, content):
    row = _row(seq, content)
    row["sender_id"] = 2          # 2 = 自己发的
    return row


def test_send_reported_failure_but_db_confirms_is_success():
    """库报失败、但消息库里已有这句 → 判定成功，不抛错（防备"重复发送"）。"""
    db = _FakeDB(messages={"wxid_a": [_self_row(1, "我来啦")]})
    c = _client(db)
    c._wx = _FailingWx(db)
    c._send_confirm_timeout = 0.2
    c.send("小明", "我来啦", chat_id="wxid_a")     # 不抛错即通过


def test_send_reported_failure_without_db_record_raises():
    db = _FakeDB(messages={"wxid_a": [_self_row(1, "别的话")]})
    c = _client(db)
    c._wx = _FailingWx(db)
    c._send_confirm_timeout = 0.2
    with pytest.raises(WeChatNotReadyError):
        c.send("小明", "我来啦", chat_id="wxid_a")


def test_send_without_chat_id_keeps_old_behaviour():
    """没传 chat_id（老调用）→ 不做复核，直接抛。"""
    db = _FakeDB(messages={"wxid_a": [_self_row(1, "我来啦")]})
    c = _client(db)
    c._wx = _FailingWx(db)
    with pytest.raises(WeChatNotReadyError):
        c.send("小明", "我来啦")


def test_failed_row_from_someone_else_does_not_count():
    """别人的同名消息不算我发出去了。"""
    db = _FakeDB(messages={"wxid_a": [_row(1, "我来啦", sender_id=7)]})
    c = _client(db)
    c._wx = _FailingWx(db)
    c._send_confirm_timeout = 0.2
    with pytest.raises(WeChatNotReadyError):
        c.send("小明", "我来啦", chat_id="wxid_a")


# ---------- 只回"个人"：屏蔽群 / 公众号服务号 / 企业微信 / 系统号 ----------


def test_is_person_session_kinds():
    from src.wechat_client import is_person_session

    assert is_person_session("wxid_une88h78z73822") is True      # 个人
    assert is_person_session("gh_a19f4d5e89e5") is False         # 公众号/服务号
    assert is_person_session("54415042263@chatroom") is False    # 群
    assert is_person_session("25984983108031557@openim") is False  # 企业微信
    assert is_person_session("filehelper") is False              # 文件传输助手
    assert is_person_session("notifymessage") is False           # 服务通知
    assert is_person_session("brandservicesessionholder") is False
    assert is_person_session("@placeholder_foldgroup") is False
    assert is_person_session("") is False


def test_service_account_session_is_never_read():
    db = _FakeDB(sessions=[_session("gh_a19f4d5e89e5", 1)],
                 nicknames={"gh_a19f4d5e89e5": "中国建设银行"})
    c = _client(db)
    db.messages["gh_a19f4d5e89e5"] = [_row(1, "账单", user="gh_a19f4d5e89e5", age=-3600)]
    c._poll_once()
    db.messages["gh_a19f4d5e89e5"].append(_row(2, "新账单", user="gh_a19f4d5e89e5", age=0))
    db.sessions[0]["last_time"] = 2
    c._poll_once()
    assert db.baselines == 0 and db.new_queries == 0   # 服务号连消息库都不打开
    assert _drain(c) == []


def test_service_account_is_read_when_only_person_off():
    db = _FakeDB(sessions=[_session("gh_a19f4d5e89e5", 1)])
    c = _client(db, only_person=False)
    db.messages["gh_a19f4d5e89e5"] = [_row(1, "账单", user="gh_a19f4d5e89e5", age=-3600)]
    c._poll_once()
    db.messages["gh_a19f4d5e89e5"].append(_row(2, "新账单", user="gh_a19f4d5e89e5", age=0))
    db.sessions[0]["last_time"] = 2
    c._poll_once()
    assert [m.content for m in _drain(c)] == ["新账单"]


def test_accept_blocks_service_account_queued_anyway():
    c = WeChatClient(include_group=False)
    bank = IncomingMessage(content="账单", chat="中国建设银行", is_group=False,
                           chat_id="gh_a19f4d5e89e5")
    assert c._accept(bank) is False
    friend = IncomingMessage(content="在吗", chat="老冯", is_group=False,
                             chat_id="wxid_u0whqlap0n4x22")
    assert c._accept(friend) is True


def test_accept_does_not_block_groups_by_only_person():
    """群归 include_group 管，两个开关不能打架。"""
    c = WeChatClient(include_group=True, only_person=True)
    g = IncomingMessage(content="hi", chat="某群", is_group=True, chat_id="999@chatroom")
    assert c._accept(g) is True

def test_chinese_type_name_is_replyable():
    """消息库里的 type 是**中文显示名**（「文本」），不是英文 text。

    2026-10-05 三哥报「群里、私聊都不回了」，根因就是这里：代码拿 text/quote 去比
    中文名，于是**每一条消息都被静默丢掉** —— 轮询正常、日志干净、状态显示运行中，
    面板却一条记录都没有。这条测试就是钉住那个夜晚。
    """
    db = _FakeDB(nicknames={'wxid_a': '小明'})
    c = _client(db)
    m = c._row_to_incoming(db, 'wxid_a', _row(1, '在吗', ttype='文本'))
    assert m is not None and m.content == '在吗'


def test_numeric_type_code_is_replyable():
    """库除了中文名还给了数字 type_code（1 = 文本），它变了也照样能回。"""
    db = _FakeDB()
    c = _client(db)
    row = _row(1, '在吗')
    row['type'] = None
    row['type_code'] = 1
    assert c._row_to_incoming(db, 'wxid_a', row) is not None


def test_chinese_non_text_types_ignored():
    """中文名一样要挡住图片/表情/系统消息这些：只回文本（含引用）。"""
    db = _FakeDB()
    c = _client(db)
    for bad in ('图片', '语音', '视频', '动画表情', '位置', '文件/链接/卡片', '系统消息'):
        assert c._row_to_incoming(db, 'wxid_a', _row(1, 'x', ttype=bad)) is None


def test_group_text_row_with_chinese_type_is_replyable():
    """群里那行的真实形状：type=文本、sender_id=10、content 带 wxid 前缀。"""
    db = _FakeDB(nicknames={'wxid_une88h78z73822': '老弟'})
    c = _client(db)
    row = _row(1, 'wxid_une88h78z73822: 三犬', ttype='文本', sender_id=10,
               user='wxid_une88h78z73822')
    m = c._row_to_incoming(db, '999@chatroom', row)
    assert m is not None
    assert m.content == '三犬'          # 前缀剥掉
    assert m.sender == '老弟'           # wxid 换成备注名
    assert m.is_group is True


# ---------- 批次 6：心跳、丢弃计数、群白名单前移 ----------


def test_group_whitelist_moved_to_read_side():
    """批次 6.3：不在白名单的群，读取侧就挡掉（连消息库都不打开）。

    以前白名单只在回复判断那一层生效：名单外的群照样要开一次消息库再丢掉。
    线上实测别的群一直在刷，每一条都白读一遍。
    """
    db = _FakeDB(group_names={"999@chatroom": "闲适老爸小屋", "888@chatroom": "工作群"})
    c = _client(db, include_group=True)
    c.set_group_whitelist(["闲适老爸小屋"])
    assert c._should_read("999@chatroom", db) is True
    assert c._should_read("888@chatroom", db) is False
    assert c.stats()["counts"]["drop_group_not_allowed"] == 1


def test_empty_group_whitelist_reads_every_group():
    """空白名单 = 不限制：必须和 bot.reject_reason 同一套语义，不能两套。"""
    db = _FakeDB(group_names={"888@chatroom": "工作群"})
    c = _client(db, include_group=True)
    c.set_group_whitelist([])
    assert c._should_read("888@chatroom", db) is True
    assert c.stats()["counts"]["drop_group_not_allowed"] == 0


def test_group_name_lookup_failure_lets_group_through():
    """拿不到群名就放行：宁可多读一个群，也不能漏掉该回的群。"""
    db = _FakeDB(groups_boom=True)
    c = _client(db, include_group=True)
    c.set_group_whitelist(["闲适老爸小屋"])
    assert c._should_read("999@chatroom", db) is True


def test_group_whitelist_contains_mode():
    """面板 match_mode=contains 时，白名单按"包含"匹配（与 bot 一致）。"""
    db = _FakeDB(group_names={"999@chatroom": "闲适老爸小屋（3人）"})
    c = _client(db, include_group=True)
    c.set_group_whitelist(["闲适老爸"], "contains")
    assert c._should_read("999@chatroom", db) is True
    c.set_group_whitelist(["闲适老爸"])          # exact
    assert c._should_read("999@chatroom", db) is False


def test_drop_reasons_counted():
    """批次 6.2：每个 return None 都要留一笔 —— 否则面板永远只能看到「没动静」。"""
    db = _FakeDB()
    c = _client(db)
    c._row_to_incoming(db, "wxid_a", _row(1, "在吗", sender_id=2))    # 自己发的
    c._row_to_incoming(db, "wxid_a", _row(2, "x", ttype="图片"))      # 类型不支持
    c._row_to_incoming(db, "wxid_a", _row(3, "   "))                  # 空内容
    assert c._should_read("gh_abc") is False                          # 服务号
    c.set_include_group(False)
    assert c._should_read("999@chatroom") is False                    # 群开关关着
    counts = c.stats()["counts"]
    assert counts["drop_self"] == 1
    assert counts["drop_type"] == 1
    assert counts["drop_empty"] == 1
    assert counts["drop_not_person"] == 1
    assert counts["drop_group_off"] == 1


def test_read_counter_and_last_message_heartbeat():
    """读到真消息要计数、记时间：面板「最近收到消息」就靠它。"""
    db = _FakeDB()
    c = _client(db)
    c._emit(db, "wxid_a", _row(1, "在吗", user="wxid_a", age=0.0))
    st = c.stats()
    assert st["counts"]["read"] == 1
    assert st["last_message_ago"] is not None and st["last_message_ago"] < 5
    assert st["waiting_in_queue"] == 1


def test_poll_heartbeat_alive_and_stale():
    """批次 6.1/6.4：心跳 —— 线程活着且刚转过才算活着（30 秒没动静 = 卡住）。"""
    db = _FakeDB()
    c = _client(db)
    assert c.poll_alive() is False            # 还没起线程
    stop = threading.Event()
    c._poll_thread = threading.Thread(target=stop.wait, daemon=True)
    c._poll_thread.start()
    c._last_poll_at = time.time()
    assert c.poll_alive() is True
    c._last_poll_at = time.time() - 100       # 心跳过期
    assert c.poll_alive() is False
    stop.set()
