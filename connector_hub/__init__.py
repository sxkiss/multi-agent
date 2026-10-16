"""连接器中心（Connector Hub）—— 自包含命名空间。

对齐 Tencent Octop 的 Connectors 体系，但完全不依赖 Octop 内部结构
（无 DB/repos/OctopConfig），只依赖标准库 + fastapi + BT 现有模块。

设计约束（不破坏现有 API）：
- 所有新增 HTTP 路由走 /api/v2/connectors/* 前缀；
- 凭据用本地 Fernet 加密存 config.json（不引入数据库、不引入多用户）；
- 鉴权复用 BT auth.py 的 JWT verify_token；
- remote 模式连接器生成 BT 已有的 mcp/config.json 条目（http 类型），
  由本层新增的 HTTP MCP 客户端执行；
- gateway 模式连接器经内部 MCP JSON-RPC 适配器调用。
"""

__all__ = [
    "http_mcp",
    "connectors_catalog",
    "connectors_service",
    "connectors_gateway",
    "connectors_api",
]

# 项目根目录（BT 主目录，含 config.json / mcp/config.json）
PROJECT_ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
