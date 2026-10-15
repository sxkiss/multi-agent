"""bt P2/P3 优化项（2026-10-15）单元测试：

- P2-a 技能包：export_package / import_package
- P2-b 技能呈现层：get_skill_presentation / _infer_category
- P2-c 知识库管线：search 的 gate/hint/citation
- P2-d persona 模板：render_soul / list_personas / write_soul
- P3-a 插件体系：PluginManager list/load/unload
- P3-b 可观测性：MetricsRegistry 计数/快照/prometheus

运行：python3 -m pytest tests/test_optimizations_p23.py -q
"""
import io
import json
import os
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# P2-a: 技能包
# ============================================================
class TestSkillPackage:
    def _make_skill(self, root, name, desc="测试技能", extra_files=None):
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\n正文内容。\n")
        for fn, content in (extra_files or {}).items():
            with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
                f.write(content)
        return d

    def test_export_creates_valid_zip(self, tmp_path):
        from chat_client.skills import SkillManager
        sm = SkillManager.get_instance()
        old = sm.skills_dir
        try:
            sm.set_skills_dir(str(tmp_path))
            sm.invalidate_all_cache()
            self._make_skill(str(tmp_path), "demo-skill", extra_files={"ref.md": "参考"})
            data = sm.export_package(["demo-skill"], "my-pack")
            zf = zipfile.ZipFile(io.BytesIO(data))
            names = zf.namelist()
            assert "manifest.json" in names
            assert any(n.startswith("skills/demo-skill/") for n in names)
            mf = json.loads(zf.read("manifest.json"))
            assert mf["format"] == SkillManager.PACKAGE_FORMAT
            assert mf["skills"][0]["name"] == "demo-skill"
        finally:
            sm.set_skills_dir(old)
            sm.invalidate_all_cache()

    def test_export_unknown_skill_raises(self, tmp_path):
        from chat_client.skills import SkillManager
        sm = SkillManager.get_instance()
        old = sm.skills_dir
        try:
            sm.set_skills_dir(str(tmp_path))
            sm.invalidate_all_cache()
            with pytest.raises(ValueError):
                sm.export_package(["no-such-skill"])
        finally:
            sm.set_skills_dir(old)
            sm.invalidate_all_cache()

    def test_export_empty_raises(self, tmp_path):
        from chat_client.skills import SkillManager
        sm = SkillManager.get_instance()
        with pytest.raises(ValueError):
            sm.export_package([])

    def test_roundtrip_export_import(self, tmp_path):
        """导出后导入到新目录，技能应被还原。"""
        from chat_client.skills import SkillManager
        sm = SkillManager.get_instance()
        old = sm.skills_dir
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        try:
            sm.set_skills_dir(str(src))
            sm.invalidate_all_cache()
            self._make_skill(str(src), "roundtrip-skill", extra_files={"helper.py": "print(1)"})
            data = sm.export_package(["roundtrip-skill"], "rt-pack")

            sm.set_skills_dir(str(dst))
            sm.invalidate_all_cache()
            res = sm.import_package(data)
            assert res.get("status") is True, res
            assert "roundtrip-skill" in res.get("installed", [])
            assert os.path.isfile(os.path.join(str(dst), "roundtrip-skill", "SKILL.md"))
        finally:
            sm.set_skills_dir(old)
            sm.invalidate_all_cache()

    def test_import_invalid_zip(self):
        from chat_client.skills import SkillManager
        sm = SkillManager.get_instance()
        res = sm.import_package(b"not a zip")
        assert res.get("status") is False


