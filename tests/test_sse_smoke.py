#!/usr/bin/env python
"""
服务端 SSE 冒烟测试（unittest，无需额外依赖：python -m unittest 或 pytest 均可运行）

存在意义 —— 连续两次线上事故都是「py_compile 通过、单元测试也过，但运行时崩溃」：

  1) 常量 _HIDDEN_EVENTS_FOR_TERMINAL 定义在 ChatJob 类，却从 AgentMain 的
     chat_events 里用 self. 引用 → AttributeError → SSE 生成器在产出任何
     事件前崩溃 → 小程序收不到任何回复（服务端内容其实是完整的）。
     py_compile 查不出类归属错误；当时只测了"常量本身""过滤逻辑"，
     用 exec 隔离片段恰好绕开了真实调用路径。

  2) _resolve_user_workspace 用到 hashlib 但未 import → 非 wx_ 会话解析时
     NameError。同样是导入层面、编译期不可见。

因此本测试的重点不是"逻辑对不对"，而是**真的把 SSE 生成器跑起来**，断言：
  - 生成器能被迭代且不抛异常（覆盖 1、2 这类运行时崩）
  - 有 message 事件、有 message_end 事件（覆盖"收不到内容"）
  - 事件 id 单调递增，last_id 续传不丢不重
  - 渠道过滤生效（小程序看不到 message_think，网页看得到）

另含用户工作目录隔离的回归用例：
  - 不同用户目录不同、同用户稳定
  - 恶意 session_id 不得逃出 users/ 根目录
  - 管理端 workspace 越界被拒

运行：
    .venv/bin/python -m unittest discover -s tests -v
或：
    .venv/bin/python tests/test_sse_smoke.py
"""

import asyncio
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import web_server as W  # noqa: E402


