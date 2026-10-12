"""webui 的 HTTP 层测试：新加的 /api/recent 路由、鉴权与 limit 夹取。

不起真实后端 —— 控制器换成假的，只提供 snapshot() 与 activity。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from src.activity import ActivityLog
from src.webui import WEB_DIR, ControlServer


class _FakeController:
    def __init__(self) -> None:
        self.activity = ActivityLog()
        self.activity.record(action="replied", chat="张三", incoming="在吗", outgoing="在的")
        self.activity.record(action="skipped", chat="李四", incoming="帮我带个饭", reason="night_silence")
        self.lists = {"whitelist": ["老冯"], "blacklist": [], "match_mode": "exact", "open_to_all": False}
        self.reconnects = 0

    def snapshot(self) -> dict:
        return {"running": False, "starting": False, "last_error": "", "persona": {}, "llm": {}}

    def reconnect(self) -> bool:
        self.reconnects += 1
        return True

    def get_lists(self) -> dict:
        return dict(self.lists)

    def update_lists(self, overrides: dict) -> dict:
        if "boom" in overrides:                     # 给 400 分支留的触发点
            raise ValueError("名单不合法")
        for key in ("whitelist", "blacklist"):
            if key in overrides:
                self.lists[key] = overrides[key]
        self.lists["open_to_all"] = not self.lists["whitelist"]
        return dict(self.lists)


@pytest.fixture()
def server():
    srv = ControlServer(("127.0.0.1", 0), _FakeController(), token="secret")
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _post(srv, path, payload=None, token="secret"):
    data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (srv.server_address[1], path), data=data, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _get(srv, path, token="secret"):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (srv.server_address[1], path))
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_recent_returns_items_and_counts(server):
    status, body = _get(server, "/api/recent")
    assert status == 200
    data = json.loads(body)
    assert [i["chat"] for i in data["items"]] == ["李四", "张三"]  # 新的在前
    assert data["counts"]["today_replied"] == 1
    assert data["counts"]["today_skipped"] == 1


def test_recent_requires_token(server):
    assert _get(server, "/api/recent", token="")[0] == 401


def test_recent_limit_is_clamped(server):
    assert len(json.loads(_get(server, "/api/recent?limit=1")[1])["items"]) == 1
    assert len(json.loads(_get(server, "/api/recent?limit=9999")[1])["items"]) == 2
    assert json.loads(_get(server, "/api/recent?limit=0")[1])["items"] == []
    # 非法值退回默认，而不是 500
    assert len(json.loads(_get(server, "/api/recent?limit=abc")[1])["items"]) == 2


def test_index_is_served_without_token(server):
    status, body = _get(server, "/", token="")
    assert status == 200
    assert "微信自动回复" in body


def test_panel_poll_loop_reschedules_itself():
    """面板的轮询必须有「自己接自己」那一下。

    漏掉它，界面就永远停在页面打开那一刻的状态 —— 2026-10-05 真的漏过一次，
    结果面板卡在「连接中…」一动不动，人以为一直在连微信。
    这段没法用普通单测跑（要浏览器），所以至少守住这个结构不变量。
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    # 全文正好两处：一处是启动，一处是回调里把链条接回去。
    assert html.count("scheduleRefresh();") == 2, "轮询链条又被改断了？"
    # 自我调用必须在 setTimeout 的回调里面 —— 放在回调外面就是空转/死循环
    fn = html.split("function scheduleRefresh()", 1)[1]
    callback = fn.split("setTimeout(() => {", 1)[1].split("}, delay);", 1)[0]
    assert "scheduleRefresh();" in callback, "回调里没有把轮询链条接回去"


# ---------- 黑白名单 ----------


def test_lists_returns_state(server):
    status, body = _get(server, "/api/lists")
    assert status == 200
    data = json.loads(body)
    assert data["whitelist"] == ["老冯"]
    assert data["open_to_all"] is False


def test_lists_requires_token(server):
    assert _get(server, "/api/lists", token="")[0] == 401
    assert _post(server, "/api/lists", {}, token="")[0] == 401


def test_post_lists_updates_and_returns_state(server):
    status, body = _post(server, "/api/lists", {"whitelist": ["甲", "乙"], "blacklist": ["广告"]})
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["lists"]["whitelist"] == ["甲", "乙"]
    assert data["lists"]["blacklist"] == ["广告"]
    assert server.controller.lists["whitelist"] == ["甲", "乙"]   # 真的写进控制器了


