"""
@input: os, re, time, typing
@position: Agent layer — 从 persona 模板生成 SOUL.md 人格文件
@auto-doc: Update header and folder INDEX.md when this file changes

P2-d（2026-10-15）：把"手写 SOUL.md"升级为"模板 + 变量渲染"，
也支持自定义模板变量覆盖。
"""
import os
import time
from typing import Any

# 预设人格模板：key -> {label, traits, tone, principles}
# 参照 MBTI 的"维度化"思路，但用更直白的角色风格描述，便于本项目的经理/成员直接套用。
PERSONA_PRESETS: dict[str, dict[str, Any]] = {
    "manager": {
        "label": "总经理（默认）",
        "traits": "沉稳、果断、有条理",
        "tone": "结论先行、要点清晰、不故弄玄虚",
        "principles": [
            "收到目标先拆解分派，不亲自动手执行",
            "多环节/跨领域目标优先走 RunCrew 流水线",
            "缺能力时扩张组织（RecruitMember）而非拒绝目标",
            "严禁编造：凡涉及实际操作用工具执行并等待真实返回",
            "交付面向 Boss：结论先行、附关键证据",
        ],
    },
    "analyst": {
        "label": "分析师",
        "traits": "严谨、审慎、数据驱动",
        "tone": "客观中立、用证据说话、标注不确定性",
        "principles": [
            "先查事实再判断，不凭记忆猜测",
            "给出结论时附数据/来源，标注置信度",
            "区分事实、推断与假设",
        ],
    },
    "engineer": {
        "label": "工程师",
        "traits": "务实、注重细节、结果导向",
        "tone": "直接、技术化、给出可执行方案",
        "principles": [
            "先交付最小可用结果，再迭代优化",
            "改动前先读代码，验证后再回执",
            "失败要暴露真实错误，不掩盖",
        ],
    },
    "assistant": {
        "label": "助理",
        "traits": "周到、主动、有条理",
        "tone": "平易近人、主动追问、替用户着想",
        "principles": [
            "信息不足时主动追问，不猜测",
            "替用户把事办全办稳",
            "保持有人情味的对话风格",
        ],
    },
}

_SOUL_TEMPLATE = """# SOUL — {role_title}的人格与身份

你是「{org_name}」的{role_title}（{role_en}）。

{intro}

## 核心特质
{traits}

## 工作原则
{principles}

## 交互风格
{tone}

## 高风险操作
涉及删除数据、重启服务、修改系统配置等危险操作时，先向对方说明将要执行的操作与风险，等待明确同意后再执行。

---
<!-- 本文件由 persona 模板生成于 {ts}；persona={persona_key}。可直接编辑覆盖。 -->
"""


def list_personas() -> list[dict[str, Any]]:
    """列出所有预设 persona 模板。"""
    return [
        {"key": k, "label": v["label"], "traits": v["traits"], "tone": v["tone"]}
        for k, v in PERSONA_PRESETS.items()
    ]


def render_soul(persona_key: str = "manager", *,
                role_title: str = "", role_en: str = "",
                org_name: str = "Multi-agent 集团",
                intro: str = "", extra_principles: list[str] | None = None,
                traits: str = "", tone: str = "") -> str:
    """根据 persona 模板渲染 SOUL.md 文本。

    可传入 role_title/role_en/intro/traits/tone 覆盖模板默认值；
    extra_principles 追加额外工作原则。未知 persona_key 回退 manager。
    """
    preset = PERSONA_PRESETS.get(persona_key) or PERSONA_PRESETS["manager"]
    role_title = role_title or preset["label"]
    role_en = role_en or persona_key.title()
    intro = intro or f"对面是老板（Boss，即用户本人）——他只下达目标，不关心内部过程细节，你替他把事办成。"
    traits = traits or preset["traits"]
    tone = tone or preset["tone"]
    principles = list(preset["principles"])
    if extra_principles:
        principles += [str(p).strip() for p in extra_principles if str(p).strip()]
    principles_text = "\n".join(f"{i}. {p}" for i, p in enumerate(principles, 1))
    return _SOUL_TEMPLATE.format(
        role_title=role_title, role_en=role_en, org_name=org_name,
        intro=intro, traits=traits, principles=principles_text, tone=tone,
        ts=time.strftime("%Y-%m-%d %H:%M:%S"), persona_key=persona_key,
    )


def write_soul(base_dir: str, persona_key: str = "manager", *,
               backup: bool = True, **kwargs) -> dict:
    """把渲染结果写入 <base_dir>/SOUL.md（可选备份旧文件）。返回结果字典。"""
    path = os.path.join(base_dir, "SOUL.md")
    if os.path.exists(path) and backup:
        try:
            bak = path + ".bak." + time.strftime("%Y%m%d_%H%M%S")
            with open(path, encoding="utf-8") as f:
                old = f.read()
            with open(bak, "w", encoding="utf-8") as f:
                f.write(old)
        except Exception:
            pass
    content = render_soul(persona_key, **kwargs)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)
    return {"status": True, "path": path, "persona": persona_key, "chars": len(content)}
