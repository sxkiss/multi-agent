"""专家市场 bundle 回退回归测试（2026-11-09）。

背景：上游专家有两类发布形态——早期专家有散装
/plugins/{name}/agents/{name}.md；新增/改版专家只发整包
/bundles/{plugin}.tar.gz，清单里的 promptFile 只是包内相对路径。
旧实现只取散装 .md，导致 2026-06 之后新增的专家全部 404、装不上。

本测试全部 mock 网络与落盘目录，不联网、不碰生产缓存。
运行：.venv/bin/python -m pytest tests/test_expert_bundle.py -q
"""
import io
import os
import tarfile

import pytest


def _make_tar(entries) -> bytes:
    """构造内存 tar.gz：entries = [(name, bytes), ...]"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class _Resp:
    def __init__(self, status_code, content=b"", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text or content.decode("utf-8", "replace") if isinstance(content, bytes) else content


def _patch(monkeypatch, *, loose_ok=False, bundle_bytes=None,
           bundle_by_name=None, meta=None, tmp_path=None):
    """装配隔离环境：mock requests.get / find_expert_meta / 缓存目录。"""
    from chat_client.tools import market as M

    calls = []

    def fake_get(url, timeout=None, **kw):
        calls.append(url)
        if "/bundles/" in url:
            name = url.split("/bundles/")[-1].split(".tar.gz")[0]
            data = (bundle_by_name or {}).get(name, bundle_bytes)
            return _Resp(200, data) if data else _Resp(404)
        # 散装 .md
        return _Resp(200, b"# loose prompt") if loose_ok else _Resp(404)

    monkeypatch.setattr(M.requests, "get", fake_get)
    monkeypatch.setattr(M, "find_expert_meta", lambda n: meta or {})
    if tmp_path is not None:
        cache = os.path.join(str(tmp_path), "market_cache")
        os.makedirs(cache, exist_ok=True)
        monkeypatch.setattr(M, "_MARKET_CACHE_DIR", cache)
    return M, calls


class TestBundleFallback:
    def test_loose_md_wins_no_bundle_download(self, monkeypatch, tmp_path):
        """早期专家：散装 .md 可用时优先，绝不下载整包。"""
        M, calls = _patch(monkeypatch, loose_ok=True, tmp_path=tmp_path)
        key, text = M.fetch_expert_prompt("content-creator")
        assert key == "content-creator" and "loose prompt" in text
        assert not any("/bundles/" in u for u in calls)

    def test_bundle_fallback_when_md_404(self, monkeypatch, tmp_path):
        """散装 404 → 回退 bundle 取包内 agents/*.md。"""
        tb = _make_tar([
            ("./agents/python-fullstack-engineer.md", b"---\nname: x\n---\nYou are a full-stack engineer."),
            ("./skills/09-python/SKILL.md", b"# skill"),
        ])
        M, calls = _patch(monkeypatch, loose_ok=False, bundle_bytes=tb,
                          meta={"plugin": "python-fullstack-engineer"},
                          tmp_path=tmp_path)
        key, text = M.fetch_expert_prompt("python-fullstack-engineer")
        assert "full-stack engineer" in text
        assert any("/bundles/python-fullstack-engineer.tar.gz" in u for u in calls)

    def test_bundle_name_uses_plugin_not_agentname(self, monkeypatch, tmp_path):
        """团队专家 bundle 名取 plugin 字段（与 agentName 不一致）。"""
        tb = _make_tar([("./agents/team.md", b"# team prompt")])
        M, calls = _patch(
            monkeypatch, loose_ok=False,
            bundle_by_name={"invoice-verify-workbuddy": tb},
            meta={"plugin": "invoice-verify-workbuddy",
                  "agentName": "invoice-verify-team-lead"},
            tmp_path=tmp_path,
        )
        _, text = M.fetch_expert_prompt("invoice-verify-team-lead")
        assert "team prompt" in text
        assert any("/bundles/invoice-verify-workbuddy.tar.gz" in u for u in calls)

    def test_path_traversal_members_skipped(self, monkeypatch, tmp_path):
        """外部包不可信：上跳/绝对路径成员必须被忽略。"""
        tb = _make_tar([
            ("../../etc/evil.md", b"PWNED"),
            ("/absolute/evil.md", b"PWNED"),
            ("agents/safe.md", b"# safe prompt"),
        ])
        M, _ = _patch(monkeypatch, loose_ok=False, bundle_bytes=tb,
                      meta={"plugin": "x"}, tmp_path=tmp_path)
        _, text = M.fetch_expert_prompt("x")
        assert "safe prompt" in text and "PWNED" not in text

    def test_skills_extracted_and_cached(self, monkeypatch, tmp_path):
        """第4条：bundle 内 skills/ 落地到隔离的专家缓存目录。"""
        tb = _make_tar([
            ("agents/x.md", b"# prompt"),
            ("skills/demo/SKILL.md", b"# demo skill"),
        ])
        M, _ = _patch(monkeypatch, loose_ok=False, bundle_bytes=tb,
                      meta={"plugin": "x"}, tmp_path=tmp_path)
        M.fetch_expert_prompt("x")
        dest = os.path.join(str(tmp_path), "expert_market", "x", "skills", "demo", "SKILL.md")
        assert os.path.isfile(dest)
        assert "demo skill" in open(dest, encoding="utf-8").read()

    def test_all_missing_raises_clear_error(self, monkeypatch, tmp_path):
        """散装与 bundle 都没有时，报错要列出尝试过的候选。"""
        M, _ = _patch(monkeypatch, loose_ok=False, meta={}, tmp_path=tmp_path)
        with pytest.raises(RuntimeError) as ei:
            M.fetch_expert_prompt("ghost-expert")
        assert "未取到专家提示词" in str(ei.value)
        assert "ghost-expert" in str(ei.value)


class TestSafeTarMember:
    def test_variants(self):
        from chat_client.tools.market import _safe_tar_member as s
        assert s("/etc/passwd") is None
        assert s("../../etc/passwd") is None
        assert s("C:/Windows/x") is None
        assert s("a\x00b") is None
        assert s("") is None
        assert s("./agents/x.md") == "agents/x.md"
        assert s("agents\\sub\\x.md") == "agents/sub/x.md"
