# Multi-agent 项目全量代码审核报告

**审核日期**: 2026-09-19  
**审核范围**: /home/sxkiss/bt（94 个 git-tracked 文件，约 17054 行 Python + 9500 行 Vue）  
**审核方式**: 只读静态分析 + 子代理并行审查，无文件修改

---

## 总体评价

> **这是一个完全无鉴权的 FastAPI 服务**。任何能到达 9876 端口的用户均可读取/写入完整配置（含 LLM API Key）、浏览文件系统、删除对话历史并触发 Agent 执行。在生产外网暴露场景下等同于裸奔。内部路径穿越防护存在、会话管理较为完善，但关键安全缺陷必须优先修复。

---

## 【严重 S】安全问题

### S-1 无任何鉴权中间件，所有 API 完全裸奔
- **文件**: `web_server.py:2416-2419`
- **证据**:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```
所有 50 个路由（/api/chat/*、/api/config、/api/files/*、/api/agent/run 等）均无鉴权。
- **建议**: 引入 `HTTPBearer` 或简单 API Key 头校验中间件；禁用 `allow_credentials=True` + `allow_origins=["*"]` 组合。

### S-2 `/api/config` 明文返回 LLM API Key
- **文件**: `web_server.py:521`、`config.json:10`
- **证据**:
```python
configs = {
    "daily_quota": {...},
    "config": self.config,  # ← 直接返回完整 config，含 api_key
    ...
}
return public.return_data(True, data=configs)
```
当前 `config.json` 中 `api_key` 为真实值（非占位符 `--`），GET /api/config 直接泄露。
- **建议**: `get_config()` 返回前对 `api_key`、`embedding_api_key`、`default_headers` 做掩码或剔除。

### S-3 POST /api/config 无输入校验可覆盖任意字段
- **文件**: `web_server.py:2512-2516`
- **证据**:
```python
@app.post("/api/config")
async def api_set_config(request: Request):
    params = await _params(request)
    config_str = params.get('config', '')
    return JSONResponse(agent_main.set_config(config_str))
```
`set_config` 内部仅 `json.loads` 后 merge，无任何字段白名单。攻击者可改 `api_base_url`、`workspace` 等。
- **建议**: 限制可修改字段为允许集合；敏感字段单独授权接口。

### S-4 API Key 明文写入用户全局 auth.json
- **文件**: `chat_client/opencode_config.py:129`
- **证据**:
```python
existing_auth["gateway"] = {"type": "api", "key": api_key}
with open(_GLOBAL_AUTH_PATH, "w", encoding="utf-8") as f:
    json.dump(existing_auth, f, indent=2)
```
每次 opencode 模式对话都覆盖 `~/.config/opencode/auth.json`，多用户并发可互相篡改。
- **建议**: 写入内存变量；若必须写盘，加进程 ID 标识或使用原子写入 + 文件锁。

### S-5 MCP 配置含硬编码 API Key
- **文件**: `mcp/config.json:106`
- **证据**:
```json
"xxtui": {
    "url": "https://mcp.xxtui.com",
    "type": "http",
    "headers": {
        "API-KEY": "0163b310f0969e41"   // ← 明文硬编码
    }
}
```
此 Key 随代码仓库暴露，任何人可得。
- **建议**: 改用环境变量注入；从不 track 含密钥的配置文件。

### S-6 前端 API Key 通过 URL 查询参数传输
- **文件**: `frontend/src/App.vue:1884-1885`、`SettingsDrawer.vue:618`
- **证据**:
```javascript
apiGet('/api/models?base_url=' + encodeURIComponent(base_url) + '&key=' + encodeURIComponent(api_key), ...)
```
完整 API Key 出现在 URL 中，服务端日志、浏览器历史、referrer header 均可泄露。
- **建议**: 改用 POST Body 或 HTTP Authorization Header 传递密钥。

### S-7 agents 目录存在 shell=True 命令注入风险
- **文件**: `agents/process_analyzer.py:14`、`agents/virus_scanner.py:12`
- **证据**:
```python
result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
result = subprocess.run(command, shell=True, capture_output=True, text=True, executable='/bin/bash')
```
若 `cmd`/`command` 包含用户可控内容（即使间接），可导致命令注入。
- **建议**: 改用 `shell=False` + 参数列表；避免 shell 解析。

---

## 【中等 M】架构与安全缺陷

### M-1 chat_events SSE 事件无上限，可能 OOM
- **文件**: `web_server.py:140`、`240`
- **证据**:
```python
self.events = []  # 无 maxlen，只追加
```
ChatJob.events 无限增长；长期挂起的任务（JOB_TTL=3600s）+ 大量 SSE 重连可撑爆内存。
- **建议**: 改用 `collections.deque(maxlen=N)` 或限制回放历史条数。

### M-2 _safe_resolve_serve 未用 realpath，符号链接可绕过
- **文件**: `web_server.py:2918-2922`
- **证据**:
```python
req = os.path.abspath(os.path.expanduser(requested))  # ← 未解析符号链接
for root in _SERVE_ROOTS:
    if req == root or req.startswith(root + os.sep):
        return req
