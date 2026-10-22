"""内存僵尸任务回收测试：ChatJobManager.prune_memory_jobs。

覆盖：
- 已终结（done/stopped/error）且超过 JOB_TTL 的 job 被移除并释放 agent/events；
- running 状态的 job 不被回收（用户可能续播）；
- 仍在 TTL 内的已终结 job 保留（刚结束不急着清）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# web_server 模块级导入会初始化 agent_main 等单例，放函数内懒加载并缓存，
# 避免每次测试都重新 import。
_web_server = None


def _ws():
    global _web_server
    if _web_server is None:
        import web_server
        _web_server = web_server
    return _web_server


def _make_manager():
    """构造无磁盘持久化的 ChatJobManager（persist=False）。"""
    mgr = _ws().ChatJobManager()
    mgr._jobs = {}  # 隔离全局状态
    return mgr


def _make_job(mgr, session_id, status, age_seconds, with_agent=True):
    ws = _ws()
    job = ws.ChatJob(session_id, persist=False)
    # 直接构造，绕过 create_if_idle 的并行计数（key 格式对齐 make_key）
    key = f"{session_id}::test0001"
    job.key = key
    job.status = status
    job.created_at = time.time() - age_seconds
    job._seq = 5
    job.events = [{"id": i, "event": "message", "data": "x"} for i in range(10)]
    if with_agent:
        agent = object()  # 用真实对象模拟 agent（仅验证引用被置空）
        job.attach_agent(agent)
    mgr._jobs[key] = job
    return job


def test_prune_removes_terminated_stale_and_frees_agent():
    mgr = _make_manager()
    j1 = _make_job(mgr, "s1", "done", age_seconds=7200, with_agent=True)
    j2 = _make_job(mgr, "s2", "stopped", age_seconds=7200, with_agent=True)
    # 仍安全的终结 job（未超 TTL）
    j3 = _make_job(mgr, "s3", "done", age_seconds=60, with_agent=True)
    # 运行中的 job：绝不回收
    j4 = _make_job(mgr, "s4", "running", age_seconds=99999, with_agent=True)

    removed = mgr.prune_memory_jobs()

    assert removed == 2
    assert j1.key not in mgr._jobs
    assert j2.key not in mgr._jobs
    # 释放大对象引用
    assert j1.agent is None
    assert j1.events == []
    assert j2.agent is None
    # 仍在 TTL 内的终结 job 保留
    assert j3.key in mgr._jobs
    assert j3.agent is not None
    # 运行中 job 保留
    assert j4.key in mgr._jobs
    assert j4.agent is not None


def test_prune_respects_batch_limit():
    mgr = _make_manager()
    for i in range(5):
        _make_job(mgr, f"s{i}", "done", age_seconds=7200, with_agent=False)
    removed = mgr.prune_memory_jobs(max_batch=2)
    assert removed == 2
    assert len(mgr._jobs) == 3


def test_prune_disabled_when_ttl_le_zero():
    mgr = _make_manager()
    mgr.JOB_TTL = 0
    _make_job(mgr, "s1", "done", age_seconds=99999, with_agent=False)
    assert mgr.prune_memory_jobs() == 0
    assert len(mgr._jobs) == 1
