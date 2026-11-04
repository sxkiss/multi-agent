"""AgentMain 工作空间解析与收敛。

优-1 第十五阶段：从 web_server.py 拆出的 workspace 解析、用户/管理员
工作空间收敛逻辑。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class WorkspaceMixin:
    def _resolve_workspace(self, mode=None):
        """解析工作空间：优先请求参数 > 模式配置 > 全局配置 > 项目根目录"""
        # 1. 尝试从模式配置获取
        if mode:
            workspaces = self.config.get('workspaces', {})
            ws = workspaces.get(mode, '') or ''
            if ws:
                ws = os.path.abspath(os.path.expanduser(ws))
                if os.path.isdir(ws):
                    return ws
        
        # 2. 回退到全局配置
        ws = self.config.get('workspace', '') or ''
        if not ws:
            return BASE_DIR
        ws = os.path.abspath(os.path.expanduser(ws))
        if not os.path.isdir(ws):
            try:
                os.makedirs(ws, exist_ok=True)
            except Exception:
                return BASE_DIR
        return ws

    # 每个渠道用户的工作目录根。所有用户级 workspace 必须落在此目录下，
    # 便于统一清理，也天然形成路径穿越的边界。
    USER_WORKSPACE_ROOT = os.path.join(BASE_DIR, "workspace", "users")

    # 管理端自定义 workspace 的允许根（收敛越界访问）。
    # 管理端需要操作项目目录本身，故允许 BASE_DIR 及其子目录。
    ADMIN_WORKSPACE_ROOTS = (BASE_DIR, os.path.expanduser("~"))

    def _clamp_workspace(self, path):
        """把管理端传入的 workspace 收敛到允许根内，越界则回退默认。

        此前 `workspace` 参数直接 os.makedirs + 交给 Agent 当 cwd，
        可指向 /etc、/root 等任意目录，属于可被前端利用的越权写入。
        """
        try:
            real = os.path.realpath(path)
            for root in self.ADMIN_WORKSPACE_ROOTS:
                r = os.path.realpath(root)
                if real == r or real.startswith(r + os.sep):
                    if not os.path.isdir(real):
                        os.makedirs(real, exist_ok=True)
                    return real
            logger.warning("[安全] workspace 越界被拒: %s（允许根: %s）", path, self.ADMIN_WORKSPACE_ROOTS)
        except Exception:
            logger.warning("[安全] workspace 解析失败，回退默认: %s", path, exc_info=True)
        return self._resolve_workspace()

    def _resolve_request_workspace(self, get, session_id):
        """统一解析请求应使用的工作目录（含多用户隔离与越权收敛）。

        优先级：终端渠道强制用户目录 > 客户端传入（收敛校验）> 配置默认。
        opencode/claude/codex 三个 CLI 分支共用本方法，避免各自实现走样。
        """
        from web_server import _is_terminal_channel
        if _is_terminal_channel(get):
            return self._resolve_user_workspace(session_id)
        override = str(get.get('workspace', '')).strip()
        if override:
            return self._clamp_workspace(os.path.abspath(os.path.expanduser(override)))
        return self._resolve_workspace()

    def _resolve_user_workspace(self, session_id):
        """按用户为粒度解析隔离工作目录：workspace/users/<用户键>/

        隔离粒度说明（重要）：
        - 会话级隔离不足：同一用户新开对话后，上次生成的文件就找不到了。
        - 全局共享（原实现）更糟：多用户并存时互相覆盖、可读彼此文件。
        因此按"用户"隔离：同一用户无论多少会话/多少设备，都落在同一目录
        （换设备续聊不丢文件）；不同用户彼此不可见。

        用户键来源：
        - 小程序：会话 ID 形如 wx_<sha256(openid)[:24]>，天然按微信用户稳定。
        - 网页/CLI：会话 ID 为普通字符串，退化为按会话隔离（管理员自用场景）。
        """
        try:
            sid = str(session_id or "").strip()
        except Exception:
            sid = ""
        if not sid:
            return self.USER_WORKSPACE_ROOT
        # 用户键：取会话 ID 中"用户标识"部分。
        # wx_<hash> 取整个（hash 已由 openid 派生，不含其它用户信息）。
        # 其它会话 ID 直接取其哈希，避免把用户可控字符串当路径。
        if sid.startswith("wx_"):
            user_key = sid[:64]
        elif sid.startswith("card_"):
            # 卡密会话：直接用前缀，不哈希（便于调试和目录管理）
            user_key = sid[:64]
        else:
            user_key = "s_" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:24]
        # 二次清洗：即使前缀判断失效，也绝不允许出现路径分隔符
        user_key = re.sub(r"[^0-9A-Za-z_.-]", "_", user_key)[:64] or "default"
        ws = os.path.join(self.USER_WORKSPACE_ROOT, user_key)
        try:
            os.makedirs(ws, exist_ok=True)
        except Exception:
            logger.warning("用户工作目录创建失败: %s", ws, exc_info=True)
            return BASE_DIR
        return ws
