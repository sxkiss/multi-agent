import difflib
import logging
import os
import re
import tempfile
from collections.abc import Generator

from . import register_tool
from .base import _xml_response

logger = logging.getLogger(__name__)


def _levenshtein(a: str, b: str) -> int:
    if not a: return len(b)
    if not b: return len(a)
    
    if len(a) > len(b):
        a, b = b, a
        
    previous_row = range(len(b) + 1)
    for i, c1 in enumerate(a):
        current_row = [i + 1]
        for j, c2 in enumerate(b):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    
    return previous_row[-1]

def _simple_replacer(content: str, find: str) -> Generator[str, None, None]:
    if find in content:
        yield find

def _line_trimmed_replacer(content: str, find: str) -> Generator[str, None, None]:
    c_lines = content.split('\n')
    f_lines = find.split('\n')
    
    if f_lines and f_lines[-1] == "":
        f_lines.pop()
        
    if len(f_lines) > len(c_lines):
        return

    offsets = [0]
    curr = 0
    for line in c_lines:
        curr += len(line) + 1 
        offsets.append(curr)

    for i in range(len(c_lines) - len(f_lines) + 1):
        match = True
        for j in range(len(f_lines)):
            if c_lines[i+j].strip() != f_lines[j].strip():
                match = False
                break
        
        if match:
            start_idx = offsets[i]
            end_line_idx = i + len(f_lines) - 1
            end_idx = offsets[end_line_idx] + len(c_lines[end_line_idx])
            yield content[start_idx:end_idx]

def _block_anchor_replacer(content: str, find: str) -> Generator[str, None, None]:
    c_lines = content.split('\n')
    f_lines = find.split('\n')
    
    if len(f_lines) < 3: return
    if f_lines[-1] == "": f_lines.pop()
    
    first = f_lines[0].strip()
    last = f_lines[-1].strip()
    search_block_size = len(f_lines)
    
    candidates = []
    for i in range(len(c_lines)):
        if c_lines[i].strip() != first: continue
        
        for j in range(i + 2, len(c_lines)):
            if c_lines[j].strip() == last:
                candidates.append((i, j))
                break 
    
    if not candidates: return
    
    SINGLE_THRESHOLD = 0.4
    
    offsets = [0]
    curr = 0
    for line in c_lines:
        curr += len(line) + 1
        offsets.append(curr)

    def get_text(start_line, end_line):
        start_idx = offsets[start_line]
        end_idx = offsets[end_line] + len(c_lines[end_line])
        return content[start_idx:end_idx]

    if len(candidates) == 1:
        start, end = candidates[0]
        actual_size = end - start + 1
        
        similarity = 0.0
        lines_to_check = min(search_block_size - 2, actual_size - 2)
        
        if lines_to_check > 0:
            for j in range(1, min(search_block_size - 1, actual_size - 1)):
                c_line = c_lines[start + j].strip()
                f_line = f_lines[j].strip()
                max_len = max(len(c_line), len(f_line))
                if max_len == 0: continue
                dist = _levenshtein(c_line, f_line)
                similarity += (1.0 - dist / max_len) / lines_to_check
                if similarity >= SINGLE_THRESHOLD: break
        else:
            similarity = 1.0
            
        if similarity >= SINGLE_THRESHOLD:
             yield get_text(start, end)
        return

    best_match = None
    max_sim = -1.0
    
    for cand in candidates:
        start, end = cand
        actual_size = end - start + 1
        similarity = 0.0
        lines_to_check = min(search_block_size - 2, actual_size - 2)
        
        if lines_to_check > 0:
             for j in range(1, min(search_block_size - 1, actual_size - 1)):
                c_line = c_lines[start + j].strip()
                f_line = f_lines[j].strip()
                max_len = max(len(c_line), len(f_line))
                if max_len == 0: continue
                dist = _levenshtein(c_line, f_line)
                similarity += (1.0 - dist / max_len)
             similarity /= lines_to_check
        else:
            similarity = 1.0
            
        if similarity > max_sim:
            max_sim = similarity
            best_match = cand

    MULTIPLE_THRESHOLD = 0.3
    if max_sim >= MULTIPLE_THRESHOLD and best_match:
        yield get_text(best_match[0], best_match[1])

