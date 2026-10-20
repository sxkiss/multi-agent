#!/usr/bin/env python
"""保护全局敏感配置文件测试。

背景：2026-10-20 会话 1792378349826 中 agent 误用 Write 工具把
/home/sxkiss/bt/config.json 覆盖成 {"result": "工具调用失败，请重试"}。

修复：Write/Edit/Patch/LargeFileEdit(write/append/edit) 对项目根的
config.json、auth.json、crew_agents.json、agents.json、scheduled_tasks.json
一律拒绝写入；Read 和 LargeFileEdit(read) 仍允许读取。
"""
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from chat_client.tools import registry


def call_tool(name, **kwargs):
    func = registry.get_tool_func(name)
    return func(**kwargs)


class ProtectedGlobalFileTest(unittest.TestCase):
    def test_is_protected_global_file(self):
        from chat_client.tools.edit import _is_protected_global_file
        for name in ("config.json", "auth.json", "crew_agents.json", "agents.json", "scheduled_tasks.json"):
            self.assertTrue(
                _is_protected_global_file(os.path.join(BASE_DIR, name)),
                f"{name} 应被标记为受保护",
            )
        # 非全局敏感文件不受影响
        self.assertFalse(_is_protected_global_file(os.path.join(BASE_DIR, "web_server.py")))
        self.assertFalse(_is_protected_global_file(os.path.join(BASE_DIR, "chat_client", "tools", "edit.py")))
        self.assertFalse(_is_protected_global_file("/tmp/anything.json"))

    def test_write_rejects_config(self):
        result = call_tool("Write", file_path=os.path.join(BASE_DIR, "config.json"), content='{"result": "bad"}')
        self.assertIn("Access denied", result)
        self.assertIn("全局敏感配置", result)

    def test_write_rejects_auth(self):
        result = call_tool("Write", file_path=os.path.join(BASE_DIR, "auth.json"), content='{"pwned": true}')
        self.assertIn("Access denied", result)

    def test_read_allows_config(self):
        result = call_tool("Read", file_path=os.path.join(BASE_DIR, "config.json"))
        self.assertNotIn("Access denied", result)
        self.assertIn("config.json", result)

    def test_large_file_edit_write_rejects_auth(self):
        result = call_tool("LargeFileEdit", file_path=os.path.join(BASE_DIR, "auth.json"), action="write", content="x")
        self.assertIn("Access denied", result)
        self.assertIn("全局敏感配置", result)

    def test_large_file_edit_append_rejects_auth(self):
        result = call_tool("LargeFileEdit", file_path=os.path.join(BASE_DIR, "auth.json"), action="append", content="x")
        self.assertIn("Access denied", result)

    def test_large_file_edit_read_allows_auth(self):
        result = call_tool("LargeFileEdit", file_path=os.path.join(BASE_DIR, "auth.json"), action="read")
        self.assertNotIn("Access denied", result)
        self.assertIn("auth.json", result)

    def test_edit_rejects_config(self):
        result = call_tool("Edit", file_path=os.path.join(BASE_DIR, "config.json"), old_str="a", new_str="b")
        self.assertIn("Access denied", result)
        self.assertIn("全局敏感配置", result)

    def test_patch_rejects_crew_agents(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: /home/sxkiss/bt/crew_agents.json\n"
            "@@\n"
            '-  "departments": []\n'
            '+  "departments": [{"name":"x"}]\n'
            "*** End Patch\n"
        )
        result = call_tool("Patch", patch_text=patch)
        self.assertIn("Access denied", result)


if __name__ == "__main__":
    unittest.main()


class GuardRuleShellOverwriteTest(unittest.TestCase):
    """Bash 工具通过 shell 重定向覆盖全局配置也应被拦截。"""

    def test_guard_rule_blocks_cat_overwrite_config(self):
        from chat_client.guard_rules import get_guard_rules
        rules = get_guard_rules(BASE_DIR)
        blocked, rule = rules.scan('cat > /home/sxkiss/bt/config.json <<EOF\n{"a":1}\nEOF')
        self.assertTrue(blocked)
        self.assertEqual(rule["id"], "overwrite-global-config")

    def test_guard_rule_blocks_echo_overwrite_auth(self):
        from chat_client.guard_rules import get_guard_rules
        rules = get_guard_rules(BASE_DIR)
        blocked, rule = rules.scan('echo x > /home/sxkiss/bt/auth.json')
        self.assertTrue(blocked)
        self.assertEqual(rule["id"], "overwrite-global-auth")

    def test_guard_rule_allows_normal_bash(self):
        from chat_client.guard_rules import get_guard_rules
        rules = get_guard_rules(BASE_DIR)
        blocked, _ = rules.scan('ls -la /home/sxkiss/bt')
        self.assertFalse(blocked)
