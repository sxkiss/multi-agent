# 向后兼容别名
SearchReplace = FileEdit
Patch = MultiFileEdit


# ---- 大文件编辑工具（支持分段读写）----

@register_tool(category="Agent", name_cn="大文件编辑", id="LargeFileEdit", risk_level="high")
class LargeFileEdit:
    """
    大文件编辑工具：支持对超大文件进行分段读写和编辑。
    
    功能：
    - read: 读取文件的指定行范围（默认 1-1000）
    - write: 写入内容到文件（可追加或覆盖）
    - edit: 替换文件中的指定文本
    - append: 在文件末尾追加内容
    
    参数：
    - file_path: 文件路径
    - action: 操作类型 (read/write/edit/append)
    - content: 要写入的内容
    - start_line: 起始行号（用于 read）
    - end_line: 结束行号（用于 read）
    - old_str: 要替换的文本（用于 edit）
    - new_str: 替换后的文本（用于 edit）
    """
    def execute(self, file_path: str, action: str = "read", content: str = "",
                start_line: int = 1, end_line: int = 1000,
                old_str: str = "", new_str: str = "") -> str:
        import os
        
        # 检查路径权限
        if not _is_path_allowed(file_path):
            return _xml_response("error", f"Path not allowed: {file_path}")
        
        action = action.lower()
        
        try:
            if action == "read":
                # 读取文件指定行范围
                if not os.path.exists(file_path):
                    return _xml_response("error", f"File not found: {file_path}")
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                
                total_lines = len(lines)
                start = max(1, start_line)
                end = min(end_line, total_lines)
                
                if start > total_lines:
                    return _xml_response("done", f"文件共 {total_lines} 行，请求的行范围 ({start}-{end}) 超出范围")
                
                selected_lines = lines[start-1:end]
                result = f"文件: {file_path}\n共 {total_lines} 行\n显示第 {start}-{end} 行:\n\n"
                for i, line in enumerate(selected_lines, start=start):
                    result += f"{i}: {line}"
                
                return _xml_response("done", result)
            
            elif action == "write":
                # 写入内容到文件（覆盖）
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return _xml_response("done", f"已写入 {len(content)} 字符到 {file_path}")
            
            elif action == "append":
                # 追加内容到文件末尾
                with open(file_path, 'a', encoding='utf-8') as f:
                    f.write(content)
                return _xml_response("done", f"已追加 {len(content)} 字符到 {file_path}")
            
            elif action == "edit":
                # 替换文件中的指定文本
                if not os.path.exists(file_path):
                    return _xml_response("error", f"File not found: {file_path}")
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    file_content = f.read()
                
                if old_str not in file_content:
                    return _xml_response("error", f"未找到要替换的文本: {old_str[:50]}...")
                
                new_content = file_content.replace(old_str, new_str, 1)
                
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                
                return _xml_response("done", f"已成功替换文本，新文件已保存")
            
            else:
                return _xml_response("error", f"未知的操作类型: {action}")
        
        except Exception as e:
            return _xml_response("error", f"操作失败: {str(e)}")
