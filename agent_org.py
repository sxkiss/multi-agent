"""AgentMain 组织、角色与技能管理职责。

优-1 第十四阶段：从 web_server.py 拆出的自定义子代理（crew）管理、
组织架构、技能安装/卸载等，通过 mixin 复用 self.config / self.plugin_path。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
from typing import ClassVar

import public
from chat_client.skills import skill_manager
from chat_client.tools import registry

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class OrgSkillMixin:
    def agent_list(self):
        agents_config, error = self._load_agents_config()
        if error:
            return public.returnMsg(False, error)
        return public.return_data(True, data=agents_config)

    def get_tool_list(self):
        # 不等待 MCP 预加载（异步加载完成后下次请求自然出现），避免阻塞
        tools = registry.get_all_tools_info()
        # 确保所有工具都显示为可用（前端会过滤 show: False 的工具）
        for tool in tools:
            tool["show"] = True
        return public.return_data(True, data=tools)

    def get_skill_list(self):
        all_skills = skill_manager.get_all_skills_info()
        skills = []
        for skill in all_skills:
            location = skill.get("location", "")
            rel_path = location.replace(skill_manager.skills_dir, "").strip("/")
            path_parts = rel_path.split("/")
            # 仅排除特殊元文件（如 SKILL.md 直接落在 skills 根目录的情况），
            # 其余一律加载，包括嵌套在组织型技能目录下的"链接/关联"子技能
            if len(path_parts) >= 2 and path_parts[-1] == "SKILL.md":
                skills.append(skill)
        enabled_count = len([skill for skill in skills if skill.get("enabled")])
        data = {
            "total": len(skills),
            "enabled": enabled_count,
            "disabled": len(skills) - enabled_count,
            "skills": skills
        }
        return public.return_data(True, data=data)

    def skill_install(self, get):
        """前端上传 ZIP 安装技能。参数: zip_b64(base64 编码的 zip), overwrite(可选 bool)"""
        import base64
        zip_b64 = str(get.get('zip_b64', '') or '').strip()
        if not zip_b64:
            return public.returnMsg(False, '缺少参数 zip_b64')
        overwrite = str(get.get('overwrite', '')).lower() in ('1', 'true', 'yes')

        # 去除 data URI 前缀（data:application/zip;base64,xxxx）
        if ',' in zip_b64 and zip_b64.strip().startswith('data:'):
            zip_b64 = zip_b64.split(',', 1)[1]

        try:
            zip_bytes = base64.b64decode(zip_b64, validate=False)
        except Exception as e:
            logger.warning("接口异常已脱敏对外返回", exc_info=True)
            return public.returnMsg(False, 'base64 解码失败')

        if not zip_bytes:
            return public.returnMsg(False, '压缩包内容为空')
        if len(zip_bytes) > 30 * 1024 * 1024:
            return public.returnMsg(False, '压缩包超过 30MB 上限')

        result = skill_manager.install_from_zip(zip_bytes, overwrite=overwrite)
        if result.get('status'):
            return public.return_data(True, data={
                'name': result.get('name'),
                'path': result.get('path'),
                'msg': result.get('msg'),
            })
        return public.returnMsg(False, result.get('msg', '安装失败'))

    def skill_uninstall(self, get):
        name = str(get.get('skill_name', '') or '')
        result = skill_manager.uninstall(name)
        return public.returnMsg(bool(result.get('status')), result.get('msg', ''))

    # ---------------- 自定义子代理（CrewAI 风格角色） ----------------

    _CREW_NAME_RE = re.compile(r"^[a-z0-9_\-]{2,40}$")
    _BUILTIN_AGENTS: ClassVar[set] = {"search", "planner", "coder"}

    def _crew_file(self):
        return os.path.join(self.plugin_path, "crew_agents.json")

    _crew_lock = threading.Lock()  # P1-2: 保护 crew JSON 读写，防止并发覆盖

    def _crew_read(self):
        try:
            with self._crew_lock, open(self._crew_file(), "r", encoding="utf-8") as f:
                return json.load(f).get("agents", [])
        except Exception:
            return []

    def _crew_write(self, agents):
        # P1-2: 加锁确保写操作原子性；外层已有 tmp+replace
        with self._crew_lock:
            tmp = self._crew_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"agents": agents}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._crew_file())

    def crew_agents_list(self, get=None):
        from chat_client.tools.task import agent_registry
        customs = {a["name"]: a for a in self._crew_read()}
        infos = []
        for a in agent_registry.list_agents():
            saved = customs.get(a.name, {})
            infos.append({
                "name": a.name,
                "description": a.description,
                "builtin": a.name not in customs,
                "tools": list(a.allowed_tools),
                "department": str(saved.get("department", "")).strip()
                              or getattr(a, "department", ""),
                "backstory": str(saved.get("backstory", "")) if a.name in customs else "",
                "model": str(saved.get("model", "")),
            })
        return public.return_data(True, data={
            "agents": infos,
            "departments": list(getattr(agent_registry, "departments", [])),
        })

    def org_chart(self, get=None):
        """首页集团组织架构：用户 → 经理 → 各部门 → 成员"""
        from chat_client.tools.task import agent_registry
        members = agent_registry.list_agents()
        departments = list(getattr(agent_registry, "departments", []))
        dept_map = {d["name"]: d for d in departments}

        groups = {}
        unassigned = []
        for m in members:
            dep = (m.department or "").strip()
            if dep and dep in dept_map:
                groups.setdefault(dep, []).append(m)
            else:
                unassigned.append(m)

        out_depts = []
        for d in departments:
            ms = groups.get(d["name"], [])
            out_depts.append({
                "name": d["name"],
                "title": d["title"],
                "description": d.get("description", ""),
                "members": [{"name": m.name, "description": m.description,
                             "tools": list(m.allowed_tools)} for m in ms],
            })
        # 综合部成员同样带 tools
        for od in out_depts:
            if od["name"] == "__general__":
                for mm in od["members"]:
                    ag = next((a for a in members if a.name == mm["name"]), None)
                    if ag:
                        mm["tools"] = list(ag.allowed_tools)
        # 未分配成员归入「综合部」
        if unassigned:
            out_depts.append({
                "name": "__general__",
                "title": "综合部",
                "description": "未分配部门的成员",
                "members": [{"name": m.name, "description": m.description} for m in unassigned],
            })
        return public.return_data(True, data={
            "boss": {"name": "user", "title": "用户（会话人）"},
            "manager": {"name": "manager", "title": "经理", "description": "接收目标、拆解计划、分派部门与成员、汇总交付"},
            "departments": out_depts,
        })

    def crew_agent_save(self, get):
        name = str(get.get("name", "")).strip().lower()
        description = str(get.get("description", "")).strip()
        backstory = str(get.get("backstory", "")).strip()
        tools = get.get("tools") or []
        model = str(get.get("model", "")).strip()

        if not self._CREW_NAME_RE.match(name):
            return public.returnMsg(False, "代理名仅允许小写字母/数字/_/-，长度 2~40")
        if name in self._BUILTIN_AGENTS:
            return public.returnMsg(False, "不能覆盖内置代理名")
        if not description:
            return public.returnMsg(False, "缺少 description（角色职责/Goal）")
        if not backstory:
            return public.returnMsg(False, "缺少 backstory（系统提示词）")
        if not isinstance(tools, list) or not tools:
            return public.returnMsg(False, "至少勾选一个工具")

        department = str(get.get("department", "")).strip()
        agents = [a for a in self._crew_read() if a.get("name") != name]
        agents.append({
            "name": name,
            "description": description[:500],
            "backstory": backstory[:8000],
            "tools": [str(t) for t in tools][:60],
            "model": model,
            "department": department,
        })
        try:
            self._crew_write(agents)
        except Exception as e:
            logger.warning("接口异常已脱敏对外返回", exc_info=True)
            return public.returnMsg(False, '写入失败')

        from chat_client.tools.task import reload_custom_agents
        names = reload_custom_agents()
        return public.return_data(True, data={"name": name, "all": names})

    def crew_agent_delete(self, get):
        name = str(get.get("name", "")).strip()
        agents = self._crew_read()
        remaining = [a for a in agents if a.get("name") != name]
        if len(remaining) == len(agents):
            return public.returnMsg(False, f"自定义代理不存在: {name}")
        try:
            self._crew_write(remaining)
        except Exception as e:
            logger.warning("接口异常已脱敏对外返回", exc_info=True)
            return public.returnMsg(False, '写入失败')
        from chat_client.tools.task import reload_custom_agents
        reload_custom_agents()
        return public.return_data(True, data={"deleted": name})

    def crew_dept_save(self, get):
        name = str(get.get("name", "")).strip().lower()
        title = str(get.get("title", "")).strip()
        description = str(get.get("description", "")).strip()
        if not re.fullmatch(r"[a-z0-9_\-]{2,30}", name):
            return public.returnMsg(False, "部门标识仅允许小写字母/数字/_/-，长度 2~30")
        if not title:
            return public.returnMsg(False, "缺少部门名称 title")
        agents = self._crew_read()
        depts = [d for d in (self.__class__.__dict__ and [])] if False else None
        # 读取现有 departments
        try:
            with open(self._crew_file(), "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded.get("departments"), list):
                    depts = loaded["departments"]
        except Exception:
            logger.warning("crew_dept_save 读取失败，已记录", exc_info=True)
        depts = [d for d in depts if d.get("name") != name]
        depts.append({"name": name, "title": title[:40], "description": description[:200]})
        tmp = self._crew_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"departments": depts, "agents": agents}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._crew_file())
        from chat_client.tools.task import reload_custom_agents
        reload_custom_agents()
        return public.return_data(True, data={"name": name})

    def crew_dept_delete(self, get):
        name = str(get.get("name", "")).strip()
        agents = self._crew_read()
        try:
            with open(self._crew_file(), "r", encoding="utf-8") as f:
                depts = json.load(f).get("departments", [])
        except Exception:
            depts = []
        new_depts = [d for d in depts if d.get("name") != name]
        if len(new_depts) == len(depts):
            return public.returnMsg(False, f"部门不存在: {name}")
        tmp = self._crew_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"departments": new_depts, "agents": agents}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._crew_file())
        from chat_client.tools.task import reload_custom_agents
        reload_custom_agents()
        return public.return_data(True, data={"deleted": name})

    def skill_install_url(self, get):
        """从 URL 安装技能。参数: url(直链 zip 或 GitHub 仓库), overwrite(可选 bool)"""
        url = str(get.get('url', '') or '').strip()
        if not url:
            return public.returnMsg(False, '缺少参数 url')
        overwrite = str(get.get('overwrite', '')).lower() in ('1', 'true', 'yes')
        result = skill_manager.install_from_url(url, overwrite=overwrite)
        if result.get('status'):
            return public.return_data(True, data={
                'name': result.get('name'),
                'path': result.get('path'),
                'msg': result.get('msg'),
            })
        return public.returnMsg(False, result.get('msg', '安装失败'))

    def _load_agent_context_prompt(self):
        """
        从 SOUL.md / AGENTS.md / USER.md 组装经理（含集团/系统派发模式）的系统设定，
        取代原来写死在 config/代码中的规则。MEMORY.md 不再全量注入，记忆由 mem0/知识库按需检索。
        文件不存在时返回 None，由调用方回退到 config['system_prompt']。
        """
        ctx_files = {
            'SOUL.md': '身份与人格（SOUL）',
            'AGENTS.md': '行为准则与能力（AGENTS）',
            'USER.md': '关于用户（USER）',
        }
        parts = []
        for fname, title in ctx_files.items():
            fpath = os.path.join(BASE_DIR, fname)
            if os.path.exists(fpath):
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                    if content:
                        parts.append(f"## {title}\n{content}")
                except Exception:
                    logger.warning("system_prompt 模板解析失败，已记录", exc_info=True)
        if not parts:
            return None
        return "\n\n".join(parts)