def _whitespace_normalized_replacer(content: str, find: str) -> Generator[str, None, None]:
    def normalize(s): return re.sub(r'\s+', ' ', s).strip()
    
    norm_find = normalize(find)
    c_lines = content.split('\n')
    
    for i, line in enumerate(c_lines):
        if normalize(line) == norm_find:
            yield line
        else:
            norm_line = normalize(line)
            if norm_find in norm_line:
                words = find.strip().split()
                if words:
                    pattern = r'\s+'.join(re.escape(w) for w in words)
                    try:
                        match = re.search(pattern, line)
                        if match:
                            yield match.group(0)
                    except Exception:
                        logger.warning("未处理的异常", exc_info=True)
    f_lines = find.split('\n')
    if len(f_lines) > 1:
        for i in range(len(c_lines) - len(f_lines) + 1):
            block = c_lines[i : i+len(f_lines)]
            block_str = '\n'.join(block) 
            if normalize(block_str) == norm_find:
                yield '\n'.join(block) 

def _indentation_flexible_replacer(content: str, find: str) -> Generator[str, None, None]:
    def remove_indent(text):
        lines = text.split('\n')
        non_empty = [l for l in lines if l.strip()]
        if not non_empty: return text
        
        min_indent = min(len(l) - len(l.lstrip()) for l in non_empty)
        return '\n'.join(l[min_indent:] if l.strip() else l for l in lines)
    
    norm_find = remove_indent(find)
    c_lines = content.split('\n')
    f_lines = find.split('\n')
    
    if len(f_lines) > len(c_lines): return
    
    for i in range(len(c_lines) - len(f_lines) + 1):
        block = '\n'.join(c_lines[i : i+len(f_lines)])
        if remove_indent(block) == norm_find:
            yield block

def _escape_normalized_replacer(content: str, find: str) -> Generator[str, None, None]:
    def unescape(s):
        ret = ""
        i = 0
        while i < len(s):
            if s[i] == '\\' and i + 1 < len(s):
                c = s[i+1]
                if c == 'n': ret += '\n'
                elif c == 't': ret += '\t'
                elif c == 'r': ret += '\r'
                elif c in ["'", '"', '`', '\\', '$']: ret += c
                else: ret += '\\' + c
                i += 2
            else:
                ret += s[i]
                i += 1
        return ret

    unescaped_find = unescape(find)
    
    if unescaped_find in content:
        yield unescaped_find
        
    c_lines = content.split('\n')
    f_lines = unescaped_find.split('\n')
    
    if len(f_lines) > len(c_lines): return
    
    for i in range(len(c_lines) - len(f_lines) + 1):
        block = '\n'.join(c_lines[i : i+len(f_lines)])
        if unescape(block) == unescaped_find:
            yield block

def _trimmed_boundary_replacer(content: str, find: str) -> Generator[str, None, None]:
    trimmed_find = find.strip()
    if trimmed_find == find: return
    
    if trimmed_find in content:
        yield trimmed_find
        
    c_lines = content.split('\n')
    f_lines = find.split('\n')
    
    if len(f_lines) > len(c_lines): return
    
    for i in range(len(c_lines) - len(f_lines) + 1):
        block = '\n'.join(c_lines[i : i+len(f_lines)])
        if block.strip() == trimmed_find:
            yield block

