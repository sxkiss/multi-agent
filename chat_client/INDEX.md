<!-- AUTO-DOC: Update me when files in this folder change -->

# Chat Client

AI Agent 核心运行时：主循环、会话记忆、检索增强、MCP 接入、技能管理、热加载。

## Files

| File | Role | Function |
|------|------|----------|
| `__init__.py` | Package | 包入口，导出 AgentConfig |
| `agent.py` | Core | 主 Agent 循环（流式/重试/续写/工具执行） |
| `memory.py` | Memory | 会话历史持久化（原子写，损坏自动备份） |
| `retrieval.py` | RAG | SimpleVectorDB + RAG + Mem0 记忆 |
| `mcp_client.py` | MCP | MCP stdio 客户端（单例+锁） |
| `mcp_manager.py` | MCP | MCP 管理器（多实例路由） |
| `mcp_server.py` | MCP | MCP HTTP Server（FastAPI 托管） |
| `skills.py` | Skills | 技能扫描/安装/卸载 |
| `hotreload.py` | Infra | 热加载中心（tools/config/crew/mcp） |
| `api_retry.py` | Infra | API 断流重试判断 |
| `agents_md.py` | Infra | Agent Markdown 描述生成 |
| `opencode_bridge.py` | MCP | OpenCode MCP Bridge |
| `opencode_config.py` | MCP | OpenCode 配置解析 |
| `opencode_templates.py` | MCP | OpenCode 模板 |
| `claude_bridge.py` | MCP | Claude MCP Bridge |

## Subdirectories

- [tools/](./tools/INDEX.md) — 工具注册与实现（142+ 工具）
