"""
AGENTS.md 层级加载机制
参考 Codex CLI 的层级发现和优先级系统
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 配置常量
MAX_AGENTS_MD_BYTES = 32 * 1024  # 32 KiB 默认上限
AGENTS_FILENAMES = ["AGENTS.override.md", "AGENTS.md"]
GLOBAL_AGENTS_MD = os.path.expanduser("~/.codex/AGENTS.md")


class AgentsMdManager:
    """AGENTS.md 层级加载管理器"""
    
    def __init__(self, max_bytes: int = MAX_AGENTS_MD_BYTES):
        self.max_bytes = max_bytes
        self._cache = {}
    
    def load(self, cwd: Optional[str] = None, load_global: bool = True, local_only: bool = False) -> str:
        """
        从项目根到 cwd 逐层加载 AGENTS.md
        优先级: override > AGENTS.md
        
        Args:
            cwd: 工作目录，默认为当前目录
            load_global: 是否加载全局 ~/.codex/AGENTS.md，默认 True
            local_only: 仅在 cwd 内搜索（含 cwd 自身），不向上爬父目录。
                        用于单AG模式，避免 cwd 非git仓库时爬上父git项目加载错AGENTS.md。
        """
        if cwd is None:
            cwd = os.getcwd()
        
        cache_key = os.path.realpath(cwd)
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        agents_files = []
        
        # 加载全局 AGENTS.md（可选）
        if load_global and os.path.exists(GLOBAL_AGENTS_MD):
            agents_files.append(GLOBAL_AGENTS_MD)
        
        # 确定搜索起点
        if local_only:
            # 仅从 cwd 自身开始找，不向上爬
            search_starts = [Path(cwd)]
        else:
            # 从 git 项目根到 cwd 逐层发现（原有行为）
            project_root = self._find_project_root(cwd)
            if project_root:
                search_starts = []
                current = Path(project_root)
                target = Path(cwd)
                while True:
                    search_starts.append(current)
                    if current == target:
                        break
                    if current == current.parent:
                        break
                    current = current.parent
            else:
                search_starts = []
        
        # 从各层找到第一个匹配的 AGENTS.md
        for start in search_starts:
            for filename in AGENTS_FILENAMES:
                agents_file = start / filename
                if agents_file.exists():
                    agents_files.append(str(agents_file))
                    break  # 每层只取第一个匹配的文件
        
        # 合并加载
        content = self._merge_agents_md(agents_files)
        self._cache[cache_key] = content
        return content
    
    def _find_project_root(self, start_path: str) -> Optional[str]:
        """查找 git 项目根目录"""
        current = Path(start_path)
        
        while True:
            # 检查 .git 目录或文件
            if (current / ".git").exists():
                return str(current)
            
            # 检查常见项目标志
            for marker in ["package.json", "pyproject.toml", "Cargo.toml", "go.mod"]:
                if (current / marker).exists():
                    return str(current)
            
            if current == current.parent:
                return None
            
            current = current.parent
    
    def _merge_agents_md(self, files: list[str]) -> str:
        """合并多个 AGENTS.md 文件，控制总大小"""
        parts = []
        total_bytes = 0
        
        for file_path in files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                # 检查大小限制
                file_bytes = len(content.encode("utf-8"))
                if total_bytes + file_bytes > self.max_bytes:
                    logger.warning(
                        f"AGENTS.md 总大小超过 {self.max_bytes} 字节，"
                        f"跳过后续文件: {file_path}"
                    )
                    break
                
                parts.append(f"<!-- {file_path} -->\n{content}")
                total_bytes += file_bytes
                
            except Exception as e:
                logger.warning(f"加载 AGENTS.md 失败 {file_path}: {e}")
        
        return "\n\n".join(parts)
    
    def clear_cache(self):
        """清除缓存"""
        self._cache.clear()


# 全局单例
agents_md_manager = AgentsMdManager()
