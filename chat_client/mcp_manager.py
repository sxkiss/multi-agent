"""
MCP Server 配置与管理（对齐 SkillManager 范式）。

- 读写项目 mcp/config.json 的 mcpServers 配置（增删查，不触碰现有生产配置）
- 市场清单拉取：GitHub 官方 MCP 市场 https://api.mcp.github.com/v0/servers
  （VSCode 内置 mcpGallery.serviceUrl = https://api.mcp.github.com）
  每个 server 包含两类启动方式：
  - packages[]: stdio 本地可执行（npx/uvx/docker），可直接生成 MCP config
  - remotes[]: 远程 HTTP/SSE 服务，生成 {"type":"http","url":"..."} 配置
- 本地缓存：60min TTL，强校验防错误数据落盘（同 SkillManager 思路）
"""
import hashlib
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# 项目根目录
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# MCP 配置文件路径（项目 mcp/config.json）
MCP_CONFIG_FILE = os.path.join(_PROJECT_ROOT, "mcp", "config.json")

# 市场源：GitHub 官方 MCP 市场（VSCode 内置 mcpGallery.serviceUrl）
_DEFAULT_MARKET_URL = "https://api.mcp.github.com/v0/servers"

# 市场清单缓存
MCP_MARKET_CACHE_FILE = os.path.join(_PROJECT_ROOT, "mcp_market_cache.json")
MCP_MARKET_CACHE_TTL = 60 * 60  # 60 分钟