def _context_aware_replacer(content: str, find: str) -> Generator[str, None, None]:
    f_lines = find.split('\n')
    if len(f_lines) < 3: return
    if f_lines[-1] == "": f_lines.pop()
    
    first = f_lines[0].strip()
    last = f_lines[-1].strip()
    
    c_lines = content.split('\n')
    
    offsets = [0]
    curr = 0
    for line in c_lines:
        curr += len(line) + 1
        offsets.append(curr)
        
    def get_text(start_line, end_line):
        start_idx = offsets[start_line]
        end_idx = offsets[end_line] + len(c_lines[end_line])
        return content[start_idx:end_idx]
    
    for i in range(len(c_lines)):
        if c_lines[i].strip() != first: continue
        
        for j in range(i + 2, len(c_lines)):
            if c_lines[j].strip() == last:
                block_lines = c_lines[i : j+1]
                
                if len(block_lines) == len(f_lines):
                    matching = 0
                    total_non_empty = 0
                    
                    for k in range(1, len(block_lines) - 1):
                        c_l = block_lines[k].strip()
                        f_l = f_lines[k].strip()
                        if c_l or f_l:
                            total_non_empty += 1
                            if c_l == f_l:
                                matching += 1
                    
                    if total_non_empty == 0 or (matching / total_non_empty >= 0.5):
                        yield get_text(i, j)
                        return

def _multi_occurrence_replacer(content: str, find: str) -> Generator[str, None, None]:
    start = 0
    while True:
        idx = content.find(find, start)
        if idx == -1: break
        yield find
        start = idx + len(find)

