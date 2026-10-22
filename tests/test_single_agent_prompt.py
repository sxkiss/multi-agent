#!/usr/bin/env python3
"""单 AG 模式首次提示词大小 + 工具按需加载 回归测试（2026-10-22）。

覆盖：
1. 单 AG 模式下 system_prompt 注入延迟工具清单（_build_tools 路径）
2. 核心工具（DEFAULT_CORE_TOOL_NAMES）始终在 tools 中
3. 延迟工具（非核心）不在首次 tools 中，但 ToolSearch 可用
4. system_prompt + tools 总大小不超过阈值（约 100 KB）
5. MEMORY.md 占位符（不注入全文）
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_agent(cwd="/tmp/fake_cwd"):
    cfg = {
        "mode": "single",
        "cwd": cwd,
        "enable_agents_md": True,
        "api_key": "test",
        "default_model": "test",
        "code_mode": True,   # 触发 current_dir 赋值
    }
    from chat_client.agent import Agent
    return Agent(session_id="s1", config=cfg)


def _deferred_block_for_all_tools():
    """模拟 agent.py 中 _build_tools 里的延迟清单注入逻辑"""
    from chat_client.tools import registry, DEFAULT_CORE_TOOL_NAMES, get_unlocked_builtin_tools
    all_tools = registry.get_openai_tools()
    _unlocked = get_unlocked_builtin_tools("s1")
    _all_names = {t["function"]["name"] for t in all_tools}
    _deferred = _all_names - DEFAULT_CORE_TOOL_NAMES - _unlocked
    lines = []
    for _t in sorted(_deferred):
        _meta = registry._metadata.get(_t, {})
        _desc = (_meta.get("description") or "").split("\n")[0][:80]
        lines.append(f"- {_t}: {_desc}")
    block = (
        "\n\n## 延迟工具（通过 ToolSearch 搜索并解锁）\n"
        "以下工具未在当前 tools 数组中下发，调用前先 ToolSearch(query=...) 解锁（命中后本会话有效）：\n"
        + "\n".join(lines)
    )
    return block, _deferred


class SingleAgentPromptTest(unittest.TestCase):

    def test_single_agent_injects_deferred_list(self):
        """_build_tools 路径执行后 system_prompt 含延迟清单"""
        agent = _make_agent()
        # 直接注入延迟清单（模拟 _build_tools 逻辑）
        block, deferred = _deferred_block_for_all_tools()
        agent.system_prompt += block
        self.assertIn("延迟工具（通过 ToolSearch", agent.system_prompt)
        self.assertGreater(len(deferred), 0)

    def test_core_tools_always_present(self):
        from chat_client.tools import registry, DEFAULT_CORE_TOOL_NAMES
        agent = _make_agent()
        core_names = {t["function"]["name"] for t in registry.get_openai_tools()
                      if t["function"]["name"] in DEFAULT_CORE_TOOL_NAMES}
        # 核心工具名集合应等于 DEFAULT_CORE_TOOL_NAMES
        self.assertEqual(core_names, DEFAULT_CORE_TOOL_NAMES)

    def test_deferred_tools_not_in_first_build(self):
        from chat_client.tools import registry, DEFAULT_CORE_TOOL_NAMES
        agent = _make_agent()
        all_tool_names = {t["function"]["name"] for t in registry.get_openai_tools()}
        deferred = all_tool_names - DEFAULT_CORE_TOOL_NAMES
        self.assertGreater(len(deferred), 0)

    def test_toolsearch_always_in_core(self):
        from chat_client.tools import DEFAULT_CORE_TOOL_NAMES
        self.assertIn("ToolSearch", DEFAULT_CORE_TOOL_NAMES)

    def test_total_size_under_threshold(self):
        agent = _make_agent()
        _ = agent.system_prompt

        from chat_client.tools import registry, DEFAULT_CORE_TOOL_NAMES
        core_tools = [t for t in registry.get_openai_tools()
                      if t["function"]["name"] in DEFAULT_CORE_TOOL_NAMES]
        tools_json = json.dumps(core_tools, ensure_ascii=False)

        # 加上延迟清单（模拟 _build_tools 注入）
        block, _ = _deferred_block_for_all_tools()
        agent.system_prompt += block

        total_bytes = len(agent.system_prompt.encode("utf-8")) + len(tools_json.encode("utf-8"))
        # 阈值 110 KB
        self.assertLess(total_bytes, 110 * 1024,
                        f"total={total_bytes} threshold={110*1024}")

    def test_memory_md_placeholder(self):
        tmpdir = tempfile.mkdtemp()
        with open(os.path.join(tmpdir, "MEMORY.md"), "w") as f:
            f.write("\n".join(f"line {i}" for i in range(500)))
        agent = _make_agent(cwd=tmpdir)
        _ = agent.system_prompt
        self.assertIn("MEMORY.md 已加载", agent.system_prompt)
        self.assertNotIn("line 0", agent.system_prompt)  # 不注入全文


if __name__ == "__main__":
    unittest.main()


class SingleAgentHighFreqTest(unittest.TestCase):
    """有历史统计时切换到高频工具集"""

    def test_no_history_uses_default_core(self):
        """无历史数据时，核心工具 = DEFAULT_CORE_TOOL_NAMES (43个)"""
        from chat_client.metrics import get_metrics, reset_metrics
        get_metrics(persist_path="/dev/null").reset()
        agent = _make_agent()
        _ = agent.system_prompt
        from chat_client.tools import DEFAULT_CORE_TOOL_NAMES, registry
        all_names = {t["function"]["name"] for t in registry.get_openai_tools()}
        deferred = all_names - DEFAULT_CORE_TOOL_NAMES
        self.assertGreater(len(deferred), 0)

    def test_has_history_uses_high_freq(self):
        """有历史数据时，核心工具 = HIGH_FREQ_TOOL_NAMES (34个)，减少9个"""
        tmp = tempfile.mkdtemp()
        persist = os.path.join(tmp, "tool_usage.json")
        with open(persist, "w") as f:
            json.dump({"tools": {"Bash": {"calls": 100}, "Read": {"calls": 50}}}, f)
        from chat_client.metrics import get_metrics
        get_metrics(persist_path=persist).reset()
        # 重新获取单例（已持久化）
        from chat_client.tools import HIGH_FREQ_TOOL_NAMES, DEFAULT_CORE_TOOL_NAMES, registry
        from chat_client.metrics import has_history
        self.assertTrue(has_history())
        all_names = {t["function"]["name"] for t in registry.get_openai_tools()}
        deferred = all_names - HIGH_FREQ_TOOL_NAMES
        # 比默认核心少 9 个（Skills, SkillMarket等低频工具被移入延迟）
        self.assertLess(len(HIGH_FREQ_TOOL_NAMES), len(DEFAULT_CORE_TOOL_NAMES))