class McpManager:
    """MCP 配置管理单例（读写 mcp/config.json + 市场清单）"""

    def __init__(self):
        self._config: dict | None = None
        self._config_mtime: float = 0.0
        self._market_urls = [_DEFAULT_MARKET_URL]

    # ---- 配置读写 ----

    @property
    def config_path(self) -> str:
        return MCP_CONFIG_FILE

    def _load_config(self) -> dict:
        """读取 mcp/config.json，带 mtime 缓存"""
        try:
            mtime = os.path.getmtime(MCP_CONFIG_FILE)
        except OSError:
            return {"mcpServers": {}, "imports": []}
        if self._config and mtime == self._config_mtime:
            return self._config
        try:
            with open(MCP_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            logger.warning("[McpManager] 读取 mcp/config.json 失败", exc_info=True)
            return {"mcpServers": {}, "imports": []}
        if not isinstance(data, dict):
            data = {"mcpServers": {}, "imports": []}
        data.setdefault("mcpServers", {})
        data.setdefault("imports", [])
        self._config = data
        self._config_mtime = mtime
        return data

    def _save_config(self, data: dict) -> bool:
        """原子写入 mcp/config.json（保留原文件末尾换行与权限，避免无意义 diff）"""
        tmp = MCP_CONFIG_FILE + ".tmp"
        try:
            os.makedirs(os.path.dirname(MCP_CONFIG_FILE), exist_ok=True)
            # 保留原文件末尾换行（json.dump 不写尾部换行，会造成无意义 diff）
            trailing_newline = False
            try:
                with open(MCP_CONFIG_FILE, "rb") as f:
                    trailing_newline = f.read().endswith(b"\n")
            except OSError:
                pass
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                if trailing_newline:
                    f.write("\n")
            # 保留原文件权限位（若存在）
            try:
                st = os.stat(MCP_CONFIG_FILE)
                os.chmod(tmp, st.st_mode)
            except OSError:
                pass
            os.replace(tmp, MCP_CONFIG_FILE)
            self._config = data
            self._config_mtime = os.path.getmtime(MCP_CONFIG_FILE)
            return True
        except Exception:
            logger.warning("[McpManager] 写入 mcp/config.json 失败", exc_info=True)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    # ---- 市场源配置 ----

    def set_market_config(self, repos: list[dict] | None) -> None:
        """注入市场源 URL 列表（config.json -> mcp_market）；无则用默认 GitHub 官方市场"""
        if not repos:
            self._market_urls = [_DEFAULT_MARKET_URL]
            return
        cleaned = []
        for r in repos:
            if isinstance(r, str):
                r = {"url": r}
            if not isinstance(r, dict):
                continue
            if not r.get("enabled", True):
                continue
            url = str(r.get("url", "")).strip()
            if url.startswith("http://") or url.startswith("https://"):
                cleaned.append(url)
        self._market_urls = cleaned or [_DEFAULT_MARKET_URL]
        logger.info("[McpManager] 市场源配置为 %d 个 URL", len(self._market_urls))

    @property
    def market_urls(self) -> list[str]:
        return list(getattr(self, "_market_urls", None) or [_DEFAULT_MARKET_URL])

    # ---- 查/增/删 MCP server 配置 ----

    def list_servers(self) -> dict:
        """列出 mcp/config.json 中已配置的 servers"""
        data = self._load_config()
        servers = data.get("mcpServers", {})
        items = []
        for name, conf in servers.items():
            if not isinstance(conf, dict):
                continue
            server_type = conf.get("type", "stdio" if "command" in conf else "http")
            summary = {
                "name": name,
                "type": server_type,
                "command": conf.get("command", ""),
                "url": conf.get("url", ""),
                "has_env": bool(conf.get("env")),
            }
            items.append(summary)
        return {"status": True, "msg": f"已配置 {len(items)} 个 MCP server", "items": items}

    def add_server(self, name: str, config: dict, overwrite: bool = False) -> dict:
        """新增一条 MCP server 配置到 mcp/config.json"""
        name = str(name).strip()
        if not name:
            return {"status": False, "msg": "缺少 server 名称"}
        if not isinstance(config, dict) or not config:
            return {"status": False, "msg": "config 必须是非空 JSON 对象"}
        data = self._load_config()
        servers = data.setdefault("mcpServers", {})
        if name in servers and not overwrite:
            return {"status": False, "msg": f"server '{name}' 已存在（设置 overwrite=true 覆盖）"}
        servers[name] = config
        if not self._save_config(data):
            return {"status": False, "msg": "写入配置文件失败"}
        return {"status": True, "msg": f"MCP server '{name}' 已添加", "name": name}

    def remove_server(self, name: str) -> dict:
        """从 mcp/config.json 移除一条 MCP server 配置"""
        name = str(name).strip()
        if not name:
            return {"status": False, "msg": "缺少 server 名称"}
        data = self._load_config()
        servers = data.get("mcpServers", {})
        if name not in servers:
            return {"status": False, "msg": f"server '{name}' 不存在"}
        del servers[name]
        if not self._save_config(data):
            return {"status": False, "msg": "写入配置文件失败"}
        return {"status": True, "msg": f"MCP server '{name}' 已移除"}

    # ---- 市场一键安装（从市场条目生成可执行配置并写入 mcp/config.json）----

    def install_from_market(self, mcp_id: str, overwrite: bool = False) -> dict:
        """
        从 GitHub 官方 MCP 市场拉取条目并直接生成可执行配置写入 mcp/config.json。
        GitHub 市场每条包含 packages[{runtime_hint, name, version}]，可直接组装命令。
        返回 {status, msg, name, config}
        """
        # 1. 在缓存或在线市场里找条目
        item = None
        cached = self._load_market_cache()
        if cached and cached.get("items"):
            for it in cached["items"]:
                if it.get("name") == mcp_id:
                    item = it
                    break
        if item is None:
            result = self.fetch_market_list()
            if result.get("status"):
                for it in result.get("items", []):
                    if it.get("name") == mcp_id:
                        item = it
                        break
        if item is None:
            return {"status": False, "msg": f"市场未找到条目: {mcp_id}"}

        # 2. 直接从 packages 字段生成启动配置（不用推导）
        config = self._build_market_config(item)
        if config is None:
            return {"status": False, "msg": f"无法为「{item.get('name')}」自动生成配置（请检查 runtime_hint）"}

        name = str(item.get("name") or mcp_id).strip()
        r = self.add_server(name, config, overwrite=overwrite)
        if r.get("status"):
            return {"status": True, "msg": f"已安装「{name}」（{r.get('msg')}）", "name": name, "config": config}
        return r

    def _build_market_config(self, item: dict) -> dict | None:
        """根据 GitHub 官方 MCP 市场条目，从 packages 或 remotes 字段生成可执行配置。

        - packages (stdio): {runtime_hint, name, version, runtime_arguments}
            runtime_hint ∈ {npx, uvx, uv, docker} → 对应命令
            runtime_hint 为空时按 name 前缀推断：
              - ghcr.io/ / docker.io/ / quay.io/ → docker（Docker Hub 镜像引用）
              - 其余 → npx（绝大多数是 npm 包）
            runtime_arguments 非空时追加为 docker args
        - remotes (http): {url, transport_type, headers} → {"type":"http","url":"..."}
        """
        pkg = item.get("_package")
        rem = item.get("_remote")

        # ── stdio 本地启动（packages）──
        if isinstance(pkg, dict) and pkg.get("name"):
            hint = str(pkg.get("runtime_hint") or "").strip().lower()
            pkg_name = str(pkg.get("name") or "").strip()
            version = pkg.get("version")
            ver_spec = f"@{version}" if version else ""
            run_args = pkg.get("runtime_arguments") or []

            # 1. runtime_hint 已知 → 直接映射
            if hint == "npx":
                return {"command": "npx", "args": ["-y", f"{pkg_name}{ver_spec}"], "type": "stdio"}
            if hint == "uvx":
                return {"command": "uvx", "args": [f"{pkg_name}{ver_spec}"], "type": "stdio"}
            if hint == "uv":
                return {"command": "uv", "args": ["run", f"{pkg_name}{ver_spec}"], "type": "stdio"}
            if hint == "docker":
                docker_args = ["run", "--rm", "-i"] + [a["value"] for a in run_args if a.get("value")] + [pkg_name]
                return {"command": "docker", "args": docker_args, "type": "stdio"}

            # 2. runtime_hint 为空 → 按 name 前缀推断
            if pkg_name.startswith(("ghcr.io/", "docker.io/", "quay.io/")) or ":latest" in pkg_name:
                docker_args = ["run", "--rm", "-i"] + [a["value"] for a in run_args if a.get("value")] + [pkg_name]
                return {"command": "docker", "args": docker_args, "type": "stdio"}

            # 3. 兜底 npx（绝大多数 npm 包没有 hint，但可以用 npx -y 运行）
            logger.debug("[mcp_market] 无 runtime_hint 条目 %s，兜底 npx: %s", pkg_name, pkg_name)
            return {"command": "npx", "args": ["-y", f"{pkg_name}{ver_spec}"], "type": "stdio"}

        # ── remote HTTP（remotes）──
        if isinstance(rem, dict) and rem.get("url"):
            return {"type": "http", "url": rem["url"]}

        return None

    # ---- 市场清单（Cline/VSCode 生态 MCP 市场）----

    def fetch_market_list(self) -> dict:
        """
        从市场源（默认 GitHub 官方 MCP 市场，可配置多源）拉取全量 server 清单。
        返回 {status, items:[{name, description, _package, _remote, env_vars, transport}], msg}

        缓存策略与 SkillManager 一致：成功结果落盘，TTL 60min；
        市场 API 错误响应绝不落盘、不覆盖旧缓存；读取强校验。
        """
        import requests as _requests

        cached = self._load_market_cache()
        if cached is not None:
            return cached

        urls = self.market_urls or [_DEFAULT_MARKET_URL]
        merged: dict[str, dict] = {}
        errors = []

        for url in urls:
            # GitHub 官方市场：分页拉全量（每页 30 条，252 总计约需 9 页）
            try:
                resp = _requests.get(url, params={"page_size": 100}, timeout=(10, 30),
                                     headers={"User-Agent": "mcp-market",
                                              "Accept": "application/json"})
            except _requests.Timeout:
                errors.append(f"{url}: 请求超时")
                continue
            except Exception as e:
                errors.append(f"{url}: {e!s}")
                continue
            if resp.status_code != 200:
                errors.append(f"{url}: HTTP {resp.status_code}")
                continue
            try:
                data = resp.json()
            except Exception:
                errors.append(f"{url}: 响应解析失败")
                continue
            if not isinstance(data, dict):
                errors.append(f"{url}: 格式异常")
                continue

            servers = data.get("servers")
            if not isinstance(servers, list):
                errors.append(f"{url}: 缺少 servers 字段")
                continue

            # 翻页拉全量（GitHub 官方接口实际每页 30 条，252 总计约需 9 页）
            all_servers = list(servers)
            meta = data.get("metadata") or {}
            total = meta.get("total") or 0
            cursor = meta.get("next_cursor")
            max_pages = max(10, (total // 30) + 3)  # 按总数估计页数，留余量
            page = 0
            while cursor and page < max_pages:
                page += 1
                try:
                    r2 = _requests.get(url, params={"page_size": 100, "cursor": cursor},
                                       timeout=(10, 30),
                                       headers={"User-Agent": "mcp-market",
                                                "Accept": "application/json"})
                    if r2.status_code != 200:
                        break
                    d2 = r2.json()
                    all_servers.extend(d2.get("servers", []))
                    cursor = (d2.get("metadata") or {}).get("next_cursor")
                except Exception:
                    break

            for s in all_servers:
                if not isinstance(s, dict) or not isinstance(s.get("server"), dict):
                    continue
                svr = s["server"]
                name = svr.get("name", "")
                if not name or name in merged:
                    continue
                pkg = (svr.get("packages") or [None])[0]
                rem = (svr.get("remotes") or [None])[0]
                env_vars = []
                if isinstance(pkg, dict):
                    for ev in pkg.get("environment_variables", []) or []:
                        if isinstance(ev, dict) and ev.get("name"):
                            env_vars.append(ev["name"])
                elif isinstance(rem, dict):
                    for h in rem.get("headers", []) or []:
                        if isinstance(h, dict) and h.get("name"):
                            env_vars.append(h["name"])
                repo = svr.get("repository") or {}
                merged[name] = {
                    "name": name,
                    "description": svr.get("description", "")[:400],
                    "_package": pkg if isinstance(pkg, dict) else None,
                    "_remote": rem if isinstance(rem, dict) else None,
                    "env_vars": env_vars,
                    "transport": (rem.get("transport_type") if isinstance(rem, dict) else "") or "",
                    "repository": repo.get("id") or "",
                    "readme_len": len(str(repo.get("readme") or "")),
                    "updated_at": svr.get("updated_at", ""),
                }

        items = list(merged.values())
        if not items:
            return {"status": False, "msg": "市场拉取失败: " + "; ".join(errors) if errors else "市场为空"}

        src_desc = "GitHub 官方" if len(urls) == 1 and urls[0] == _DEFAULT_MARKET_URL else f"{len(urls)} 个源"
        msg = f"MCP 市场共 {len(items)} 个 server（来自{src_desc}）"
        if errors:
            msg += f"；{len(errors)} 个源失败: {'; '.join(errors)}"
        result = {"status": True, "msg": msg, "items": items}
        self._save_market_cache(result)
        return result

    # ---- 市场缓存（强校验，防错误数据）----

    def _market_cache_signature(self) -> str:
        """GitHub 官方市场源固定，签名用版本指纹"""
        return hashlib.sha256(b"mcp-github-market-v0").hexdigest()[:16]

    def _is_valid_market_item(self, it: object) -> bool:
        if not isinstance(it, dict):
            return False
        if not isinstance(it.get("name"), str) or not it["name"]:
            return False
        pkg = it.get("_package")
        rem = it.get("_remote")
        # stdio 条目必须有 package name；remote 条目必须有 url——至少满足其一
        pkg_ok = isinstance(pkg, dict) and isinstance(pkg.get("name"), str) and pkg["name"]
        rem_ok = isinstance(rem, dict) and isinstance(rem.get("url"), str) and rem["url"]
        if not (pkg_ok or rem_ok):
            return False
        if not isinstance(it.get("description"), str):
            return False
        return True

    def _load_market_cache(self) -> dict | None:
        try:
            if not os.path.exists(MCP_MARKET_CACHE_FILE):
                return None
            with open(MCP_MARKET_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            logger.warning("[mcp market cache] 读取失败，忽略缓存", exc_info=True)
            return None
        if not isinstance(data, dict):
            return None
        if data.get("status") is not True:
            return None
        items = data.get("items")
        if not isinstance(items, list) or len(items) == 0:
            return None
        if not all(self._is_valid_market_item(it) for it in items):
            return None
        if data.get("signature") != self._market_cache_signature():
            return None
        saved_at = data.get("saved_at", 0)
        try:
            saved_at = float(saved_at)
        except (TypeError, ValueError):
            return None
        if time.time() - saved_at > MCP_MARKET_CACHE_TTL:
            return None
        return data

    def _save_market_cache(self, result: dict) -> None:
        if not isinstance(result, dict) or result.get("status") is not True:
            return
        items = result.get("items")
        if not isinstance(items, list) or len(items) == 0:
            return
        if not all(self._is_valid_market_item(it) for it in items):
            return
        payload = {
            "status": True,
            "msg": result.get("msg", ""),
            "items": items,
            "signature": self._market_cache_signature(),
            "saved_at": time.time(),
        }
        tmp = MCP_MARKET_CACHE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, MCP_MARKET_CACHE_FILE)
        except Exception:
            logger.warning("[mcp market cache] 写入失败", exc_info=True)
            try:
                os.remove(tmp)
            except OSError:
                pass


# 全局单例
mcp_manager = McpManager()
