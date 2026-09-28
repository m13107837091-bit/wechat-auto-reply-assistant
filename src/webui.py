"""本地 Web 控制面板：纯标准库 http.server，无第三方依赖。

提供页面与 JSON API：

- ``GET  /``            -> 控制面板页面 web/index.html
- ``GET  /api/status``  -> controller.snapshot()
- ``POST /api/start``   -> 开始/恢复自动回复
- ``POST /api/stop``    -> 暂停自动回复
- ``POST /api/persona`` -> 更新人设（body 为 JSON）
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

    # ---- 路由 ----
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._serve_index()
        elif path == "/api/status":
            self._send_json(self.server.controller.snapshot())
        else:
            self._send_json({"error": "not found"}, code=404)

    def do_POST(self) -> None:
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
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ControlServer(ThreadingHTTPServer):
    """把 controller 挂给 handler，让每个请求都能触达机器人。"""

    controller: BotController

    def __init__(self, addr, controller: BotController, handler: type = ControlHandler):
        super().__init__(addr, handler)
        self.controller = controller


def run_server(controller: BotController, host: str = "127.0.0.1", port: int = 8000) -> ControlServer:
    """构建控制面板服务器；调用方负责 serve_forever()。"""
    return ControlServer((host, port), controller)