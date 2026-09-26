"""对 config 的人设叠加与 persona.json 持久化做单元测试。

覆盖：save_persona / _load_persona_overrides 往返，坏文件容错，
_apply_persona_overrides 只覆盖给定字段，以及 AppConfig.load 端到端叠加。
"""
from __future__ import annotations

import json

from src.config import (
    AppConfig,
    _apply_persona_overrides,
    _load_persona_overrides,
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