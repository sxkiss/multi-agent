"""AgentMain CLI 配置职责（opencode / claude / codex）。

优-1 第十三阶段：从 web_server.py 拆出的 CLI 配置读写与状态检测。
chat_jobs 通过 configure 注入（避免循环导入）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat as _stat
import subprocess

import public

logger = logging.getLogger(__name__)

_chat_jobs = None


def configure(chat_jobs) -> None:
    global _chat_jobs
    _chat_jobs = chat_jobs


class CliConfigMixin:
    def opencode_get_config(self, get=None):
        """获取 opencode 模式配置：内置模板 + 自定义模板"""
        try:
            from chat_client.opencode_templates import OPENCODE_TEMPLATES
        except Exception:
            OPENCODE_TEMPLATES = {}
        oc = self.config.get('opencode', {})
        return public.return_data(True, data={
            "opencode": oc,
            "builtin_templates": list(OPENCODE_TEMPLATES.keys()),
        })

    def opencode_save_config(self, get):
        """保存 opencode 模式配置到 config.json"""
        new_oc = get.get('opencode')
        if not isinstance(new_oc, dict):
            new_oc = get  # 兼容：请求体直接就是 opencode 内容
        if not isinstance(new_oc, dict):
            return public.returnMsg(False, "参数格式错误")
        self.config['opencode'] = new_oc
        path = os.path.join(self.plugin_path, 'config.json')
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("接口异常已脱敏对外返回", exc_info=True)
            return public.returnMsg(False, '保存配置失败')
        return public.return_data(True, data=self.config['opencode'])

    def opencode_status(self, get):
        """查询 opencode 模式整体状态：可用性 + 运行中的 opencode 任务"""
        import shutil as _shutil
        from chat_client.opencode_config import _PROJECT_ROOT
        from chat_client.opencode_bridge import OpenCodeProcess
        bin_path = OpenCodeProcess._opencode_path()
        available = False
        if os.path.isabs(bin_path):
            available = os.path.exists(bin_path)
        else:
            available = bool(_shutil.which(bin_path))
        # 运行中的 opencode 任务
        running = []
        for k, j in _chat_jobs._jobs.items():
            if j.is_running and isinstance(getattr(j, 'agent', None), OpenCodeProcess):
                running.append(k)
        return public.return_data(True, data={
            "available": available,
            "binary": bin_path,
            "running_jobs": running,
        })

    def opencode_cli_get_config(self, get=None):
        """获取 opencode CLI 全局配置 (~/.config/opencode/opencode.json) 的 gateway provider"""
        import shutil as _shutil
        from chat_client.opencode_config import _GLOBAL_OC_PATH, _GLOBAL_AUTH_PATH
        oc = {}
        try:
            with open(_GLOBAL_OC_PATH, 'r', encoding='utf-8') as f:
                oc = json.load(f)
        except Exception as e:
            logger.warning("读取 opencode.json 失败: %s", e)
        gateway = (oc.get('provider') or {}).get('gateway', {})
        options = gateway.get('options', {})
        models = gateway.get('models', {})
        # 获取第一个 model 的 reasoningEffort
        reasoning_effort = 'max'
        for mname, mconf in models.items():
            reasoning_effort = mconf.get('options', {}).get('reasoningEffort', 'max')
            break
        oc_bin = _shutil.which("opencode") or os.path.expanduser("~/.local/nodejs/node-latest/bin/opencode")
        return public.return_data(True, data={
            "base_url": options.get('baseURL', ''),
            "api_key": options.get('apiKey', ''),
            "model": list(models.keys())[0] if models else '',
            "reasoning_effort": reasoning_effort,
            "opencode_binary": oc_bin,
            "config_path": _GLOBAL_OC_PATH,
        })

    def opencode_cli_save_config(self, get):
        """保存 opencode CLI 全局配置 gateway provider"""
        from chat_client.opencode_config import _GLOBAL_OC_PATH, _GLOBAL_AUTH_PATH
        import stat as _stat
        base_url = str(get.get('base_url', '')).strip()
        api_key = str(get.get('api_key', '')).strip()
        model = str(get.get('model', 'auto')).strip() or 'auto'
        reasoning_effort = str(get.get('reasoning_effort', 'max')).strip().lower() or 'max'
        if not base_url:
            return public.returnMsg(False, "base_url 不能为空")
        try:
            existing = {}
            if os.path.exists(_GLOBAL_OC_PATH):
                with open(_GLOBAL_OC_PATH, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            # 只覆盖 gateway provider
            existing.setdefault('provider', {})['gateway'] = {
                "name": "gateway",
                "type": "openai",
                "options": {"baseURL": base_url, "apiKey": api_key},
                "models": {
                    model: {
                        "name": model,
                        "reasoning": True,
                        "options": {"reasoningEffort": reasoning_effort},
                        "limit": {"context": 262144, "output": 32000},
                    }
                },
            }
            os.makedirs(os.path.dirname(_GLOBAL_OC_PATH), exist_ok=True)
            with open(_GLOBAL_OC_PATH, 'w', encoding='utf-8') as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            # auth.json
            existing_auth = {}
            try:
                with open(_GLOBAL_AUTH_PATH, 'r', encoding='utf-8') as f:
                    existing_auth = json.load(f)
            except Exception:
                pass
            existing_auth["gateway"] = {"type": "api", "key": api_key}
            with open(_GLOBAL_AUTH_PATH, 'w', encoding='utf-8') as f:
                json.dump(existing_auth, f, indent=2)
            os.chmod(_GLOBAL_AUTH_PATH, _stat.S_IRUSR | _stat.S_IWUSR)
            return public.return_data(True, data={"saved": True})
        except Exception as e:
            logger.warning("接口异常已脱敏对外返回", exc_info=True)
            return public.returnMsg(False, '保存失败')

    def claude_get_config(self, get=None):
        """获取 Claude CLI 配置：effortLevel + env 环境变量"""
        import shutil as _shutil
        claude_dir = os.path.expanduser("~/.claude")
        settings_path = os.path.join(claude_dir, "settings.json")
        settings = {}
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)
        except Exception as e:
            logger.warning("读取 Claude 配置失败: %s", e)
        # 查找 claude 二进制路径
        claude_bin = _shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        return public.return_data(True, data={
            "effortLevel": settings.get("effortLevel", "high"),
            "env": settings.get("env", {}),
            "claude_binary": claude_bin,
            "settings_path": settings_path,
        })

    def claude_save_config(self, get):
        """保存 Claude CLI 配置到 ~/.claude/settings.json"""
        effort_level = str(get.get('effortLevel', 'high')).strip().lower()
        if effort_level not in ('low', 'medium', 'high', 'max'):
            return public.returnMsg(False, "effortLevel 必须是 low/medium/high/max 之一")
        env = get.get('env', {})
        if isinstance(env, str):
            try:
                env = json.loads(env)
            except Exception:
                return public.returnMsg(False, "env 格式错误")
        if not isinstance(env, dict):
            return public.returnMsg(False, "env 必须是 JSON 对象")

        settings_path = os.path.expanduser("~/.claude/settings.json")
        try:
            existing = {}
            if os.path.exists(settings_path):
                with open(settings_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            existing["effortLevel"] = effort_level
            existing["env"] = {**existing.get("env", {}), **env}
            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            return public.return_data(True, data=existing)
        except Exception as e:
            logger.warning("接口异常已脱敏对外返回", exc_info=True)
            return public.returnMsg(False, '保存失败')

    # ── codex 管理面板：~/.codex/config.toml 读写 + 状态探测 ────────────────
    _CODEX_CFG_PATH = os.path.expanduser("~/.codex/config.toml")

    def codex_cli_get_config(self, get=None):
        """读取 ~/.codex/config.toml（简化 TOML 解析）+ codex 二进制状态"""
        import shutil as _shutil
        from chat_client.codex_bridge import _codex_path
        bin_path = _codex_path()
        available = bool(bin_path) and (os.path.exists(bin_path) if os.path.isabs(bin_path) else bool(_shutil.which(bin_path)))
        cfg = {"model": "auto", "reasoning_effort": "max", "base_url": "", "api_key": "", "config_path": self._CODEX_CFG_PATH}
        try:
            if os.path.exists(self._CODEX_CFG_PATH):
                with open(self._CODEX_CFG_PATH, 'r', encoding='utf-8') as f:
                    txt = f.read()
                import re as _re
                for m in _re.finditer(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|([^#\n]+))', txt, _re.MULTILINE):
                    k, v_quoted, v_raw = m.group(1), m.group(2), m.group(3)
                    v = v_quoted if v_quoted is not None else (v_raw or '').strip()
                    if k == "model":
                        cfg["model"] = v
                    elif k == "model_reasoning_effort":
                        cfg["reasoning_effort"] = v
                    elif k == "base_url":
                        cfg["base_url"] = v
                    elif k == "api_key":
                        cfg["api_key"] = v
                env_m = _re.search(r'\[env\](.*?)(?=\n\[|$)', txt, _re.DOTALL)
                if env_m:
                    for m in _re.finditer(r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*"([^"]*)"', env_m.group(1), _re.MULTILINE):
                        if m.group(1) == "OPENAI_BASE_URL" and not cfg["base_url"]:
                            cfg["base_url"] = m.group(2)
                        elif m.group(1) == "OPENAI_API_KEY" and not cfg["api_key"]:
                            cfg["api_key"] = m.group(2)
        except Exception as e:
            logger.warning("读取 codex config.toml 失败: %s", e)
        cfg["binary"] = bin_path
        cfg["available"] = bool(available)
        return public.return_data(True, data=cfg)

    def codex_cli_save_config(self, get):
        """写入 ~/.codex/config.toml"""
        model = str(get.get('model', 'auto')).strip() or 'auto'
        effort = str(get.get('reasoning_effort', 'max')).strip().lower() or 'max'
        if effort not in ('low', 'medium', 'high', 'xhigh', 'max'):
            return public.returnMsg(False, "reasoning_effort 非法")
        base_url = str(get.get('base_url', '')).strip()
        api_key = str(get.get('api_key', '')).strip()
        try:
            bak = ""
            if os.path.exists(self._CODEX_CFG_PATH):
                import shutil as _shutil
                bak = self._CODEX_CFG_PATH + f".bak.{int(time.time())}"
                _shutil.copy2(self._CODEX_CFG_PATH, bak)
            os.makedirs(os.path.dirname(self._CODEX_CFG_PATH), exist_ok=True)
            body = (
                f'model = "{model}"\n'
                f'model_reasoning_effort = "{effort}"\n\n'
                f'[env]\n'
                f'OPENAI_BASE_URL = "{base_url}"\n'
                f'OPENAI_API_KEY = "{api_key}"\n'
            )
            with open(self._CODEX_CFG_PATH, 'w', encoding='utf-8') as f:
                f.write(body)
            import stat as _stat
            os.chmod(self._CODEX_CFG_PATH, _stat.S_IRUSR | _stat.S_IWUSR)
            return public.return_data(True, data={"saved": True, "backup": bak})
        except Exception as e:
            logger.warning("接口异常已脱敏对外返回", exc_info=True)
            return public.returnMsg(False, '保存失败')

    def codex_get_status(self, get=None):
        """探测 codex 二进制状态"""
        import shutil as _shutil
        from chat_client.codex_bridge import _codex_path
        bin_path = _codex_path()
        available = False
        if bin_path and os.path.isabs(bin_path) and os.path.exists(bin_path):
            available = os.access(bin_path, os.X_OK)
        elif bin_path and _shutil.which(bin_path):
            available = True
        return public.return_data(True, data={"binary": bin_path, "available": bool(available)})

    def codex_test_exec(self, get=None):
        """跑 codex --version 验证二进制可用"""
        import shutil as _shutil
        from chat_client.codex_bridge import _codex_path
        bin_path = _codex_path()
        if not bin_path:
            return public.returnMsg(False, "未定位到 codex 二进制")
        try:
            proc = subprocess.run(
                [bin_path, "--version"], capture_output=True, text=True, timeout=10
            )
            ver = (proc.stdout or proc.stderr or "").strip().splitlines()[0] if (proc.stdout or proc.stderr) else ""
            return public.return_data(True, data={"version": ver, "returncode": proc.returncode})
        except Exception as e:
            logger.warning("接口异常已脱敏对外返回", exc_info=True)
            return public.returnMsg(False, 'codex 测试失败')
