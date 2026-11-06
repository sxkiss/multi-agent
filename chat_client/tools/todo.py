import json
import logging
import os
import re
import time
from enum import Enum
from typing import Any

from . import register_tool
from .base import _xml_response

logger = logging.getLogger(__name__)


class TodoStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TodoPriority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def _coerce_enum(value: Any, enum_cls: type, default):
    """枚举值容错：合法值直接使用，非法/缺失回退默认值，绝不抛异常。

    兼容传入枚举成员本身（merge 分支会传 TodoStatus/TodoPriority 对象）。
    """
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            return default
    return default


class TodoItem:
    def __init__(self, content: str, status: TodoStatus = TodoStatus.PENDING,
                 id: str | None = None, priority: TodoPriority = TodoPriority.MEDIUM):
        self.id = id or str(int(time.time() * 1000))  # Simple ID generation
        self.content = content
        self.status = status
        self.priority = priority
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status,
            "priority": self.priority
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'TodoItem':
        # data 可能不是 dict（模型脏数据），防御 .get
        if not isinstance(data, dict):
            raise ValueError("todo 条目不是对象")
        content = data.get("content")
        # content 缺失/为空/非字符串 → 视为非法，绝不造出无意义的空 todo
        if not isinstance(content, str) or not content.strip():
            raise ValueError("todo 条目缺少 content")
        return cls(
            content=content,
            status=_coerce_enum(data.get("status"), TodoStatus, TodoStatus.PENDING),
            id=data.get("id"),
            priority=_coerce_enum(data.get("priority"), TodoPriority, TodoPriority.MEDIUM)
        )


