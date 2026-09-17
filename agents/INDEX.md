<!-- AUTO-DOC: Update me when files in this folder change -->

# Agents

预置 Agent 定义模块（面板兼容模式）。提供 BaseAgent 基类及多种专业 Agent 实现。

## Files

| File | Role | Function |
|------|------|----------|
| `__init__.py` | Package | 包入口，导出 AgentConfig |
| `base.py` | Core | BaseAgent 基类：OpenAI API 封装、系统信息收集 |
| `process_analyzer.py` | Agent | 进程分析 Agent |
| `security_expert.py` | Agent | 安全专家 Agent |
| `site_analyzer.py` | Agent | 网站分析 Agent |
| `site_safe_analyzer.py` | Agent | 网站安全分析 Agent |
| `virus_scanner.py` | Agent | 病毒扫描 Agent |
