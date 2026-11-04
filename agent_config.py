"""AgentMain 配置管理职责。

优-1 第十一阶段：从 web_server.py 的 AgentMain 拆出的配置读写、
脱敏、模型列表与搜索配置；通过 mixin 复用 self.config 等既有属性。
"""
from __future__ import annotations

import json
import logging
import os
import random

import public

logger = logging.getLogger(__name__)


class ConfigMixin:
    def _merge_config(self, base, update):
        for k, v in update.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._merge_config(base[k], v)
            else:
                base[k] = v

    def _load_config(self):
        if not os.path.exists(self.config_path):
            return {}
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        try:
            # P1-3: 原子写——并发 POST /api/config 时先写 tmp 再替换，避免 JSON 损坏
            tmp = self.config_path + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            os.replace(tmp, self.config_path)
            return True, '保存成功'
        except Exception as e:
            return False, f'保存配置失败: {e!s}'

    def _load_agents_config(self):
        agents_config_file = os.path.join(self.plugin_path, 'agents.json')
        if not os.path.exists(agents_config_file):
            return None, '缺少配置文件 agents.json'
        try:
            with open(agents_config_file, 'r', encoding='utf-8') as f:
                return json.load(f), None
        except Exception as e:
            return None, f'加载配置失败 agents.json: {e!s}'

    def get_config(self):
        data = {}
        # In standalone mode, skip remote usage call
        api_key = self.config.get('api_key', '')
        if api_key and api_key != self.DEFAULT_CONFIG['api_key']:
            # Custom API configured, skip usage check
            pass
        else:
            # Using default key - in standalone mode, don't call remote usage API
            # Just return empty quota data
            pass

        questions = [
            {"question": "查看服务器资源使用情况", "tools": ["get_system_resources"]},
            {"question": "查询服务器IP地址", "tools": ["get_server_ip"]},
            {"question": "CPU、磁盘负载过高怎么办?", "tools": ["get_system_resources", "get_top_processes"]},
            {"question": "检查Docker运行状态", "tools": ["get_docker_info", "get_docker_containers"]},
            {"question": "Nginx服务无法启动", "tools": ["get_service_status"]},
        ]
        questions = random.sample(questions, min(5, len(questions)))

        configs = {
            "daily_quota": {
                "used": data.get("used", 0),
                "total": data.get("limit", 0),
                "reset_time": "独立模式",
                "activate": data.get("activate", 50),
            },
            "config": self._redacted_config(),
            "is_custom_api": bool(api_key and api_key != self.DEFAULT_CONFIG['api_key']),
            "questions": questions,
        }
        return public.return_data(True, data=configs)

    # 这些字段属于凭据，一律不回传原文；只回 "<field>_set" 布尔，
    # 与 get_search_config 的既有约定保持一致。
    _SECRET_KEYS = {"api_key", "embedding_api_key"}
    _PLACEHOLDER = "--"  # 与 DEFAULT_CONFIG 中的占位约定一致

    def _redacted_config(self) -> dict:
        """深拷贝配置并将凭据字段清空 + _set 布尔（与 search 配置同一约定）。

        目的：GET /api/config 原先明文返回 api_key，任何人访问端口即可
        拿到模型密钥。改为脱敏后，前端仍能判断"是否已配置"（_set 布尔），
        但永远拿不到任何原文片段；留空提交=不修改，由后端保留原值。
        """
        cfg = json.loads(json.dumps(self.config))
        default = self.DEFAULT_CONFIG or {}
        for key in self._SECRET_KEYS:
            if key not in cfg:
                continue
            raw = cfg.get(key) or ""
            # 先依据原文算 _set，再清空原文（顺序不可颠倒）
            cfg[f"{key}_set"] = bool(raw) and str(raw) != str(default.get(key, ""))
            cfg[key] = ""
        # embedding 为嵌套结构，单独处理
        emb = cfg.get("embedding")
        if isinstance(emb, dict) and "embedding_api_key" in emb:
            raw_emb = emb.get("embedding_api_key") or ""
            emb_default = (default.get("embedding") or {}).get("embedding_api_key", "")
            emb["embedding_api_key_set"] = bool(raw_emb) and str(raw_emb) != str(emb_default)
            emb["embedding_api_key"] = ""
        # mm_api 为嵌套结构，单独处理
        mm = cfg.get("mm_api")
        if isinstance(mm, dict) and "api_key" in mm:
            raw_mm = mm.get("api_key") or ""
            mm_default = (default.get("mm_api") or {}).get("api_key", "")
            mm["api_key_set"] = bool(raw_mm) and str(raw_mm) != str(mm_default)
            mm["api_key"] = ""
        return cfg

    def get_models(self, base_url='', key=''):
        """获取模型列表。

        key 为空或为脱敏占位符时，回退服务端已存凭据；为防止凭据被诱导
        发往任意地址，回退仅在 base_url 为空或与已存 api_base_url 一致时生效。
        """
        import openai
        saved_base = str(self.config.get('api_base_url') or '').strip()
        saved_key = str(self.config.get('api_key') or '').strip()
        effective_base = str(base_url or '').strip()
        effective_key = str(key or '').strip()
        if not effective_key or effective_key == self._PLACEHOLDER:
            if not saved_key or saved_key == self._PLACEHOLDER:
                return public.returnMsg(False, '缺少参数 key')
            if effective_base and effective_base.rstrip('/') != saved_base.rstrip('/'):
                # 目标地址与已配置地址不符时拒绝回退，避免真实 key 被发往任意地址
                return public.returnMsg(False, '目标地址与已配置地址不一致，需提供对应的 API Key')
            effective_key = saved_key
        if not effective_base:
            effective_base = saved_base
        if not effective_base:
            return public.returnMsg(False, '缺少参数 base_url')
        client = openai.OpenAI(api_key=effective_key, base_url=effective_base, default_headers=self.config['default_headers'])
        try:
            response = client.models.list()
            model_names = [model.id for model in response.data]
            return public.return_data(True, data=model_names)
        except Exception:
            return public.return_data(True, data=[])

    def set_config(self, config_str=''):
        if not config_str:
            return public.returnMsg(False, '缺少配置参数 config')
        try:
            user_config = json.loads(config_str)
        except Exception:
            return public.returnMsg(False, '配置参数格式错误')
        # 从当前用户配置深拷贝，保留已有字段不被 DEFAULT_CONFIG 覆盖
        new_config = json.loads(json.dumps(self.config))
        self._merge_config_with_rules(new_config, user_config)
        self._merge_config_with_rules(self.config, user_config)
        self.config = new_config
        status, msg = self._save_config()
        if status:
            return public.returnMsg(True, '设置成功')
        else:
            return public.returnMsg(False, msg)

    # ---- 搜索配置（独立于主 config，避免 api_key 混入 GET /api/config 的明文返回）----

    # 这些 provider 字段属于凭据，返回前端时一律不返回任何原文片段
    _SEARCH_SECRET_KEYS = {"api_key"}

    def get_search_config(self):
        """返回搜索配置。api_key 只回"<name>_set"布尔，不含任何原文片段。"""
        try:
            from chat_client.tools.websearch import PROVIDERS
        except Exception:
            PROVIDERS = {}
        raw = (self.config.get("search") or {})
        providers = {}
        for name, meta in PROVIDERS.items():
            opts = (raw.get("providers") or {}).get(name) or {}
            safe = {}
            for k, v in opts.items():
                # 凭据字段：只告知"是否已配置"，输入框永远为空（留空=不修改）
                if k in self._SEARCH_SECRET_KEYS:
                    safe[k] = ""                      # 不回任何原文片段
                    safe[k + "_set"] = bool(v)        # 是否已配置
                else:
                    safe[k] = v
            providers[name] = {
                "label": meta.get("label", name),
                "need_key": meta.get("need_key", False),
                "options": safe,
            }
        return public.return_data(True, data={
            "provider": raw.get("provider", "bing"),
            "result_size": raw.get("result_size", 10),
            "providers": providers,
        })

    def set_search_config(self, config_str=''):
        """保存搜索配置。

        约定：secret 字段留空（或为 null）表示"不修改"，保留原值；
        只有填了非空新值才覆盖。这样天然支持 key 里含 '*' 的情况——
        旧实现用 "含 * 就跳过" 猜测是否为脱敏值，会把含 * 的真实 key 误吞。
        """
        try:
            update = json.loads(config_str)
        except Exception:
            return public.returnMsg(False, '搜索配置格式错误')

        cur = self.config.get("search")
        if not isinstance(cur, dict):
            cur = {}
        cur.setdefault("providers", {})

        if isinstance(update.get("provider"), str) and update["provider"]:
            cur["provider"] = update["provider"]
        if update.get("result_size"):
            try:
                cur["result_size"] = max(1, min(int(update["result_size"]), 20))
            except (TypeError, ValueError):
                pass

        for name, payload in (update.get("providers") or {}).items():
            if not isinstance(payload, dict):
                continue
            opts = payload.get("options")
            if not isinstance(opts, dict):
                continue
            target = cur["providers"].setdefault(name, {})
            for k, v in opts.items():
                # 忽略前端回传的辅助字段（api_key_set / api_key_hint）
                if k.endswith("_set") or k.endswith("_hint"):
                    continue
                if k in self._SEARCH_SECRET_KEYS:
                    # 空/None = 不修改，保留原值；非空才覆盖（key 可含 '*'）
                    if v is None or (isinstance(v, str) and not v.strip()):
                        continue
                target[k] = v

        self.config["search"] = cur
        status, msg = self._save_config()
        if status:
            return public.returnMsg(True, '搜索设置已保存')
        return public.returnMsg(False, msg)

    def _merge_config_with_rules(self, base, update):
        for k, v in update.items():
            # 前端辅助字段（api_key_set / api_key_hint 等）仅用于展示，不落库，
            # 防止 GET 脱敏结果被原样回传时污染 config.json
            if isinstance(k, str) and (k.endswith("_set") or k.endswith("_hint")):
                continue
            if isinstance(v, str):
                v = v.strip()
            # 前端保存时可能把 GET 返回的脱敏占位符原样回传；
            # 若当作有效值写入，会覆盖真实凭据。此处视为"未修改"跳过。
            if isinstance(v, str) and v == self._PLACEHOLDER and k in self._SECRET_KEYS:
                continue
            is_empty = (v is None) or (v == "") or (isinstance(v, list) and len(v) == 0)
            if is_empty:
                # 这些字段允许显式清空（运行时回退默认值，如 workspace → 项目根目录）
                if k in self.CLEARABLE_CONFIG_KEYS:
                    base[k] = [] if isinstance(v, list) else ""
                continue
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._merge_config_with_rules(base[k], v)
            else:
                base[k] = v