class TodoManager:
    # P2-35: session_id 正则白名单，防止路径穿越
    _SID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,128}$')
    # 真实会话 id 含冒号/点（如 single:assistant:sxkiss_com:20261015-8），
    # 直接拒绝会误伤正常会话。改为：先严格校验原始 id 无路径穿越特征，
    # 再转义为安全目录名（非法字符→下划线），既安全又不再误报。
    _UNSAFE_RE = re.compile(r'[/\\]|\.\.|\x00')

    @staticmethod
    def _safe_dirname(session_id: str) -> str:
        """把任意会话 id 转义为安全的目录名（防路径穿越，不误伤合法 id）。

        - 含路径分隔/上跳/空字节 → 直接拒绝（ValueError）
        - 其余字符中，非 [A-Za-z0-9_-] 的替换为 _（保持可读、可逆性不要求）
        - 结果长度钳制到 128（超长时取前 64 + 哈希后缀，避免碰撞）
        """
        sid = str(session_id or "")
        if not sid:
            raise ValueError("session_id 不能为空")
        if TodoManager._UNSAFE_RE.search(sid):
            raise ValueError(f"session_id 含非法路径特征: {session_id!r}")
        safe = re.sub(r'[^a-zA-Z0-9_-]', '_', sid)
        if len(safe) > 128:
            import hashlib
            h = hashlib.sha1(sid.encode('utf-8')).hexdigest()[:8]
            safe = safe[:119] + "_" + h
        return safe

    def __init__(self, session_id: str, sessions_dir: str = "sessions"):
        safe = self._safe_dirname(session_id)
        self.session_id = safe
        self.session_dir = os.path.join(sessions_dir, safe)
        self.file_path = os.path.join(self.session_dir, "todos.json")
        # 最近一次 update_todos 的脏数据诊断信息，供 TodoWrite 组装可读提示
        self.last_errors: list[str] = []
        self.last_input_invalid: bool = False
        self.last_accepted: int = 0
        self._ensure_sessions_dir()
    
    def _ensure_sessions_dir(self):
        if not os.path.exists(self.session_dir):
            try:
                os.makedirs(self.session_dir)
            except OSError:
                logger.warning("异常被静默吞掉，已记录", exc_info=True)
    
    def get_todos(self) -> list[TodoItem]:
        if not os.path.exists(self.file_path):
            return []

        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            todos = []
            for item in data:
                # 逐条容错：历史文件里若有缺 content 的脏条目，只跳过该条，
                # 保留其余合法条目，不让整个读取退化为空列表
                try:
                    todos.append(TodoItem.from_dict(item))
                except (ValueError, TypeError, KeyError):
                    logger.warning("读取 todo 时跳过非法条目: %r", item)
            return todos
        except Exception:
            return []
    
    def save_todos(self, todos: list[TodoItem]):
        data = [item.to_dict() for item in todos]
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.warning("未处理的异常", exc_info=True)
    def update_todos(self, new_todos_data: list[dict[str, Any]], merge: bool = False) -> list[TodoItem]:
        # 每次调用重置诊断信息
        self.last_errors = []
        self.last_input_invalid = False
        self.last_accepted = 0

        # 整体输入非法（dict/None/字符串等）：不动现有数据，返回明确失败信号
        if not isinstance(new_todos_data, list):
            self.last_input_invalid = True
            self.last_errors.append(
                f"todos 应为数组(list)，实际为 {type(new_todos_data).__name__}，本次写入已忽略"
            )
            return self.get_todos()

        if merge:
            current_todos = self.get_todos()
            current_map = {t.id: t for t in current_todos}

            for idx, item_data in enumerate(new_todos_data, 1):
                try:
                    if not isinstance(item_data, dict):
                        raise ValueError("todo 条目不是对象")
                    item_id = item_data.get("id")
                    if item_id and item_id in current_map:
                        # 更新已有条目：content 非法时保留原值；枚举非法回退原值，
                        # 避免把脏数据覆盖进合法条目
                        existing = current_map[item_id]
                        content = item_data.get("content")
                        if isinstance(content, str) and content.strip():
                            existing.content = content
                        existing.status = _coerce_enum(
                            item_data.get("status", existing.status),
                            TodoStatus, existing.status)
                        existing.priority = _coerce_enum(
                            item_data.get("priority", existing.priority),
                            TodoPriority, existing.priority)
                    else:
                        new_item = TodoItem.from_dict(item_data)
                        current_todos.append(new_item)
                        current_map[new_item.id] = new_item
                    self.last_accepted += 1
                except (ValueError, TypeError, KeyError) as e:
                    self.last_errors.append(f"第 {idx} 条已跳过：{e}")

            final_todos = current_todos
        else:
            final_todos = []
            for idx, item in enumerate(new_todos_data, 1):
                try:
                    final_todos.append(TodoItem.from_dict(item))
                    self.last_accepted += 1
                except (ValueError, TypeError, KeyError) as e:
                    self.last_errors.append(f"第 {idx} 条已跳过：{e}")

        self.save_todos(final_todos)
        return final_todos


# Load descriptions from files would be ideal, but for now we embed them or read them if possible.
# Since we are implementing them in python, we can just put the text in docstrings.

TODO_READ_DESC = """Use this tool to read the current to-do list for the session. This tool should be used proactively and frequently to ensure that you are aware of
the status of the current task list. You should make use of this tool as often as possible, especially in the following situations:
- At the beginning of conversations to see what's pending
- Before starting new tasks to prioritize work
- When the user asks about previous tasks or plans
- Whenever you're uncertain about what to do next
- After completing tasks to update your understanding of remaining work
- After every few messages to ensure you're on track

Usage:
- This tool takes in no parameters. So leave the input blank or empty. DO NOT include a dummy object, placeholder string or a key like "input" or "empty". LEAVE IT BLANK.
- Returns a list of todo items with their status, priority, and content
- Use this information to track progress and plan next steps
- If no todos exist yet, an empty list will be returned"""

