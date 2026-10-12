"""面板静态资源的护栏。

手机面板是唯一的控制入口，内联 JS 一旦语法错误，整页按钮全废 ——
而 HTML 是静态渲染的，浏览器不报错也能"看起来正常"，很难发现。
这里用最便宜的办法钉住两个已知坑。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "web" / "index.html"


def _html() -> str:
    return INDEX.read_text(encoding="utf-8")


def _inline_script() -> str:
    m = re.search(r"<script>([\s\S]*?)</script>", _html())
    assert m, "index.html 里找不到内联 <script>"
    return m.group(1)


def test_panel_has_group_controls():
    html = _html()
    for el_id in ("groupEnabled", "groupWhitelist", "groupKeywords", "onlyPerson", "whitelist"):
        assert 'id="' + el_id + '"' in html


def test_no_string_literal_split_across_lines():
    """写坏过的坑：源码里的 \\n 变成真换行，字符串被截成两行。

    症状是某行以引号开头（例如  ')); ），整个 <script> 直接语法错误，
    面板不会报错，只是所有按钮都不工作。
    """
    for i, line in enumerate(_inline_script().splitlines(), 1):
        assert not line.startswith(("'", '"')), (
            "第 %d 行以引号开头，像是字符串被换行截断了：%r" % (i, line)
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="没装 node，跳过 JS 语法检查")
def test_panel_script_parses(tmp_path):
    js = tmp_path / "panel.js"
    js.write_text(_inline_script(), encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_panel_has_receive_heartbeat():
    """批次 6.1/6.2：面板必须有「收消息心跳」那一行。

    理由不是好看：2026-10-05 机器人聋了一整晚，面板却一路显示「运行中」。
    没了这一行，同样的故障还是只能靠人猜。"""
    html = _html()
    assert 'id="beatLine"' in html
    assert 'function renderBeat' in html
    assert 'renderBeat(status)' in html, 'render() 里没调用 renderBeat'
    assert 'function fmtAgo' in html
    assert '.beat' in html
