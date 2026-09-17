import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from . import PROJECT_ROOT, emit_progress, register_tool
from .base import _xml_response


@dataclass
class AgentDefinition:
    name: str
    description: str
    allowed_tools: list[str]
    system_prompt_template: str
    department: str = ""   # 所属部门名（空=未分配）
    skills: list[str] = None   # 该角色专属预加载技能（运行时自动注入系统提示词）

class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, AgentDefinition] = {}
        self.departments: list[dict[str, Any]] = []   # [{name, title, description}]
        self._register_default_agents()
        self.load_custom_agents()

    def register(self, agent: AgentDefinition):
        self._agents[agent.name] = agent

    def load_agent_skills_block(self, agent_def: "AgentDefinition") -> str:
        """
        把某角色专属的预加载技能（agent_def.skills）内容拼成文本块，
        用于注入到该角色的系统提示词，使其开局即"带上"专属技能，无需再调 Skills 工具。
        """
        names = agent_def.skills or []
        if not names:
            return ""
        from ..skills import skill_manager
        blocks = []
        for nm in names:
            nm = str(nm).strip()
            if not nm:
                continue
            sk = skill_manager.get_enabled(nm)
            if not sk:
                continue
            skill_dir = os.path.dirname(sk.location)
            files = skill_manager.list_files(skill_dir)
            file_list = "\n".join(f"<file>{f}</file>" for f in files)
            blocks.append(
                f'<skill_content name="{sk.name}">\n'
                f'# Skill: {sk.name}\n\n{sk.content.strip()}\n\n'
                f'Base directory: {skill_dir}\n'
                f'Relative paths in this skill (e.g. scripts/, reference/) are relative to this base directory.\n'
                f'<skill_files>\n{file_list}\n</skill_files>\n'
                f'</skill_content>'
            )
        if not blocks:
            return ""
        return (
            "\n\n[已为你的角色预加载以下专属技能——直接遵循其中的工作流与脚本，"
            "无需再用 Skills 工具加载]\n" + "\n\n".join(blocks)
        )

    def get(self, name: str) -> AgentDefinition | None:
        return self._agents.get(name)

    def list_agents(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    # 自定义角色代理（CrewAI 风格 Role/Goal/Backstory/Tools）持久化文件
    CUSTOM_AGENTS_FILE = os.environ.get(
        "AI_AGENT_CREW_AGENTS",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "crew_agents.json"),
    )

    def load_custom_agents(self):
        """从 crew_agents.json 加载用户自定义角色代理，覆盖同名内置角色"""
        import json as _json
        try:
            if not os.path.exists(self.CUSTOM_AGENTS_FILE):
                return
            data = _json.load(open(self.CUSTOM_AGENTS_FILE, encoding="utf-8"))
            depts = data.get("departments", [])
            if isinstance(depts, list):
                self.departments = [
                    {"name": str(d.get("name","")).strip(),
                     "title": str(d.get("title","")).strip() or str(d.get("name","")).strip(),
                     "description": str(d.get("description","")).strip()}
                    for d in depts if isinstance(d, dict) and str(d.get("name","")).strip()
                ]
            for item in data.get("agents", []):
                name = str(item.get("name", "")).strip()
                if not name or len(name) > 64:
                    continue
                self.register(AgentDefinition(
                    name=name,
                    description=str(item.get("description", ""))[:500],
                    allowed_tools=list(item.get("tools", [])),
                    system_prompt_template=str(item.get("backstory", "")),
                    department=str(item.get("department", "")).strip(),
                    skills=list(item.get("skills", [])) or None,
                ))
        except Exception as e:
            print(f"[Crew] 加载自定义代理失败: {e}")

    def _register_default_agents(self):
        # Search Agent
        self.register(AgentDefinition(
            name="search",
            description="A specialist for searching the codebase and file system. Use this for exploration and information gathering.",
            allowed_tools=["Glob", "Grep", "LS", "Read", "CheckCommandStatus", "RunCommand"],
            system_prompt_template="You are a search specialist. Your goal is to find information in the codebase efficiently. Use Glob and Grep tools to locate files and content. Use Read to inspect file contents. Do not modify files.",
            department="research"
        ))

        # Planner Agent
        self.register(AgentDefinition(
            name="planner",
            description="A specialist for planning tasks and managing todo lists.",
            allowed_tools=["TodoWrite", "Read", "Task"],
            system_prompt_template="You are a planner. Your goal is to break down complex tasks into manageable steps. Use the TodoWrite tool to manage the task list. You can delegate subtasks to other agents using the Task tool."
        ))

        # Coder Agent
        self.register(AgentDefinition(
            name="coder",
            description="A specialist for writing and modifying code.",
            allowed_tools=["Glob", "Grep", "LS", "Read", "Write", "DeleteFile", "SearchReplace", "RunCommand", "CheckCommandStatus", "StopCommand", "Task"],
            system_prompt_template="You are a coding specialist. Your goal is to implement features and fix bugs. You can read and write files. You can also run commands to verify your work. If you need to search extensively, delegate to the search agent.",
            department="dev"
        ))