TODO_WRITE_DESC = """Use this tool to create and manage a structured task list for your current coding session. This helps you track progress, organize complex tasks, and demonstrate thoroughness to the user.
It also helps the user understand the progress of the task and overall progress of their requests.

## When to Use This Tool
Use this tool proactively in these scenarios:
1. Complex multistep tasks - When a task requires 3 or more distinct steps or actions
2. Non-trivial and complex tasks - Tasks that require careful planning or multiple operations
3. User explicitly requests todo list - When the user directly asks you to use the todo list
4. User provides multiple tasks - When users provide a list of things to be done (numbered or comma-separated)
5. After receiving new instructions - Immediately capture user requirements as todos. Feel free to edit the todo list based on new information.
6. After completing a task - Mark it complete and add any new follow-up tasks
7. When you start working on a new task, mark the todo as in_progress. Ideally you should only have one todo as in_progress at a time. Complete existing tasks before starting new ones.

## Task States and Management
1. **Task States**: Use these states to track progress:
   - pending: Task not yet started
   - in_progress: Currently working on (limit to ONE task at a time)
   - completed: Task finished successfully
   - cancelled: Task no longer needed

2. **Task Management**:
   - Update task status in real-time as you work
   - Mark tasks complete IMMEDIATELY after finishing (don't batch completions)
   - Only have ONE task in_progress at any time
   - Complete current tasks before starting new ones
   - Cancel tasks that become irrelevant

3. **Task Breakdown**:
   - Create specific, actionable items
   - Break complex tasks into smaller, manageable steps
   - Use clear, descriptive task names"""

@register_tool(category="Agent", name_cn="读取待办", risk_level="low")
def TodoRead(**kwargs) -> str:
    """
    Use this tool to read your todo list.
    """
    # Override docstring with full description
    TodoRead.__doc__ = TODO_READ_DESC
    
    session_id = kwargs.get("session_id") or kwargs.get("parent_session_id")
    sessions_dir = kwargs.get("sessions_dir", "sessions")
    if not session_id:
        return _xml_response("error", "Session ID not found in context.")
        
    manager = TodoManager(session_id, sessions_dir=sessions_dir)
    todos = manager.get_todos()
    
    pending_count = len([t for t in todos if t.status != "completed"])
    
    output = {
        "title": f"{pending_count} todos",
        "output": json.dumps([t.to_dict() for t in todos], indent=2, ensure_ascii=False),
        "metadata": {
            "todos": [t.to_dict() for t in todos]
        }
    }
    
    # Return formatted string similar to other tools
    return _xml_response("done", output['output'])

@register_tool(category="Agent", name_cn="写入待办", risk_level="low")
def TodoWrite(todos: list[dict[str, Any]], merge: bool = False, summary: str | None = None, **kwargs) -> str:
    """
    Use this tool to create and manage a structured task list for your current coding session.
    
    Args:
        todos: Array of todo items. Each item should have 'content', 'status' (pending/in_progress/completed), 'id', and 'priority'.
        merge: Whether to merge with existing todos.
        summary: Optional summary of work accomplished.
    """
    # Override docstring with full description
    TodoWrite.__doc__ = TODO_WRITE_DESC
    
    session_id = kwargs.get("session_id") or kwargs.get("parent_session_id")
    sessions_dir = kwargs.get("sessions_dir", "sessions")
    if not session_id:
        return _xml_response("error", "Session ID not found in context.")

    try:
        manager = TodoManager(session_id, sessions_dir=sessions_dir)
        updated_todos = manager.update_todos(todos, merge=merge)

        # 整体输入非法（todos 不是 list）：明确失败信号，绝不当作成功
        if manager.last_input_invalid:
            msg = "; ".join(manager.last_errors) or "todos 参数格式非法"
            return _xml_response("error", msg)

        pending_count = len([t for t in updated_todos if t.status != "completed"])
        result_str = json.dumps(
            [t.to_dict() for t in updated_todos], indent=2, ensure_ascii=False)

        # 脏条目提示：明确告知模型写入了多少条、跳过了多少条及原因，
        # 不静默丢弃，避免模型对 todo 状态产生困惑
        if manager.last_errors:
            reasons = "; ".join(manager.last_errors)
            note = (f"\n\n[提示] 已写入 {manager.last_accepted} 条，"
                    f"跳过 {len(manager.last_errors)} 条非法条目：{reasons}")
            result_str += note

        return _xml_response("done", result_str)
    except Exception as e:
        # 兜底：任何未预期异常都转成可读 error，绝不外抛拖垮 Agent.chat
        logger.warning("TodoWrite 未预期异常（已降级）", exc_info=True)
        return _xml_response("error", f"TodoWrite 执行失败（已降级）：{type(e).__name__}: {e}")
