"""程序入口：加载配置 → 连接微信 → 监听消息 → 自动回复。

支持两种启动方式：`python -m src.main`，或直接在 PyCharm 里右键运行本文件（脚本方式）。
"""
from __future__ import annotations

import logging
import os
import sys
import time

# 脚本方式直接运行（python src/main.py）时，把项目根目录加入 sys.path，
# 这样才能 `import src.*`，同时让 src 内部模块的相对导入正常生效。
if __package__ in (None, ""):
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from src.bot import Bot
from src.config import AppConfig
from src.llm import LLMClient, LLMError
from src.search import build_search
from src.session import SessionStore
from src.wechat_client import WeChatClient, WeChatNotReadyError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wechat-bot")


def main() -> int:
    config = AppConfig.load()

    try:
        llm = LLMClient(config.llm)
    except LLMError as exc:
        log.error("LLM 配置错误：%s", exc)
        return 2

    sessions = SessionStore(config.session.max_turns, config.session.ttl_seconds)
    search = build_search(config.search)
    bot = Bot(config, llm, sessions, search)
    wechat = WeChatClient()

    try:
        log.info("正在连接 PC 微信……")
        wechat.prepare()
    except WeChatNotReadyError as exc:
        log.error("%s", exc)
        return 1

    log.info("已就绪，开始监听消息（Ctrl+C 退出）。")

    while True:
        try:
            for msg in wechat.get_new_messages():
                reply = bot.on_message(msg.chat, msg.content, msg.is_group, msg.sender, msg.at_list)
                if reply:
                    wechat.send(msg.chat, reply)
                    log.info("回复 %s: %s", msg.chat, reply[:40])
        except WeChatNotReadyError as exc:
            log.error("%s；5 秒后重试……", exc)
            time.sleep(5)
        except KeyboardInterrupt:
            log.info("已退出。")
            return 0
        except Exception as exc:  # noqa: BLE001 - 主循环保持存活
            log.exception("处理消息时出错: %s", exc)
            time.sleep(1)
        time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main())