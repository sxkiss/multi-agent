"""
opencode.json 生成器：读取本服务 config.json 网关配置，生成 opencode 可识别的 provider/MCP/agent 格式。

架构：
  - 直接使用用户全局 ~/.config/opencode/opencode.json
  - 写入时只覆盖 gateway provider，保留用户已有的 MCP、其他 provider 等配置
  - XDG_DATA_HOME 不设置 → opencode.db 写入全局 ~/.local/share/opencode/opencode.db
  - CWD = 用户选择的 workspace 目录 → session.directory 归属正确
"""

import json
import os
import stat

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GLOBAL_OC_PATH = os.path.expanduser("~/.config/opencode/opencode.json")
_GLOBAL_AUTH_PATH = os.path.expanduser("~/.config/opencode/auth.json")


def _build_gateway_config() -> dict:
    cfg_path = os.path.join(_PROJECT_ROOT, "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_model(cfg: dict) -> str:
    m = cfg.get("default_model", "auto")
    if not m and cfg.get("models"):
        m = cfg["models"][0]
    return m or "auto"


def _load_global_oc() -> dict:
    """加载用户现有全局 opencode.json，缺失则返回空 dict"""
    try:
        with open(_GLOBAL_OC_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def generate_opencode_config(
    session_id: str,
    config_dir: str,   # 保留参数签名兼容，实际不使用
    workspace: str = "",
    system_prompt: str | None = None,
    reasoning_effort: str = "max",
) -> dict:
    """
    将 gateway provider 合并写入用户全局 ~/.config/opencode/opencode.json。
    仅覆盖 gateway provider 和 agent.default.prompt，保留用户现有 MCP 和其他 provider。
    auth.json 同步更新（只写 gateway key）。

    Returns: {"opencode_json": path, "auth_path": path}
    """
    cfg = _build_gateway_config()
    model = _resolve_model(cfg)
    base_url = cfg.get("api_base_url", "")
    api_key = cfg.get("api_key", "")

    os.makedirs(os.path.dirname(_GLOBAL_OC_PATH), exist_ok=True)

    # ── 提示词 ──────────────────────────────────────────────────────────────
    prompt = system_prompt or cfg.get("system_prompt", "")
    if not system_prompt:
        try:
            from chat_client.agents_md import agents_md_manager
            md = agents_md_manager.load(_PROJECT_ROOT, load_global=False)
            if md.strip():
                prompt = md
        except Exception:
            pass
    prompt = (prompt or "").strip() + "\n\n【重要】完成用户的当前请求后立即停止回复，不要主动继续执行下一步，除非用户明确要求。"

    # ── 加载现有全局配置，只覆盖 gateway provider ───────────────────────
    existing = _load_global_oc()
    existing.setdefault("provider", {})["gateway"] = {
        "name": "gateway",
        "type": "openai",
        "options": {
            "baseURL": base_url,
            "apiKey": api_key,
        },
        "models": {
            model: {
                "name": model,
                "reasoning": True,  # 启用 thinking，思考内容通过单独 part 发送
                "options": {
                    "reasoningEffort": {
                        "max": "max", "high": "high",
                        "medium": "medium", "low": "low", "off": "none",
                    }.get(reasoning_effort.lower(), "max"),
                },
                "limit": {
                    "context": cfg.get("context_window_kb", 256) * 1024,
                    "output": 32000,
                },
            }
        },
    }
    # agent.default.prompt 始终覆盖（这是本服务注入的系统提示词）
    existing.setdefault("agent", {})["default"] = {"prompt": prompt, "mode": "primary"}
    # 注意：不改写全局 "model" 默认值——前端发请求时显式指定 providerID/modelID，
    # 用户自己的 opencode CLI 默认模型不应被本配置劫持

    # headless 会话需要自动放行权限，否则 opencode 会等待授权导致任务挂起
    existing.setdefault("permission", {}).update({
        "doom_loop": "allow",
        "external_directory": {"*": "allow"},
        "read": {"*.env": "allow", "*.env.*": "allow"},
    })
    existing["snapshot"] = False

    with open(_GLOBAL_OC_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    # ── auth.json：只写 gateway key（不覆盖其他 provider 的认证）──────────
    existing_auth: dict = {}
    try:
        with open(_GLOBAL_AUTH_PATH, "r", encoding="utf-8") as f:
            existing_auth = json.load(f)
    except Exception:
        pass
    existing_auth["gateway"] = {"type": "api", "key": api_key}
    with open(_GLOBAL_AUTH_PATH, "w", encoding="utf-8") as f:
        json.dump(existing_auth, f, indent=2)
    os.chmod(_GLOBAL_AUTH_PATH, stat.S_IRUSR | stat.S_IWUSR)

    return {
        "opencode_json": _GLOBAL_OC_PATH,
        "auth_path": _GLOBAL_AUTH_PATH,
    }
