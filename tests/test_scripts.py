"""脚本文件的"编码/换行"保护性检查 —— 这两类坑都真踩过（2026-10-05）。

1) `scripts/*.ps1` 必须存成 UTF-8 **带 BOM**。PowerShell 5.1 对无 BOM 的 .ps1 会按系统
   ANSI 代码页（中文 Windows 是 GBK）解码，脚本里的中文注释/字符串会破坏引号配对，
   整个脚本变成语法错误。表现极具迷惑性：计划任务"启动成功"但立刻退出
   （LastTaskResult=1），面板再也起不来。当天就是改看门狗时用编辑器丢了 BOM。

2) `scripts/*.cmd` 必须用 **CRLF**。cmd.exe 不认 LF-only 的批处理，会把相邻命令行
   粘成一条，于是既不报"文件有错"、也不干活（一堆 "xxx is not recognized"）。
   同理 .cmd **不能**有 BOM：`@echo off` 前面多三个字节会让第一行认不出来。

顺带钉住看门狗的两条行为约定（都是当天修的真问题：
「点了退出它自己又回来」和「两个看门狗抢着拉壳」）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

PS1 = sorted(SCRIPTS.glob("*.ps1"))
CMD = sorted(SCRIPTS.glob("*.cmd"))
WATCHDOG = SCRIPTS / "backend-loop.ps1"


@pytest.mark.parametrize("path", PS1, ids=[p.name for p in PS1])
def test_ps1_keeps_utf8_bom(path: Path):
    assert path.read_bytes()[:3] == b"\xef\xbb\xbf", (
        f"{path.name} 丢了 UTF-8 BOM —— PowerShell 5.1 会按 GBK 解析，脚本直接语法错误")


@pytest.mark.parametrize("path", CMD, ids=[p.name for p in CMD])
def test_cmd_uses_crlf_and_has_no_bom(path: Path):
    raw = path.read_bytes()
    assert raw[:3] != b"\xef\xbb\xbf", f"{path.name} 不该有 BOM（cmd.exe 第一行会认不出来）"
    assert b"\r\n" in raw, f"{path.name} 必须是 CRLF 换行"
    # 不能混进"裸 LF"：把 CRLF 都去掉之后还剩 \n 就说明混了
    assert b"\n" not in raw.replace(b"\r\n", b""), f"{path.name} 里混进了 LF-only 的换行"


def test_watchdog_exits_zero_when_user_quits():
    """用户从托盘点「退出」后，脚本必须以 0 退出。

    不然计划任务会把它当成失败，按 RestartCount=3 重新拉起来 ——
    也就是「点了退出它自己又回来了」（当天实测复现）。
    """
    text = WATCHDOG.read_text(encoding="utf-8-sig")
    assert "桌面壳正常退出" in text
    quit_branch = text.split("桌面壳正常退出")[1][:500]
    assert "exit 0" in quit_branch


def test_watchdog_refuses_to_start_a_second_shell():
    """已经有壳在跑时不能再拉一个：两个壳会抢 8000 端口，后启的必然失败 → 重试风暴。"""
    text = WATCHDOG.read_text(encoding="utf-8-sig")
    assert "已在运行" in text


def test_start_cmd_and_installer_agree_on_task_name():
    """一键启动脚本和安装脚本必须指同一个计划任务，否则双击图标等于什么都没干。"""
    installer = (SCRIPTS / "install-autostart.ps1").read_text(encoding="utf-8-sig")
    starter = (SCRIPTS / "start-backend.cmd").read_text(encoding="utf-8-sig")
    name = re.search(r"TaskName\s*=\s*'([^']+)'", installer)
    assert name, "install-autostart.ps1 里找不到 $TaskName"
    assert 'set "TASK=%s"' % name.group(1) in starter
    assert '/TN "%TASK%"' in starter
