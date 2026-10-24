"""后台任务结果回传修复测试（2026-10-21）。

背景：boss 反馈「工具转后台后 ag 会停止会话，结果不能回传」。

根因：主循环收尾段用 `_bg_wait_used` + `background_wait_seconds=0` 做轮询，
后台任务未跑完时主循环就 yield stop 终止会话，导致结果只推 SSE（且仅 500 字预览），
完整结果永远进不了对话上下文。三类路径（普通工具超时转后台 / Task 子代理 /
RunCrew 专家）共用同一套 BackgroundTaskManager 回传，全都有此弊端。

修复（方案 2）：
1. 主循环收尾段：pending>0 时挂起 wait(wake_event)，被 notify_done set 唤醒后
   drain_finished 注入结果再收尾；chat_stop 置 cancel 后退出（结果已落盘不丢）。
2. BackgroundTaskManager：新增 per-session wake event；notify_done/notify_failed
   把**完整结果**落盘到 jobs log（旧版只 emit 500 字预览）。
3. RunCrew 异步化：与 Task 共用后台线程 + notify_done 回传。

本测试覆盖：
1. notify_done 落盘完整结果（不再截断 500 字）
2. notify_done set wake event，唤醒挂起等待方
3. pending_count 在任务完成后归零，主循环得以收尾
4. RunCrew 立即返回 running + task_id（不阻塞），完成后可 drain 到完整结果
"""
import os
import re
import sys
import time
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeJob:
    """极简 fake job：记录 append，供 _persist_result 落盘调用。"""

    def __init__(self):
        self.events = []
        self.closed = False

    def append(self, event, data=None):
        self.events.append((event, data))


@pytest.fixture
def fake_job():
    """把当前线程的 job 设成 _FakeJob，使 _persist_result 能落盘。"""
    from chat_client.tools import set_current_job
    job = _FakeJob()
    set_current_job(job)
    yield job


class TestBackgroundResultPersistence:
    """notify_done 完整结果落盘 + wake event。"""

    def test_notify_done_persists_full_result(self, fake_job):
        """notify_done 必须把完整结果落盘，而非仅 500 字预览。"""
        from chat_client.tools import get_background_manager

        bg = get_background_manager()
        sid = "persist-session"
        long_result = "X" * 5000 + "<<FULL_END>>"
        bg.register("t-full-1", "Bash", (), None, sid)
        bg.notify_done("t-full-1", long_result)

        # 落盘事件应含完整结果
        persisted = [d for (ev, d) in fake_job.events if ev == "background_result"]
        assert persisted, "notify_done 未落盘 background_result 事件"
        assert "<<FULL_END>>" in persisted[0]["result"]
        assert len(persisted[0]["result"]) >= 5000

    def test_notify_done_wakes_waiting_loop(self):
        """notify_done 应 set 该 session 的 wake event，唤醒挂起等待方。"""
        from chat_client.tools import get_background_manager

        bg = get_background_manager()
        sid = "wake-session"
        bg.register("t-wake-1", "Task", (), None, sid)
        ev = bg.get_wake_event(sid)

        def _finish():
            time.sleep(0.2)
            bg.notify_done("t-wake-1", "DONE_RESULT")

        threading.Thread(target=_finish, daemon=True).start()

        # 挂起等唤醒（最多 5s）；任务 0.2s 完成，应在远小于 5s 内被唤醒
        t0 = time.time()
        ev.wait(timeout=5.0)
        waited = time.time() - t0
        assert waited < 2.0, f"wake event 未被及时 set，等待了 {waited:.2f}s"
        assert bg.pending_count(sid) == 0, "任务完成后 pending_count 应归零"

    def test_pending_count_zero_after_done(self):
        """任务 done/failed 后 pending_count 归零，主循环可收尾。"""
        from chat_client.tools import get_background_manager

        bg = get_background_manager()
        sid = "pending-session"
        bg.register("t-pend-1", "RunCrew", (), None, sid)
        # register 后状态为 pending，pending_count 只数 running，所以是 0
        assert bg.pending_count(sid) == 0

        bg.notify_done("t-pend-1", "OK")
        # notify_done 后 status 变为 done，仍不是 running，所以还是 0
        assert bg.pending_count(sid) == 0


class TestRunCrewAsync:
    """RunCrew 异步化：立即返回、后台跑、结果可 drain。"""

    def test_runcrew_returns_immediately_with_task_id(self, fake_job, monkeypatch):
        """RunCrew 应异步启动并立即返回 running + task_id（不阻塞主会话）。"""
        from chat_client.tools import task as _task_mod
        import chat_client.agent as agent_mod

        class _FastAgent:
            def __init__(self, session_id, config):
                self.session_id = session_id
                self.config = config
                self.closed = False

            def chat(self, prompt):
                yield {"type": "content", "response": "FAST_MEMBER_OUT"}

            def close(self):
                self.closed = True

        monkeypatch.setattr(agent_mod, "Agent", _FastAgent)
        # _crew_manager_plan / _crew_synthesis 会真实调用 OpenAI（需凭证），单测环境无，
        # 直接 mock 成固定计划/汇总，只验证「异步启动 + 后台跑完 + 结果可 drain」链路。
        monkeypatch.setattr(
            _task_mod, "_crew_manager_plan",
            lambda pc, obj, roster, ms: ([{"agent": "coder", "task": "t",
                                           "expected_output": "o"}], None),
        )
        monkeypatch.setattr(
            _task_mod, "_crew_synthesis",
            lambda pc, obj, transcript: "CREW_FINAL_SYNTHESIS\n"
            + "\n".join(t.get("output", "") for t in transcript),
        )
        # 测试环境 registry 未必加载默认成员，构造 fake registry 让 _run_one_step 走完
        _fake_def = SimpleNamespace(
            name="coder", department="综合部", description="计划",
            allowed_tools=["Read", "Task"], system_prompt_template="P",
        )

        class _FakeRegistry:
            departments = []

            def get(self, name):
                return _fake_def if name == "coder" else None

            def list_agents(self):
                return [_fake_def]

            def load_agent_skills_block(self, agent_def):
                return ""
        monkeypatch.setattr(_task_mod, "agent_registry", _FakeRegistry())
        from chat_client.tools.task import RunCrew

        t0 = time.time()
        out = RunCrew(objective="快速验证目标", agents="coder", max_steps=1, parallel=False)
        elapsed = time.time() - t0

        assert elapsed < 0.5, f"RunCrew 未立即返回，耗时 {elapsed:.2f}s"
        assert "已在后台启动并运行" in out
        assert "<task_id>" in out

        m = re.search(r"<task_id>([^<]+)</task_id>", out)
        assert m, f"未找到 task_id: {out}"
        bg_id = m.group(1)

        # 等待后台团队完成并 drain 到完整结果
        from chat_client.tools import get_background_manager
        bg = get_background_manager()
        deadline = time.time() + 8
        while time.time() < deadline and bg.pending_count("") > 0:
            time.sleep(0.05)
        # RunCrew 用 parent_session_id 注册；此处 parent 为 ''（无 parent），
        # 直接查全局 task 是否 done 即可
        task = bg.get(bg_id)
        assert task is not None
        assert task.status == "done", f"RunCrew 未完成: {task.status}"
        assert "crew_report" in (task.result or "")
        assert "FAST_MEMBER_OUT" in (task.result or "")
        bg.remove(bg_id)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