# ============================================================
# P2-b: 技能呈现层
# ============================================================
class TestSkillPresentation:
    def test_infer_category(self):
        from chat_client.skills import SkillManager
        sm = SkillManager.get_instance()
        assert sm._infer_category("excel-analyzer") == "数据分析"
        assert sm._infer_category("seo-optimizer") == "SEO/GEO"
        assert sm._infer_category("unknown-thing-xyz") == "其他"

    def test_presentation_from_frontmatter(self, tmp_path):
        from chat_client.skills import SkillManager
        sm = SkillManager.get_instance()
        old = sm.skills_dir
        try:
            sm.set_skills_dir(str(tmp_path))
            sm.invalidate_all_cache()
            d = tmp_path / "styled-skill"
            d.mkdir()
            (d / "SKILL.md").write_text(
                "---\nname: styled-skill\ndescription: 有样式的技能\n"
                "icon: 🎨\ncategory: 设计\ntriggers: [画图, 设计]\n---\n正文\n",
                encoding="utf-8")
            pres = sm.get_skill_presentation("styled-skill")
            assert pres["icon"] == "🎨"
            assert pres["category"] == "设计"
            assert pres["triggers"] == ["画图", "设计"]
        finally:
            sm.set_skills_dir(old)
            sm.invalidate_all_cache()

    def test_presentation_unknown_returns_empty(self):
        from chat_client.skills import SkillManager
        sm = SkillManager.get_instance()
        assert sm.get_skill_presentation("nope-nope-nope") == {}


# ============================================================
# P2-c: 知识库管线 gate/hint/citation
# ============================================================
class TestKnowledgeGate:
    def _fake_kb(self, tmp_path):
        from chat_client.knowledge_base import KnowledgeBase

        def embed(texts):
            # 简单确定性 embedding：按字符 hash 生成定长向量
            out = []
            for t in texts:
                v = [0.0] * 8
                for i, ch in enumerate(t):
                    v[i % 8] += (ord(ch) % 13) / 13.0
                out.append(v)
            return out

        return KnowledgeBase(str(tmp_path / "kb"), embed)

    def test_empty_kb_has_hint(self, tmp_path):
        kb = self._fake_kb(tmp_path)
        res = kb.search("任意查询")
        assert res["status"] is True
        assert res["results"] == []
        assert "知识库为空" in res["hint"]
        assert res["gate"]["passed"] is False

    def test_hit_has_citation_and_gate(self, tmp_path):
        kb = self._fake_kb(tmp_path)
        kb.add_document(text="苹果香蕉橙子 水果 维生素", name="水果文档")
        res = kb.search("水果 维生素", score=0.0)
        assert res["results"], res
        r = res["results"][0]
        assert "水果文档" in r["citation"]
        assert "相似度" in r["citation"]
        assert res["gate"]["passed"] is True

    def test_no_hit_has_hint(self, tmp_path):
        kb = self._fake_kb(tmp_path)
        kb.add_document(text="苹果香蕉", name="水果")
        # 高门槛 + 不相关查询 → 无命中 → hint
        res = kb.search("完全不相关的技术话题 xyz", score=0.99)
        assert res["results"] == []
        assert "未检索到相关内容" in res["hint"]


# ============================================================
# P2-d: persona 模板
# ============================================================
class TestPersona:
    def test_list_personas(self):
        from chat_client.persona import list_personas
        ps = list_personas()
        keys = {p["key"] for p in ps}
        assert {"manager", "analyst", "engineer", "assistant"} <= keys

    def test_render_soul_default(self):
        from chat_client.persona import render_soul
        txt = render_soul("manager")
        assert "# SOUL" in txt
        assert "工作原则" in txt
        assert "persona=manager" in txt

    def test_render_soul_overrides(self):
        from chat_client.persona import render_soul
        txt = render_soul("analyst", role_title="首席分析师", traits="冷静", tone="克制",
                          extra_principles=["额外原则A"])
        assert "首席分析师" in txt
        assert "冷静" in txt
        assert "额外原则A" in txt

    def test_render_unknown_persona_fallback(self):
        from chat_client.persona import render_soul
        txt = render_soul("no-such-persona")
        assert "persona=no-such-persona" in txt  # 记录 key，但内容回退 manager
        assert "工作原则" in txt

    def test_write_soul_with_backup(self, tmp_path):
        from chat_client.persona import write_soul
        soul = tmp_path / "SOUL.md"
        soul.write_text("旧内容", encoding="utf-8")
        res = write_soul(str(tmp_path), "engineer", backup=True)
        assert res["status"] is True
        assert "工程" in soul.read_text(encoding="utf-8") or "SOUL" in soul.read_text(encoding="utf-8")
        baks = [f for f in os.listdir(str(tmp_path)) if f.startswith("SOUL.md.bak")]
        assert baks, "未生成备份"