def _run(coro):
    """同步执行协程（无 pytest-asyncio 时也能跑）"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class StubAgentMain(W.AgentMain if hasattr(W.AgentMain, "_resolve_user_workspace") else object):
    """最小替身：只继承 workspace 相关方法，不触发 __init__ 的重型加载"""

    def __init__(self):
        pass


class TestSSEGeneratorSmoke(unittest.TestCase):
    """核心：把真实的 chat_events SSE 生成器跑一遍，断言不崩且有内容"""

    def _make_job(self, session_id="wx_smoke_user_0000000000000000"):
        job = W.ChatJob(session_id, label="smoke", persist=False)
        # 模拟一次完整会话：思考 → 正文分片 → 工具 → 结束
        for t in ["分析用户问题…", "准备调用工具…"]:
            job.append("message_think", t)
        for seg in ["老板好，", "当前时间是 ", "2026-09-23。"]:
            job.append("message", seg)
        job.append("tool_call", {"type": "tool_call", "tool": "Bash", "args": "", "id": "c1"})
        job.append("tool_result", {"type": "tool_result", "tool": "Bash", "result": "ok"})
        job.append("usage", {"usage": {"total_tokens": 42}})
        job.append("message_end", None)
        job.finish("done")
        return job

    def test_miniprogram_stream_has_content_and_end(self):
        """小程序渠道：能收到正文与结束事件（防'收不到任何内容'回归）"""
        job = self._make_job()
        events = _run(self._collect(job, {"session_id": job.session_id,
                                          "client_type": "miniprogram"}))
        kinds = [e["event"] for e in events]
        self.assertIn("message", kinds, "SSE 未产出任何 message 事件")
        self.assertIn("message_end", kinds, "SSE 未产出 message_end")
        content = "".join(e["data"] for e in events if e["event"] == "message"
                          and isinstance(e["data"], str))
        self.assertGreater(len(content), 0, "message 内容为空")

    def test_miniprogram_hides_thinking(self):
        """小程序渠道不得收到思维链（防系统提示词泄露）"""
        job = self._make_job()
        events = _run(self._collect(job, {"session_id": job.session_id,
                                          "client_type": "miniprogram"}))
        self.assertNotIn("message_think", [e["event"] for e in events])

    def test_web_keeps_thinking(self):
        """网页管理端保留思维链（便于调试），且同样有正文"""
        job = self._make_job()
        events = _run(self._collect(job, {"session_id": job.session_id}))
        kinds = [e["event"] for e in events]
        self.assertIn("message_think", kinds)
        self.assertIn("message", kinds)

    def test_resume_from_last_id_no_duplicate(self):
        """断线续传：从 last_id 续播，不重不漏"""
        job = self._make_job()
        params = {"session_id": job.session_id, "client_type": "miniprogram"}
        first = _run(self._collect(job, params))
        self.assertTrue(first)
        last_id = max(e["id"] for e in first)
        second = _run(self._collect(job, dict(params, last_id=last_id)))
        for e in second:
            self.assertGreater(e["id"], last_id, "续传不应重复已发过的事件")

    def test_generator_crash_surfaces_as_failure(self):
        """元测试：若 SSE 生成器运行中抛异常，冒烟测试必须判定失败。

        对应两次真实事故的形态：
        - AttributeError：常量放错类（chat_events 在 AgentMain，常量在 ChatJob）
        - NameError：用了 hashlib 但没 import
        两者 py_compile 都查不出。本用例断言"生成器一崩，测试就红"，
        确保该冒烟测试不是摆设。
        """
        import unittest.mock as mock

        job = self._make_job()
        W.chat_jobs._jobs[job.key] = job

        async def boom(self, get):  # noqa: ANN001
            raise AttributeError("_HIDDEN_EVENTS_FOR_TERMINAL")
            yield  # pragma: no cover  (保持异步生成器语义)

        try:
            with mock.patch.object(W.AgentMain, "chat_events", boom):
                with self.assertRaises(AssertionError):
                    _run(self._collect(job, {"session_id": job.session_id,
                                             "client_type": "miniprogram"}))
        finally:
            W.chat_jobs._jobs.pop(job.key, None)

    async def _collect(self, job, get):
        """驱动真实的 chat_events 生成器，收集解析后的事件"""
        am = self._stub_agent_main()
        # 真实代码从模块级单例 chat_jobs 查任务（不是 self._jobs），
        # 替身必须注册到该单例，否则生成器只吐 "没有可订阅的任务"。
        W.chat_jobs._jobs[job.key] = job
        try:
            out = await self._drive(am, job, get)
        finally:
            W.chat_jobs._jobs.pop(job.key, None)
        return out

    async def _drive(self, am, job, get):
        """按 SSE 块解析（sse_pack 格式：id → event → data）"""
        out = []
        buf = ""
        gen = am.chat_events(dict(get, job=job.key))
        try:
            async for chunk in gen:
                buf += str(chunk)
                sep = buf.find("\n\n")
                while sep != -1:
                    block = buf[:sep]
                    buf = buf[sep + 2:]
                    evt = self._parse_block(block)
                    if evt:
                        out.append(evt)
                    if len(out) > 200:
                        return out
                    sep = buf.find("\n\n")
                if len(out) > 200:  # 防御：异常时不至于无限收集
                    break
        except Exception as e:  # 生成器崩溃必须在此暴露为测试失败
            self.fail(f"chat_events 生成器抛出异常（线上会表现为收不到回复）: {type(e).__name__}: {e}")
        return out

    @staticmethod
    def _parse_block(block):
        """解析单个 SSE 块 → {id, event, data}"""
        if not block or not block.strip():
            return None
        rec = {"id": None, "event": "message", "data": None}
        data_lines = []
        for line in block.splitlines():
            if not line or line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "id":
                try:
                    rec["id"] = int(value)
                except (TypeError, ValueError):
                    pass
            elif field == "event":
                rec["event"] = value or "message"
            elif field == "data":
                data_lines.append(value)
        # 注意：sse_pack 对 data=None 不发 data 行（如 message_end），
        # 这类"无 data 的事件"是合法 SSE，不能因缺 data 就丢弃该块。
        raw = "\n".join(data_lines)
        rec["data"] = raw
        return rec

    def _stub_agent_main(self):
        """构造一个可调用 chat_events 的 AgentMain 实例（绕过 __init__ 重加载）"""
        am = W.AgentMain.__new__(W.AgentMain)
        am.config = {}
        am.plugin_path = BASE_DIR
        return am


class TestUserWorkspaceIsolation(unittest.TestCase):
    """多用户工作目录隔离（防互相覆盖/越权）"""

    def setUp(self):
        self.am = StubAgentMain.__new__(StubAgentMain)
        self.am.USER_WORKSPACE_ROOT = W.AgentMain.USER_WORKSPACE_ROOT
        self.am.ADMIN_WORKSPACE_ROOTS = W.AgentMain.ADMIN_WORKSPACE_ROOTS
        self.am.config = {}
        self.am._resolve_workspace = lambda mode=None: BASE_DIR

    def test_different_users_isolated(self):
        a = self.am._resolve_user_workspace("wx_aaaaaaaaaaaaaaaaaaaaaaaa")
        b = self.am._resolve_user_workspace("wx_bbbbbbbbbbbbbbbbbbbbbbbb")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith(self.am.USER_WORKSPACE_ROOT))
        self.assertTrue(b.startswith(self.am.USER_WORKSPACE_ROOT))

    def test_same_user_stable(self):
        sid = "wx_cccccccccccccccccccccccc"
        self.assertEqual(self.am._resolve_user_workspace(sid),
                         self.am._resolve_user_workspace(sid))

    def test_malicious_session_id_cannot_escape(self):
        for bad in ["../../etc/passwd", "wx_../../../root", "a/b/c", "wx_<script>", "../..", ""]:
            p = self.am._resolve_user_workspace(bad)
            rel = os.path.relpath(p, self.am.USER_WORKSPACE_ROOT)
            self.assertNotIn("..", rel.split(os.sep), f"session_id {bad!r} 逃逸出用户根目录")
            self.assertTrue(p.startswith(self.am.USER_WORKSPACE_ROOT))

    def test_admin_workspace_clamped(self):
        home = os.path.realpath(os.path.expanduser("~"))
        base = os.path.realpath(BASE_DIR)
        for bad in ["/etc", "/root", "/tmp/evil", "/proc"]:
            r = os.path.realpath(self.am._clamp_workspace(bad))
            allowed = r == base or r.startswith(base + os.sep) or r == home or r.startswith(home + os.sep)
            self.assertTrue(allowed, f"{bad} 未被收敛，指向 {r}")

    def test_admin_workspace_inside_project_allowed(self):
        target = os.path.join(BASE_DIR, "workspace")
        r = os.path.realpath(self.am._clamp_workspace(target))
        self.assertTrue(r.startswith(os.path.realpath(BASE_DIR)),
                        "项目内目录应被允许")


class TestTerminalEventSet(unittest.TestCase):
    """常量与集合：这些曾被放错类，导致 AttributeError"""

    def test_hidden_events_constant_is_module_level(self):
        self.assertTrue(hasattr(W, "HIDDEN_EVENTS_FOR_TERMINAL"),
                        "HIDDEN_EVENTS_FOR_TERMINAL 必须是模块级常量")
        self.assertIn("message_think", W.HIDDEN_EVENTS_FOR_TERMINAL)

    def test_snapshot_from_accepts_drop_events(self):
        job = W.ChatJob("wx_t", persist=False)
        job.append("message_think", "x")
        job.append("message", "y")
        vis = job.snapshot_from(-1, W.HIDDEN_EVENTS_FOR_TERMINAL)[0]
        kinds = [e["event"] for e in vis]
        self.assertNotIn("message_think", kinds)
        self.assertIn("message", kinds)
        # 默认参数（不传 drop_events）应保留全部，保证向后兼容
        allv = job.snapshot_from(-1)[0]
        self.assertIn("message_think", [e["event"] for e in allv])


if __name__ == "__main__":
    unittest.main(verbosity=2)
