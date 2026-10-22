"""bt P6（2026-10-15）单元测试：工具结果精简+归档。

- archive_result：大结果精简（头尾保留+折叠标记）、小结果原样、可禁用
- read_tool_archive：完整召回 / 分段读取 / 非法 id 防护
- XML 结构：tool_name/duration 元信息保留，仅正文折叠
- archive_stats：统计

运行：python3 -m pytest tests/test_optimizations_p6.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat_client.tool_archive import (  # noqa: E402
    archive_result,
    archive_stats,
    read_tool_archive,
)

CFG = {
    "tool_result_archive": True,
    "tool_result_max_chars": 500,
    "tool_result_head_chars": 150,
    "tool_result_tail_chars": 60,
}


def _big_text(rows: int = 100) -> str:
    return "第1行 数据..." + "\n" + "\n".join(f"row-{i} " + "x" * 30 for i in range(rows)) + "\n最后一行 结果OK"


class TestArchiveResult:
    def test_big_result_slimmed_and_archived(self, tmp_path):
        slim, meta = archive_result(_big_text(), base_dir=str(tmp_path),
                                    session_id="s1", tool_name="Bash", config=CFG)
        assert len(slim) < len(_big_text())
        assert meta is not None and meta["id"]
        assert "ReadToolArchive" in slim  # 折叠标记带召回提示
        assert "最后一行" in slim          # 尾部保留
        assert "第1行" in slim             # 头部保留

    def test_small_result_unchanged(self, tmp_path):
        s, m = archive_result("ok", base_dir=str(tmp_path), session_id="s1",
                              tool_name="Bash", config=CFG)
        assert s == "ok" and m is None

    def test_disabled_returns_original(self, tmp_path):
        cfg = {**CFG, "tool_result_archive": False}
        s, m = archive_result(_big_text(), base_dir=str(tmp_path),
                              session_id="s1", tool_name="Bash", config=cfg)
        assert m is None

    def test_xml_meta_preserved(self, tmp_path):
        xml = ("<tool>\n<tool_name>Bash</tool_name>\n<toolcall_result>\n"
               "<duration>0.05s</duration>\n" + _big_text() + "\n</toolcall_result>\n</tool>")
        slim, meta = archive_result(xml, base_dir=str(tmp_path), session_id="s1",
                                    tool_name="Bash", config=CFG)
        assert "<tool_name>Bash</tool_name>" in slim
        assert "<duration>0.05s</duration>" in slim
        assert meta is not None

    def test_archive_file_exists(self, tmp_path):
        _, meta = archive_result(_big_text(), base_dir=str(tmp_path),
                                 session_id="s1", tool_name="Bash", config=CFG)
        assert os.path.isfile(meta["path"])


class TestReadArchive:
    def test_full_recall(self, tmp_path):
        _, meta = archive_result(_big_text(), base_dir=str(tmp_path),
                                 session_id="s1", tool_name="Bash", config=CFG)
        r = read_tool_archive(meta["id"], base_dir=str(tmp_path), session_id="s1")
        assert r["ok"]
        assert "第1行" in r["content"] and "最后一行" in r["content"]
        assert r["total_lines"] == 102

    def test_range_read(self, tmp_path):
        _, meta = archive_result(_big_text(), base_dir=str(tmp_path),
                                 session_id="s1", tool_name="Bash", config=CFG)
        r = read_tool_archive(meta["id"], base_dir=str(tmp_path), session_id="s1",
                              offset=50, limit=3)
        assert r["ok"] and r["returned_lines"] == 3
        assert "row-50" in r["content"]

    def test_missing_archive(self, tmp_path):
        r = read_tool_archive("deadbeef", base_dir=str(tmp_path), session_id="s1")
        assert not r["ok"]

    def test_illegal_id_rejected(self, tmp_path):
        r = read_tool_archive("../../etc/passwd", base_dir=str(tmp_path))
        assert not r["ok"]

    def test_archive_stats(self, tmp_path):
        archive_result(_big_text(), base_dir=str(tmp_path), session_id="s1",
                       tool_name="Bash", config=CFG)
        st = archive_stats(str(tmp_path), session_id="s1")
        assert st["files"] >= 1
        assert st["bytes"] > 0
