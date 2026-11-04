"""AgentMain 会话管理职责。

优-1 第十二阶段：从 web_server.py 拆出的会话 ID 校验、历史读写，
以及会话列表/读取/删除相关方法。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import sqlite3

import public

logger = logging.getLogger(__name__)


# claude 会话 ID 格式（UUID），用于区分 opencode（ses_ 前缀）/ claude / native 会话
_CLAUDE_SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _is_claude_session_id(session_id: str) -> bool:
    return bool(_CLAUDE_SID_RE.match(str(session_id or "")))


# L-2: 统一 session_id 校验：仅允许安全字符，防注入与意外路径穿越。
# 允许 : @ . 是因为真实会话 ID 形如：
#   single:assistant:sxkiss_com:52806025813@chatroom
#   claude:assistant:sxkiss_com:52806025813@chatroom
# 这些字符不足以构成路径穿越（已排除 / \ .. 等），但必须放行否则单ag/微信会话无法删除。
_SESSION_ID_RE = re.compile(r'^[a-zA-Z0-9_:\-.@]{1,128}$')


def _validate_session_id(session_id) -> tuple:
    """校验 session_id 格式；合法返回 (True, session_id)，非法返回 (False, 错误信息)"""
    sid = str(session_id or "").strip()
    if not sid:
        return False, "缺少参数 session_id"
    if ".." in sid or "/" in sid or "\\" in sid or "\x00" in sid:
        return False, "session_id 含非法字符（路径穿越字符）"
    if not _SESSION_ID_RE.match(sid):
        return False, "session_id 格式不合法（仅允许字母数字、下划线、短横线、冒号、@、点，最长128字符）"
    return True, sid


def _session_history_mtime(session_file: str) -> float:
    """会话历史的最后修改时间（快照与 journal 取较新者）。

    只取 sessions.json 会在"只有 journal"的会话上抛 FileNotFoundError /
    得到过期时间，导致列表排序错乱。
    """
    latest = 0.0
    for path in (session_file, session_file[: -len('sessions.json')] + 'sessions.journal.jsonl'):
        try:
            latest = max(latest, os.path.getmtime(path))
        except OSError:
            continue
    return latest


def _session_has_history(session_file: str) -> bool:
    """会话是否存在任何可读历史（快照或增量日志任一存在即为真）。

    不能只判断 sessions.json：memory.py 的 add_message 只把新消息 append 到
    journal，快照要等 journal 累积到 JOURNAL_COMPACT_BYTES 才合并生成。
    新会话在相当长一段时间内根本没有 sessions.json，若以此做门禁，
    历史接口会静默返回空（实测小程序每次刷新都是空白）。
    """
    if os.path.exists(session_file):
        return True
    jpath = session_file[: -len('sessions.json')] + 'sessions.journal.jsonl'
    return os.path.exists(jpath)


def _load_session_history(session_file: str) -> list:
    """读取会话完整历史 = sessions.json 快照 + sessions.journal.jsonl 增量。

    memory.py 的 add_message 只把新消息 append 到 journal（避免每次全量重写 5MB+ 快照），
    快照仅在日志累积到阈值时才合并。因此这里凡是直接读 sessions.json 的地方都必须
    补上 journal，否则会看不到最新消息。文件不存在返回 []。
    """
    history = []
    if os.path.exists(session_file):
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except Exception:
            logger.warning("会话快照读取失败: %s", session_file, exc_info=True)
            history = []
    jpath = session_file[: -len('sessions.json')] + 'sessions.journal.jsonl'
    if os.path.exists(jpath):
        try:
            with open(jpath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        history.append(json.loads(line))
                    except ValueError:
                        # 单行损坏只丢该条，不影响其余增量
                        logger.warning("journal 行损坏已跳过: %s", line[:80])
        except OSError:
            logger.warning("journal 读取失败: %s", jpath, exc_info=True)
    return history


# ============================================================
# Agent Main
# ============================================================
class SessionMixin:
    def get_chat_historys(self, get):
        workspace_filter = str(get.get('workspace', '')).strip()
        # mode 隔离：每个 mode 只返回自己 source 的历史，互不串
        #   group   → 仅 native（集团对话 + 集团子代理）
        #   single  → 仅 native（单 Agent 主对话）
        #   opencode/claude/codex → 各自 db 全量
        raw_mode = str(get.get('mode', '')).strip().lower() or 'group'
        mode = raw_mode if raw_mode in ('group', 'single', 'opencode', 'claude', 'codex') else 'group'
        # source 参数（旧兼容，保留）
        source_param = str(get.get('source', '')).strip().lower()
        sessions = []

        # ── opencode 全局历史（仅 opencode 模式）─────────────────────────
        if source_param in ('opencode', '') and mode == 'opencode':
            try:
                from chat_client.opencode_bridge import query_opencode_sessions
                oc_sessions = query_opencode_sessions(workspace_filter)
                for s in oc_sessions:
                    title = s.get("title", "")
                    if title.startswith("New session"):
                        title = "opencode 会话"
                    sessions.append({
                        "session_id": s["id"],
                        "title": title,
                        "timestamp": s.get("time_updated", 0),
                        "time_str": datetime.datetime.fromtimestamp(s.get("time_updated", 0) / 1000).astimezone().strftime('%Y-%m-%d %H:%M:%S') if s.get("time_updated") else "",
                        "workspace": s.get("directory", ""),
                        "source": "opencode",
                    })
            except Exception:
                logger.warning("opencode 历史查询异常，跳过", exc_info=True)

        # ── claude 全局历史（仅 claude 模式）──────────────────────
        if source_param in ('claude', '') and mode == 'claude':
            try:
                from chat_client.claude_bridge import query_claude_sessions
                cl_sessions = query_claude_sessions(workspace_filter)
                for s in cl_sessions:
                    sessions.append({
                        "session_id": s["id"],
                        "title": s.get("title", "claude 会话"),
                        "timestamp": s.get("time_updated", 0),
                        "time_str": datetime.datetime.fromtimestamp(s.get("time_updated", 0) / 1000).astimezone().strftime('%Y-%m-%d %H:%M:%S') if s.get("time_updated") else "",
                        "workspace": s.get("directory", ""),
                        "source": "claude",
                    })
            except Exception:
                logger.warning("claude 历史查询异常，跳过", exc_info=True)

        # ── codex 全局历史（仅 codex 模式）──────────────────────────
        if source_param in ('codex', '') and mode == 'codex':
            try:
                from chat_client.codex_bridge import query_codex_sessions
                cd_sessions = query_codex_sessions(workspace_filter)
                for s in cd_sessions:
                    ts = s.get("time_updated", 0)
                    sessions.append({
                        "session_id": s["id"],
                        "title": s.get("title", "codex 会话"),
                        "timestamp": ts,
                        "time_str": datetime.datetime.fromtimestamp(ts / 1000).astimezone().strftime('%Y-%m-%d %H:%M:%S') if ts else "",
                        "workspace": s.get("directory", ""),
                        "source": "codex",
                    })
            except Exception:
                logger.warning("codex 历史查询异常，跳过", exc_info=True)

        # ── native 历史（group / single 模式各自过滤）─────────────────────
        if mode in ('group', 'single'):
            pass

        # ── native 历史（从 sessions.json 读取，集团模式）─────────────────────
        # 只有 group/single 模式才读本地 native 会话（集团对话 + 单 Agent + 子代理）
        if mode in ('group', 'single'):
            sessions_dir = os.path.join(self.plugin_path, 'sessions')
            # CLI 会话 ID 前缀（这些是 opencode/claude/codex 模式，不应混入 native 历史）
            _cli_prefixes = ('ses_', 'agent-', 'rollout-')

            def _is_crew_session(sid: str, history, session_path) -> bool:
                """严格识别集团会话：必须有 CreateDepartment/RecruitMember（建立部门结构）或 meta.json source=crew"""
                # 1) meta.json source=crew（最可靠）
                mf = os.path.join(session_path, 'meta.json')
                if os.path.exists(mf):
                    try:
                        if json.load(open(mf)).get('source') == 'crew':
                            return True
                    except Exception:
                        pass
                # 2) history 里有 CreateDepartment 或 RecruitMember（建立部门结构的关键工具）
                if history:
                    for m in history:
                        for tc in (m.get('tool_calls') or []):
                            if not isinstance(tc, dict):
                                continue
                            name = (tc.get('function') or {}).get('name', '') or tc.get('name', '')
                            if name in ('CreateDepartment', 'RecruitMember'):
                                return True
                return False

            if os.path.exists(sessions_dir):
                try:
                    dirs = os.listdir(sessions_dir)
                    dirs.sort(key=lambda x: os.path.getmtime(os.path.join(sessions_dir, x)), reverse=True)
                    for session_id in dirs:
                        # 跳过 CLI 会话 ID 的顶层目录（属于 opencode/claude/codex）
                        if session_id.startswith(_cli_prefixes):
                            continue
                        session_path = os.path.join(sessions_dir, session_id)
                        if not os.path.isdir(session_path):
                            continue
                        session_file = os.path.join(session_path, 'sessions.json')
                        if not _session_has_history(session_file):
                            continue
                        try:
                            mtime = _session_history_mtime(session_file)
                            time_str = datetime.datetime.fromtimestamp(mtime).astimezone().strftime('%Y-%m-%d %H:%M:%S')
                            title = session_id
                            history = _load_session_history(session_file)
                            if history:
                                for msg in history:
                                    if msg.get('role') == 'user':
                                        content = msg.get('content', '')
                                        if isinstance(content, list):
                                            text_item = next(
                                                (item for item in content
                                                 if isinstance(item, dict) and item.get('type') == 'text'),
                                                None
                                            )
                                            content = text_item.get('text', '') if text_item else ''
                                        if not isinstance(content, str):
                                            content = ''
                                        title = content[:20] + '...' if len(content) > 20 else content
                                        break
                            # 从 meta.json 读 workspace（chat_start 时持久化）
                            ws = ''
                            meta_file = os.path.join(session_path, 'meta.json')
                            if os.path.exists(meta_file):
                                try:
                                    with open(meta_file, 'r', encoding='utf-8') as mf:
                                        ws = (json.load(mf) or {}).get('workspace', '') or ''
                                except Exception:
                                    ws = ''
                            item = {
                                "session_id": session_id,
                                "title": title,
                                "timestamp": int(mtime),
                                "time_str": time_str,
                                "workspace": ws,
                                "source": "single",
                                "is_group": _is_crew_session(session_id, history, session_path),
                            }
                            sessions.append(item)
                        except Exception:
                            continue
                except Exception:
                    pass

            # ── 旧版 jobs-only 检测已合并到 _is_crew_session，块删除 ──

            # ── 扫描集团模式子代理会话（读 meta.json 判断来源，避免误判）────
            if os.path.exists(sessions_dir):
                try:
                    dirs2 = os.listdir(sessions_dir)
                    for session_id in dirs2:
                        parent_path = os.path.join(sessions_dir, session_id)
                        if not os.path.isdir(parent_path):
                            continue
                        sub_dirs = os.listdir(parent_path)
                        for sub_dir in sub_dirs:
                            sub_path = os.path.join(parent_path, sub_dir)
                            if not os.path.isdir(sub_path):
                                continue
                            meta_file = os.path.join(sub_path, 'meta.json')
                            sub_file = os.path.join(sub_path, 'sessions.json')
                            # 子代理必须有 meta.json（明确标记 source/agent/dept），不能仅靠父 jobs crew_plan 推断
                            if not os.path.exists(meta_file):
                                continue
                            try:
                                if os.path.exists(meta_file):
                                    mtime = os.path.getmtime(meta_file)
                                else:
                                    mtime = 0
                                mtime = max(mtime, _session_history_mtime(sub_file))
                                time_str = datetime.datetime.fromtimestamp(mtime).astimezone().strftime('%Y-%m-%d %H:%M:%S')
                                if os.path.exists(meta_file):
                                    with open(meta_file, 'r', encoding='utf-8') as f:
                                        meta = json.load(f)
                                else:
                                    meta = {"source": "crew", "parent": session_id, "agent": "", "dept": ""}
                                title = sub_dir[:36]
                                if _session_has_history(sub_file):
                                    history = _load_session_history(sub_file)
                                    if history:
                                        for msg in history:
                                            if msg.get('role') == 'user':
                                                content = msg.get('content', '')
                                                if isinstance(content, list):
                                                    text_item = next(
                                                        (item for item in content
                                                         if isinstance(item, dict) and item.get('type') == 'text'),
                                                        None
                                                    )
                                                    content = text_item.get('text', '') if text_item else ''
                                                if not isinstance(content, str):
                                                    content = ''
                                                title = content[:20] + '...' if len(content) > 20 else content
                                                break
                                sessions.append({
                                    "session_id": sub_dir,
                                    "title": title,
                                    "timestamp": int(mtime),
                                    "time_str": time_str,
                                    # 子代理 workspace 优先读 meta.json，没有则继承父会话
                                    "workspace": meta.get("workspace", "") or "",
                                    "source": meta.get("source", "crew"),
                                    "parent": session_id,
                                    "agent": meta.get("agent", ""),
                                    "dept": meta.get("dept", ""),
                                })
                            except Exception:
                                continue
                except Exception:
                    pass

        # 去重       # 去重：同一 session_id 可能同时出现在 native 和 opencode/claude 历史
        # 先建立 session_id → workspace 映射，供 crew 子代理继承父会话 workspace
        _parent_ws = {}
        for _s in sessions:
            _sid = _s.get('session_id', '')
            _ws = _s.get('workspace', '') or ''
            if _sid and _ws:
                _parent_ws[_sid] = _ws
        for _s in sessions:
            if not _s.get('workspace') and _s.get('parent') and _s['parent'] in _parent_ws:
                _s['workspace'] = _parent_ws[_s['parent']]
        # 优先保留有 is_group 标记或 source=crew 的记录
        seen = {}
        for s in sessions:
            sid = s.get("session_id", "")
            if sid not in seen:
                seen[sid] = s
            else:
                # 保留信息更全的记录（有 is_group 或 source=crew 的优先）
                if s.get("is_group") or s.get("source") == "crew":
                    seen[sid] = s
        sessions = list(seen.values())

        # 按 mode 二次过滤：native 模式下按 is_group / source 切分，CLI 模式只留自己 source
        if mode == 'group':
            # group 只要：集团主对话(is_group=True) + 集团子代理(source=crew)
            sessions = [s for s in sessions if (s.get('source') == 'crew') or (s.get('source') == 'single' and s.get('is_group'))]
        elif mode == 'single':
            # single 只要：单 Agent 主对话（source=single 且 !is_group）
            sessions = [s for s in sessions if s.get('source') == 'single' and not s.get('is_group')]
        else:
            # CLI 模式（opencode/claude/codex）：只留对应 source
            sessions = [s for s in sessions if s.get('source') == mode]

        # 按时间正序排序，返回前 200 条（旧在前，新在后，匹配文件夹模式）
        sessions.sort(key=lambda s: s.get("timestamp", 0))
        return public.return_data(True, data=sessions[:200])

    def get_chat(self, get):
        session_id = get.get('session_id')
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")

        sessions_dir = os.path.join(self.plugin_path, 'sessions')
        native_file = os.path.join(sessions_dir, session_id, 'sessions.json')
        # 子代理会话：UUID 且不在 sessions/<id>/，可能在 sessions/<parent>/<id>/
        if not os.path.exists(native_file) and _is_claude_session_id(session_id):
            for parent in os.listdir(sessions_dir) if os.path.isdir(sessions_dir) else []:
                cand = os.path.join(sessions_dir, parent, session_id, 'sessions.json')
                if os.path.exists(cand):
                    native_file = cand
                    break

        # ── native 历史（包括集团子代理）：从 sessions.json 读取 ──────────────
        if _session_has_history(native_file):
            try:
                history = _load_session_history(native_file)
                try:
                    for msg in history:
                        if msg.get('role') == 'tool':
                            content = msg.get('content')
                            if isinstance(content, list) and len(content) > 0:
                                first_item = content[0]
                                if isinstance(first_item, dict) and first_item.get('type') == 'text':
                                    msg['content'] = first_item.get('text', '')
                    return public.return_data(True, data=history)
                except Exception as e:
                    logger.warning("接口异常已脱敏对外返回", exc_info=True)
                    return public.returnMsg(False, '读取会话记录失败')
            except Exception:
                return public.return_data(True, data=[])

        # ── opencode 全局历史：从 opencode.db 读取 ─────────────────────────
        if session_id.startswith("ses_"):
            try:
                from chat_client.opencode_bridge import query_opencode_history
                history = query_opencode_history(session_id)
                return public.return_data(True, data=history)
            except Exception:
                return public.return_data(True, data=[])

        # ── claude 全局历史：从 ~/.claude/projects/ jsonl 读取 ───────────────
        if _is_claude_session_id(session_id):
            try:
                from chat_client.claude_bridge import query_claude_history
                history = query_claude_history(session_id)
                return public.return_data(True, data=history)
            except Exception:
                return public.return_data(True, data=[])

# ── codex 全局历史：从 ~/.codex/sessions/ rollout jsonl 读取 ─────────
        if session_id.startswith("rollout-"):
            try:
                from chat_client.codex_bridge import query_codex_history
                history = query_codex_history(session_id)
                return public.return_data(True, data=history)
            except Exception:
                return public.return_data(True, data=[])

    def del_chat(self, get):
        session_id = get.get('session_id')
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")
        # P2-28: session_id 必须只含安全字符，防路径穿越
        # 统一用 _validate_session_id（放行 : @ .，以兼容 single:/claude: 前缀的会话）
        ok, sid = _validate_session_id(session_id)
        if not ok:
            return public.returnMsg(False, sid)
        session_id = sid

        # ── opencode 会话：从全局 opencode.db 删除 ──────────────────────
        if session_id.startswith("ses_"):
            import sqlite3
            db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
            if not os.path.exists(db_path):
                return public.returnMsg(False, "opencode 数据库不存在")
            # P2-30: 用 with 语句保证 conn 在任何情况下都被关闭
            try:
                with sqlite3.connect(db_path, timeout=5) as conn:
                    # 外键级联：删 session 会连带删 message/part/todo 等
                    cur = conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
                    conn.commit()
                    if cur.rowcount == 0:
                        return public.returnMsg(False, "会话不存在")
                return public.returnMsg(True, "删除成功")
            except Exception as e:
                logger.warning("接口异常已脱敏对外返回", exc_info=True)
                return public.returnMsg(False, '删除失败')

        # ── claude 会话：从 ~/.claude/projects/ 删除对应 jsonl 文件 ──────
        if _is_claude_session_id(session_id):
            import glob as _glob
            hits = _glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
            for p in hits:
                try:
                    os.remove(p)
                    return public.returnMsg(True, "删除成功")
                except Exception as e:
                    logger.warning("接口异常已脱敏对外返回", exc_info=True)
                    return public.returnMsg(False, '删除失败')
            return public.returnMsg(False, "claude 会话文件不存在")

# ── codex 会话：从 ~/.codex/sessions/ 删除对应 rollout jsonl ──────
        if session_id.startswith("rollout-"):
            import glob as _glob
            hits = _glob.glob(os.path.expanduser(f"~/.codex/sessions/**/{session_id}.jsonl"), recursive=True)
            for p in hits:
                try:
                    os.remove(p)
                    return public.returnMsg(True, "删除成功")
                except Exception as e:
                    logger.warning("接口异常已脱敏对外返回", exc_info=True)
                    return public.returnMsg(False, '删除失败')
            return public.returnMsg(False, "codex 会话文件不存在")

        # ── native 会话：删除本地 sessions 目录 ──────────────────────────
        import shutil
        custom_sessions_dir = get.get('sessions_dir', '')
        sessions_dir = custom_sessions_dir if custom_sessions_dir else 'sessions'
        session_dir = os.path.join(self.plugin_path, sessions_dir, session_id)
        if not os.path.exists(session_dir):
            return public.returnMsg(False, "会话不存在")
        try:
            shutil.rmtree(session_dir)
            return public.returnMsg(True, "删除成功")
        except Exception as e:
            logger.warning("接口异常已脱敏对外返回", exc_info=True)
            return public.returnMsg(False, '删除失败')

    def del_chat_batch(self, get):
        ids = get.get('session_ids')
        if not isinstance(ids, list) or not ids:
            return public.returnMsg(False, "缺少 session_ids")
        deleted, failed = [], []
        for sid in ids:
            sid = str(sid).strip()
            if not sid:
                continue
            res = self.del_chat({"session_id": sid})
            if res.get("status"):
                deleted.append(sid)
            else:
                failed.append(sid)
        return public.return_data(True, data={
            "deleted": deleted,
            "failed": failed,
            "count": len(deleted),
        })

    def del_chat_msg(self, get):
        """删除单个消息（native 会话：从 sessions/<id>/sessions.json 过滤掉该消息）"""
        session_id = get.get('session_id')
        message_id = get.get('id')
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")
        if not message_id:
            return public.returnMsg(False, "缺少参数 id")
        # 防路径穿越：统一用 _validate_session_id（放行 : @ .，兼容 single:/claude: 会话）
        ok, sid = _validate_session_id(session_id)
        if not ok:
            return public.returnMsg(False, sid)
        session_id = sid
        sessions_dir = get.get('sessions_dir', '') or 'sessions'
        session_file = os.path.join(self.plugin_path, sessions_dir, session_id, 'sessions.json')
        if not _session_has_history(session_file):
            return public.returnMsg(False, "会话记录不存在")
        try:
            history = _load_session_history(session_file)
            original_len = len(history)
            history = [m for m in history if m.get('id') != message_id]
            if len(history) == original_len:
                return public.returnMsg(False, "未找到指定消息")
            with open(session_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=4)
            # 必须清掉 journal：删除是对全量历史的操作，结果已整体写回快照。
            # 若残留 journal，下次加载会把刚删掉的消息重新追加回来（"复活"）。
            jpath = os.path.join(self.plugin_path, sessions_dir, session_id, 'sessions.journal.jsonl')
            try:
                if os.path.exists(jpath):
                    os.remove(jpath)
            except OSError:
                logger.warning("journal 清理失败: %s", jpath, exc_info=True)
            return public.returnMsg(True, "删除成功")
        except Exception as e:
            logger.warning("接口异常已脱敏对外返回", exc_info=True)
            return public.returnMsg(False, '删除失败')
