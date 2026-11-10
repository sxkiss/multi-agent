"""专家市场多源回归测试（2026-11-09）。

覆盖：
1. 同源多镜像降级：首选镜像网络异常/5xx 才切换；4xx（含 404）不切换。
2. 第三方市场聚合：外部清单归一化、恶意 source 防护、缓存与逐源容错。
3. 清单合并：主源 + 第三方，第三方全挂不拖累主源。
4. 第三方专家取词：按 plugin.json 的 teamInfo.leadAgent / agents[0] 解析入口，
   且 fetch_expert_prompt 对第三方条目走独立路径（不打 COS）。

全部 mock 网络与缓存目录，不联网、不碰生产缓存。
运行：.venv/bin/python -m pytest tests/test_expert_multisource.py -q
"""
import os

import pytest


class _Resp:
    def __init__(self, status_code, content=b"", json_data=None):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    from chat_client.tools import market as M
    cache = os.path.join(str(tmp_path), "market_cache")
    os.makedirs(cache, exist_ok=True)
    monkeypatch.setattr(M, "_MARKET_CACHE_DIR", cache)
    M._market_cache_state.clear()
    return M


class TestMirrorFallback:
    def test_primary_network_error_falls_to_second(self, isolated, monkeypatch):
        M = isolated
        calls = []

        def fake_get(url, timeout=None, **kw):
            calls.append(url)
            if "acc-1258344699.cos.accelerate.myqcloud.com" in url:
                raise OSError("primary down")
            return _Resp(200, b'{"experts":[]}')

        monkeypatch.setattr(M.requests, "get", fake_get)
        r = M._expert_get("expert_center.json")
        assert r is not None and r.status_code == 200
        assert len(calls) == 2  # 首选挂 → 第二个成功，未再打第三个

    def test_5xx_falls_but_404_does_not(self, isolated, monkeypatch):
        M = isolated
        calls = []

        def fake_5xx(url, timeout=None, **kw):
            calls.append(url)
            return _Resp(503)

        monkeypatch.setattr(M.requests, "get", fake_5xx)
        M._expert_get("featuredScenes.json")
        assert len(calls) == len(M.EXPERT_BASES)  # 5xx 每个镜像都试

        calls.clear()

        def fake_404(url, timeout=None, **kw):
            calls.append(url)
            return _Resp(404)

        monkeypatch.setattr(M.requests, "get", fake_404)
        r = M._expert_get("plugins/nope/agents/nope.md")
        assert r.status_code == 404 and len(calls) == 1  # 404 立即返回

    def test_scenes_and_catalog_use_fallback(self, isolated, monkeypatch):
        """清单/场景入口必须走 _expert_get_json（即镜像降级）而非硬编码单地址。"""
        M = isolated
        seen = []

        def fake_json(rel):
            seen.append(rel)
            return {"experts": [{"agentName": "x"}]} if rel == "expert_center.json" else {"scenes": []}

        monkeypatch.setattr(M, "_expert_get_json", fake_json)
        monkeypatch.setattr(M, "_load_third_party_experts", lambda: [])
        M._load_experts(force=True)
        M.ExpertScenes()
        assert "expert_center.json" in seen and "featuredScenes.json" in seen