def _perform_replace(content: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
    if old_str == new_str:
         raise ValueError("No changes to apply: old_str and new_str are identical.")
    
    replacers = [
        _simple_replacer,
        _line_trimmed_replacer,
        _block_anchor_replacer,
        _whitespace_normalized_replacer,
        _indentation_flexible_replacer,
        _escape_normalized_replacer,
        _trimmed_boundary_replacer,
        _context_aware_replacer,
        _multi_occurrence_replacer
    ]
    
    not_found = True
    
    for replacer in replacers:
        for search in replacer(content, old_str):
            index = content.find(search)
            if index == -1: continue
            
            not_found = False
            
            if replace_all:
                return content.replace(search, new_str)
            
            last_index = content.rfind(search)
            if index != last_index:
                continue 
                
            return content[:index] + new_str + content[index + len(search):]
            
    if not_found:
        raise ValueError("Could not find old_str in the file. It must match exactly, including whitespace, indentation, and line endings.")
    
    raise ValueError("Found multiple matches for old_str. Provide more surrounding context to make the match unique.")

def _trim_diff(diff: str) -> str:
    lines = diff.split('\n')
    content_lines = [l for l in lines if (l.startswith('+') or l.startswith('-') or l.startswith(' ')) and not l.startswith('---') and not l.startswith('+++')]
    
    if not content_lines: return diff
    
    min_indent = float('inf')
    for line in content_lines:
        content = line[1:]
        if content.strip():
            match = re.match(r'^(\s*)', content)
            if match:
                min_indent = min(min_indent, len(match.group(1)))
                
    if min_indent == float('inf') or min_indent == 0:
        return diff
        
    trimmed_lines = []
    for line in lines:
        if (line.startswith('+') or line.startswith('-') or line.startswith(' ')) and not line.startswith('---') and not line.startswith('+++'):
            prefix = line[0]
            content = line[1:]
            trimmed_lines.append(prefix + content[min_indent:])
        else:
            trimmed_lines.append(line)
            
    return '\n'.join(trimmed_lines)

# 路径白名单基目录：项目目录 + 用户主目录 + 系统临时目录
# 可通过 write_paths.txt 追加授权目录（一行一个，由 GrantWriteAccess 工具或管理员维护）
_ALLOWED_BASE_DIRS = [
    os.path.realpath(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    os.path.realpath(os.path.expanduser("~")),
    os.path.realpath(tempfile.gettempdir()),
]

_EXTRA_PATHS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "write_paths.txt",
)

_extra_cache = {"mtime": 0, "paths": []}


def _load_extra_paths():
    """读取追加授权目录（带 mtime 缓存，文件变更自动生效）"""
    try:
        mt = os.path.getmtime(_EXTRA_PATHS_FILE)
        if mt != _extra_cache["mtime"]:
            paths = []
            with open(_EXTRA_PATHS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        paths.append(os.path.realpath(line))
            _extra_cache["mtime"] = mt
            _extra_cache["paths"] = paths
    except FileNotFoundError:
        _extra_cache["paths"] = []
    except Exception:
        logger.warning("未处理的异常", exc_info=True)
    return _extra_cache["paths"]


def _add_extra_path(path: str):
    rp = os.path.realpath(path)
    current = set(_load_extra_paths())
    if rp in current:
        return False
    with open(_EXTRA_PATHS_FILE, "a", encoding="utf-8") as f:
        f.write(rp + "\n")
    _load_extra_paths()  # 刷新缓存
    return True


def _remove_extra_path(path: str):
    rp = os.path.realpath(path)
    lines = []
    removed = False
    try:
        with open(_EXTRA_PATHS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return False
    new_lines = []
    for ln in lines:
        if os.path.realpath(ln.strip()) == rp:
            removed = True
        else:
            new_lines.append(ln)
    if removed:
        with open(_EXTRA_PATHS_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        _load_extra_paths()
    return removed


def _is_path_allowed(file_path: str) -> bool:
    real = os.path.realpath(os.path.abspath(os.path.expanduser(file_path)))
    bases = list(_ALLOWED_BASE_DIRS) + _load_extra_paths()
    for base in bases:
        try:
            if os.path.commonpath([real, base]) == base:
                return True
        except ValueError:

            logger.warning("循环处理时跳过异常", exc_info=True)
            continue
    return False


@register_tool(category="Agent", name_cn="搜索替换", id="Edit", risk_level="high")
class FileEdit:
    """
    Performs exact string replacements in files. 
    
    Usage:
    - You must use your `Read` tool at least once in the conversation before editing. This tool will error if you attempt an edit without reading the file. 
    - When editing text from Read tool output, ensure you preserve the exact indentation (tabs/spaces) as it appears AFTER the line number prefix. The line number prefix format is: line number + colon + space (e.g., `1: `). Everything after that space is the actual file content to match. Never include any part of the line number prefix in the oldString or newString.
    - ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
    - Only use emojis if the user explicitly requests it. Avoid adding emojis to files unless asked.
    - The edit will FAIL if `oldString` is not found in the file with an error "oldString not found in content".
    - The edit will FAIL if `oldString` is found multiple times in the file with an error "Found multiple matches for oldString. Provide more surrounding lines in oldString to identify the correct match." Either provide a larger string with more surrounding context to make it unique or use `replaceAll` to change every instance of `oldString`. 
    - Use `replaceAll` for replacing and renaming strings across the file. This parameter is useful if you want to rename a variable for instance.
    
    Args:
        file_path: The absolute path to the file to modify
        old_str: The text to replace
        new_str: The text to replace it with (must be different from oldString)
        replace_all: Replace all occurrences of oldString (default false)
    """
    def execute(self, file_path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
        try:
            if not _is_path_allowed(file_path):
                return _xml_response("error", f"Path not allowed: {file_path}")
            if not os.path.exists(file_path):
                return _xml_response("error", f"File not found: {file_path}")
            
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            new_content = _perform_replace(content, old_str, new_str, replace_all)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
                
            # Generate diff for display
            diff_gen = difflib.unified_diff(
                content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=file_path,
                tofile=file_path,
                n=3 # Context lines
            )
            diff = "".join(diff_gen)
            
            # Trim diff using the ported function
            trimmed_diff = _trim_diff(diff)
            
            return _xml_response("done", f"Edit applied successfully.\n<file_changes>\nThe toolcall made the following changes to the file `{file_path}`:\n```\n{trimmed_diff}\n```\n</file_changes>")
        except Exception as e:
            return _xml_response("error", str(e))


# --- Patch 工具（参考 opencode） ---
import json
from . import PROJECT_ROOT


@register_tool(category="Agent", name_cn="多文件补丁", id="Patch", risk_level="high")
class MultiFileEdit:
    """
    Applies a patch to multiple files in one operation.
    
    The patch text must follow this format:
    *** Begin Patch
    *** Update File: /path/to/file
    @@ Context line (unique within the file)
     Line to keep
    -Line to remove
    +Line to add
     Line to keep
    *** Add File: /path/to/new/file
    +Content of the new file
    +More content
    *** Delete File: /path/to/file/to/delete
    *** End Patch
    
    Features:
    - Apply changes to multiple files atomically
    - Support Update, Add, Delete operations
    - Context-based matching for precision
    
    Args:
        patch_text: The full patch text that describes all changes to be made
    """
    def execute(self, patch_text: str) -> str:
        try:
            if not patch_text:
                return _xml_response("error", "patch_text is required")
            
            # 解析补丁
            operations = self._parse_patch(patch_text)
            if not operations:
                return _xml_response("error", "Invalid patch format")
            
            results = []
            errors = []
            
            for op in operations:
                op_type = op['type']
                file_path = op['path']
                
                # 转换为绝对路径
                if not os.path.isabs(file_path):
                    file_path = os.path.join(PROJECT_ROOT, file_path)
                
                try:
                    if op_type == 'update':
                        result = self._apply_update(file_path, op)
                        results.append(result)
                    elif op_type == 'add':
                        result = self._apply_add(file_path, op)
                        results.append(result)
                    elif op_type == 'delete':
                        result = self._apply_delete(file_path)
                        results.append(result)
                except Exception as e:
                    errors.append(f"{file_path}: {str(e)}")
            
            # 构建响应
            output_parts = []
            if results:
                output_parts.append("Successfully applied changes:")
                for r in results:
                    output_parts.append(f"  ✓ {r}")
            
            if errors:
                output_parts.append("\nErrors:")
                for e in errors:
                    output_parts.append(f"  ✗ {e}")
            
            output = "\n".join(output_parts)
            status = "done" if not errors else "error"
            return _xml_response(status, output)
            
        except Exception as e:
            return _xml_response("error", str(e))
    
    def _parse_patch(self, patch_text: str) -> list:
        """解析补丁文本"""
        operations = []
        lines = patch_text.strip().split('\n')
        
        i = 0
        current_op = None
        
        while i < len(lines):
            line = lines[i]
            
            if line.startswith('*** Begin Patch'):
                i += 1
                continue
            
            if line.startswith('*** End Patch'):
                break
            
            if line.startswith('*** Update File:'):
                if current_op:
                    operations.append(current_op)
                path = line[len('*** Update File:'):].strip()
                current_op = {'type': 'update', 'path': path, 'hunks': []}
                i += 1
                
                # 解析 hunks
                while i < len(lines) and not lines[i].startswith('***'):
                    if lines[i].startswith('@@'):
                        hunk = {'lines': []}  # 保持原始顺序
                        i += 1
                        while i < len(lines) and not lines[i].startswith('@@') and not lines[i].startswith('***'):
                            hunk['lines'].append(lines[i])
                            i += 1
                        current_op['hunks'].append(hunk)
                    else:
                        i += 1
                continue
            
            if line.startswith('*** Add File:'):
                if current_op:
                    operations.append(current_op)
                path = line[len('*** Add File:'):].strip()
                current_op = {'type': 'add', 'path': path, 'content': []}
                i += 1
                
                # 读取文件内容
                while i < len(lines) and not lines[i].startswith('***'):
                    if lines[i].startswith('+'):
                        current_op['content'].append(lines[i][1:])
                    else:
                        current_op['content'].append(lines[i])
                    i += 1
                continue
            
            if line.startswith('*** Delete File:'):
                if current_op:
                    operations.append(current_op)
                path = line[len('*** Delete File:'):].strip()
                current_op = {'type': 'delete', 'path': path}
                i += 1
                continue
            
            i += 1
        
        if current_op:
            operations.append(current_op)
        
        return operations
    
    def _apply_update(self, file_path: str, op: dict) -> str:
        """应用更新操作"""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        original_content = content
        lines = content.split('\n')
        
        for hunk in op.get('hunks', []):
            hunk_lines = hunk.get('lines', [])
            
            # 查找匹配位置
            match_idx, search_lines = self._find_match(lines, hunk_lines)
            if match_idx == -1:
                raise ValueError(f"Could not find matching context for hunk")
            
            # 替换行
            new_lines = lines[:match_idx]
            for line in hunk_lines:
                if line.startswith('+'):
                    new_lines.append(line[1:])
                elif line.startswith('-'):
                    pass  # 删除
                elif line.startswith(' '):
                    new_lines.append(line[1:])
                else:
                    new_lines.append(line)
            new_lines.extend(lines[match_idx + len(search_lines):])
            lines = new_lines
        
        new_content = '\n'.join(lines)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        # 生成 diff
        diff_gen = difflib.unified_diff(
            original_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{op['path']}",
            tofile=f"b/{op['path']}",
            n=2
        )
        diff_str = "".join(diff_gen)
        
        return f"Updated {op['path']}\n{diff_str}"
    
    def _find_match(self, lines: list, hunk_lines: list) -> tuple:
        """查找匹配位置，返回 (index, search_lines)"""
        # 构建搜索模式
        search_lines = []
        for line in hunk_lines:
            if line.startswith('-'):
                search_lines.append(line[1:])
            elif line.startswith('+'):
                pass  # add 行不参与搜索
            elif line.startswith(' '):
                search_lines.append(line[1:])
            else:
                search_lines.append(line)
        
        for i in range(len(lines) - len(search_lines) + 1):
            match = True
            for j, pattern_line in enumerate(search_lines):
                if lines[i + j].strip() != pattern_line.strip():
                    match = False
                    break
            if match:
                return i, search_lines
        
        return -1, search_lines
    
    def _apply_add(self, file_path: str, op: dict) -> str:
        """应用添加操作"""
        if os.path.exists(file_path):
            raise FileExistsError(f"File already exists: {file_path}")
        
        # 创建父目录
        dir_path = os.path.dirname(file_path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
        
        content = '\n'.join(op['content'])
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return f"Created {op['path']}"
    
    def _apply_delete(self, file_path: str) -> str:
        """应用删除操作"""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        
        os.remove(file_path)
        return f"Deleted {os.path.basename(file_path)}"


# 向后兼容别名
SearchReplace = FileEdit
Patch = MultiFileEdit


# ---- 大文件编辑工具（支持分段读写）----

@register_tool(category="Agent", name_cn="大文件编辑", id="LargeFileEdit", risk_level="high")
class LargeFileEdit:
    """
    大文件编辑工具：支持对超大文件进行分段读写和编辑。
    - read: 读取文件的指定行范围
    - write: 写入内容到文件（覆盖）
    - append: 在文件末尾追加内容
    - edit: 替换文件中的指定文本
    """
    def execute(self, file_path: str, action: str = "read", content: str = "",
                start_line: int = 1, end_line: int = 1000,
                old_str: str = "", new_str: str = "") -> str:
        if not _is_path_allowed(file_path):
            return _xml_response("error", f"Path not allowed: {file_path}")
        action = action.lower()
        try:
            if action == "read":
                if not os.path.exists(file_path):
                    return _xml_response("error", f"File not found: {file_path}")
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                total = len(lines)
                s, e = max(0, start_line - 1), min(end_line, total)
                result = f"文件: {file_path} (共 {total} 行, 显示 {s+1}-{e})\n\n"
                for i in range(s, e):
                    result += f"{i+1}: {lines[i]}"
                return _xml_response("done", result)
            elif action == "write":
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return _xml_response("done", f"已写入 {len(content)} 字符到 {file_path}")
            elif action == "append":
                with open(file_path, 'a', encoding='utf-8') as f:
                    f.write(content)
                return _xml_response("done", f"已追加 {len(content)} 字符到 {file_path}")
            elif action == "edit":
                if not os.path.exists(file_path):
                    return _xml_response("error", f"File not found: {file_path}")
                with open(file_path, 'r', encoding='utf-8') as f:
                    fc = f.read()
                if old_str not in fc:
                    return _xml_response("error", f"未找到要替换的文本: {old_str[:50]}...")
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(fc.replace(old_str, new_str, 1))
                return _xml_response("done", f"已替换文本并保存")
            else:
                return _xml_response("error", f"未知操作: {action}")
        except Exception as e:
            return _xml_response("error", str(e))
