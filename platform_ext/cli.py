"""薄 CLI —— 复用 BT 现有 HTTP API。

设计：不重写业务逻辑，只做 HTTP 客户端 + 少量本地命令，零新增依赖
（仅标准库 urllib + argparse）。

用法：
    python -m platform_ext.cli --help
    python -m platform_ext.cli status
    python -m platform_ext.cli models
    python -m platform_ext.cli skills list
    python -m platform_ext.cli experts list
    python -m platform_ext.cli connectors catalog
    python -m platform_ext.cli connectors instances
    python -m platform_ext.cli ask "帮我看看磁盘占用"

环境变量 / 参数：
    --base-url  BT 服务地址（默认 http://127.0.0.1:9876）
    --token     JWT（默认读 env BT_TOKEN 或 auth.json 的 admin key）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE_URL = os.environ.get("BT_BASE_URL", "http://127.0.0.1:9876")


def _load_token(base_url: str) -> str:
    tok = os.environ.get("BT_TOKEN", "").strip()
    if tok:
        return tok
    # 尝试从项目 auth.json 读 admin api key
    here = os.path.dirname(os.path.abspath(__file__))
    auth_file = os.path.join(os.path.dirname(here), "auth.json")
    try:
        with open(auth_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return str(data.get("admin_api_key") or data.get("api_key") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


class Client:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _req(self, method: str, path: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            return {"status": False, "msg": f"HTTP {e.code}: {detail[:300]}"}
        except urllib.error.URLError as e:
            return {"status": False, "msg": f"连接失败: {e.reason}"}

    def get(self, path, params=None):
        return self._req("GET", path, params=params)

    def post(self, path, body=None, params=None):
        return self._req("POST", path, params=params, body=body or {})


def _out(res: Any, pretty: bool = True) -> int:
    if isinstance(res, dict) and res.get("status") is False:
        print(f"✗ {res.get('msg', '失败')}", file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2 if pretty else None))
    return 0


# ── 子命令实现 ─────────────────────────────────────────────
def cmd_status(c: Client, args) -> int:
    models = c.get("/api/models")
    agents = c.get("/api/agents")
    experts = c.get("/api/v2/connectors/catalog", {"locale": "zh"})
    print("BT 网关状态")
    print(f"  地址      : {c.base_url}")
    print(f"  模型      : {', '.join(models.get('data', [])) if models.get('status') else '不可用'}")
    print(f"  Agent     : {len(agents.get('data', [])) if isinstance(agents.get('data'), list) else '?'}")
    if experts.get("status"):
        print(f"  连接器    : {experts['data'].get('total', '?')} 个")
    return 0


def cmd_models(c: Client, args) -> int:
    return _out(c.get("/api/models"))


def cmd_skills(c: Client, args) -> int:
    if args.action == "list":
        res = c.get("/api/skills")
        if res.get("status") and isinstance(res.get("data"), dict):
            skills = res["data"].get("skills", res["data"])
            if isinstance(skills, list):
                for s in skills[: args.limit]:
                    name = s.get("name") if isinstance(s, dict) else s
                    print(name)
                return 0
        return _out(res)
    return _out(c.get("/api/skills"))


def cmd_experts(c: Client, args) -> int:
    if args.action == "list":
        res = c.get("/api/experts")
        if res.get("status") and isinstance(res.get("data"), dict):
            for e in res["data"].get("experts", []):
                print(f"  {e.get('id', '?'):20} {e.get('name', '')}")
            return 0
        return _out(res)
    if args.action == "show" and args.name:
        return _out(c.get(f"/api/experts/{args.name}"))
    return _out(c.get("/api/experts"))


def cmd_connectors(c: Client, args) -> int:
    if args.action == "catalog":
        res = c.get("/api/v2/connectors/catalog", {"locale": "zh"})
        if res.get("status") and isinstance(res.get("data"), dict):
            for e in res["data"].get("connectors", []):
                mark = "✓" if e.get("supported") else "·"
                print(f"  {mark} {e.get('kind', ''):22} {e.get('name', '')}")
            return 0
        return _out(res)
    if args.action == "instances":
        res = c.get("/api/v2/connectors/instances")
        if res.get("status") and isinstance(res.get("data"), dict):
            insts = res["data"].get("instances", [])
            if not insts:
                print("  （无实例）")
            for i in insts:
                print(f"  {i.get('instance_id', ''):18} {i.get('kind', ''):22} {i.get('name', '')}")
            return 0
        return _out(res)
    if args.action == "cli-status":
        return _out(c.get("/api/v2/connectors/cli/status"))
    return _out(c.get("/api/v2/connectors/catalog"))


def cmd_ask(c: Client, args) -> int:
    """发起一次对话并流式打印结果（用 /api/chat/start + /api/chat/events）。"""
    start = c.post("/api/chat/start", {"message": args.text, "session_id": args.session or ""})
    if start.get("status") is False:
        return _out(start)
    job_id = (start.get("data") or {}).get("job_id") or start.get("job_id")
    print(f"（任务已启动 job={job_id}；流式输出请用 Web 界面或 /api/chat/events）")
    return 0


def cmd_health(c: Client, args) -> int:
    return _out(c.get("/api/models"))


# ── 参数解析 ───────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bt", description="BT 网关薄 CLI（复用 HTTP API）")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"BT 服务地址（默认 {DEFAULT_BASE_URL}）")
    p.add_argument("--token", default="", help="JWT（默认读 BT_TOKEN 环境变量或 auth.json）")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status", help="概览状态")
    sub.add_parser("models", help="列出模型")
    sub.add_parser("health", help="健康检查")

    sk = sub.add_parser("skills", help="技能管理")
    sk.add_argument("action", nargs="?", default="list", choices=["list"])
    sk.add_argument("--limit", type=int, default=200)

    ex = sub.add_parser("experts", help="专家管理")
    ex.add_argument("action", nargs="?", default="list", choices=["list", "show"])
    ex.add_argument("name", nargs="?", default="")

    cn = sub.add_parser("connectors", help="连接器管理")
    cn.add_argument("action", nargs="?", default="catalog", choices=["catalog", "instances", "cli-status"])

    ask = sub.add_parser("ask", help="发起一次对话")
    ask.add_argument("text", help="提问内容")
    ask.add_argument("--session", default="", help="会话 ID（可选）")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    token = args.token or _load_token(args.base_url)
    c = Client(args.base_url, token)

    handlers = {
        "status": cmd_status, "models": cmd_models, "health": cmd_health,
        "skills": cmd_skills, "experts": cmd_experts, "connectors": cmd_connectors,
        "ask": cmd_ask,
    }
    fn = handlers.get(args.cmd)
    if fn is None:
        parser.print_help()
        return 2
    return fn(c, args)


if __name__ == "__main__":
    sys.exit(main())