# ============================================================
# P3-a: 插件体系
# ============================================================
class TestPluginManager:
    def test_list_and_load(self, tmp_path):
        from chat_client.plugins import PluginManager
        # 构造一个测试插件
        pdir = tmp_path / "test-plug"
        pdir.mkdir()
        (pdir / "plugin.json").write_text(json.dumps({"name": "test-plug", "version": "1.0"}), encoding="utf-8")
        (pdir / "main.py").write_text(
            "def register(ctx):\n"
            "    ctx.on('demo_event', lambda **kw: None)\n", encoding="utf-8")
        pm = PluginManager(str(tmp_path))
        plugins = pm.list_plugins()
        assert any(p["id"] == "test-plug" for p in plugins)
        res = pm.load_plugin("test-plug")
        assert res["status"] is True, res
        assert "test-plug" in pm.status()["loaded"]

    def test_load_missing(self, tmp_path):
        from chat_client.plugins import PluginManager
        pm = PluginManager(str(tmp_path))
        res = pm.load_plugin("ghost")
        assert res["status"] is False

    def test_unload(self, tmp_path):
        from chat_client.plugins import PluginManager
        pdir = tmp_path / "p1"
        pdir.mkdir()
        (pdir / "main.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
        pm = PluginManager(str(tmp_path))
        pm.load_plugin("p1")
        res = pm.unload_plugin("p1")
        assert res["status"] is True
        assert "p1" not in pm.status()["loaded"]

    def test_plugin_register_tool(self, tmp_path):
        """插件通过 ctx.register_tool 注册的工具应进入主注册表。"""
        from chat_client.plugins import PluginManager
        from chat_client.tools import registry
        pdir = tmp_path / "tool-plug"
        pdir.mkdir()
        (pdir / "main.py").write_text(
            "def register(ctx):\n"
            "    def P23TestTool(x: str = 'a') -> str:\n"
            "        '''测试工具。Args: x: 参数'''\n"
            "        return 'ok:' + x\n"
            "    ctx.register_tool(P23TestTool, name_cn='测试', risk_level='low')\n",
            encoding="utf-8")
        pm = PluginManager(str(tmp_path))
        res = pm.load_plugin("tool-plug")
        assert res["status"] is True
        assert "P23TestTool" in registry._metadata, "插件工具未注册"


# ============================================================
# P3-b: 可观测性
# ============================================================
class TestMetrics:
    def test_counter_and_tool(self):
        from chat_client.metrics import MetricsRegistry
        m = MetricsRegistry()
        m.inc("foo", 2)
        m.inc("foo", 3)
        m.record_tool("Bash", 100.0, "ok")
        m.record_tool("Bash", 200.0, "error")
        snap = m.snapshot()
        assert snap["counters"]["foo"] == 5
        assert snap["tools"]["Bash"]["calls"] == 2
        assert snap["tools"]["Bash"]["errors"] == 1
        assert snap["tools"]["Bash"]["max_ms"] == 200.0

    def test_llm_and_prometheus(self):
        from chat_client.metrics import MetricsRegistry
        m = MetricsRegistry()
        m.record_llm("gpt-5", 500.0, "ok")
        m.record_llm("gpt-5", 700.0, "error")
        snap = m.snapshot()
        assert snap["llm"]["gpt-5"]["calls"] == 2
        text = m.to_prometheus()
        assert "bt_llm_calls_total" in text
        assert 'model="gpt-5"' in text

    def test_reset(self):
        from chat_client.metrics import MetricsRegistry
        m = MetricsRegistry()
        m.inc("x", 1)
        m.reset()
        assert m.snapshot()["counters"] == {}

    def test_blocked_recorded(self):
        from chat_client.metrics import MetricsRegistry
        m = MetricsRegistry()
        m.record_tool("DangerTool", 5.0, "blocked-high-risk")
        assert m.snapshot()["tools"]["DangerTool"]["blocked"] == 1
