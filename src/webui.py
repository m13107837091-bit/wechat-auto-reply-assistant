"""本地 Web 控制面板：纯标准库 http.server，无第三方依赖。

提供页面与 JSON API：

- ``GET  /``            -> 控制面板页面 web/index.html
- ``GET  /api/status``  -> controller.snapshot()
- ``POST /api/start``   -> 开始/恢复自动回复
- ``POST /api/stop``    -> 暂停自动回复
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
        self._send_json({"error": "需要访问令牌"}, code=401)

    # ---- 路由 ----
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            # 页面本身不含机密，始终可打开（前端会按需提示输入令牌）；
            # 真正的数据与操作都在 /api/* 上鉴权。
            self._serve_index()
            return
        if not self._authorized():
            self._unauthorized()
            return
        if path == "/api/status":
            self._send_json(self.server.controller.snapshot())
        else:
            self._send_json({"error": "not found"}, code=404)

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

    def _serve_index(self) -> None:
        try:
            body = (WEB_DIR / "index.html").read_bytes()
        except OSError:
            self._send_json({"error": "缺 web/index.html"}, code=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # 页面必须每次取最新的：手机浏览器对无缓存头的 HTML 会长期缓存，
        # 一旦缓存到旧版本（例如加令牌逻辑之前的那份），改令牌/点按钮都会「没反应」。
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ControlServer(ThreadingHTTPServer):
    """把 controller 挂给 handler，让每个请求都能触达机器人。"""

    controller: BotController
    token: str

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