```
- **建议**: 改用 `os.path.realpath()`，并在白名单校验后再 resolve。

### M-3 ast.literal_eval 无深度限制，可 DoS
- **文件**: `web_server.py:1036`
- **证据**:
```python
raw_tools = ast.literal_eval(raw_tools)  # 恶意嵌套结构可触发 RecursionError
```
- **建议**: 在调用外层设置 `sys.setrecursionlimit` 保护，或直接拒绝非预期类型。

### M-4 定时任务 scheduler 无二次确认
- **文件**: `chat_client/tools/scheduler.py`
- **证据**: ScheduleTask 工具直接创建任务，无权限校验或操作审计。
- **建议**: 增加调度请求的审计日志；高危操作要求二次确认。

### M-5 AI_AGENT_SKILL_ALLOW_PRIVATE 可绕过 SSRF 防护
- **文件**: `chat_client/skills.py:610-616`
- **证据**:
```python
allow_private = os.environ.get("AI_AGENT_SKILL_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes")
if not allow_private and self._is_private_host(host):
    return {"status": False, ...}
```
该环境变量一开，所有内网/本机地址拦截失效（含 AWS 元数据 `169.254.169.254`）。
- **建议**: 仅在隔离环境启用；安装后增加技能沙箱检查。

### M-6 SettingsDrawer.vue 缺少 onUnmounted 清理
- **文件**: `frontend/src/components/SettingsDrawer.vue`
- **证据**: 全文件无 `onUnmounted` import，未清理可能残留的 `setTimeout`/`setInterval`。
- **建议**: 补充组件卸载时的清理逻辑。

### M-7 前端链接 target="_blank" 缺少 rel="noopener"
- **文件**: `frontend/src/components/MessageItem.vue:40,231`
- **证据**:
```html
<a target="_blank" ...>  <!-- ← 无 rel="noopener" -->
```
可导致 tabnabbing 攻击（新标签页可操纵原窗口的 `window.opener`）。
- **建议**: 添加 `rel="noopener noreferrer"`。

### M-8 ZIP 炸弹压缩比检测被 compress_size=0 绕过
- **文件**: `chat_client/skills.py:441-443`
- **证据**:
```python
compress_size = zi.compress_size or 0
if compress_size > 0 and zi.file_size > 0 \
   and zi.file_size / compress_size > self._ZIP_BOMB_RATIO:
    return {"status": False, ...}  # ← compress_size=0 时直接跳过
```
恶意 ZIP 可将所有条目 `compress_size` 设为 0 绕过检测。
- **建议**: `compress_size == 0` 时记录告警日志，依赖 `total_size` 实时累计作为备用防线。

### M-9 大量裸 except Exception（~100+ 处）
- **文件**: 全局多处，`chat_client/tools/__init__.py` 有 12 处、`chat_client/agent.py` 有 12 处
- **证据**:
```python
except Exception:
    pass
