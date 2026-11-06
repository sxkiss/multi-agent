"""测试全局隔离。

关键约束：metrics 是进程级单例，一旦被工具执行入口创建，落盘路径就固定了。
若测试跑在项目目录下（WorkingDirectory=/home/sxkiss/bt），就会直接写进
生产统计文件 metrics/tool_usage.json、metrics/skill_usage.json —— 这些文件
承载的是「哪些工具/技能真被用过」的长期决策数据，被测试污染后就失去参考价值。

因此在这里强制把统计目录重定向到临时目录，并在每个测试前后重置单例。
"""
import os
import tempfile

import pytest

# 必须在任何 chat_client 导入之前设置（pytest 会先加载 conftest）
_METRICS_TMP = tempfile.mkdtemp(prefix="bt_metrics_test_")
os.environ["BT_METRICS_DIR"] = _METRICS_TMP

# 后台任务仓库同样是进程级单例，若测试跑在项目目录下会直接写进生产
# background_tasks.sqlite，污染真实任务状态机。这里强制重定向到临时 DB。
_BG_DB_TMP = tempfile.mktemp(prefix="bt_bgtasks_test_", suffix=".sqlite")
os.environ["BT_BG_TASKS_DB"] = _BG_DB_TMP

# 日志同理：web_server 模块级调用 _setup_app_logging()，一旦被测试 import，
# 所有测试日志都会写进生产 logs/app.log，造成"生产日志里出现 object()/FakeAgent
# 之类测试桩报错"的假象（排查时误判为线上 bug）。这里提前把日志目录指向临时目录。
_LOG_TMP = tempfile.mkdtemp(prefix="bt_logs_test_")
os.environ["BT_LOG_DIR"] = _LOG_TMP


@pytest.fixture(autouse=True)
def _isolate_metrics():
    """每个测试前后重置 metrics 单例，保证路径隔离生效且用例互不干扰。"""
    from chat_client import metrics as metrics_mod
    from chat_client.tools import bg_repo, set_current_job

    metrics_mod.reset_metrics()
    bg_repo.close_repo()
    # job 上下文是线程级全局状态，测试桩（FakeJob/J 等）若不清理会泄漏到后续
    # 测试，导致 Task 读取 parent_job.key 时报 AttributeError。这里统一隔离。
    set_current_job(None)
    yield
    try:
        metrics_mod._registry and metrics_mod._registry.flush()
    except Exception:
        pass
    metrics_mod.reset_metrics()
    bg_repo.close_repo()
    set_current_job(None)
