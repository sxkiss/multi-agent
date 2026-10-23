"""AgentCatalog 测试"""
import os
import json
import tempfile
from pathlib import Path


def _make_expert_dir(base: Path, eid: str, label_zh: str, cat: str = "研发") -> Path:
    d = base / eid
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "id": eid,
        "label": {"zh": label_zh, "en": label_zh},
        "description": {"zh": f"{label_zh} 描述", "en": f"{label_zh} desc"},
        "icon": "🔧",
        "category": cat,
        "triggers": ["测试", "test"],
        "tools": ["Read", "Bash"],
        "prompt_files": ["SOUL.md"],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "SOUL.md").write_text(f"你是 {label_zh}", encoding="utf-8")
    return d


def test_catalog_discovers_experts(tmp_path):
    """测试从 experts/ 目录发现专家"""
    from chat_client.agent_catalog import AgentCatalog

    experts_dir = tmp_path / "experts"
    _make_expert_dir(experts_dir, "expert-a", "专家甲")
    _make_expert_dir(experts_dir, "expert-b", "专家乙")

    cat = AgentCatalog(str(tmp_path), expert_dirs=[str(experts_dir)])
    agents = cat.scan()
    assert len(agents) == 2

    ids = {a.summary.id for a in agents}
    assert "expert-a" in ids
    assert "expert-b" in ids


def test_catalog_get_by_id(tmp_path):
    """测试按 ID 获取"""
    from chat_client.agent_catalog import AgentCatalog

    experts_dir = tmp_path / "experts"
    _make_expert_dir(experts_dir, "expert-x", "专家X")

    cat = AgentCatalog(str(tmp_path), expert_dirs=[str(experts_dir)])
    a = cat.get("expert-x")
    assert a is not None
    assert a.summary.label_zh == "专家X"
    assert a.summary.category == "研发"
    assert "Read" in a.summary.tools


def test_catalog_subagent_md(tmp_path):
    """测试从 markdown 发现 subagent"""
    from chat_client.agent_catalog import AgentCatalog

    sub_dir = tmp_path / "agents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / "divisions.json").write_text(json.dumps({
        "divisions": {"engineering": {"label": "工程", "icon": "Code", "color": "#3B82F6"}}
    }, ensure_ascii=False), encoding="utf-8")
    zh = sub_dir / "zh" / "engineering"
    zh.mkdir(parents=True, exist_ok=True)
    (zh / "my-subagent.md").write_text(
        "---\nname: 我的子代理\ndescription: 测试子代理\nemoji: 🤖\ncolor: '#3B82F6'\n---\n\n# 我的子代理\n\n你是测试子代理。\n",
        encoding="utf-8",
    )

    cat = AgentCatalog(str(tmp_path), subagent_dirs=[str(sub_dir)])
    a = cat.get("my-subagent")
    assert a is not None
    assert a.summary.label_zh == "我的子代理"
    assert a.summary.division == "engineering"
    assert a.summary.icon == "🤖"


def test_catalog_market_cache(tmp_path):
    """测试市场缓存目录也能被发现"""
    from chat_client.agent_catalog import AgentCatalog

    market_dir = tmp_path / "expert_market"
    _make_expert_dir(market_dir, "market-expert", "市场专家")

    cat = AgentCatalog(str(tmp_path), expert_dirs=[str(market_dir)])
    a = cat.get("market-expert")
    assert a is not None
    assert a.summary.source == "market"


def test_catalog_ttl_cache(tmp_path):
    """测试 mtime 缓存"""
    import time
    from chat_client.agent_catalog import AgentCatalog

    experts_dir = tmp_path / "experts"
    _make_expert_dir(experts_dir, "ttl-a", "TTL专家")

    cat = AgentCatalog(str(tmp_path), expert_dirs=[str(experts_dir)])
    first = cat.scan()
    assert len(first) == 1

    # 不改动文件，第二次扫描应命中缓存（不重新扫描）
    time.sleep(0.01)
    second = cat.scan()
    assert len(second) == 1

    # 新增文件后强制刷新
    _make_expert_dir(experts_dir, "ttl-b", "TTL专家2")
    cat.refresh()
    third = cat.scan()
    assert len(third) == 2


def test_catalog_divisions(tmp_path):
    """测试 divisions 加载"""
    from chat_client.agent_catalog import AgentCatalog

    sub_dir = tmp_path / "agents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / "divisions.json").write_text(json.dumps({
        "divisions": {"design": {"label": "设计", "icon": "PenTool", "color": "#EC4899"}}
    }, ensure_ascii=False), encoding="utf-8")

    cat = AgentCatalog(str(tmp_path), subagent_dirs=[str(sub_dir)])
    divs = cat.get_divisions()
    assert "design" in divs
    assert divs["design"].label == "设计"
