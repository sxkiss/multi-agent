# Multi-agent

通用型多智能体协作平台（Group Mode），基于开源 AI 助手改造的独立 Web 服务，提供「一人一集团」对话、技能（Skills）系统、内置工具链（终端/文件/检索/任务）与 SSE 流式输出。

## 技术栈

| 部分 | 技术 |
|------|------|
| 后端 | Python 3.12 · FastAPI · Uvicorn · openai SDK 3.x |
| 前端 | Vue 3 · Vite 5 · marked |
| 部署 | systemd（multi-agent.service） |

## 目录结构

```
bt/
├── web_server.py          # 后端入口（FastAPI 应用）
├── config.json            # 运行时配置（skills_dir 等）
├── chat_client/           # Agent 核心：会话、技能、工具、热重载
│   ├── skills.py          # SkillManager：三层披露 + 缓存
│   ├── hotreload.py       # 工具模块热重载
│   └── tools/             # 内置工具集（终端/文件/任务等）
├── skills/                # 技能目录（相对路径，可配 skills_dir 切换）
├── frontend/              # 前端源码（Vue 3 + Vite）
├── static/                # 前端构建产物（由 dist 拷贝而来，git 忽略）
├── sessions/              # 会话数据（运行时生成）
├── logs/                  # 运行日志（运行时生成）
└── requirements.txt       # Python 依赖
```

## 环境要求

- Python 3.12+
- Node.js 18+（仅构建前端时需要）

## 后端部署

```bash
# 1. 安装依赖（建议虚拟环境）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 直接启动（开发调试）
.venv/bin/python -m uvicorn web_server:app --host 0.0.0.0 --port 9876

# 3. 或使用 systemd（生产，自动重启）
# 参考配置：
#   WorkingDirectory=<项目根目录>
#   ExecStart=<项目根目录>/.venv/bin/python -m uvicorn web_server:app --host 0.0.0.0 --port 9876
sudo systemctl daemon-reload && sudo systemctl restart multi-agent.service
```

## 前端构建

前端源码在 `frontend/`，构建产物输出到 `static/` 供后端直接服务：

```bash
cd frontend
npm install
npm run build          # 产物在 frontend/dist/
cp -r dist/* ../static/   # 拷贝到 static/（保持 /static 路径下的资源引用一致）
```

注意：`static/` 目录整体被 git 忽略，构建产物不入库。

## 配置说明（config.json）

| 键 | 说明 | 相对路径支持 |
|----|------|-------------|
| `skills_dir` | 技能目录，支持相对路径（相对项目根目录解析） | ✅ |
| `workspace` | Agent 工作区，留空则用项目根目录 | ✅ |
| `mcp_config_path` | MCP 配置文件路径 | ✅ |
| `server.port` | 服务端口（默认 9876） | - |

示例：

```json
{
  "skills_dir": "skills",
  "workspace": "",
  "enable_mcp": true
}
```

## 技能系统（Skills）

- 默认从 `skills/` 目录加载（可用 `config.json` 的 `skills_dir` 覆盖，相对路径基于项目根目录解析）
- 三层渐进披露：L1 描述注入系统提示词 → L2 按需加载 SKILL.md → L3 按需读取 references/scripts
- 运行时通过 `set_skills_dir()` 动态切换目录，无需重启

## 常用目录

- `sessions/` — 会话记录（运行时数据）
- `logs/` — 运行日志
- `static/` — 前端构建产物（git 忽略）

## 相关文档

- `ARCHITECTURE.md` — 架构说明
- `API.md` — API 接口文档
- `CLAUDE.md` / `AGENTS.md` — AI 协作行为准则