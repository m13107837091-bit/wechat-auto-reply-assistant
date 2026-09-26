"""程序入口：启动自动回复 + 本地 Web 控制面板。

运行后会自动开始监听并起一个本地控制面板（默认 http://127.0.0.1:8000），
浏览器里可改人设、暂停/恢复自动回复。支持两种启动方式：

- ``python -m src.main``
- 直接在 PyCharm 里右键运行本文件（脚本方式）

环境变量：``UI_HOST``（默认 127.0.0.1）、``UI_PORT``（默认 8000）。
"""
from __future__ import annotations

import logging
import os
import sys

# 脚本方式直接运行（python src/main.py）时，把项目根目录加入 sys.path，
# 这样才能 `import src.*`，同时让 src 内部模块的相对导入正常生效。
if __package__ in (None, ""):
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from src.controller import BotController
from src.webui import run_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wechat-bot")


def main() -> int:
    controller = BotController()

    host = os.getenv("UI_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("UI_PORT", "8000"))
    except ValueError:
        log.error("UI_PORT 必须是数字，当前值为 %r", os.getenv("UI_PORT"))
        return 2

    # 自动开始自动回复（维持「一运行就回消息」的旧行为）；失败不退出，面板照常显示错误。
    if not controller.start():
        log.error("%s", controller.last_error)

    try:
        server = run_server(controller, host, port)
    except OSError as exc:
        log.error("无法启动控制面板于 http://%s:%d（端口被占用？）: %s", host, port, exc)
        controller.stop()
        return 1

    log.info("控制面板：http://%s:%d  （Ctrl+C 退出）", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("正在退出……")
    finally:
        controller.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())