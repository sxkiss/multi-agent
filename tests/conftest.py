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


@pytest.fixture(autouse=True)
def _isolate_metrics():
    """每个测试前后重置 metrics 单例，保证路径隔离生效且用例互不干扰。"""
    from chat_client import metrics as metrics_mod

    metrics_mod.reset_metrics()
    yield
    try:
        metrics_mod._registry and metrics_mod._registry.flush()
    except Exception:
        pass
    metrics_mod.reset_metrics()
