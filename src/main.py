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
from src.wechat_client import IncomingMessage, WeChatClient, WeChatNotReadyError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wechat-bot")


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
            for msg in _coalesce(wechat.get_new_messages()):
                # 读取阶段：消息从「监听线程入队」到「主循环取到」的等待时长。
                # received_at 是监听线程入队时打的时间戳，值越大说明越积压、读取越慢。
                if msg.received_at:
                    log.info("[读取] %s 的新消息，排队等待 %.2fs", msg.chat, time.time() - msg.received_at)
                else:
                    log.info("[读取] %s 的新消息", msg.chat)

                gen_start = time.time()
                log.info("[生成] 正在给 %s 生成回复中……", msg.chat)
                reply = bot.on_message(msg.chat, msg.content, msg.is_group, msg.sender, msg.at_list)
                gen_cost = time.time() - gen_start

                if reply:
                    send_start = time.time()
                    wechat.send(msg.chat, reply)
                    send_cost = time.time() - send_start
                    log.info(
                        "[发送] %s：生成 %.2fs / 发送 %.2fs | %s",
                        msg.chat, gen_cost, send_cost, reply[:40],
                    )
                else:
                    log.info("[生成] %s 无需回复（耗时 %.2fs）", msg.chat, gen_cost)
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