class TestThirdPartyNormalize:
    def test_malicious_source_filtered(self, isolated):
        M = isolated
        out = M._normalize_third_party("evil", "http://x", [
            {"name": "ok", "source": "./plugins/ok"},
            {"name": "bare", "source": "plugins/bare"},
            {"name": "up", "source": "../../etc"},
            {"name": "url", "source": "http://evil.com/x"},
            {"name": "abs", "source": "/etc/x"},
            {"name": "win", "source": "C:/Windows/x"},
            {"name": "backslash", "source": "plugins\\..\\x"},
        ])
        assert sorted(e["agentName"] for e in out) == ["bare", "ok"]
        assert all(e.get("_source") == "third_party" for e in out)
        assert all(e["_source_rel"].startswith("plugins/") for e in out)

    def test_source_failure_is_silent(self, isolated, monkeypatch):
        M = isolated
        monkeypatch.setattr(M, "_THIRD_PARTY_MARKETPLACES", [("bad", "http://127.0.0.1:1/nope")])
        assert M._load_third_party_experts() == []  # 不抛异常

    def test_cache_used_on_second_call(self, isolated, monkeypatch):
        M = isolated
        calls = []

        def fake_get(url, timeout=None, **kw):
            calls.append(url)
            return _Resp(200, json_data={"plugins": [{"name": "p1"}]})

        monkeypatch.setattr(M.requests, "get", fake_get)
        monkeypatch.setattr(M, "_THIRD_PARTY_MARKETPLACES", [("m", "http://x")])
        a = M._load_third_party_experts()
        M._market_cache_state.clear()
        b = M._load_third_party_experts()
        assert len(a) == len(b) == 1
        assert len(calls) == 1  # 第二次命中磁盘缓存


class TestCatalogMerge:
    def test_merge_and_third_party_failure_tolerance(self, isolated, monkeypatch):
        M = isolated
        monkeypatch.setattr(M, "_expert_get_json", lambda rel: {"experts": [
            {"agentName": "main-1"}, {"agentName": "dup"}]})
        monkeypatch.setattr(M, "_load_third_party_experts", lambda: [
            {"agentName": "tp-1", "_source": "third_party"},
            {"agentName": "dup", "_source": "third_party"},  # 同名以主源为准
        ])
        items = M._load_experts(force=True)
        assert len(items) == 3
        assert sum(1 for x in items if x.get("_source") == "third_party") == 1

        # 第三方整体抛异常也不影响主源
        def boom():
            raise RuntimeError("all third-party down")

        monkeypatch.setattr(M, "_load_third_party_experts", boom)
        M._market_cache_state.clear()
        items2 = M._load_experts(force=True)
        assert [x["agentName"] for x in items2] == ["main-1", "dup"]


class TestThirdPartyPrompt:
    def test_resolves_lead_agent_md(self, isolated, monkeypatch):
        M = isolated
        urls = []

        def fake_get(url, timeout=None, **kw):
            urls.append(url)
            if url.endswith("plugin.json"):
                return _Resp(200, json_data={
                    "teamInfo": {"leadAgent": "team-lead"},
                    "agents": ["./agents/team-lead.md"],
                })
            if "agents/team-lead.md" in url:
                return _Resp(200, b"---\nname: team-lead\n---\nLEAD PROMPT")
            return _Resp(404)

        monkeypatch.setattr(M.requests, "get", fake_get)
        meta = {"agentName": "tp", "plugin": "tp", "_raw_base": "http://x",
                "_source_rel": "plugins/tp", "_source": "third_party"}
        text = M._fetch_third_party_prompt(meta)
        assert "LEAD PROMPT" in text
        assert any("agents/team-lead.md" in u for u in urls)

    def test_fetch_prompt_routes_third_party_without_cos(self, isolated, monkeypatch):
        """第三方条目必须直接分流，不发起任何 COS 请求。"""
        M = isolated
        meta = {"agentName": "tp", "_source": "third_party", "_market": "m"}
        monkeypatch.setattr(M, "find_expert_meta", lambda n: meta)
        cos_calls = []

        def fake_expert_get(rel, timeout=None):
            cos_calls.append(rel)
            return _Resp(404)

        monkeypatch.setattr(M, "_expert_get", fake_expert_get)
        monkeypatch.setattr(M, "_fetch_third_party_prompt", lambda m: "THIRD PARTY PROMPT")
        k, text = M.fetch_expert_prompt("tp")
        assert k == "tp" and text == "THIRD PARTY PROMPT"
        assert cos_calls == [], "第三方专家不应触发 COS 请求"
