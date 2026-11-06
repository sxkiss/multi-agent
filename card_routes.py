"""卡密验证、卡密管理与卡密用户资源 API。

优-1 第七阶段：从 web_server.py 拆出的卡密域路由。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse

import public

logger = logging.getLogger(__name__)


def register_card_routes(app, base_dir: str, agent_main) -> None:
    # ============================================================
    # 卡密验证（前端实时校验用，无需 Bearer token）
    # ============================================================
    @app.post("/api/auth/card/verify")
    async def api_card_verify(request: Request):
        """验证卡密并返回前缀与持有人信息（供前端校验显示）。

        无需 token：此接口本身在白名单中，调用方还拿着有效令牌。
        返回前缀用于前端展示"欢迎 XXX"等信息，同时作为会话锚定键。
        """
        from auth import normalize_card_key, verify_card_key, load_card_keys
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw_key = str(body.get("key", "")).strip()
        if not raw_key:
            return JSONResponse(public.returnMsg(False, "缺少参数 key"), status_code=400)
        ok, result = verify_card_key(base_dir, raw_key)
        if not ok:
            return JSONResponse(public.returnMsg(False, result), status_code=401)
        prefix = result
        cards = load_card_keys(base_dir)
        rec = cards.get(prefix, {})
        expires = str(rec.get("expires") or "").strip()
        days = int(rec.get("days") or 0)
        created_at = str(rec.get("created_at") or "").strip()
        activated_at = str(rec.get("activated_at") or "").strip()
        return JSONResponse(public.return_data(True, data={
            "prefix": prefix,
            "owner": str(rec.get("owner") or ""),
            "expires": expires if expires else "长期有效",
            "days": days,
            "created_at": created_at,
            "activated_at": activated_at,
            "is_activated": bool(activated_at),
        }))


    # ============================================================
    # 卡密管理后台（仅管理员，需 admin token）
    # ============================================================
    # 与普通卡密登录严格分离：卡密用户 token 的 sub 是 "card:<prefix>"，
    # 这里只认 sub == "admin"（密码登录签发的），杜绝卡密用户自己改卡密。

    def _admin_guard(request: Request):
        """管理员校验：返回 (None, None) 通过，否则返回 error_response。"""
        from auth import extract_token, is_admin_token
        token = extract_token(request)
        if not token or not is_admin_token(base_dir, token):
            return JSONResponse(
                {"status": False, "msg": "需要管理员登录", "code": 403}, status_code=403)
        return None


    @app.get("/api/card/admin/list")
    async def api_card_admin_list(request: Request):
        """列出全部卡密（脱敏展示）。"""
        err = _admin_guard(request)
        if err:
            return err
        from auth import list_cards
        return JSONResponse(public.return_data(True, data={"cards": list_cards(base_dir)}))


    @app.post("/api/card/admin/create")
    async def api_card_admin_create(request: Request):
        """生成新卡密。参数：days(有效期天数,0=长期) / owner / note。

        完整卡密只在创建时返回一次 —— 列表接口是脱敏的，
        管理员若当时没复制走，只能删了重建。
        """
        err = _admin_guard(request)
        if err:
            return err
        from auth import create_card
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            days = int(body.get("days") or 30)
        except (TypeError, ValueError):
            days = 30
        try:
            rec = create_card(base_dir, days=days,
                              owner=str(body.get("owner") or ""),
                              note=str(body.get("note") or ""))
        except Exception as e:
            logger.warning("[card_admin] 创建失败: %s", e, exc_info=True)
            return JSONResponse(public.returnMsg(False, "卡密生成失败"), status_code=500)
        return JSONResponse(public.return_data(True, data=rec))


    @app.post("/api/card/admin/update")
    async def api_card_admin_update(request: Request):
        """更新卡密：owner / note / days（days 重算有效期，0=长期）。"""
        err = _admin_guard(request)
        if err:
            return err
        from auth import update_card
        try:
            body = await request.json()
        except Exception:
            body = {}
        prefix = str(body.get("prefix") or "").strip()
        if not prefix:
            return JSONResponse(public.returnMsg(False, "缺少参数 prefix"), status_code=400)
        rec = update_card(base_dir, prefix, body)
        if not rec:
            return JSONResponse(public.returnMsg(False, "卡密不存在"), status_code=404)
        return JSONResponse(public.return_data(True, data=rec))


    @app.post("/api/card/admin/delete")
    async def api_card_admin_delete(request: Request):
        """删除卡密。"""
        err = _admin_guard(request)
        if err:
            return err
        from auth import delete_card
        try:
            body = await request.json()
        except Exception:
            body = {}
        prefix = str(body.get("prefix") or "").strip()
        if not prefix:
            return JSONResponse(public.returnMsg(False, "缺少参数 prefix"), status_code=400)
        if not delete_card(base_dir, prefix):
            return JSONResponse(public.returnMsg(False, "卡密不存在"), status_code=404)
        return JSONResponse(public.return_data(True, data={"prefix": prefix}))


    # ============================================================
    # 卡密用户个人配置（用户级 API/提示词模板）
    # ============================================================
    _USER_CONFIG_FILE = "card_user_configs.json"


    def _load_user_config() -> dict:
        """加载用户配置文件。"""
        path = os.path.join(base_dir, _USER_CONFIG_FILE)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}


    def _save_user_config(data: dict) -> bool:
        """保存用户配置文件（原子写入）。"""
        path = os.path.join(base_dir, _USER_CONFIG_FILE)
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except Exception as e:
            logger.warning("用户配置保存失败: %s", e)
            return False


    @app.get("/api/card/user/config")
    async def api_card_user_config(request: Request):
        """获取当前卡密用户的个人配置。"""
        from auth import extract_token, verify_token, is_card_token
        token = extract_token(request)
        if not token or not is_card_token(base_dir, token):
            return JSONResponse({"status": False, "msg": "未授权", "code": 403}, status_code=403)
        payload = verify_token(base_dir, token)
        sub = str(payload.get("sub") or "")
        prefix = sub[5:]  # "card:XXXXXX" -> "XXXXXX"
        configs = _load_user_config()
        user_cfg = configs.get(prefix, {})
        # 返回默认模型列表
        default_config = agent_main.get_config()
        models = default_config.get("data", {}).get("config", {}).get("models", [])
        # 返回内置提示词模板列表
        try:
            from chat_client.opencode_templates import OPENCODE_TEMPLATES
            builtin_templates = list(OPENCODE_TEMPLATES.keys())
        except Exception:
            builtin_templates = []
    
        return JSONResponse({
            "status": True,
            "data": {
                **user_cfg,
                "models": models,
                "builtin_templates": builtin_templates,
            }
        })


    @app.post("/api/card/user/config")
    async def api_card_user_config_save(request: Request):
        """保存当前卡密用户的个人配置。"""
        from auth import extract_token, verify_token, is_card_token
        token = extract_token(request)
        if not token or not is_card_token(base_dir, token):
            return JSONResponse({"status": False, "msg": "未授权", "code": 403}, status_code=403)
        payload = verify_token(base_dir, token)
        sub = str(payload.get("sub") or "")
        prefix = sub[5:]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": False, "msg": "请求体解析失败"}, status_code=400)
        configs = _load_user_config()
        if prefix not in configs:
            configs[prefix] = {}
        # 允许保存的字段
        allowed_keys = ['model', 'api_base_url', 'api_key', 'system_prompt']
        for key in allowed_keys:
            if key in body:
                configs[prefix][key] = str(body[key]).strip() or None
        _save_user_config(configs)
        return JSONResponse({"status": True, "msg": "保存成功"})


    # ---- 卡密用户 MCP Server 管理 ----

    def _card_prefix_from_request(request: Request) -> tuple:
        """从请求中提取 card token 并返回 (prefix, 403_response_or_None)"""
        from auth import extract_token, verify_token, is_card_token
        token = extract_token(request)
        if not token or not is_card_token(base_dir, token):
            return None, JSONResponse({"status": False, "msg": "未授权", "code": 403}, status_code=403)
        payload = verify_token(base_dir, token)
        sub = str(payload.get("sub") or "")
        if not sub.startswith("card:"):
            return None, JSONResponse({"status": False, "msg": "非卡密用户", "code": 403}, status_code=403)
        prefix = sub[5:]
        return prefix, None


    @app.get("/api/card/mcp/servers")
    async def api_card_mcp_servers(request: Request):
        prefix, err = _card_prefix_from_request(request)
        if err:
            return err
        from chat_client.mcp_manager import mcp_manager
        path = os.path.join(base_dir, "mcp", "users", f"card_{prefix}", "config.json")
        # 临时切换配置路径查询
        orig = mcp_manager.config_path
        mcp_manager.set_config_path(path)
        try:
            return JSONResponse(mcp_manager.list_servers())
        finally:
            mcp_manager.set_config_path(orig)


    @app.post("/api/card/mcp/servers/add")
    async def api_card_mcp_server_add(request: Request):
        prefix, err = _card_prefix_from_request(request)
        if err:
            return err
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
        path = os.path.join(base_dir, "mcp", "users", f"card_{prefix}", "config.json")
        orig = mcp_manager.config_path
        mcp_manager.set_config_path(path)
        try:
            return JSONResponse(mcp_manager.add_server(name, config, overwrite=overwrite))
        finally:
            mcp_manager.set_config_path(orig)


    @app.post("/api/card/mcp/servers/remove")
    async def api_card_mcp_server_remove(request: Request):
        prefix, err = _card_prefix_from_request(request)
        if err:
            return err
        params = await _params(request)
        from chat_client.mcp_manager import mcp_manager
        name = str(params.get('name', '') or '').strip()
        path = os.path.join(base_dir, "mcp", "users", f"card_{prefix}", "config.json")
        orig = mcp_manager.config_path
        mcp_manager.set_config_path(path)
        try:
            return JSONResponse(mcp_manager.remove_server(name))
        finally:
            mcp_manager.set_config_path(orig)


    @app.post("/api/card/mcp/servers/install")
    async def api_card_mcp_server_install(request: Request):
        prefix, err = _card_prefix_from_request(request)
        if err:
            return err
        params = await _params(request)
        from chat_client.mcp_manager import mcp_manager
        mcp_id = str(params.get('mcp_id', '') or '').strip()
        overwrite = str(params.get("overwrite", "")).lower() in ("1", "true", "yes")
        if not mcp_id:
            return JSONResponse({"status": False, "msg": "缺少参数 mcp_id"})
        path = os.path.join(base_dir, "mcp", "users", f"card_{prefix}", "config.json")
        orig = mcp_manager.config_path
        mcp_manager.set_config_path(path)
        try:
            return JSONResponse(await asyncio.to_thread(
                mcp_manager.install_from_market, mcp_id, overwrite))
        finally:
            mcp_manager.set_config_path(orig)


    # ---- 卡密用户技能管理 ----

    @app.get("/api/card/skills")
    async def api_card_skills(request: Request):
        prefix, err = _card_prefix_from_request(request)
        if err:
            return err
        from chat_client.skills import skill_manager
        orig_dir = skill_manager.skills_dir
        user_dir = os.path.join(base_dir, "skills", "users", f"card_{prefix}")
        os.makedirs(user_dir, exist_ok=True)
        skill_manager.set_skills_dir(user_dir)
        try:
            all_skills = skill_manager.get_all_skills_info()
            skills = []
            for skill in all_skills:
                location = skill.get("location", "")
                rel_path = location.replace(skill_manager.skills_dir, "").strip("/")
                if not rel_path or rel_path == location:
                    pass  # 不排除
                skills.append(skill)
            enabled_count = len([s for s in skills if s.get("enabled")])
            return JSONResponse({
                "total": len(skills),
                "enabled": enabled_count,
                "disabled": len(skills) - enabled_count,
                "skills": skills,
            })
        finally:
            skill_manager.set_skills_dir(orig_dir)


    @app.post("/api/card/skills/install")
    async def api_card_skill_install(request: Request):
        prefix, err = _card_prefix_from_request(request)
        if err:
            return err
        form = await request.form()
        zip_bytes = form.get("zip").read() if form.get("zip") else b""
        overwrite = str(form.get("overwrite", "0")).lower() in ("1", "true", "yes")
        if not zip_bytes:
            return JSONResponse({"status": False, "msg": "缺少 zip 文件"})
        from chat_client.skills import skill_manager
        orig_dir = skill_manager.skills_dir
        user_dir = os.path.join(base_dir, "skills", "users", f"card_{prefix}")
        os.makedirs(user_dir, exist_ok=True)
        skill_manager.set_skills_dir(user_dir)
        try:
            return JSONResponse(skill_manager.install_from_zip(zip_bytes, overwrite=overwrite))
        finally:
            skill_manager.set_skills_dir(orig_dir)


    @app.post("/api/card/skills/uninstall")
    async def api_card_skill_uninstall(request: Request):
        prefix, err = _card_prefix_from_request(request)
        if err:
            return err
        params = await _params(request)
        from chat_client.skills import skill_manager
        name = str(params.get("skill_name", "") or "").strip()
        orig_dir = skill_manager.skills_dir
        user_dir = os.path.join(base_dir, "skills", "users", f"card_{prefix}")
        os.makedirs(user_dir, exist_ok=True)
        skill_manager.set_skills_dir(user_dir)
        try:
            return JSONResponse(skill_manager.uninstall(name))
        finally:
            skill_manager.set_skills_dir(orig_dir)
