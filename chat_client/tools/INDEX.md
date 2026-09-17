<!-- AUTO-DOC: Update me when files in this folder change -->

# Tools

工具注册中心与实现模块。所有工具经 `@register_tool` 装饰器注册，统一获得 Schema 生成、参数校验、危险黑名单、超时控制、审计日志等能力。

## Files

| File | Role | Function |
|------|------|----------|
| `__init__.py` | Core | ToolRegistry 核心：注册/热重载/黑名单/权限上下文 |
| `base.py` | Core | `_xml_response()` 格式化响应 |
| `agent_tools.py` | Tools | 文件/系统/网络/网站/数据库操作工具集 |
| `terminal.py` | Tools | RunCommand 后台命令管理 |
| `sys_ops.py` | Tools | 运维增强 13 件套（服务器状态/Docker/防火墙等） |
| `scheduler.py` | Tools | 定时任务系统（4 个调度工具） |
| `task.py` | Tools | Task 子代理 + RunCrew 集团模式 + 组织扩张 |
| `edit.py` | Tools | 文件写入/编辑 |
| `edit_append.py` | Tools | 追加内容到文件末尾 |
| `edit_append_content.py` | Tools | 追加内容变体 |
| `edit_append_final.py` | Tools | 追加内容最终版 |
| `edit_extra.py` | Tools | 编辑增强工具 |
| `edit_tail.py` | Tools | 编辑尾部工具 |
| `mcp_tool.py` | Tools | MCP 工具封装 |
| `mem0.py` | Tools | Mem0 记忆工具 |
| `skill.py` | Tools | 技能安装/卸载工具 |
| `summary.py` | Tools | 摘要生成工具 |
| `todo.py` | Tools | TODO 管理工具 |
| `webfetch.py` | Tools | 网页获取工具（curl/WebFetch/http_check） |
