"""本地 Web 控制面板：纯标准库 http.server，无第三方依赖。

提供页面与 JSON API：

- ``GET  /``            -> 控制面板页面 web/index.html
- ``GET  /manifest.json`` ``/icon-*.png`` ``/apple-touch-icon.png`` ``/favicon.png``
                       -> 手机主屏图标与 PWA 清单（**免令牌**，见 PUBLIC_FILES）
- ``GET  /api/status``  -> controller.snapshot()
- ``GET  /api/recent``  -> 最近动态（`?limit=N`，默认 50，上限 200）
- ``POST /api/start``   -> 开始/恢复自动回复
- ``POST /api/stop``    -> 暂停自动回复
- ``GET  /api/lists``   -> 黑白名单（含 open_to_all 提示）
- ``POST /api/lists``   -> 更新黑白名单（body 为 JSON，改完立即生效）
- ``POST /api/persona`` -> 更新人设（body 为 JSON）
- ``POST /api/llm``     -> 更新 LLM 连接设置（key/base_url/model）
- ``POST /api/llm/test``-> 连通性自检

鉴权：设置环境变量 ``UI_TOKEN`` 后，所有 ``/api/*`` 请求需带令牌
（``Authorization: Bearer`` / ``X-Auth-Token`` / cookie ``ui_token`` / ``?token=`` 任一）。
页面本身不含机密、始终可打开；不设 ``UI_TOKEN`` 则完全维持原有无鉴权行为。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .controller import BotController

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 免令牌的静态资源：页面、图标、PWA 清单。
# 两个理由：① 这些文件不含任何机密（令牌、聊天内容都不在里面）；
# ② 浏览器抓取 HTML 的子资源（图标、manifest）时**不会**带 Authorization 头，
#    纳入鉴权只会让图标裂图、"添加到主屏幕"失败。真正的数据与操作都在 /api/* 上鉴权。
PUBLIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/manifest.json": "manifest.json",
    "/favicon.png": "favicon.png",
    "/favicon.ico": "favicon.ico",
    "/icon-192.png": "icon-192.png",
    "/icon-512.png": "icon-512.png",
    "/apple-touch-icon.png": "apple-touch-icon.png",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class ControlHandler(BaseHTTPRequestHandler):
    server: "ControlServer"  # 由 ControlServer 注入

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 匹配基类签名
        log.debug("%s %s", self.address_string(), format % args)

    # ---- 工具 ----
    def _send_json(self, obj: object, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # 状态数据实时性优先，禁止浏览器/中间层缓存（否则手机可能拿到过期的 401/状态）。
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # ---- 鉴权（可选：仅当设置了 UI_TOKEN 才启用）----
    def _presented_token(self) -> str:
        """取出客户端出示的令牌：Bearer 头 / X-Auth-Token / cookie / ?token= 任一。"""
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        header = (self.headers.get("X-Auth-Token") or "").strip()
        if header:
            return header
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "ui_token":
                return value.strip()
        values = parse_qs(urlparse(self.path).query).get("token")
        return values[0].strip() if values else ""

    def _authorized(self) -> bool:
        """未配置令牌则一律放行（保持本地默认行为）；配置了则恒定时间比较。"""
        token = self.server.token
        if not token:
            return True
        return secrets.compare_digest(self._presented_token(), token)

    def _unauthorized(self) -> None:
        # 先把请求体读掉再回 401。不读的话，客户端还在往外发 body、服务端已经走人，
        # Windows 上会变成「连接被中止」而不是一个干净的 401（面板只能报网络错误，
        # 看不出其实是令牌不对）。
        self._read_body()
        self._send_json({"error": "需要访问令牌"}, code=401)

    # ---- 路由 ----
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in PUBLIC_FILES:
            # 页面/图标/清单不含机密，始终可打开（前端会按需提示输入令牌）；
            # 真正的数据与操作都在 /api/* 上鉴权。
            self._serve_static(PUBLIC_FILES[path])
            return
        if not self._authorized():
            self._unauthorized()
            return
        if path == "/api/status":
            self._send_json(self.server.controller.snapshot())
        elif path == "/api/recent":
            self._send_json(self.server.controller.activity.snapshot(self._recent_limit()))
        elif path == "/api/lists":
            self._send_json(self.server.controller.get_lists())
        else:
            self._send_json({"error": "not found"}, code=404)

    # 「最近动态」一次最多回多少条。200 与缓冲容量一致 —— 要更大也只是把整个缓冲
    # 倒出来，没有意义，但必须挡住离谱的 limit 值（面板在公网上是可达的）。
    MAX_RECENT = 200
    DEFAULT_RECENT = 50

    def _recent_limit(self) -> int:
        """解析 `?limit=`；非法或缺失退回默认值，不因为一个坏参数就 500。"""
        values = parse_qs(urlparse(self.path).query).get("limit")
        if not values:
            return self.DEFAULT_RECENT
        try:
            limit = int(values[0])
        except (TypeError, ValueError):
            return self.DEFAULT_RECENT
        return max(0, min(limit, self.MAX_RECENT))

    def do_POST(self) -> None:
        if not self._authorized():
            self._unauthorized()
            return
        path = urlparse(self.path).path
        controller = self.server.controller
        if path == "/api/start":
            ok = controller.start()
            self._send_json({"ok": ok, **controller.snapshot()}, code=200 if ok else 500)
        elif path == "/api/stop":
            controller.stop()
            self._send_json({"ok": True, **controller.snapshot()})
        elif path == "/api/reconnect":
            # 后台重连：立刻返回，进度看 /api/status 的 warm.state
            started = controller.reconnect()
            self._send_json({"ok": started, **controller.snapshot()})
        elif path == "/api/lists":
            try:
                updated = controller.update_lists(self._read_body())
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, code=400)
                return
            self._send_json({"ok": True, "lists": updated})
        elif path == "/api/persona":
            try:
                updated = controller.update_persona(self._read_body())
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, code=400)
                return
            self._send_json({"ok": True, "persona": updated})
        elif path == "/api/llm":
            result = controller.update_llm(self._read_body())
            self._send_json(result, code=200 if result.get("ok") else 400)
        elif path == "/api/llm/test":
            result = controller.test_llm()
            self._send_json(result, code=200 if result.get("ok") else 400)
        else:
            self._send_json({"error": "not found"}, code=404)

    def _serve_static(self, name: str) -> None:
        """发一个免令牌的静态文件（页面 / 图标 / PWA 清单）。"""
        file = WEB_DIR / name
        try:
            body = file.read_bytes()
        except OSError:
            self._send_json({"error": "缺 web/" + name}, code=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
        if file.suffix == ".html":
            # 页面必须每次取最新的：手机浏览器对无缓存头的 HTML 会长期缓存，
            # 一旦缓存到旧版本（例如加令牌逻辑之前的那份），改令牌/点按钮都会「没反应」。
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
        else:
            # 图标/清单内容稳定，缓存一小时：既省流量，改图标后也不用等太久才更新。
            self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ControlServer(ThreadingHTTPServer):
    """把 controller 挂给 handler，让每个请求都能触达机器人。"""

    controller: BotController
    token: str

    # http.server.HTTPServer 默认 allow_reuse_address = 1（即 SO_REUSEADDR）。
    # 在 Windows 上这个选项的语义与 POSIX 不同：它允许两个进程**同时**绑定同一
    # 个端口，而且两边都会成功。实测后果是「端口被占用」的保护完全失效——两个
    # 后端一起跑，面板请求被随机分给其中之一，甚至造成重复回复。
    # 关掉它，第二个实例就会在 bind 时直接拿到 OSError 并退出（见 main.py 的提示）。
    allow_reuse_address = False

    def __init__(
        self,
        addr,
        controller: BotController,
        handler: type = ControlHandler,
        token: str = "",
    ):
        super().__init__(addr, handler)
        self.controller = controller
        self.token = token


def run_server(
    controller: BotController,
    host: str = "127.0.0.1",
    port: int = 8000,
    token: str | None = None,
) -> ControlServer:
    """构建控制面板服务器；调用方负责 serve_forever()。

    ``token`` 为 None 时读环境变量 ``UI_TOKEN``；留空则不启用鉴权（保持本地默认行为）。
    """
    if token is None:
        token = os.getenv("UI_TOKEN", "").strip()
    return ControlServer((host, port), controller, token=token)