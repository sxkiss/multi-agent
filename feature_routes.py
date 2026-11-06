"""安全增强与能力扩展 API。

优-1 第五阶段：从 web_server.py 拆出的护栏、审批、斜杠命令、
专家库、主动关怀等路由。
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

import public

logger = logging.getLogger(__name__)

_BASE_DIR = ""


def register_feature_routes(app, base_dir: str) -> None:
    global _BASE_DIR
    _BASE_DIR = base_dir

    @app.get("/api/guard/rules")
    async def api_guard_rules():
        """查看当前生效的护栏规则（可编辑 YAML 规则库）。"""
        from chat_client.guard_rules import get_guard_rules
        gr = get_guard_rules(_BASE_DIR)
        return JSONResponse(public.return_data(True, data={
            "path": gr.path, "count": len(gr.rules), "rules": gr.rules,
        }))

    @app.post("/api/guard/reload")
    async def api_guard_reload():
        """强制重载护栏规则（编辑 rules.yaml 后调用）。"""
        from chat_client.guard_rules import get_guard_rules
        gr = get_guard_rules(_BASE_DIR)
        gr._load(force=True)
        return JSONResponse(public.return_data(True, data={"count": len(gr.rules)}))

    @app.get("/api/hitl/pending")
    async def api_hitl_pending(session_id: str = ""):
        """列出待审批的高风险工具请求。"""
        from chat_client.hitl_store import get_hitl_store
        return JSONResponse(public.return_data(True, data={
            "pending": get_hitl_store(_BASE_DIR).list_pending(session_id),
        }))

    @app.post("/api/hitl/decide")
    async def api_hitl_decide(request: Request):
        """批准/拒绝一个审批请求。body: {id, approve: bool}"""
        from chat_client.hitl_store import get_hitl_store
        try:
            body = await request.json()
        except Exception:
            body = {}
        rid = str(body.get("id") or "")
        approve = bool(body.get("approve"))
        rec = get_hitl_store(_BASE_DIR).decide(rid, approve)
        if rec is None:
            return JSONResponse(public.returnMsg(False, f"审批请求不存在: {rid}"))
        return JSONResponse(public.return_data(True, data=rec))

    @app.get("/api/slash/commands")
    async def api_slash_commands():
        """列出可用斜杠命令。"""
        from chat_client.slash import get_slash_catalog
        return JSONResponse(public.return_data(True, data={
            "commands": get_slash_catalog().list_commands(),
        }))

    @app.post("/api/slash/run")
    async def api_slash_run(request: Request):
        """执行一条斜杠命令。body: {text, session_id?}"""
        from chat_client.slash import get_slash_catalog
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = str(body.get("text") or "")
        res = get_slash_catalog().dispatch(
            text,
            session_id=str(body.get("session_id") or ""),
            base_dir=_BASE_DIR,
        )
        if res is None:
            return JSONResponse(public.returnMsg(False, "不是有效的斜杠命令"))
        return JSONResponse(public.return_data(res.get("status", False), data=res))

    @app.get("/api/skills/packages")
    async def api_skill_packages():
        """列出已打包的技能包。"""
        from chat_client.skill_package import list_packages
        return JSONResponse(public.return_data(True, data={
            "packages": list_packages(_BASE_DIR),
        }))

    @app.get("/api/kb/jobs")
    async def api_kb_jobs():
        """知识库索引任务队列状态。"""
        from chat_client.kb_jobs import get_index_queue
        q = get_index_queue()
        return JSONResponse(public.return_data(True, data={
            "stats": q.stats(), "jobs": q.list_jobs(20),
        }))

    @app.get("/api/proactive/status")
    async def api_proactive_status():
        """主动关怀服务状态。"""
        from chat_client.proactive import ProactiveService
        svc = ProactiveService(getattr(app.state, "config", {}) or {})
        return JSONResponse(public.return_data(True, data=svc.status()))

    @app.get("/api/conversation/modes")
    async def api_conversation_modes():
        """列出可用会话模式（Ask/Plan/Craft）。"""
        from chat_client.conversation_mode import MODES, HOST_READ_TOOLS, PLAN_ALLOW_EXTRA
        return JSONResponse(public.return_data(True, data={
            "modes": list(MODES),
            "read_tools": sorted(HOST_READ_TOOLS),
            "plan_extra": sorted(PLAN_ALLOW_EXTRA),
        }))

    @app.get("/api/experts")
    async def api_experts():
        """列出本地专家库（自动发现 experts/ 目录）。"""
        from chat_client.experts import get_catalog
        cat = get_catalog(_BASE_DIR)
        return JSONResponse(public.return_data(True, data={
            "experts": cat.list_dicts(),
            "total": len(cat.scan()),
            "experts_dir": cat.experts_dir,
        }))

    @app.get("/api/experts/{expert_id}")
    async def api_expert_detail(expert_id: str):
        """获取单个专家详情（含人格文件）。"""
        from chat_client.experts import get_catalog
        e = get_catalog(_BASE_DIR).get(expert_id)
        if not e:
            return JSONResponse(public.returnMsg(False, f"专家不存在: {expert_id}"))
        return JSONResponse(public.return_data(True, data=e.to_dict(with_prompt=True)))
