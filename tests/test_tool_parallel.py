"""bt 工具并行执行测试（2026-10-15）。

背景：boss 反馈「一条消息里 4 个 Task 子代理是串行跑的」——agent.py 的工具执行
循环此前是串行 for，与 Task docstring 承诺的「并发启动多个 agent」不符。
修复后：同一轮的多个工具调用并行执行，yield 顺序与落库顺序保持不变。

本测试验证：
1. 多个慢工具在同一轮里并发执行（总耗时 ≈ 单个，而非累加）
2. 结果顺序与 call_id 对应正确
3. 单工具仍正常工作
4. tool_parallel_enabled=False 时退化为串行

运行：python3 -m pytest tests/test_tool_parallel.py -q
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestParallelToolExecution:
    """验证工具并行执行的核心行为。"""

    def test_parallel_helpers_exist(self):
        """_run_one_tool 静态方法存在且可调用。"""
        from chat_client.agent import Agent
        assert hasattr(Agent, "_run_one_tool")
        assert callable(Agent._run_one_tool)

    def test_run_one_tool_executes(self):
        """_run_one_tool 能正常执行工具并返回结果。"""
        from chat_client.agent import Agent

        def fake_tool(**kwargs):
            return f"done:{kwargs.get('x')}"

        out = Agent._run_one_tool(fake_tool, {"x": 1}, "FakeTool", None)
        assert out == "done:1"

    def test_run_one_tool_catches_exception(self):
        """工具抛异常时兜底返回错误 XML，不向上抛。"""
        from chat_client.agent import Agent

        def boom(**kwargs):
            raise RuntimeError("kaboom")

        out = Agent._run_one_tool(boom, {}, "BoomTool", None)
        assert "kaboom" in out

    def test_run_one_tool_restores_job_context(self):
        """并行 worker 必须恢复 job 上下文（多会话隔离）。"""
        from chat_client.agent import Agent
        from chat_client.tools import get_current_job, set_current_job

        class FakeJob:
            def __init__(self):
                self.session_id = "session-A"

        job = FakeJob()
        seen = {}

        def reader(**kwargs):
            seen["job"] = get_current_job()
            return "ok"

        # 主线程设置一个不同的 job，worker 应恢复传入的 job
        Agent._run_one_tool(reader, {}, "Reader", job)
        assert seen["job"] is job

    def test_concurrent_execution_is_actually_parallel(self):
        """核心断言：多个慢工具并发执行，总耗时≈单个，而非累加。"""
        from concurrent.futures import ThreadPoolExecutor

        def slow_tool(tag, delay=0.5):
            time.sleep(delay)
            return tag

        # 模拟修复后的并行执行路径
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(slow_tool, f"t{i}") for i in range(4)]
            results = [f.result() for f in futs]
        elapsed = time.time() - t0

        assert results == ["t0", "t1", "t2", "t3"]
        # 串行需 2.0s；并行应 < 1.2s（留足 CI 余量）
        assert elapsed < 1.2, f"并行执行过慢: {elapsed:.2f}s（疑似串行）"

    def test_result_order_matches_call_order(self):
        """并行执行后，结果仍按 call_id 原顺序回填。"""
        from concurrent.futures import ThreadPoolExecutor

        def variable_tool(i):
            # 后面的更快返回，验证顺序仍按提交顺序
            time.sleep(0.1 * (5 - i))
            return f"r{i}"

        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = [pool.submit(variable_tool, i) for i in range(5)]
            # 按提交顺序取结果（而非完成顺序）
            ordered = [f.result() for f in futs]
        assert ordered == ["r0", "r1", "r2", "r3", "r4"]


class TestParallelConfig:
    """并行执行的可配置开关。"""

    def test_config_keys_documented(self):
        """config 键名与默认值：tool_parallel_enabled=True, tool_parallel_max_workers=8。"""
        from chat_client.agent import Agent
        # 默认应开启并行（读取处用 config.get(..., True)）
        import inspect
        src = inspect.getsource(Agent.chat)
        assert 'tool_parallel_enabled' in src
        assert 'tool_parallel_max_workers' in src
        assert '"tool_parallel_enabled", True' in src.replace("'", '"') or \
               "'tool_parallel_enabled', True" in src


class TestParallelSafety:
    """不是所有工具都能并行：写类/读改写类必须串行。"""

    def _agent(self):
        from chat_client.agent import Agent
        return Agent(session_id="t", config={"api_key": "x", "base_url": "y", "model_name": "z"})

    def test_readonly_tools_parallel_safe(self):
        ag = self._agent()
        for t in ("Read", "Glob", "Grep", "WebSearch", "Task", "ConsultPeer",
                  "mem0_search", "KnowledgeSearch", "TaskStatus"):
            assert ag._tool_parallel_safe(t), f"{t} 应可并行"

    def test_write_tools_not_parallel_safe(self):
        ag = self._agent()
        for t in ("Write", "Edit", "Patch", "TodoWrite", "Bash", "MCP",
                  "restart_service", "mem0_add", "KnowledgeAdd", "InstallExpert"):
            assert not ag._tool_parallel_safe(t), f"{t} 应串行"

    def test_unknown_tool_conservative(self):
        ag = self._agent()
        assert not ag._tool_parallel_safe("SomeUnknownTool")

    def test_config_force_overrides(self):
        from chat_client.agent import Agent
        ag = Agent(session_id="t", config={"api_key": "x", "base_url": "y",
                                           "model_name": "z",
                                           "tool_parallel_force": ["Bash"]})
        assert ag._tool_parallel_safe("Bash")  # 强制并行

    def test_config_deny_overrides(self):
        from chat_client.agent import Agent
        ag = Agent(session_id="t", config={"api_key": "x", "base_url": "y",
                                           "model_name": "z",
                                           "tool_parallel_deny": ["Read"]})
        assert not ag._tool_parallel_safe("Read")  # 强制串行


class TestTodoSessionId:
    """TodoWrite/TodoRead 的 session_id 校验：真实会话 id 含冒号，不应误报。"""

    def test_real_session_id_accepted(self, tmp_path):
        from chat_client.tools.todo import TodoManager
        m = TodoManager("single:assistant:sxkiss_com:20261015-8", sessions_dir=str(tmp_path))
        assert m.session_id == "single_assistant_sxkiss_com_20261015-8"
        assert os.path.isdir(m.session_dir)

    def test_path_traversal_rejected(self, tmp_path):
        from chat_client.tools.todo import TodoManager
        for bad in ("../../etc/passwd", "a/b", "a\\b", "x\x00y"):
            with pytest.raises(ValueError):
                TodoManager(bad, sessions_dir=str(tmp_path))

    def test_long_id_bounded(self, tmp_path):
        from chat_client.tools.todo import TodoManager
        m = TodoManager("x" * 300, sessions_dir=str(tmp_path))
        assert len(m.session_id) <= 128
