"""动态组装 system prompt：基础人设 + persona 风格 + 当前时间上下文。"""
from __future__ import annotations

from datetime import datetime

from .config import AppConfig

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _now_text(now: datetime) -> str:
    return f"{now.year}年{now.month}月{now.day}日 {_WEEKDAYS[now.weekday()]} {now.hour:02d}:{now.minute:02d}"


def build_system_prompt(cfg: AppConfig, now: datetime | None = None) -> str:
    """组合基础人设、persona 风格、当前时间与长度/多样性指令。

    `now` 用于测试注入固定时间，缺省取当前系统时间。
    """
    persona = cfg.persona
    now = now or datetime.now()

    parts: list[str] = [cfg.llm.system_prompt.strip()]

    parts.append(f"你叫{persona.name}，说话风格是「{persona.style}」。")

    if persona.emoji:
        parts.append("可以适度插入一个 emoji 让语气更自然，但不要堆砌、不要每句都用。")
    else:
        parts.append("回复中不要使用 emoji。")

    if persona.length == "short":
        parts.append("回复尽量简短，一两句话即可。")
    elif persona.length == "detailed":
        parts.append("回答要详细、有条理，必要时分点说明。")
    else:  # auto
        parts.append("回复长度贴合对方：短句寒暄就简短回应，复杂问题就详细解答。")

    parts.append(
        f"当前时间是 {_now_text(now)}。涉及“现在/今天/最近”等问题时，请结合这个时间作答，不要凭空猜测。"
    )

    parts.append("同一个意思换着说法，不要每次都套一样的句式和开场白，让回复更像真人聊天。")

    return "\n".join(parts)