# Global registry instance
agent_registry = AgentRegistry()


def reload_custom_agents():
    """重新加载 crew_agents.json 中的自定义角色代理与部门（供配置保存后热更新）"""
    agent_registry._agents.clear()
    agent_registry.departments = []
    agent_registry._register_default_agents()
    agent_registry.load_custom_agents()
    return [a.name for a in agent_registry.list_agents()]


@register_tool(category="Agent", name_cn="Task子代理", risk_level="medium", timeout=1800)
def Task(description: str, prompt: str, subagent_type: str, task_id: str | None = None,
         expected_output: str = "", model: str = "", **kwargs) -> str:
    """
    Launch a sub-agent to handle a complex task autonomously.
    
        Args:
        description: A short description of the task.
        prompt: The detailed instructions for the agent.
        subagent_type: The type of agent to use (内置: search/planner/coder；亦可用设置中自定义的角色代理).
        task_id: Optional ID to resume a previous task session.
        expected_output: Optional. 期望的输出格式/内容要求（将作为硬约束注入子代理）。
        model: Optional. 指定该子任务使用的模型；留空继承父会话模型。
    
    
Launch a new agent to handle complex, multistep tasks autonomously.

Available agent types and the tools they have access to:
["search", "planner", "coder"]

When using the Task tool, you must specify a subagent_type parameter to select which agent type to use.

When to use the Task tool:
- When you are instructed to execute custom slash commands. Use the Task tool with the slash command invocation as the entire prompt. The slash command can take arguments. For example: Task(description="Check the file", prompt="/check-file path/to/file.py")

When NOT to use the Task tool:
- If you want to read a specific file path, use the Read or Glob tool instead of the Task tool, to find the match more quickly
- If you are searching for a specific class definition like "class Foo", use the Glob tool instead, to find the match more quickly
- If you are searching for code within a specific file or set of 2-3 files, use the Read tool instead of the Task tool, to find the match more quickly
- Other tasks that are not related to the agent descriptions above


Usage notes:
1. Launch multiple agents concurrently whenever possible, to maximize performance; to do that, use a single message with multiple tool uses
2. When the agent is done, it will return a single message back to you. The result returned by the agent is not visible to the user. To show the user the result, you should send a text message back to the user with a concise summary of the result. The output includes a task_id you can reuse later to continue the same subagent session.
3. Each agent invocation starts with a fresh context unless you provide task_id to resume the same subagent session (which continues with its previous messages and tool outputs). When starting fresh, your prompt should contain a highly detailed task description for the agent to perform autonomously and you should specify exactly what information the agent should return back to you in its final and only message to you.
4. The agent's outputs should generally be trusted
5. Clearly tell the agent whether you expect it to write code or just to do research (search, file reads, web fetches, etc.), since it is not aware of the user's intent. Tell it how to verify its work if possible (e.g., relevant test commands).
6. If the agent description mentions that it should be used proactively, then you should try your best to use it without the user having to ask for it first. Use your judgement.

Example usage (NOTE: The agents below are fictional examples for illustration only - use the actual agents listed above):

<example_agent_descriptions>
"code-reviewer": use this agent after you are done writing a significant piece of code
"greeting-responder": use this agent when to respond to user greetings with a friendly joke
</example_agent_description>

<example>
user: "Please write a function that checks if a number is prime"
assistant: Sure let me write a function that checks if a number is prime
assistant: First let me use the Write tool to write a function that checks if a number is prime
assistant: I'm going to use the Write tool to write the following code:
<code>
function isPrime(n) {
  if (n <= 1) return false
  for (let i = 2; i * i <= n; i++) {
    if (n % i === 0) return false
  }
  return true
}
</code>
<commentary>
Since a significant piece of code was written and the task was completed, now use the code-reviewer agent to review the code
</commentary>
assistant: Now let me use the code-reviewer agent to review the code
assistant: Uses the Task tool to launch the code-reviewer agent
</example>

<example>
user: "Hello"
<commentary>
Since the user is greeting, use the greeting-responder agent to respond with a friendly joke
</commentary>
assistant: "I'm going to use the Task tool to launch the with the greeting-responder agent"
</example>
    """
    
    # Deferred import to avoid circular dependency
    from ..agent import Agent

    # 1. Validate Agent Type
    agent_def = agent_registry.get(subagent_type)
    if not agent_def:
        available = [a.name for a in agent_registry.list_agents()]
        return _xml_response("error", f"Unknown agent type: '{subagent_type}'. Available agents: {', '.join(available)}")

    # 2. Session Management
    if task_id:
        session_id = task_id
    else:
        # Create new session ID
        session_id = str(uuid.uuid4())

    # 3. Configure Agent
    # We need to construct a config that enables the specific tools for this agent
    # and sets the system prompt.
    
    # 默认工作目录：项目根目录
    cwd = PROJECT_ROOT
    
    # Get parent config if available
    parent_config = kwargs.get("parent_config", {})
    parent_session_id = kwargs.get("parent_session_id")
    
    # Start with default base config
    config = {
        "model_name": "", # 由 parent_config 继承实际模型
        "cwd": cwd,
        "code_mode": True, 
        "max_tool_iterations": 20
    }
    
    # Merge parent config (if any), but be careful not to overwrite critical agent-specific fields yet
    if parent_config:
        # Update config with parent config, but exclude 'tools' and 'system_prompt' which are specific to the subagent
        # We also want to preserve 'cwd' if parent has it
        for k, v in parent_config.items():
            if k not in ["tools", "system_prompt"]:
                config[k] = v
        
        # Determine sessions_dir
        # If we have a parent session, the sub-agent session should be stored inside it
        if parent_session_id:
            parent_sessions_dir = parent_config.get("sessions_dir", "sessions")
            # Structure: sessions/parent_id
            # The Agent class will append its session_id: sessions/parent_id/sub_id
            # So we set sessions_dir to: sessions/parent_id
            config["sessions_dir"] = os.path.join(parent_sessions_dir, parent_session_id)
    
    # Force agent-specific configuration
    _sp = agent_def.system_prompt_template
    _skills_block = agent_registry.load_agent_skills_block(agent_def)
    if _skills_block:
        _sp += "\n\n" + _skills_block
    config.update({
        "tools": agent_def.allowed_tools,
        "system_prompt": _sp
    })

    # CrewAI 式约束：可选独立模型覆盖（expected_output 在 full_prompt 中注入）
    if str(model).strip():
        config["model_name"] = str(model).strip()

    # 4. Initialize Agent
    agent = Agent(session_id=session_id, config=config)
    
    try:
        # 5. Execute Agent Loop
        # Agent.chat is a generator. We need to consume it to let the agent run.
        # We collect the final response content.
        
        full_response = ""
        
        # Add a clear instruction to the prompt
        full_prompt = f"Task: {description}\n\nInstructions:\n{prompt}"
        if expected_output:
            full_prompt += f"\n\n[Expected Output] {expected_output.strip()}"
        
        generator = agent.chat(full_prompt)
        
        for chunk in generator:
            if chunk.get("type") == "content":
                full_response += chunk.get("response", "")
            elif chunk.get("type") == "error":
                return _xml_response("error", f"Agent error: {chunk.get('data')}")
                
        # 6. Return Result
        output = [
            f"task_id: {session_id}",
            "",
            "<task_result>",
            full_response,
            "</task_result>"
        ]
        
        return _xml_response("done", "\n".join(output))

    except Exception as e:
        return _xml_response("error", f"Task execution failed: {e!s}")
    finally:
        # Clean up agent resources
        if hasattr(agent, "close"):
            agent.close()


