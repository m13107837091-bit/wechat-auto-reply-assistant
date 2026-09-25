"""程序入口：加载配置 → 连接微信 → 监听消息 → 自动回复。"""
from __future__ import annotations

import logging
import sys
import time

from .bot import Bot
from .config import AppConfig
from .llm import LLMClient, LLMError
from .session import SessionStore
from .wechat_client import WeChatClient, WeChatNotReadyError

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
    bot = Bot(config, llm, sessions)
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
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())