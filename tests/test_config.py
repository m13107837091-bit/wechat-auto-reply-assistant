"""对 config 的人设叠加与 persona.json 持久化做单元测试。

覆盖：save_persona / _load_persona_overrides 往返，坏文件容错，
_apply_persona_overrides 只覆盖给定字段，以及 AppConfig.load 端到端叠加。
"""
from __future__ import annotations

import json

from src.config import (
    AppConfig,
    _apply_lists_overrides,
    _apply_persona_overrides,
    _load_lists,
    _load_persona_overrides,
    save_lists,
    save_persona,
)


def test_save_and_load_persona_roundtrip(tmp_path):
    p = tmp_path / "persona.json"
    overrides = {
        "name": "小刚",
        "style": "冷淡",
        "emoji": False,
        "length": "short",
        "system_prompt": "你是小刚。",
    }
    save_persona(overrides, p)
    assert json.loads(p.read_text(encoding="utf-8")) == overrides
    assert _load_persona_overrides(p) == overrides


def test_load_persona_missing_file_returns_empty(tmp_path):
    assert _load_persona_overrides(tmp_path / "nope.json") == {}


def test_load_persona_corrupt_file_returns_empty(tmp_path):
    p = tmp_path / "persona.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert _load_persona_overrides(p) == {}


def test_apply_overrides_only_touches_given_keys(tmp_path):
    p = tmp_path / "persona.json"
    save_persona({"name": "小刚", "emoji": False}, p)
    cfg = AppConfig()
    old_style = cfg.persona.style
    old_prompt = cfg.llm.system_prompt
    _apply_persona_overrides(cfg, p)
    assert cfg.persona.name == "小刚"
    assert cfg.persona.emoji is False
    assert cfg.persona.style == old_style  # 未覆盖字段保持默认
    assert cfg.llm.system_prompt == old_prompt


def test_appconfig_load_applies_persona_overlay(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("persona:\n  name: 默认名\n", encoding="utf-8")
    persona_path = tmp_path / "persona.json"
    save_persona({"name": "界面改的名", "system_prompt": "新提示词"}, persona_path)
    cfg = AppConfig.load(config_path=cfg_path, persona_path=persona_path)
    assert cfg.persona.name == "界面改的名"
    assert cfg.llm.system_prompt == "新提示词"


def test_appconfig_load_without_persona_file_uses_defaults(tmp_path):
    cfg = AppConfig.load(config_path=tmp_path / "config.yaml", persona_path=tmp_path / "none.json")
    assert cfg.persona.name == "小助手"  # 默认值兜底
    assert cfg.persona.length == "auto"


# ---------- 黑白名单（面板可编辑，存 lists.local.json） ----------


def test_save_and_load_lists_roundtrip(tmp_path):
    p = tmp_path / "lists.local.json"
    save_lists({"whitelist": ["老冯"], "blacklist": ["广告号"]}, p)
    assert _load_lists(p) == {"whitelist": ["老冯"], "blacklist": ["广告号"]}


def test_load_lists_missing_or_corrupt_returns_empty(tmp_path):
    assert _load_lists(tmp_path / "nope.json") == {}
    p = tmp_path / "lists.local.json"
    p.write_text("{坏掉的 json", encoding="utf-8")
    assert _load_lists(p) == {}


def test_apply_lists_overrides_only_touches_given_keys(tmp_path):
    p = tmp_path / "lists.local.json"
    save_lists({"whitelist": ["老冯"]}, p)
    cfg = AppConfig()
    cfg.reply.blacklist = ["广告号"]
    _apply_lists_overrides(cfg, p)
    assert cfg.reply.whitelist == ["老冯"]
    assert cfg.reply.blacklist == ["广告号"]  # 文件里没这个键 → 保持原值


def test_appconfig_load_applies_lists_overlay(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "reply:\n  whitelist: [yaml里的白]\n  blacklist: [yaml里的黑]\n", encoding="utf-8"
    )
    lists_path = tmp_path / "lists.local.json"
    save_lists({"whitelist": ["面板改的白"]}, lists_path)
    cfg = AppConfig.load(
        config_path=cfg_path,
        persona_path=tmp_path / "none.json",
        settings_path=tmp_path / "none.json",
        lists_path=lists_path,
    )
    assert cfg.reply.whitelist == ["面板改的白"]   # 面板改过的覆盖 yaml
    assert cfg.reply.blacklist == ["yaml里的黑"]    # 没改过的继续用 yaml


def test_appconfig_load_reads_only_person(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("reply:\n  friend:\n    only_person: false\n", encoding="utf-8")
    cfg = AppConfig.load(config_path=cfg_path, persona_path=tmp_path / "none.json",
                         settings_path=tmp_path / "none.json", lists_path=tmp_path / "none.json")
    assert cfg.reply.friend_only_person is False


def test_lists_overlay_can_flip_only_person(tmp_path):
    p = tmp_path / "lists.local.json"
    save_lists({"only_person": False}, p)
    cfg = AppConfig()
    _apply_lists_overrides(cfg, p)
    assert cfg.reply.friend_only_person is False


def test_appconfig_load_reads_group_whitelist_and_keywords(tmp_path):
    """群聊：只回哪些群 + 群里命中哪些词才回。"""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "reply:\n"
        "  group:\n"
        "    enabled: true\n"
        "    only_when_mentioned: false\n"
        "    whitelist:\n"
        "      - 闲适老爸小屋\n"
        "    trigger_keywords:\n"
        "      - 三哥\n"
        "      - 老三\n",
        encoding="utf-8",
    )
    cfg = AppConfig.load(config_path=cfg_path, persona_path=tmp_path / "none.json",
                         settings_path=tmp_path / "none.json", lists_path=tmp_path / "none.json")
    assert cfg.reply.group_enabled is True
    assert cfg.reply.group_only_when_mentioned is False
    assert cfg.reply.group_whitelist == ["闲适老爸小屋"]
    assert cfg.reply.group_trigger_keywords == ["三哥", "老三"]


def test_lists_overlay_can_change_group_lists(tmp_path):
    """面板改的群设置存在 lists.local.json，重启后仍然生效。"""
    p = tmp_path / "lists.local.json"
    save_lists({
        "group_whitelist": ["闲适老爸小屋"],
        "group_trigger_keywords": ["三哥"],
        "group_enabled": False,
    }, p)
    cfg = AppConfig()
    _apply_lists_overrides(cfg, p)
    assert cfg.reply.group_whitelist == ["闲适老爸小屋"]
    assert cfg.reply.group_trigger_keywords == ["三哥"]
    assert cfg.reply.group_enabled is False

def test_appconfig_load_reads_fallback_replies(tmp_path):
    """兜底话术可以配多条（大模型出错时随机挑一句）。"""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "reply:\n"
        "  fallback_replies:\n"
        "    - 甲兜底\n"
        "    - 乙兜底\n",
        encoding="utf-8",
    )
    cfg = AppConfig.load(config_path=cfg_path, persona_path=tmp_path / "none.json")
    assert cfg.reply.fallback_replies == ["甲兜底", "乙兜底"]
    assert AppConfig().reply.fallback_replies == []   # 默认空：回落到单条 fallback_reply