# ============================================================
# 集团模式（hierarchical）：经理规划 → 专家团队执行 → 汇总
# ============================================================

import openai as _openai

_CREW_MANAGER_PROMPT = """你是多智能体团队的调度经理（Manager）。发话的是老板（Boss，即用户本人）——他只下达目标，不参与过程细节。根据老板的「总体目标」与「可用团队成员」的能力说明，制定一份顺序执行计划，替老板把事办成。

可用团队成员：
{roster}

总体目标：
{objective}

 要求：
 1. 只输出一个 JSON 数组，不要任何其他文字或代码块标记。
 2. 数组每项格式：{{"agent": "成员名", "task": "该成员要完成的具体任务描述", "expected_output": "期望产出说明", "depends_on": [依赖的步骤序号数组(1-based)，无依赖填 [] 或省略]}}
 3. 最多 {max_steps} 步；用 depends_on 表达依赖（如步骤3依赖步骤1的结果则 depends_on:[1]）；无依赖的步骤会并发执行，有依赖的会等依赖步骤完成后再并发；目标简单时允许只有一步；成员名必须来自上面的列表。
 """


def _load_manager_context():
    """
    从 SOUL.md / AGENTS.md / USER.md / MEMORY.md 组装经理（集团模式）的系统设定，
    取代原来写死在代码中的 _CREW_MANAGER_PROMPT。文件不存在时返回 None，
    由调用方回退到硬编码的 _CREW_MANAGER_PROMPT。
    """
    ctx_files = {
        'SOUL.md': '身份与人格（SOUL）',
        'AGENTS.md': '行为准则与能力（AGENTS）',
        'USER.md': '关于用户（USER）',
        'MEMORY.md': '记忆与偏好（MEMORY）',
    }
    parts = []
    for fname, title in ctx_files.items():
        fpath = os.path.join(PROJECT_ROOT, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if content:
                    parts.append(f"## {title}\n{content}")
            except Exception:
                logger.warning("未处理的异常", exc_info=True)
    if not parts:
        return None
    return "你是 AI 运维助手「经理」，以下为你的核心设定，请严格遵守：\n\n" + "\n\n".join(parts)


def _crew_make_client(parent_config: dict[str, Any]):
    return _openai.OpenAI(
        api_key=parent_config.get("api_key"),
        base_url=parent_config.get("base_url"),
        default_headers=parent_config.get("default_headers") or {},
    )


# 仅用于注入的「动态变量」模板（规则本身全部在 SOUL/AGENTS/USER/MEMORY 文件中）
_CREW_STRUCTURAL_SLOTS = (
    "\n\n可用团队成员：\n{roster}\n\n"
    "总体目标：\n{objective}\n\n"
    "（最多 {max_steps} 步；每项可含 depends_on 表达依赖（步骤序号1-based，无依赖填 [] 或省略），无依赖的步骤将并发执行，有依赖的等其依赖完成后并发；成员名必须来自上面的列表。）"
)


def _crew_manager_plan(parent_config, objective: str, roster_text: str, max_steps: int):
    """经理第一步：生成 JSON 执行计划。返回 (steps, err)"""
    client = _crew_make_client(parent_config)
    ctx = _load_manager_context()
    if ctx:
        # 文件优先：用 SOUL/AGENTS/USER/MEMORY 组装设定，再追加动态变量占位符并注入
        prompt = (ctx + _CREW_STRUCTURAL_SLOTS)\
            .replace("{roster}", roster_text)\
            .replace("{objective}", objective)\
            .replace("{max_steps}", str(max_steps))
    else:
        prompt = _CREW_MANAGER_PROMPT.format(roster=roster_text, objective=objective, max_steps=max_steps)
    resp = client.chat.completions.create(
        model=parent_config.get("model_name"),
        messages=[
            {"role": "system", "content": "你是严谨的多智能体任务调度器，只输出合法 JSON 数组。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        timeout=120,
    )
    text = (resp.choices[0].message.content or "").strip()
    # 剥离可能的 ```json 包裹，截取首个 [ 到最后一个 ]
    l, r = text.find("["), text.rfind("]")
    if l == -1 or r == -1 or r <= l:
        # 兜底：经理返回单个 JSON 对象（非数组），自动包裹为数组
        try:
            steps = json.loads(text)
            if isinstance(steps, dict):
                steps = [steps]
            else:
                return None, f"经理输出无法解析为 JSON 计划（非对象非数组）：{text[:200]}"
        except Exception:
            return None, f"经理输出无法解析为 JSON 计划：{text[:200]}"
    else:
        try:
            steps = json.loads(text[l:r + 1])
        except Exception as e:
            return None, f"计划 JSON 解析失败: {e}"
    if not isinstance(steps, list) or not steps:
        return None, "计划为空"
    return steps[:max_steps], None


def _crew_synthesis(parent_config, objective: str, transcript: list[dict[str, str]]):
    """经理最后一步：汇总各成员产出为最终答复"""
    client = _crew_make_client(parent_config)
    lines = []
    for i, t in enumerate(transcript, 1):
        lines.append(f"【步骤{i} · {t['agent']}】任务：{t['task']}\n产出摘要：{t['output'][:1500]}")
    resp = client.chat.completions.create(
        model=parent_config.get("model_name"),
        messages=[
            {"role": "system", "content": "你是团队负责人。老板只关心结果——基于各成员产出整合成面向老板的最终成果汇报：直接给结论与关键信息，简洁、可执行，不复述内部过程。"},
            {"role": "user", "content": f"老板的目标：{objective}\n\n各步骤产出：\n" + "\n\n".join(lines)},
        ],
        temperature=0.4,
        timeout=180,
    )
    return resp.choices[0].message.content or ""


@register_tool(category="Agent", name_cn="集团模式(经理+团队)", risk_level="medium", timeout=1800)
def RunCrew(objective: str, agents: list[str] | None = None, max_steps: int = 5, parallel: bool = True, **kwargs) -> str:
    """
    启动内置「一人一集团」层级协作模式：用户是 Boss，只负责下达目标；
    AI 侧自动组成集团——经理拆解计划、专家团队成员并发执行（默认并行，真正提速），
    最后以面向老板的成果汇报形式返回最终答案。

    适用：多环节复杂任务（调研→分析→产出）、需要多种专长协作的目标。

    Args:
        objective: 总体目标描述（要达成什么）。
        agents: 可选。限定参与的团队成员名单（内置 search/planner/coder 及自定义角色）；留空则全部可用成员参与。
        max_steps: 计划最大步数，默认 5（上限 10）。
        parallel: 是否启用依赖感知的并发（默认 True）。启用时按各步 depends_on 计算拓扑层级，
            同层（无相互依赖）的步骤并发执行，依赖的步骤等其依赖全部完成后再并发；所依赖步骤的产出会注入为上下文。
            设为 False 则退化为逐条串行（每步注入其依赖产出）。
    """
    from ..agent import Agent

    parent_config = kwargs.get("parent_config", {}) or {}
    parent_session_id = kwargs.get("parent_session_id")

    _roster_all = agent_registry.list_agents()
    # 兼容 LLM 乱传 agents 参数的多种格式：
    #   - list[str]  (正确格式)
    #   - 逗号分隔字符串 "search, coder"
    #   - JSON 数组字符串 '["search", "coder"]'
    #   - 字面量 "null"/"" (视为不过滤)
    if isinstance(agents, str):
        agents = agents.strip()
        if agents.lower() in ("null", ""):
            agents = []
        else:
            import json as _json
            try:
                agents = _json.loads(agents)
            except Exception:
                agents = [x.strip() for x in agents.split(",") if x.strip()]
            if not isinstance(agents, list):
                agents = []
    roster = [a for a in _roster_all
              if not agents or a.name in set(agents)]
    if not roster:
        return _xml_response("error", "没有可用的团队成员")

    # 排除自身与 planner 的递归 Task，避免失控嵌套
    roster = [a for a in roster if a.name != "planner"]
    # 按部门分组呈现，经理可感知组织结构
    dept_title = {d["name"]: d["title"] for d in getattr(agent_registry, "departments", [])}
    groups = {}
    for a in roster:
        key = a.department or "综合部"
        groups.setdefault(key, []).append(a)
    parts = []
    for dn, members in groups.items():
        parts.append(f"【{dept_title.get(dn, dn)}】")
        for a in members:
            parts.append(f"- {a.name}：{a.description}（工具: {', '.join(a.allowed_tools[:8])}）")
    roster_text = "\n".join(parts)

    max_steps = min(max(int(max_steps or 5), 1), 10)

    # ---- 第一步：经理制定计划 ----
    steps, err = _crew_manager_plan(parent_config, objective, roster_text, max_steps)
    if err:
        return _xml_response("error", err)

    # 实时推送计划到前端
    dept_of = {a.name: a.department for a in agent_registry.list_agents()}
    dept_title = {d["name"]: d["title"] for d in getattr(agent_registry, "departments", [])}
    plan_summary = []
    for i, st in enumerate(steps):
        sn = str(st.get("agent","")).strip()
        plan_summary.append({
            "agent": sn, "dept": dept_title.get(dept_of.get(sn,""), sn),
            "task": str(st.get("task",""))[:80], "step": i+1, "status": "pending",
        })
    emit_progress("crew_plan", {"steps": plan_summary, "objective": objective[:120]})

    valid_names = {a.name for a in roster}
    plan_lines = []
    transcript = []

    # ---- 并行/顺序执行各步（默认并行：独立子任务并发运行，真正提速）----
    def _run_one_step(i, step, deps=None):
        sname = str(step.get("agent", "")).strip()
        stask = str(step.get("task", "")).strip()
        seout = str(step.get("expected_output", "")).strip()
        if sname not in valid_names:
            return i, sname or "?", stask, "[跳过] 成员不存在"
        agent_def = agent_registry.get(sname)
        session_id = str(uuid.uuid4())

        config = {
            "model_name": "",
            "cwd": PROJECT_ROOT,
            "code_mode": True,
            "max_tool_iterations": 15,
        }
        for k, v in parent_config.items():
            if k not in ("tools", "system_prompt"):
                config[k] = v
        if parent_session_id:
            base_dir = parent_config.get("sessions_dir", os.path.join(PROJECT_ROOT, "sessions"))
            config["sessions_dir"] = os.path.join(base_dir, parent_session_id)
            # 写 meta.json 标记来源，供历史列表分类使用
            try:
                meta = {
                    "source": "crew",
                    "parent": parent_session_id,
                    "agent": sname,
                    "dept": dept_title.get(agent_def.department, agent_def.department or "综合部"),
                }
                # 写入 meta.json：优先写入子代理目录（同 sessions.json 同级）
                meta_dir = os.path.join(config["sessions_dir"], session_id)
                os.makedirs(meta_dir, exist_ok=True)
                with open(os.path.join(meta_dir, "meta.json"), "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        # 协作原语自动注入（所有 crew 成员都能咨询同事）
        member_tools = list(agent_def.allowed_tools)
        if "ConsultPeer" not in member_tools:
            member_tools.append("ConsultPeer")
        config["tools"] = member_tools
        # 成员感知团队与部门
        _dept_of = {a.name: a.department for a in agent_registry.list_agents()}
        my_dept = dept_title.get(agent_def.department, agent_def.department or "综合部")

        sp = agent_def.system_prompt_template
        sp += f"\n\n[你的部门] {my_dept}"

        # 同部门同事
        peers = [a.name for a in agent_registry.list_agents()
                 if a.department == agent_def.department and a.name != sname]
        if peers:
            sp += f"\n[同部门同事] {', '.join(peers)}（可用 ConsultPeer 快速咨询）"

        # 专属预加载技能（开局即带，无需再调 Skills 工具）
        _skills_block = agent_registry.load_agent_skills_block(agent_def)
        if _skills_block:
            sp += "\n\n" + _skills_block

        # 依赖感知：仅注入所依赖步骤（必已先于本步完成）的产出作为上下文
        if deps:
            dep_ctx = []
            for di in sorted(deps):
                _d = _done.get(di)
                if not _d:
                    continue
                _dname, _dtask, _dout = _d
                t_dept = _dept_title.get(_dept_of.get(_dname, ""), "")
                dep_ctx.append(f"[{t_dept or '未分部'} · {_dname}] 任务:{_dtask}\n产出:{_dout[:1200]}")
            if dep_ctx:
                sp += "\n\n[依赖步骤产出（请衔接，不要重复）]\n" + "\n---\n".join(dep_ctx)
        config["system_prompt"] = sp

        full_prompt = f"Task: {stask}\nInstructions:\n{stask}"
        if seout:
            full_prompt += f"\n\n[Expected Output] {seout}"

        # 推送步骤开始
        emit_progress("crew_step", {"agent": sname, "dept": _dept_title.get(_dept_of.get(sname, ""), sname),
                                     "status": "running", "step": i, "task": stask[:60]})

        agent = Agent(session_id=session_id, config=config)
        step_out = ""
        try:
            for chunk in agent.chat(full_prompt):
                if chunk.get("type") == "content":
                    step_out += chunk.get("response", "")
                elif chunk.get("type") == "error":
                    step_out = f"[执行错误] {chunk.get('data')}"
                    break
        except Exception as e:
            step_out = f"[执行异常] {e!s}"
        finally:
            try:
                agent.close()
            except Exception:
                logger.warning("未处理的异常", exc_info=True)
        # 推送步骤完成
        emit_progress("crew_step", {"agent": sname, "dept": _dept_title.get(_dept_of.get(sname, ""), sname),
                                     "status": "done", "step": i})

        return i, sname, stask, step_out

    # 1) 规整每步 depends_on（步骤序号或成员名，1-based；空/无=无依赖）
    _name_to_idx = {str(s.get("agent", "")).strip(): i for i, s in enumerate(steps, 1)}
    _deps = {}
    for i, step in enumerate(steps, 1):
        raw = step.get("depends_on") or step.get("depends") or []
        if isinstance(raw, str):
            raw = [raw]
        deps = set()
        for d in raw:
            d = str(d).strip()
            if d in ("", "无", "none", "null", "[]", "无依赖"):
                continue
            if d.isdigit():
                di = int(d)
            elif d in _name_to_idx:
                di = _name_to_idx[d]
            else:
                _m = re.search(r"\d+", d)
                di = int(_m.group()) if _m else None
            if di and 1 <= di < i:  # 仅允许依赖更早步骤，避免自环/后向边
                deps.add(di)
        _deps[i] = deps

    # 2) 计算拓扑层级（wave 等级）；含环则降级为全部并行（level=0）
    _level = {}
    def _calc(i):
        if i in _level:
            return _level[i]
        ds = _deps.get(i, set())
        if not ds:
            _level[i] = 0
            return 0
        _level[i] = 0
        _level[i] = 1 + max(_calc(x) for x in ds)
        return _level[i]
    try:
        for i in range(1, len(steps) + 1):
            _calc(i)
    except (RecursionError, ValueError):
        _level = {i: 0 for i in range(1, len(steps) + 1)}

    # 3) 按 wave 分批并发（同 level 并发；依赖必在更早 wave 完成）；parallel=False 退化为逐条串行
    # 捕获当前线程的 job，传给 worker 线程以便子步骤 emit_progress 能推到 SSE
    from chat_client.tools import get_current_job, _restore_job_ctx
    _ctx_job = get_current_job()
    _done = {}
    _remaining = set(range(1, len(steps) + 1))
    if parallel:
        from concurrent.futures import ThreadPoolExecutor
        _MAX_WORKERS = 5
        while _remaining:
            _cur = min(_level[i] for i in _remaining)
            _wave = sorted([i for i in _remaining if _level[i] == _cur])
            with ThreadPoolExecutor(max_workers=min(len(_wave), _MAX_WORKERS)) as _ex:
                def _submit_with_job(args, _job=_ctx_job):
                    _restore_job_ctx(_job)
                    return _run_one_step(*args)
                _futs = {_ex.submit(_submit_with_job, (i, steps[i - 1], _deps[i])): i for i in _wave}
                for _fut, i in _futs.items():
                    _ri, _rsn, _rst, _rso = _fut.result()
                    _done[i] = (_rsn, _rst, _rso)
                    transcript.append({"agent": _rsn, "task": _rst, "output": _rso})
                    plan_lines.append(f"步骤{_ri} [{_rsn}] {_rst} → 完成({len(_rso)} 字)")
            _remaining -= set(_wave)
    else:
        # 串行：每步跑完立即累积 _done，后续步骤可衔接其依赖产出
        for _i, _step in enumerate(steps, 1):
            _ri, _rsn, _rst, _rso = _run_one_step(_i, _step, _deps[_i])
            _done[_i] = (_rsn, _rst, _rso)
            transcript.append({"agent": _rsn, "task": _rst, "output": _rso})
            plan_lines.append(f"步骤{_ri} [{_rsn}] {_rst} → 完成({len(_rso)} 字)")

    # ---- 最后：经理汇总 ----
    emit_progress("crew_step", {"agent": "manager", "dept": "经理", "status": "running", "step": "汇总"})
    final = _crew_synthesis(parent_config, objective, transcript)
    emit_progress("crew_step", {"agent": "manager", "dept": "经理", "status": "done", "step": "汇总"})

    report = [
        "<crew_report>",
        "<plan>",
        *plan_lines,
        "</plan>",
        "<final_answer>",
        final,
        "</final_answer>",
        "</crew_report>",
    ]
    return _xml_response("done", "\n".join(report))


@register_tool(category="Agent", name_cn="咨询同事", risk_level="low", timeout=300)
def ConsultPeer(target_agent: str, question: str, context: str = "", **kwargs) -> str:
    """
    向团队中另一位成员发起快速咨询（不创建子任务会话）。
    用于跨部门协作：比如研发部想确认调研部的发现、运维部想问财务部成本数据。

    Args:
        target_agent: 目标成员名（如 researcher / coder / sys-ops）
        question: 要咨询的具体问题
        context: 可选。补充背景信息帮助对方理解
    """
    import uuid as _uuid

    from ..agent import Agent

    agent_def = agent_registry.get(target_agent)
    if not agent_def:
        avail = [a.name for a in agent_registry.list_agents()]
        return _xml_response("error", f"找不到成员: {target_agent}。可用: {', '.join(avail)}")

    parent_config = kwargs.get("parent_config", {}) or {}
    parent_session_id = kwargs.get("parent_session_id")

    config = {
        "model_name": "",
        "cwd": PROJECT_ROOT,
        "code_mode": True,
        "max_tool_iterations": 8,
    }
    for k, v in parent_config.items():
        if k not in ("tools", "system_prompt"):
            config[k] = v
    if parent_session_id:
        config["sessions_dir"] = os.path.join(
            parent_config.get("sessions_dir", "sessions"), parent_session_id
        )
    config["tools"] = agent_def.allowed_tools
    _sp = agent_def.system_prompt_template
    _skills_block = agent_registry.load_agent_skills_block(agent_def)
    if _skills_block:
        _sp += "\n\n" + _skills_block
    config["system_prompt"] = (
        _sp
        + "\n\n[当前模式] 你正在接受一位同事的快速咨询，请简洁精准地回答，不需要展开执行任务。"
    )

    full_prompt = f"【来自同事的咨询】\n{question}"
    if context:
        full_prompt += f"\n\n【背景】{context[:2000]}"

    agent = Agent(session_id=f"consult_{_uuid.uuid4().hex[:8]}", config=config)
    try:
        result = ""
        for chunk in agent.chat(full_prompt):
            if chunk.get("type") == "content":
                result += chunk.get("response", "")
            elif chunk.get("type") == "error":
                return _xml_response("error", f"{target_agent} 回复异常: {chunk.get('data')}")
    except Exception as e:
        return _xml_response("error", f"咨询失败: {e!s}")
    finally:
        try:
            agent.close()
        except Exception:
            logger.warning("未处理的异常", exc_info=True)
    return _xml_response("done", f"[{target_agent} 的回复]\n{result}")


# ============================================================
# 组织自我扩张：经理/Boss 可直接开设部门、招聘成员
# ============================================================

import logging
import re as _re_mod

logger = logging.getLogger(__name__)

_CREW_LOCK = threading.Lock()
_DEPT_NAME_RE = _re_mod.compile(r"^[a-z0-9_\-]{2,30}$")
_AGENT_NAME_RE = _re_mod.compile(r"^[a-z0-9_\-]{2,40}$")


def _crew_load_file() -> dict[str, Any]:
    try:
        with open(AgentRegistry.CUSTOM_AGENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"departments": [], "agents": []}
            data.setdefault("departments", [])
            data.setdefault("agents", [])
            return data
    except Exception:
        return {"departments": [], "agents": []}


def _crew_save_file(data: dict[str, Any]):
    tmp = AgentRegistry.CUSTOM_AGENTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, AgentRegistry.CUSTOM_AGENTS_FILE)


@register_tool(category="Agent", name_cn="创建部门", risk_level="low", timeout=30)
def CreateDepartment(name: str, title: str, description: str = "") -> str:
    """
    在集团中新建一个部门（组织扩张）。创建后即可向该部门招聘成员。

    Args:
        name: 部门标识（小写字母/数字/_/-，如 research）
        title: 部门显示名称（如 调研部）
        description: 部门职责说明（可选）
    """
    name = str(name).strip().lower()
    title = str(title).strip()
    if not _DEPT_NAME_RE.match(name):
        return _xml_response("error", f"部门标识不合法：{name}（仅允许小写字母/数字/_/-，长度 2~30）")
    if not title:
        return _xml_response("error", "缺少部门名称 title")

    with _CREW_LOCK:
        data = _crew_load_file()
        if any(d.get("name") == name for d in data["departments"]):
            return _xml_response("error", f"部门已存在: {name}")
        if len(data["departments"]) >= 20:
            return _xml_response("error", "部门数量已达上限（20）")
        data["departments"].append({
            "name": name,
            "title": title[:40],
            "description": str(description).strip()[:200],
        })
        _crew_save_file(data)

    reload_custom_agents()
    titles = [f"{d.get('title')}({d['name']})" for d in data["departments"]]
    return _xml_response("done", f"部门「{title}」已成立。当前组织：{'; '.join(titles)}。"
                                 f"接下来可使用 RecruitMember 向该部门招聘成员。")


@register_tool(category="Agent", name_cn="招聘成员", risk_level="medium", timeout=60)
def RecruitMember(name: str, department: str, description: str, backstory: str, tools: list[str]) -> str:
    """
    为指定部门招聘一名新成员代理（自定义角色），招聘后立即可投入任务。
    成员拥有自己的专属工具集；工具名必须来自系统已注册的工具。

    Args:
        name: 成员名（小写字母/数字/_/-，如 qa-engineer）
        department: 目标部门标识（须已存在；不存在时请先用 CreateDepartment 创建）
        description: 成员职责一句话描述（Goal）
        backstory: 成员系统提示词：角色设定、工作方式、输出要求（Backstory）
        tools: 授予该成员的工具名列表
    """
    name = str(name).strip().lower()
    department = str(department).strip().lower()
    if not _AGENT_NAME_RE.match(name):
        return _xml_response("error", f"成员名不合法：{name}（仅允许小写字母/数字/_/-，长度 2~40）")

    with _CREW_LOCK:
        data = _crew_load_file()

        if any(a.get("name") == name for a in data["agents"]):
            return _xml_response("error", f"成员已存在: {name}（可考虑换名或先卸载）")
        if len(data["agents"]) >= 50:
            return _xml_response("error", "成员数量已达上限（50）")

        depts = {d.get("name") for d in data["departments"]}
        if department not in depts:
            avail = ", ".join(sorted(x for x in depts if x)) or "（暂无）"
            return _xml_response(
                "error",
                f"部门 {department} 不存在。请先调用 CreateDepartment 创建。现有部门：{avail}",
            )

        # 工具白名单过滤：只授予真实存在的工具
        from . import registry as _tool_registry
        known = set(_tool_registry._metadata.keys()) | {
            s["function"]["name"] for s in _tool_registry._schemas
        }
        granted = [t for t in tools if t in known]
        dropped = [t for t in tools if t not in known]
        if not granted:
            # 动态列出真实存在的工具名，避免误导 AI 用不存在的工具名反复重试
            sample = ", ".join(sorted(known)[:15])
            return _xml_response(
                "error",
                f"tools 全部无效。系统共 {len(known)} 个真实可用工具，例如：{sample}。\n"
                f"请只从上述真实工具名中选择，不要使用 RunCommand/PythonExecute 等旧名（已改名为 Bash）。",
            )

        data["agents"].append({
            "name": name,
            "department": department,
            "description": str(description).strip()[:500],
            "backstory": str(backstory).strip()[:8000],
            "tools": granted[:40],
            "model": "",
        })
        _crew_save_file(data)

    reload_custom_agents()

    note = f"（以下工具未识别已忽略: {dropped}）" if dropped else ""
    return _xml_response(
        "done",
        f"成员「{name}」已入职「{department}」部，授予工具：{', '.join(granted)}。{note}\\n"
        f"立即生效：可通过 Task(subagent_type='{name}') 派发任务，或在 RunCrew 计划中使用。",
    )
