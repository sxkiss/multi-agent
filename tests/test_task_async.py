"""Task 子代理异步化测试（2026-10-17）。

背景：boss 反馈「单ag subagent_type 不能并行？要等子代理回来？堵塞主会话了」。
调查结论：Task 工具内部同步消费子代理 agent.chat() 直到完成才返回，单发子代理
会阻塞主会话；且 _guarded_execute 前台等待钳制到 [10,120] 秒，超时转后台后主循环
还要再等一轮 background_wait_seconds（默认 120s），最坏 ≈240s 静默。

修复（方案 A）：
1. Task 改为异步启动：立即返回 running + task_id + subagent_session_id，
   子代理在后台线程跑，结果通过 BackgroundTaskManager 回灌父会话上下文。
2. 主循环收尾等待 background_wait_seconds 默认改为 0（不阻塞收尾）。

本测试用 FakeAgent 替代真实 LLM 子代理，验证：
1. Task 立即返回（不等待子代理完成）且包含 task_id / subagent_session_id
2. 后台子代理完成后，BackgroundTaskManager 可查到结果并注入
3. 结果包含 subagent_session_id（续聊用）
4. 未知 subagent_type 直接报错，不启动线程
"""
import os
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeAgent:
    """替代真实 Agent：chat 延迟 0.3s 后返回固定内容。"""

    instances = []

    def __init__(self, session_id, config):
        self.session_id = session_id
        self.config = config
        self.closed = False
        _FakeAgent.instances.append(self)

    def chat(self, prompt):
        time.sleep(0.3)
        yield {"type": "content", "response": "FAKE_SUBAGENT_RESULT"}

    def close(self):
        self.closed = True


@pytest.fixture
def fake_agent(monkeypatch):
    _FakeAgent.instances.clear()
    monkeypatch.setattr("chat_client.agent.Agent", _FakeAgent)
    return _FakeAgent


class TestTaskAsync:
    """Task 工具异步启动行为。"""

    def test_task_returns_immediately_with_ids(self, fake_agent):
        """Task 立即返回 running 状态，包含 task_id 与 subagent_session_id。"""
        from chat_client.tools.task import Task

        t0 = time.time()
        out = Task(
            description="测试任务",
            prompt="返回 hello",
            subagent_type="planner",
            parent_session_id="test-parent-session",
        )
        elapsed = time.time() - t0

        assert elapsed < 0.2, f"Task 未立即返回，耗时 {elapsed:.2f}s"
        assert "已启动并在后台运行" in out
        assert "<task_id>" in out
        assert "<subagent_session_id>" in out

    def test_task_result_in_background_manager(self, fake_agent):
        """子代理完成后，BackgroundTaskManager 可查询到结果。"""
        from chat_client.tools import get_background_manager
        from chat_client.tools.task import Task

        out = Task(
            description="测试任务",
            prompt="只回复 OK 即可",
            subagent_type="planner",
            parent_session_id="test-parent-session",
        )
        m = re.search(r"<task_id>([^<]+)</task_id>", out)
        assert m, f"未找到 task_id: {out}"
        bg_id = m.group(1)

        bg = get_background_manager()
        task = bg.get(bg_id)
        assert task is not None, "Task 应立即注册到后台管理器"
        assert task.status == "running"

        # 等待子代理完成（FakeAgent 0.3s 即完成）
        deadline = time.time() + 5
        while time.time() < deadline and task.status == "running":
            time.sleep(0.05)

        assert task.status == "done", f"子代理未完成: {task.status}"
        assert "FAKE_SUBAGENT_RESULT" in (task.result or "")

        # 结果应可被父会话 drain 到（主循环注入路径）
        finished = bg.drain_finished("test-parent-session")
        assert any(t["task_id"] == bg_id for t in finished)
        item = next(t for t in finished if t["task_id"] == bg_id)
        assert "FAKE_SUBAGENT_RESULT" in item["result"]

        bg.remove(bg_id)

    def test_agent_closed_after_completion(self, fake_agent):
        """子代理完成后必须调用 close() 释放资源。"""
        from chat_client.tools import get_background_manager
        from chat_client.tools.task import Task

        out = Task(
            description="测试任务",
            prompt="hi",
            subagent_type="planner",
            parent_session_id="test-parent-session-2",
        )
        bg_id = re.search(r"<task_id>([^<]+)</task_id>", out).group(1)
        bg = get_background_manager()
        task = bg.get(bg_id)

        deadline = time.time() + 5
        while time.time() < deadline and task.status == "running":
            time.sleep(0.05)

        assert fake_agent.instances, "应创建过 FakeAgent"
        assert fake_agent.instances[-1].closed, "子代理完成后未 close()"
        bg.remove(bg_id)

    def test_unknown_agent_type_errors(self):
        """未知 subagent_type 直接报错，不启动线程。"""
        from chat_client.tools.task import Task

        out = Task(
            description="非法任务",
            prompt="这会失败",
            subagent_type="not_exist_type_xyz",
        )
        assert "Unknown agent type" in out or "error" in out


if __name__ == "__main__":
    pytest.main([__file__, "-q"])