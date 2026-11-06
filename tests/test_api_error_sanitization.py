"""建-4 回归：API 不得把异常原文拼进对外消息。"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_web_server_does_not_return_raw_exceptions():
    text = (ROOT / "web_server.py").read_text(encoding="utf-8")
    bad = re.findall(r"returnMsg\(False,\s*f?[\"'][^\"']*\{e(?::!s)?\}", text)
    assert bad == []
