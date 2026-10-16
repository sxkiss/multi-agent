"""平台扩展层（Platform Ext）—— 自包含命名空间。

对齐 Tencent Octop 的若干平台能力，但不依赖 Octop 内部结构
（无 DB/repos/OctopConfig），只依赖标准库 + fastapi + BT 现有模块。

覆盖四项（对齐 Octop，不破坏现有 API）：
- teams    ：专家团队实体（持久化 roster + 模板），复用 BT 既有 AgentRegistry/RunCrew
- channels ：可插拔通道抽象（BaseChannel + 注册表 + WebSocket 通道）
- storage  ：可插拔工作区后端（本地/文件系统/Docker），默认本地
- cli      ：薄 CLI 入口（复用现有 HTTP API）

设计约束（不破坏现有 API）：
- 所有新增 HTTP 路由走 /api/v2/* 前缀；
- 配置存 BT config.json 的 platform_ext 段（不引入数据库、不引入多用户）；
- 鉴权复用 BT auth.py 的 JWT verify_token。
"""

__all__ = ["teams", "channels", "storage", "cli", "api"]

PROJECT_ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
