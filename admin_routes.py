"""系统管理 API：配置、工具、技能、MCP、团队、热加载与重启。

优-1 第三阶段：从 web_server.py 拆出的管理域路由。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

import public
import chat_client.hotreload as _hotreload

logger = logging.getLogger(__name__)

_BASE_DIR = ""
_AGENT = None


async def _params(request: Request) -> dict:
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("管理接口参数解析失败，已忽略 body", exc_info=True)
    return params


def _admin_guard(request: Request):
    from auth import extract_token, is_admin_token
    token = extract_token(request)
    if not token or not is_admin_token(_BASE_DIR, token):
        return JSONResponse(
            {"status": False, "msg": "需要管理员登录", "code": 403}, status_code=403)
    return None


def _auth_disabled() -> bool:
    try:
        from auth import load_auth
        return not bool(load_auth(_BASE_DIR).get("enabled"))
    except Exception:
        return True


def register_admin_routes(app, base_dir: str, agent_main) -> None:
    global _BASE_DIR, _AGENT
    _BASE_DIR = base_dir
    _AGENT = agent_main

    @app.get("/api/config")
    async def api_config(request: Request):
        from auth import extract_token, is_admin_token
        # 未开启鉴权 → 完全放行（与 auth_middleware 行为一致）
        if _auth_disabled():
            return JSONResponse(_AGENT.get_config())
        token = extract_token(request)
        if not token:
            raise HTTPException(status_code=401, detail="未登录")
        if not is_admin_token(_BASE_DIR, token):
            raise HTTPException(status_code=403, detail="只有管理员可以访问此接口")
        return JSONResponse(_AGENT.get_config())


    @app.post("/api/config")
    async def api_set_config(request: Request):
        from auth import extract_token, is_admin_token
        # 未开启鉴权 → 完全放行（与 auth_middleware 行为一致）
        if _auth_disabled():
            params = await _params(request)
            config_str = params.get('config', '')
            return JSONResponse(_AGENT.set_config(config_str))
        token = extract_token(request)
        if not token or not is_admin_token(_BASE_DIR, token):
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="只有管理员可以修改配置")
        params = await _params(request)
        config_str = params.get('config', '')
        return JSONResponse(_AGENT.set_config(config_str))


    @app.get("/api/search/config")
    async def api_search_config():
        return JSONResponse(_AGENT.get_search_config())


    @app.post("/api/search/config")
    async def api_set_search_config(request: Request):
        params = await _params(request)
        return JSONResponse(_AGENT.set_search_config(params.get('config', '')))


    @app.get("/api/models")
    async def api_models(base_url: str = "", key: str = ""):
        return JSONResponse(_AGENT.get_models(base_url=base_url, key=key))


    @app.get("/api/agents")
    async def api_agents():
        return JSONResponse(_AGENT.agent_list())


    @app.get("/api/tools")
    async def api_tools():
        # get_tool_list 内部可能等待 MCP 预加载线程（同步 join），放入线程池避免阻塞事件循环
        return JSONResponse(await asyncio.to_thread(_AGENT.get_tool_list))


    # ── P3-b: 可观测性（进程内指标）──────────────────────────────────
    @app.get("/api/metrics")
    async def api_metrics(format: str = "json"):
        """进程内运行指标：工具/LLM 调用计数与耗时。format=prometheus 返回文本格式。"""
        from chat_client.metrics import default_persist_path, get_metrics
        m = get_metrics(persist_path=default_persist_path())
        if (format or "").lower() in ("prometheus", "text"):
            return PlainTextResponse(m.to_prometheus())
        return JSONResponse(public.return_data(True, data=m.snapshot()))


    # ── P3-a: 插件体系 ──────────────────────────────────────────────
    @app.get("/api/plugins")
    async def api_plugins():
        """列出磁盘上的插件及加载状态。"""
        from chat_client.plugins import get_plugin_manager
        return JSONResponse(public.return_data(True, data=get_plugin_manager().status()))


    @app.post("/api/plugins/load")
    async def api_plugins_load(request: Request):
        """加载插件（name 留空则加载全部）。"""
        from chat_client.plugins import get_plugin_manager
        params = await _params(request)
        pm = get_plugin_manager()
        name = str(params.get("name", "")).strip()
        if name:
            res = pm.load_plugin(name)
        else:
            res = pm.load_all()
        return JSONResponse(public.return_data(True, data=res) if (isinstance(res, dict) and res.get("status", True))
                            else public.returnMsg(False, (res or {}).get("msg", "加载失败")))


    @app.post("/api/plugins/unload")
    async def api_plugins_unload(request: Request):
        """卸载插件。"""
        from chat_client.plugins import get_plugin_manager
        params = await _params(request)
        name = str(params.get("name", "")).strip()
        if not name:
            return JSONResponse(public.returnMsg(False, "缺少 name"))
        res = get_plugin_manager().unload_plugin(name)
        return JSONResponse(public.return_data(True, data=res) if res.get("status")
                            else public.returnMsg(False, res.get("msg", "卸载失败")))


    @app.get("/api/skills")
    async def api_skills():
        return JSONResponse(_AGENT.get_skill_list())


    @app.post("/api/skills/install")
    async def api_skill_install(request: Request):
        params = await _params(request)
        return JSONResponse(_AGENT.skill_install(params))


    @app.post("/api/skills/uninstall")
    async def api_skill_uninstall(request: Request):
        params = await _params(request)
        return JSONResponse(_AGENT.skill_uninstall(params))


    @app.post("/api/skills/install_url")
    async def api_skill_install_url(request: Request):
        params = await _params(request)
        return JSONResponse(_AGENT.skill_install_url(params))


    @app.post("/api/skills/status")
    async def api_skill_status(request: Request):
        """切换技能启用状态"""
        params = await _params(request)
        # 前端字段为 skill_name/enabled，与 _AGENT.set_skill_status 一致，原样透传
        return JSONResponse(_AGENT.set_skill_status(params))


    @app.get("/api/skills/usage")
    async def api_skills_usage(limit: int = 50):
        """技能使用统计：按技能名的加载次数排行 + 从未被加载过的技能清单。

        工具级指标只能看到 "Skills" 被调了多少次，无法回答「哪个技能真被用过」。
        本接口把两者合起来，用于判断技能摘要（system prompt 里那 10.8KB）该保留
        哪些、哪些可以移出常驻列表改为按需加载。
        """
        from chat_client.metrics import default_persist_path, get_metrics
        from chat_client.skills import skill_manager

        m = get_metrics(persist_path=default_persist_path())
        lifetime = dict(m.snapshot().get("skills_lifetime") or {})

        try:
            all_names = [s.name for s in skill_manager.all()]
        except Exception:
            all_names = []

        ranked = sorted(
            (
                {"name": n, **{k: int(v.get(k, 0)) for k in ("loads", "installs", "uninstalls", "errors")}}
                for n, v in lifetime.items()
            ),
            key=lambda x: (x["loads"], x["installs"]),
            reverse=True,
        )
        used = {r["name"] for r in ranked if r["loads"] or r["installs"]}
        never_used = sorted(n for n in all_names if n not in used)

        return JSONResponse(public.return_data(True, data={
            "total_installed": len(all_names),
            "used_count": len(used),
            "never_used_count": len(never_used),
            "ranked": ranked[:max(1, int(limit))],
            "never_used": never_used,
        }))


    # ── P2-a: 技能包（导出/导入）────────────────────────────────────
    @app.get("/api/skills/presentation")
    async def api_skills_presentation():
        """技能展示元数据（icon/分类/触发词），供前端呈现层使用。"""
        from chat_client.skills import skill_manager
        return JSONResponse(public.return_data(True, data={
            "skills": skill_manager.get_all_presentations(),
        }))


    @app.post("/api/skills/export_package")
    async def api_skills_export_package(request: Request):
        """把一组技能导出为技能包 ZIP（base64 返回）。"""
        import base64
        from chat_client.skills import skill_manager
        params = await _params(request)
        names = params.get("names") or []
        if isinstance(names, str):
            names = [n.strip() for n in names.split(",") if n.strip()]
        try:
            data = skill_manager.export_package(names, params.get("package_name", ""))
        except Exception as e:
            logger.warning("技能包导出失败", exc_info=True)
            return JSONResponse(public.returnMsg(False, "导出失败"))
        return JSONResponse(public.return_data(True, data={
            "package_name": params.get("package_name", "") or "skill-package",
            "size": len(data),
            "zip_base64": base64.b64encode(data).decode("ascii"),
        }))


    @app.post("/api/skills/import_package")
    async def api_skills_import_package(request: Request):
        """导入技能包 ZIP（接受 base64 或 zip_base64 字段）。"""
        import base64
        from chat_client.skills import skill_manager
        params = await _params(request)
        raw = params.get("zip_base64") or params.get("content") or params.get("zip") or ""
        if not raw:
            return JSONResponse(public.returnMsg(False, "缺少 zip_base64 内容"))
        try:
            zip_bytes = base64.b64decode(raw)
        except Exception as e:
            logger.warning("技能包 base64 解码失败", exc_info=True)
            return JSONResponse(public.returnMsg(False, "base64 解码失败"))
        res = skill_manager.import_package(zip_bytes, overwrite=bool(params.get("overwrite")))
        return JSONResponse(public.return_data(True, data=res) if res.get("status")
                            else public.returnMsg(False, res.get("msg", "导入失败")))


    @app.post("/api/tools/show_status")
    async def api_tool_show_status(request: Request):
        """切换工具显示状态"""
        params = await _params(request)
        # 前端传 id（单工具）或 category（分类）；agent_main 用 tool_id，做归一化
        if params.get("id"):
            params["tool_id"] = params.pop("id")
        return JSONResponse(_AGENT.set_tool_show_status(params))


    # ---- MCP Server 管理（读写 mcp/config.json + 市场）----

    @app.get("/api/mcp/servers")
    async def api_mcp_servers():
        from chat_client.mcp_manager import mcp_manager
        return JSONResponse(mcp_manager.list_servers())


    @app.post("/api/mcp/servers/add")
    async def api_mcp_server_add(request: Request):
        params = await _params(request)
        from chat_client.mcp_manager import mcp_manager
        name = str(params.get("name", "") or "").strip()
        config_raw = params.get("config", "")
        overwrite = str(params.get("overwrite", "")).lower() in ("1", "true", "yes")
        if not config_raw:
            return JSONResponse({"status": False, "msg": "缺少参数 config"})
        try:
            config = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
        except Exception:
            return JSONResponse({"status": False, "msg": "config 不是合法 JSON"})
        if not isinstance(config, dict):
            return JSONResponse({"status": False, "msg": "config 必须是 JSON 对象"})
        return JSONResponse(mcp_manager.add_server(name, config, overwrite=overwrite))


    @app.post("/api/mcp/servers/remove")
    async def api_mcp_server_remove(request: Request):
        params = await _params(request)
        from chat_client.mcp_manager import mcp_manager
        name = str(params.get('name', '') or '').strip()
        return JSONResponse(mcp_manager.remove_server(name))


    @app.post("/api/mcp/servers/install")
    async def api_mcp_server_install(request: Request):
        """从市场一键安装：按 mcp_id 生成配置并写入 mcp/config.json"""
        params = await _params(request)
        from chat_client.mcp_manager import mcp_manager
        mcp_id = str(params.get('mcp_id', '') or '').strip()
        overwrite = str(params.get("overwrite", "")).lower() in ("1", "true", "yes")
        if not mcp_id:
            return JSONResponse({"status": False, "msg": "缺少参数 mcp_id"})
        return JSONResponse(await asyncio.to_thread(
            mcp_manager.install_from_market, mcp_id, overwrite))


    @app.get("/api/mcp/market")
    async def api_mcp_market():
        from chat_client.mcp_manager import mcp_manager
        return JSONResponse(await asyncio.to_thread(mcp_manager.fetch_market_list))


    @app.get("/api/crew/agents")
    async def api_crew_agents():
        return JSONResponse(_AGENT.crew_agents_list())


    @app.post("/api/crew/save")
    async def api_crew_save(request: Request):
        params = await _params(request)
        return JSONResponse(_AGENT.crew_agent_save(params))


    @app.post("/api/crew/delete")
    async def api_crew_delete(request: Request):
        params = await _params(request)
        return JSONResponse(_AGENT.crew_agent_delete(params))


    @app.get("/api/org")
    async def api_org():
        return JSONResponse(_AGENT.org_chart())


    @app.post("/api/crew/dept_save")
    async def api_crew_dept_save(request: Request):
        params = await _params(request)
        return JSONResponse(_AGENT.crew_dept_save(params))


    @app.post("/api/crew/dept_delete")
    async def api_crew_dept_delete(request: Request):
        params = await _params(request)
        return JSONResponse(_AGENT.crew_dept_delete(params))


    @app.post("/api/hotreload")
    async def api_hotreload(request: Request):
        params = await _params(request)
        target = str(params.get("target", "all")).strip().lower()
        targets = list(_hotreload._LOADERS.keys()) if target == "all" else [target]
        results = _hotreload.reload_targets(targets)
        ok = all(not v.startswith("失败") for v in results.values())
        return JSONResponse({"status": ok, "data": results, "msg": "; ".join(f"{k}: {v}" for k, v in results.items())})


    @app.post("/api/restart")
    async def api_restart(request: Request):
        """安全重启服务（异步执行，立即返回）。仅管理员可用。"""
        err = _admin_guard(request)
        if err:
            return err

        def _do_restart():
            import subprocess as _sp
            import time as _time
            _sp.Popen(
                ["bash", os.path.join(_BASE_DIR, "restart_bt.sh"), "3"],
                cwd=_BASE_DIR,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                start_new_session=True,
            )

        threading.Thread(target=_do_restart, daemon=True).start()
        return JSONResponse({"status": True, "msg": "重启已触发，约 5 秒后服务恢复"})


