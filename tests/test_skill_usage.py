"""技能粒度统计（metrics.record_skill + Skills 埋点）测试。

背景：工具级统计只能记到 "Skills" 这一层，无法回答「哪个技能真被用过」，
也就无法支撑「技能摘要该不该瘦身 / 哪些技能该延迟加载」的长期决策。
本文件覆盖：
1. MetricsRegistry 技能计数与 load/install/uninstall 分类
2. 跨重启持久化（skill_usage.json 独立落盘，不与工具统计混）
3. snapshot 的 skills / skills_lifetime 结构
4. Prometheus 导出
5. reset 语义
6. Skills 工具的埋点（成功 / 失败 / install 无 name 时按 source 推断）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat_client.metrics import MetricsRegistry  # noqa: E402


def _reg(tmp_path):
    return MetricsRegistry(persist_path=str(tmp_path / "tool_usage.json"))


# ---------- 基础计数 ----------
def test_record_skill_counts_by_name_and_action(tmp_path):
    r = _reg(tmp_path)
    r.record_skill("aihot", "load")
    r.record_skill("aihot", "load")
    r.record_skill("sn-image-ocr", "load")
    r.record_skill("new-skill", "install")
    r.record_skill("old-skill", "uninstall")

    s = r.snapshot()["skills"]
    assert s["aihot"]["loads"] == 2
    assert s["sn-image-ocr"]["loads"] == 1
    assert s["new-skill"]["installs"] == 1
    assert s["old-skill"]["uninstalls"] == 1


def test_record_skill_error_counted(tmp_path):
    r = _reg(tmp_path)
    r.record_skill("missing-skill", "load", "error")
    s = r.snapshot()["skills"]["missing-skill"]
    assert s["loads"] == 1
    assert s["errors"] == 1


def test_list_and_market_do_not_count_as_usage(tmp_path):
    """list/market 是「查看」不是「使用」，不应污染技能排行。"""
    r = _reg(tmp_path)
    r.record_skill("", "list")
    r.record_skill("", "market")
    assert r.snapshot()["skills"] == {}


def test_unknown_name_falls_back(tmp_path):
    r = _reg(tmp_path)
    r.record_skill("", "load")
    assert r.snapshot()["skills"]["unknown"]["loads"] == 1


# ---------- 持久化 ----------
def test_skill_stats_persist_across_restart(tmp_path):
    r1 = _reg(tmp_path)
    r1.record_skill("aihot", "load")
    r1.record_skill("aihot", "load")
    r1.flush()

    # 独立文件，不与 tool_usage.json 混在一起
    assert os.path.exists(tmp_path / "skill_usage.json")
    raw = json.load(open(tmp_path / "skill_usage.json", encoding="utf-8"))
    assert raw["skills"]["aihot"]["loads"] == 2
    assert "tools" not in raw

    # 模拟进程重启
    r2 = _reg(tmp_path)
    lifetime = r2.snapshot()["skills_lifetime"]
    assert lifetime["aihot"]["loads"] == 2
    # 新进程内计数从 0 开始
    assert r2.snapshot()["skills"] == {}


def test_lifetime_merges_history_and_process(tmp_path):
    r1 = _reg(tmp_path)
    r1.record_skill("aihot", "load")
    r1.flush()

    r2 = _reg(tmp_path)
    r2.record_skill("aihot", "load")
    r2.record_skill("brand-new", "load")
    lifetime = r2.snapshot()["skills_lifetime"]
    assert lifetime["aihot"]["loads"] == 2
    assert lifetime["brand-new"]["loads"] == 1


def test_corrupt_skill_file_degrades_gracefully(tmp_path):
    (tmp_path / "skill_usage.json").write_text("{not json", encoding="utf-8")
    r = _reg(tmp_path)
    assert r.snapshot()["skills_lifetime"] == {}
    r.record_skill("aihot", "load")
    assert r.snapshot()["skills"]["aihot"]["loads"] == 1


def test_flush_is_idempotent(tmp_path):
    r = _reg(tmp_path)
    r.record_skill("aihot", "load")
    r.flush()
    r.flush()
    assert r.snapshot()["skills_lifetime"]["aihot"]["loads"] == 1


# ---------- 导出 ----------
def test_snapshot_has_skill_fields(tmp_path):
    r = _reg(tmp_path)
    snap = r.snapshot()
    assert "skills" in snap
    assert "skills_lifetime" in snap
    assert snap["skill_persist_path"].endswith("skill_usage.json")


def test_prometheus_exports_skill_metrics(tmp_path):
    r = _reg(tmp_path)
    r.record_skill("aihot", "load")
    r.record_skill("aihot", "load", "error")
    text = r.to_prometheus()
    assert 'bt_skill_loads_total{skill="aihot"} 2' in text
    assert 'bt_skill_errors_total{skill="aihot"} 1' in text


def test_reset_include_history_clears_skills(tmp_path):
    r = _reg(tmp_path)
    r.record_skill("aihot", "load")
    r.flush()
    r.reset(include_history=True)
    assert r.snapshot()["skills_lifetime"] == {}
    raw = json.load(open(tmp_path / "skill_usage.json", encoding="utf-8"))
    assert raw["skills"] == {}


# ---------- Skills 工具埋点 ----------
def test_skills_tool_records_usage(tmp_path, monkeypatch):
    from chat_client import metrics as metrics_mod
    from chat_client.tools import skill as skill_mod

    reg = _reg(tmp_path)
    monkeypatch.setattr(metrics_mod, "_registry", reg)

    # 直接调实现函数，避免依赖真实技能目录
    monkeypatch.setattr(
        skill_mod, "_skills_impl",
        lambda **kw: '<tool>\n<toolcall_status>done</toolcall_status>\n</tool>',
    )
    skill_mod.Skills(name="aihot", action="load")
    skill_mod.Skills(name="aihot", action="load")

    assert reg.snapshot()["skills"]["aihot"]["loads"] == 2


def test_skills_tool_records_error_status(tmp_path, monkeypatch):
    from chat_client import metrics as metrics_mod
    from chat_client.tools import skill as skill_mod

    reg = _reg(tmp_path)
    monkeypatch.setattr(metrics_mod, "_registry", reg)
    monkeypatch.setattr(
        skill_mod, "_skills_impl",
        lambda **kw: '<tool>\n<toolcall_status>error</toolcall_status>\n</tool>',
    )
    skill_mod.Skills(name="missing", action="load")
    s = reg.snapshot()["skills"]["missing"]
    assert s["loads"] == 1 and s["errors"] == 1


def test_install_without_name_uses_source_basename(tmp_path, monkeypatch):
    """install 常不传 name，退化为 source 末段，避免全挤进 unknown。"""
    from chat_client import metrics as metrics_mod
    from chat_client.tools import skill as skill_mod

    reg = _reg(tmp_path)
    monkeypatch.setattr(metrics_mod, "_registry", reg)
    monkeypatch.setattr(
        skill_mod, "_skills_impl",
        lambda **kw: '<tool>\n<toolcall_status>done</toolcall_status>\n</tool>',
    )
    skill_mod.Skills(action="install", source="https://github.com/x/y/awesome-skill.zip")
    assert reg.snapshot()["skills"]["awesome-skill"]["installs"] == 1


def test_metrics_failure_never_breaks_skill_loading(tmp_path, monkeypatch):
    """统计是旁路，任何异常都不能影响技能加载结果。"""
    from chat_client.tools import skill as skill_mod

    monkeypatch.setattr(
        skill_mod, "_skills_impl",
        lambda **kw: '<tool>\n<toolcall_status>done</toolcall_status>\n</tool>',
    )

    def boom(*a, **kw):
        raise RuntimeError("metrics down")

    monkeypatch.setattr(skill_mod, "_record_skill_usage", boom)
    # 埋点炸了也不该抛出
    out = skill_mod.Skills(name="aihot", action="load")
    assert "toolcall_status>done" in out
