#!/usr/bin/env python
"""
回归测试：ChatJob 的持久化路径必须与 job.key 一致。

背景：create_if_idle 曾先构造 ChatJob（此时 persist_path 按默认随机 key 生成），
再覆盖 job.key。导致所有 job 的事件混写到一个随机文件名，磁盘恢复时 key 对不上，
SSE 续传死锁（last_id 卡住、前端收不到新消息）。本测试确保 key 与 persist_path 永远一致。

运行：
    .venv/bin/python -m pytest tests/test_job_persist_key.py -q
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_server as W  # noqa: E402

def test_create_if_idle_persist_path_matches_key():
    ws = W
    original_base = ws.BASE_DIR
    tmp = tempfile.mkdtemp()
    ws.BASE_DIR = tmp
    try:
        mgr = ws.ChatJobManager()
        mgr._jobs = {}
        job = mgr.create_if_idle("sess_protocol_test", "hello")
        assert job is not None
        # 关键断言：persist_path 文件名必须等于 job.key（而不是默认随机 key）
        expected = os.path.join(tmp, "jobs", job.key.replace("/", "_") + ".jsonl")
        assert job.persist_path == expected
        assert os.path.basename(job.persist_path) == job.key + ".jsonl"
        # 模拟事件落盘，确认写入正确文件
        job.append("message", "hi")
        with open(job.persist_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert '"message"' in content
        # 目录里不应出现其他随机 key 文件
        files = os.listdir(os.path.join(tmp, "jobs"))
        assert files == [job.key + ".jsonl"]
    finally:
        ws.BASE_DIR = original_base


def test_recover_uses_same_key():
    """恢复时按文件名 key 重建 job，应与原 job.key 一致。"""
    ws = W
    original_base = ws.BASE_DIR
    tmp = tempfile.mkdtemp()
    ws.BASE_DIR = tmp
    try:
        mgr = ws.ChatJobManager()
        mgr._jobs = {}
        job = mgr.create_if_idle("sess_recover", "hello")
        job.append("message", "x")
        job.finish("done")
        key = job.key
        # 从磁盘恢复
        recovered = ws.ChatJob.recover(key)
        assert recovered is not None
        assert recovered.key == key
        assert recovered.persist_path == os.path.join(tmp, "jobs", key + ".jsonl")
        # 恢复的事件应存在
        assert len(recovered.events) >= 1
    finally:
        ws.BASE_DIR = original_base


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
