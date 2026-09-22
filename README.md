# Multi-agent

通用型多智能体协作平台（Group Mode），基于开源 AI 助手改造的独立 Web 服务，提供「一人一集团」对话、技能（Skills）系统、内置工具链（终端/文件/检索/任务）与 SSE 流式输出。

> **一句话**：你只负责下目标，剩下的交给集团 —— Boss 对经理说话，经理拆解计划、分派部门，一切自动推进。

## 介绍视频

77 秒讲清这个平台是什么、怎么运转的（1920×1080 · 中文配音 · 硬字幕）：

https://github.com/sxkiss/multi-agent/blob/main/docs/media/intro.mp4

<details>
<summary>视频内容结构</summary>

| 时间 | 章节 | 要点 |
|------|------|------|
| 0:00 | 开场钩子 | 一个人能不能指挥一整个团队 |
| 0:09 | 组织架构 | Boss → 经理 → 6 个部门 → 成员，缺人现招、组织自我扩张 |
| 0:29 | 技术栈 | Python 3.12 + FastAPI / Vue3 + Vite / SSE 流式 / systemd |
| 0:41 | 技能系统 | 283 个技能，三层渐进披露，不浪费上下文 |
| 0:51 | 工具与安全 | 142 个工具，命令拦截 / 风险确认 / 白名单 / 审计留痕 |
| 1:05 | 扩展能力 | MCP 接入 / 定时任务自动开工 / 子代理并行派单 |
| 1:11 | 收尾 | 开源地址与一句话主张 |

配套资源：

- `docs/media/intro.mp4` — 成片（77s）
- `docs/media/intro.srt` — 字幕（上传平台时作为可检索文本）
- `docs/media/cover_169.png` — 16:9 封面（信息流 / 播放页）
- `docs/media/cover_34.png` — 3:4 封面（主页栅格，独立排版防切字）

</details>

## 技术栈

| 部分 | 技术 |
|------|------|
| 后端 | Python 3.12 · FastAPI · Uvicorn · openai SDK 3.x |
| 前端 | Vue 3 · Vite 5 · marked |
| 部署 | systemd（bt-agent.service） |

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
sudo systemctl daemon-reload && sudo systemctl restart bt-agent.service
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