```
生产环境 traceback 丢失，定位困难；部分错误（`ConnectionError`、`ImportError`）被静默吞掉。
- **建议**: 只捕获具体异常；至少保留 `logger.exception`。

### M-10 web_server.py codex_test_exec 可被重放探测
- **文件**: `web_server.py:1822-1836`
- **证据**: GET `/api/codex/test` 触发 `subprocess.run([bin_path, "--version"])`，无鉴权。
- **建议**: 缓存探针结果；对敏感探测接口加鉴权或 IP 白名单。

---

## 【低级 L】可维护性 & 小问题

### L-1 Token 估算精度低
- **文件**: `chat_client/agent.py:988-990`
- **证据**:
```python
def _estimate_tokens(text: str) -> int:
    return len(text) // 2  # 英文高估 4 倍，中文可能低估
```
- **建议**: 使用 `tiktoken` 库做精确估算。

### L-2 session_id 校验不统一
- **文件**: `web_server.py:2170` vs `1473-1474`、`1524-1525`
- **证据**: `del_chat` 有严格正则校验，但 `chat_events`、`chat_status`、`chat_stop` 无统一校验。
- **建议**: 封装为公共函数 `_validate_session_id()`，所有入口统一调用。

### L-3 _map_agent_chunk 未知 type 直接透传
- **文件**: `web_server.py:128`
- **证据**:
```python
return [(str(t or "message"), chunk)]  # 非标准 event 名污染客户端
```
- **建议**: 未知 type 统一映射到 `"error"` 或 `"message"`。

### L-4 异常信息含服务器内部路径泄露
- **文件**: `web_server.py:2944`、`2988`、`3031`
- **证据**:
```python
return JSONResponse(public.returnMsg(False, f"读取文件失败: {e!s}"))
```
- **建议**: 对外只返回通用提示，详细 trace 保留在 server log。

### L-5 daemon 线程优雅退出缺失
- **文件**: `web_server.py:1300-1305` 等
- **证据**: 所有后台线程使用 `daemon=True`，uvicorn shutdown 时强制终止，agent close 不执行。
- **建议**: 注册 `atexit` 或 uvicorn shutdown handler 做优雅清理。

### L-6 MCP command 参数无白名单校验
- **文件**: `chat_client/mcp_manager.py:150-164`
- **证据**: `add_server()` 接受任意 dict，`command` 字段无校验。
- **建议**: 对 `command` 做白名单（只允许已知 MCP 服务器路径）。

### L-7 claude_bridge.ts 孤立表达式（伪报，已验证 OK）
- **文件**: `chat_client/claude_bridge.py:563`
- **注**: 子代理报告的 `ts,` 语法错误为误报；实际 `sed -n '555,570p'` 显示该行为合法 dict 字面量 `"timestamp": ts,`；Python AST 解析通过。**不影响运行**。

---

## 统计摘要

| 类别 | 数量 |
|------|------|
| 严重（S） | 7 |
| 中等（M） | 10 |
| 低级（L） | 7 |
| **合计** | **24** |
| Python 源文件 | 46 个 |
| 总 Python 行数 | ~5,952 行（tools 层）+ 3,075 行（web_server） |
| Vue 源文件 | 7 个 |
| 总 Vue 行数 | ~9,500 行 |
| 裸 except 总数 | ~100+ |
| shell=True 使用点 | 2 处（agents 目录） |

---

## 修复优先级建议

| 优先级 | 项目 |
|--------|------|
| **P0（立即）** | S-1 加鉴权中间件；S-2 脱敏 /api/config 响应；S-5 移除硬编码 MCP Key |
| **P1（本周）** | S-3 POST /api/config 加字段白名单；S-6 前端改用 Header 传 Key；S-4 API Key 不落盘；S-7 改 shell=False |
| **P2（本月）** | M-1 SSE 事件队列上限；M-2 改用 realpath；M-5 评估环境变量风险；M-6 补充组件清理 |
| **P3（后续）** | L-1 tiktoken 替换；L-2 session_id 校验统一；L-4 异常信息脱敏；L-5 优雅退出 |

---

*报告生成于 2026-09-19，基于只读静态分析，所有行号引用自 /home/sxkiss/bt 当前工作树。*