def test_post_lists_bad_body_returns_400(server):
    status, body = _post(server, "/api/lists", {"boom": True})
    assert status == 400
    assert json.loads(body)["ok"] is False


def test_panel_does_not_overwrite_fields_being_edited():
    """render() 每 5 秒跑一次；写值必须避开用户正在编辑的输入框。

    漏掉这一点，正在敲的提示词/性格会被原地重置回旧值 —— 用户看到的就是
    「改了没反应」（2026-10-05 三哥在 exe 里报的 bug）。这类行为只能靠结构守住。
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    render = html.split("function render()", 1)[1].split("// ---- 最近动态 ----", 1)[0]
    for field in ("name", "style", "systemPrompt", "length"):
        assert "setUnlessFocused('%s'" % field in render, field
    # 旧写法必须彻底消失，否则"正在输入被冲掉"会悄悄回来
    for old in ("getElementById('name').value =", "getElementById('style').value =",
                "getElementById('systemPrompt').value =", "getElementById('length').value ="):
        assert old not in render, old

    lists_fn = html.split("function renderLists()", 1)[1]
    assert "setUnlessFocused('whitelist'" in lists_fn
    assert "setUnlessFocused('blacklist'" in lists_fn


def test_panel_has_only_person_toggle_and_sends_it():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="onlyPerson"' in html
    assert "only_person: document.getElementById('onlyPerson').checked" in html
    assert "lists.only_person !== false" in html


def _get_bytes(srv, path, token=""):
    """和 _get 一样，但要原始字节（PNG 不是文本）。"""
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (srv.server_address[1], path))
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def test_pwa_assets_are_served_without_token(server):
    """图标与清单必须免令牌：浏览器抓 HTML 的子资源时不会带 Authorization 头，
    纳入鉴权只会让图标裂图、"添加到主屏幕"失败。"""
    for path, ctype in (("/manifest.json", "application/manifest+json"),
                        ("/icon-192.png", "image/png"),
                        ("/icon-512.png", "image/png"),
                        ("/apple-touch-icon.png", "image/png"),
                        ("/favicon.png", "image/png")):
        status, got, body = _get_bytes(server, path)
        assert status == 200, path
        assert got.startswith(ctype), (path, got)
        assert body, path


def test_manifest_is_valid_and_its_icons_exist(server):
    status, body = _get(server, "/manifest.json", token="")
    assert status == 200
    data = json.loads(body)
    assert data["start_url"] == "./" and data["display"] == "standalone"
    assert data["theme_color"] == "#101418"          # 与面板底色一致，状态栏才无缝
    assert {"192x192", "512x512"} <= {i["sizes"] for i in data["icons"]}
    for icon in data["icons"]:                        # 声明的图标必须真的存在
        assert (WEB_DIR / icon["src"].lstrip("./")).exists(), icon["src"]


def test_icons_are_real_png_with_expected_size(server):
    for name, px in (("icon-192.png", 192), ("icon-512.png", 512),
                     ("apple-touch-icon.png", 180), ("favicon.png", 48)):
        status, _, body = _get_bytes(server, "/" + name)
        assert status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n", name
        # 宽高写在 IHDR 里（第 16..24 字节）—— 防的是"把 404 页面当图标发出去"
        assert int.from_bytes(body[16:20], "big") == px, name
        assert int.from_bytes(body[20:24], "big") == px, name


def test_only_whitelisted_static_paths_are_public(server):
    """免令牌只限白名单里那几个文件，别顺手把整个 web 目录暴露出去。"""
    assert _get(server, "/etc/passwd", token="")[0] == 401
    assert _get(server, "/manifest.json.bak", token="")[0] == 401
    assert _get(server, "/", token="")[0] == 200      # 页面照旧可开


def test_reconnect_posts_and_returns_snapshot(server):
    """面板那个「重连」按钮：后台重连 + 立刻返回快照（进度靠轮询状态）。"""
    assert _post(server, "/api/reconnect", {}, token="")[0] == 401
    status, body = _post(server, "/api/reconnect", {})
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["running"] is False        # 一起回快照，面板拿到就能刷新
    assert server.controller.reconnects == 1
