"""专家团队实体（对齐 Octop teams/team_manager + TeamService）。

BT 已有集团模式（RunCrew/Task/AgentRegistry，见 chat_client/tools/task.py）。
本模块补上 Octop 那种「持久化团队实体」：
- 团队 = 一个 roster（成员名单）+ 元信息（名称/描述/协调者）
- 存 config.json 的 platform_ext.teams 段（不引入 DB）
- 提供 CRUD + 模板预览 + 成员校验
- 可与 BT 既有 AgentRegistry 成员联动（校验成员是否存在）

不重写协作逻辑：真正执行仍走 BT 既有 RunCrew。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from platform_ext import PROJECT_ROOT

CONFIG_FILE = os.path.join(PROJECT_ROOT, "config.json")

TEAM_MIN_MEMBERS = 2

# 默认团队工作区模板（对齐 Octop teams/template）
DEFAULT_TEMPLATE = {
    "SOUL.md": (
        "# 团队协调者\n\n"
        "你是一个专家团队的协调者（host）。团队成员各有专长，你负责：\n"
        "1. 理解用户（Boss）的目标；\n"
        "2. 拆解为可并行的子任务；\n"
        "3. 分派给最合适的成员；\n"
        "4. 汇总成员产出，以面向 Boss 的成果汇报形式回答。\n"
    ),
    "AGENTS.md": (
        "# 团队协作约定\n\n"
        "- 成员之间通过 Task/ConsultPeer 相互调用；\n"
        "- 协调者只做编排与汇总，具体工作交给成员；\n"
        "- 每步产出要能独立验证。\n"
    ),
    "MEMORY.md": "# 团队记忆\n\n（记录团队协作中的关键决策与踩坑）\n",
}


def _load_config() -> dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            return {}


def _write_config(cfg: dict[str, Any]) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


def _teams() -> list[dict[str, Any]]:
    cfg = _load_config()
    return cfg.setdefault("platform_ext", {}).setdefault("teams", {}).setdefault("teams", [])


def _save_teams(teams: list[dict[str, Any]]) -> None:
    cfg = _load_config()
    cfg.setdefault("platform_ext", {}).setdefault("teams", {})["teams"] = teams
    _write_config(cfg)


def _known_member_names() -> set[str]:
    """从 BT 既有 crew_agents.json + AgentRegistry 收集可用成员名。"""
    names: set[str] = set()
    crew_file = os.path.join(PROJECT_ROOT, "crew_agents.json")
    try:
        with open(crew_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for a in data.get("agents", []):
            if a.get("name"):
                names.add(a["name"])
    except Exception:  # noqa: BLE001
        pass
    # 内置默认子代理
    names.update({"search", "planner", "coder"})
    return names


# ── CRUD ────────────────────────────────────────────────────
def list_teams() -> list[dict[str, Any]]:
    return _teams()


def get_team(team_id: str) -> dict[str, Any] | None:
    for t in _teams():
        if t["team_id"] == team_id:
            return t
    return None


def create_team(name: str, member_ids: list[str], *, description: str = "",
                host_id: str = "") -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("team name is required")
    members = [str(m).strip() for m in (member_ids or []) if str(m).strip()]
    if len(members) < TEAM_MIN_MEMBERS:
        raise ValueError(f"团队至少需要 {TEAM_MIN_MEMBERS} 名成员（当前 {len(members)}）")
    unknown = [m for m in members if m not in _known_member_names()]
    if unknown:
        raise ValueError(f"未知成员: {', '.join(unknown)}（须为 crew_agents.json 中的成员或 search/planner/coder）")
    team = {
        "team_id": uuid.uuid4().hex[:16],
        "name": name,
        "description": description,
        "host_id": host_id or "",
        "member_ids": members,
        "kind": "team",
    }
    teams = _teams()
    teams.append(team)
    _save_teams(teams)
    return team


def update_team(team_id: str, *, name: str | None = None, description: str | None = None,
                member_ids: list[str] | None = None, host_id: str | None = None) -> dict[str, Any] | None:
    teams = _teams()
    for t in teams:
        if t["team_id"] == team_id:
            if name is not None:
                t["name"] = name
            if description is not None:
                t["description"] = description
            if host_id is not None:
                t["host_id"] = host_id
            if member_ids is not None:
                members = [str(m).strip() for m in member_ids if str(m).strip()]
                if len(members) < TEAM_MIN_MEMBERS:
                    raise ValueError(f"团队至少需要 {TEAM_MIN_MEMBERS} 名成员")
                unknown = [m for m in members if m not in _known_member_names()]
                if unknown:
                    raise ValueError(f"未知成员: {', '.join(unknown)}")
                t["member_ids"] = members
            _save_teams(teams)
            return t
    return None


def delete_team(team_id: str) -> bool:
    teams = _teams()
    new = [t for t in teams if t["team_id"] != team_id]
    if len(new) == len(teams):
        return False
    _save_teams(new)
    return True


def team_template() -> dict[str, Any]:
    """返回默认团队工作区文件模板预览。"""
    return {
        "files": DEFAULT_TEMPLATE,
        "min_members": TEAM_MIN_MEMBERS,
    }


def team_roster_text(team_id: str) -> str:
    """生成团队 roster 文本（供 RunCrew 注入提示词）。"""
    t = get_team(team_id)
    if not t:
        return ""
    lines = [f"团队：{t['name']}", f"成员（{len(t['member_ids'])}）："]
    for m in t["member_ids"]:
        lines.append(f"- {m}")
    return "\n".join(lines)