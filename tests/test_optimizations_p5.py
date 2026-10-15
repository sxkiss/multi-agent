"""bt P5 优化项（2026-10-15）单元测试：本地专家库自动发现。

- ExpertCatalog 扫描 experts/ 目录
- 专家摘要生成（注入系统提示用）
- InstallExpert 优先从本地库落地为成员

运行：python3 -m pytest tests/test_optimizations_p5.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mk_expert(base, eid, label="测试专家", desc="描述", triggers=None, tools=None,
               soul="你是测试专家。", identity="## Name\n测试专家"):
    d = os.path.join(base, "experts", eid)
    os.makedirs(d, exist_ok=True)
    manifest = {
        "id": eid,
        "label": {"zh": label, "en": eid},
        "description": {"zh": desc},
        "icon": "🧪",
        "category": "测试",
        "triggers": triggers or ["测试"],
        "tools": tools or ["Read", "Bash"],
    }
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    with open(os.path.join(d, "SOUL.md"), "w", encoding="utf-8") as f:
        f.write(soul)
    with open(os.path.join(d, "IDENTITY.md"), "w", encoding="utf-8") as f:
        f.write(identity)
    return d


class TestExpertCatalog:
    def test_scan_finds_experts(self, tmp_path):
        from chat_client.experts import ExpertCatalog
        base = str(tmp_path)
        _mk_expert(base, "e1", label="专家一")
        _mk_expert(base, "e2", label="专家二")
        cat = ExpertCatalog(base)
        experts = cat.scan(force=True)
        ids = {e.id for e in experts}
        assert ids == {"e1", "e2"}

    def test_scan_skips_non_expert_dirs(self, tmp_path):
        from chat_client.experts import ExpertCatalog
        base = str(tmp_path)
        os.makedirs(os.path.join(base, "experts", "not-an-expert"))
        _mk_expert(base, "real")
        cat = ExpertCatalog(base)
        ids = {e.id for e in cat.scan(force=True)}
        assert ids == {"real"}

    def test_expert_fields(self, tmp_path):
        from chat_client.experts import ExpertCatalog
        base = str(tmp_path)
        _mk_expert(base, "e1", label="运维专家", desc="运维描述",
                   triggers=["运维", "部署"], tools=["Read", "Bash"])
        e = ExpertCatalog(base).get("e1")
        assert e.label == "运维专家"
        assert e.description == "运维描述"
        assert e.triggers == ["运维", "部署"]
        assert e.tools == ["Read", "Bash"]
        assert "测试专家" in e.soul

    def test_missing_expert_returns_none(self, tmp_path):
        from chat_client.experts import ExpertCatalog
        assert ExpertCatalog(str(tmp_path)).get("nope") is None

    def test_summary_generation(self, tmp_path):
        from chat_client.experts import ExpertCatalog
        base = str(tmp_path)
        _mk_expert(base, "e1", label="专家一")
        summary = ExpertCatalog(base).generate_expert_summary()
        assert "本地专家库" in summary
        assert "专家一" in summary

    def test_empty_summary(self, tmp_path):
        from chat_client.experts import ExpertCatalog
        assert ExpertCatalog(str(tmp_path)).generate_expert_summary() == ""

    def test_to_dict_with_prompt(self, tmp_path):
        from chat_client.experts import ExpertCatalog
        base = str(tmp_path)
        _mk_expert(base, "e1")
        e = ExpertCatalog(base).get("e1")
        d = e.to_dict(with_prompt=True)
        assert "soul" in d and "identity" in d


class TestInstallFromLocal:
    def test_install_prefers_local_library(self, tmp_path, monkeypatch):
        """InstallExpert 应优先从本地库取专家（无需联网）。"""
        from chat_client.tools import task as TK
        from chat_client.tools import market as M
        from chat_client import experts as EX

        base = str(tmp_path)
        _mk_expert(base, "local-expert", label="本地专家", tools=["Read", "Bash"])
        crew_file = str(tmp_path / "crew_agents.json")
        monkeypatch.setattr(TK.AgentRegistry, "CUSTOM_AGENTS_FILE", crew_file)
        # 让 InstallExpert 里的 get_catalog 指向测试目录
        monkeypatch.setattr(EX, "get_catalog", lambda b: EX.ExpertCatalog(base))
        # 若误走网络应报错——借此验证走了本地分支
        def _boom(n):
            raise RuntimeError("should not hit network")
        monkeypatch.setattr(M, "fetch_expert_prompt", _boom)

        out = M.InstallExpert(name="local-expert")
        assert "已安装为团队成员" in out, out
        data = json.load(open(crew_file, encoding="utf-8"))
        member = data["agents"][0]
        assert member["name"] == "local-expert"
        assert "测试专家" in member["backstory"]  # backstory = SOUL + IDENTITY 内容
        assert member["tools"] == ["Read", "Bash"]  # 用本地专家推荐工具

    def test_install_falls_back_to_market(self, tmp_path, monkeypatch):
        """本地没有时回退到市场。"""
        from chat_client.tools import task as TK
        from chat_client.tools import market as M
        from chat_client import experts as EX

        base = str(tmp_path)
        os.makedirs(os.path.join(base, "experts"))
        crew_file = str(tmp_path / "crew_agents.json")
        monkeypatch.setattr(TK.AgentRegistry, "CUSTOM_AGENTS_FILE", crew_file)
        monkeypatch.setattr(EX, "get_catalog", lambda b: EX.ExpertCatalog(base))
        monkeypatch.setattr(M, "fetch_expert_prompt", lambda n: (n, "市场专家提示词"))
        monkeypatch.setattr(M, "find_expert_meta", lambda n: {"displayName": "市场专家"})

        out = M.InstallExpert(name="market-expert")
        assert "已安装为团队成员" in out, out
        data = json.load(open(crew_file, encoding="utf-8"))
        assert "市场专家提示词" in data["agents"][0]["backstory"]


class TestMarketCache:
    """P5 对齐 Octop：市场专家落盘后自动发现。"""

    def test_cache_market_expert_then_discovered(self, tmp_path):
        from chat_client.experts import ExpertCatalog
        base = str(tmp_path)
        cat = ExpertCatalog(base)
        d = cat.cache_market_expert("marketing-expert", "你是营销专家。",
                                    {"displayName": {"zh": "营销专家"},
                                     "description": {"zh": "增长与营销"},
                                     "tags": [{"zh": "营销"}]})
        assert os.path.isdir(d)
        e = cat.get("marketing-expert")
        assert e is not None
        assert e.source == "market"
        assert "营销专家" in e.soul
        assert e.label == "营销专家"

    def test_bundled_takes_precedence_over_market(self, tmp_path):
        from chat_client.experts import ExpertCatalog
        base = str(tmp_path)
        _mk_expert(base, "dup", label="内置版")
        cat = ExpertCatalog(base)
        cat.cache_market_expert("dup", "市场版", {"displayName": "市场版"})
        e = cat.get("dup")
        assert e.source == "bundled"
        assert e.label == "内置版"

    def test_summary_shows_both_sources(self, tmp_path):
        from chat_client.experts import ExpertCatalog
        base = str(tmp_path)
        _mk_expert(base, "b1", label="内置一")
        cat = ExpertCatalog(base)
        cat.cache_market_expert("m1", "市场专家", {"displayName": "市场一"})
        s = cat.generate_expert_summary()
        assert "内置专家" in s and "市场专家缓存" in s
        assert "内置一" in s and "市场一" in s

    def test_cache_rejects_illegal_id(self, tmp_path):
        from chat_client.experts import ExpertCatalog
        cat = ExpertCatalog(str(tmp_path))
        with pytest.raises(ValueError):
            cat.cache_market_expert("../../etc/passwd", "x")

    def test_market_cache_not_in_git(self, tmp_path):
        """market 目录应可被识别（source=market），便于前端区分。"""
        from chat_client.experts import ExpertCatalog
        cat = ExpertCatalog(str(tmp_path))
        cat.cache_market_expert("m2", "x", {})
        dicts = {e["id"]: e for e in cat.list_dicts()}
        assert dicts["m2"]["source"] == "market"
