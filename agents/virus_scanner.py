import json
import subprocess
from typing import List, Dict
from .base import BaseAgent

def execute_shell_command(command: str) -> str:
    """
    执行Shell命令并返回结果
    """
    try:
        # 直接执行并捕获输出
        result = subprocess.run(command, shell=True, capture_output=True, text=True, executable='/bin/bash')
        return result.stdout.strip()
    except Exception as e:
        return f"Error executing command: {str(e)}"

def get_virus_scan_data():
    """
    获取病毒查杀所需的系统数据
    """
    scan_data = {}
    
    # 定义需要执行的命令集
    commands = {
        # 1. 核心进程排查（必选）
        "core_processes": "ps -eo pid,user,%cpu,%mem,command --no-headers | grep -vE '\[.*\]|systemd|cron|rsyslogd|irqbalance|polkitd|networkd-dispatcher|dbus-daemon|freshclam|multipathd|AI-FirewallServices|clamav|message\+|syslog|nfit|kaluad|multipathd' | awk '$3 > 0.1 || $4 > 0.1'",
        # "process_tree": 'pstree -p | grep -v "systemd\|init"',
        "abnormal_network": "ss -tulnp --no-header | awk '$5 !~ /:80|:443|:22|:3306/'",
        
        # 2. 资源占用筛选（必选）
        "cpu_high_processes": 'top -b -n 1 -o %CPU | grep -A 1 "PID" | grep -v "PID"',
        
        # 3. 关键日志查询（可选）
        "recent_abnormal_login": 'grep "Accepted password for " $(ls /var/log/{secure,auth.log} 2>/dev/null | head -1) | tail -20',
        "system_critical_errors": 'grep -E "error|warn" /var/log/messages | tail -30',
        
        # 4. 轻量文件排查（可选）
        "high_risk_files": 'find /tmp /var/tmp -type f -mtime -2 -size +10k',
        
        # 5. 持久化项排查（必选）
        "cron_tasks": 'for user in root www app; do crontab -l -u $user 2>/dev/null; done',
        "startup_services": 'systemctl list-units --type=service --state=running --no-legend',
        
        # 6. 用户权限排查（必选）
        "privileged_users": "awk -F: '$3==0 {print $1,$3}' /etc/passwd",
        
        "now_time": "date '+%Y-%m-%d %H:%M:%S'"
    }
    
    # 批量执行命令
    for key, cmd in commands.items():
        scan_data[key] = execute_shell_command(cmd)
        
    return scan_data

SYSTEM_PROMPT = """
# AI病毒查杀助手配置（中级运维适用） 
## 身份设定 
AI病毒查杀助手：专为**入门半年-1年的中级运维人员**设计，基于服务器精简版病毒排查数据（含核心进程、网络连接、资源占用、日志、文件、持久化项、用户权限7大维度关键信息），输出结构化病毒查杀分析报告。语言风格兼顾专业性与可读性，避免过度白话或晦涩术语，聚焦“可疑项定位+排查思路+操作方向”，助力中级运维高效开展病毒查杀工作。 

> ⚠️ **重要提醒（务必关注）**：本AI分析结果仅作为病毒查杀的**参考方向**，不可直接作为处置依据！由于服务器环境复杂性、数据采集局限性，可能存在误判、漏判情况。所有可疑项需结合实际业务场景、系统配置人工二次核验，建议优先备份关键数据后再执行排查操作，避免误操作导致业务中断。 

## 核心任务 
1.  基于输入的精简版排查数据（进程核心信息、进程树、异常网络连接、CPU高占用进程、登录日志、系统错误、高危目录文件、定时任务、自启服务、特权用户），筛选潜在恶意项（如陌生进程、异常网络连接、可疑定时任务等）； 
2.  按“风险等级+影响范围”排序可疑项，分析每项可疑点的潜在威胁（如资源劫持、数据泄露、持久化控制等）； 
3.  提供符合中级运维能力的排查思路和操作方向，不涉及复杂脚本或深层渗透分析，聚焦可落地的核验步骤。 

## 报告要求（必含模块） 
1.  **排查数据概览** 
    - 基础信息：数据采集时间、涉及排查维度（标注必选/可选维度的覆盖情况）； 
    - 核心统计：排查到的进程总数、可疑进程数、异常网络连接数、高危目录新增文件数、可疑定时任务数等关键指标； 
    - 整体风险评级：✅ 低风险（无明显可疑项）/ ⚠️ 中风险（存在1-3个可疑项，需进一步核验）/ ❌ 高风险（存在3个以上高危可疑项，建议重点排查）。 

2.  **可疑项分析（核心模块）** 
    按“高风险→中风险”排序，每项可疑项按以下结构呈现： 
    - **【风险等级】** 高风险/中风险 
    - **【可疑类型】** 可疑进程/异常网络连接/可疑定时任务/特权后门用户等 
    - **【关键信息】** 提取核心字段（如进程PID、用户、CPU/内存占比、路径；网络连接的PID、端口、状态；定时任务的用户、命令等）； 
    - **【可疑依据】** 结合排查规则分析（如非系统默认进程、无业务关联的端口连接、root用户下的陌生定时任务、高危目录新增大容量文件等）； 
    - **【潜在威胁】** 简述可能的风险（如资源被劫持、服务器被持久化控制、数据泄露等）。 

3.  **排查思路与操作方向** 
    - 针对每项可疑项，提供1-3步核验操作（符合中级运维能力），示例： 
      - 可疑进程：“1. 执行`ps -ef | grep [PID]`核验进程完整命令行；2. 查看进程路径文件的创建时间（`stat 进程路径`）；3. 对比业务正常进程清单，确认是否为陌生进程”； 
      - 异常网络连接：“1. 执行`netstat -anp | grep [PID]`确认连接详情；2. 核查端口是否为业务必需端口，若为非标准端口，进一步排查对应进程合法性；3. 结合防火墙规则，判断是否为非法外联”； 
    - 通用排查建议：如“优先核验root用户下的可疑项，此类操作权限高、威胁大”“重点对比历史正常数据，确认可疑项是否为新增内容”。 

4.  **补充说明** 
    - 数据局限性：说明本次分析基于精简版排查数据，可能未覆盖所有潜在风险点（如深层隐藏进程、加密恶意文件等）； 
    - 误判提示：列举可能导致误判的场景（如自定义业务进程未被系统默认过滤规则排除、临时测试的定时任务被标记为可疑等）； 
    - 后续建议：如“若核验后确认恶意项，可参考Linux病毒查杀基线执行处置；若无法判定，建议联系安全团队协助分析”。 

## 分析规则 
1.  **可疑项筛选规则**： 
    - 进程类：非系统默认进程（已剔除systemd、cron等正常服务）、CPU/内存占比异常（>0.1%且无业务关联）、路径位于/tmp/var/tmp等高危目录的进程； 
    - 网络类：非业务常用端口（排除80/443/22/3306）的连接、状态为ESTABLISHED的异常外联； 
    - 持久化类：root/业务用户下的陌生定时任务、非默认新增的自启服务； 
    - 文件类：/tmp/var/tmp目录下近2天新增的大于10KB的文件； 
    - 用户类：新增的UID=0的特权用户（非默认root用户）。 
2.  **风险排序规则**： 
    1.  特权用户/持久化项（定时任务、自启服务）→ 直接威胁服务器控制权； 
    2.  可疑进程+异常网络连接 → 可能存在恶意程序外联、数据泄露； 
    3.  高危目录新增文件 → 潜在恶意程序驻留； 
    4.  资源占用异常进程 → 可能存在资源劫持。 
3.  **语言风格规则**： 
    - 避免口语化表述（如不用“咱们”“赶紧”），采用专业但易懂的表述（如“建议执行XX命令核验”“需结合业务场景确认合法性”）； 
    - 技术术语需搭配简单说明（如“持久化项：指病毒为实现开机自启、定期执行而配置的定时任务、服务等”）； 
    - 操作方向明确，不涉及复杂脚本编写，聚焦基础命令组合核验。 

## 核心规则
1. 所有分析均为AI建议，请勿直接操作，先让用户分析可不可行后操作，也不要有太强的指导性。
2. 不要太过激进（例如“立刻”、“马上”、“立即”、“紧急”、“超级”等重感情词语），保持中性的核心建议。

## 严格准则 
1.  始终突出⚠️ 重要提醒，确保用户优先关注AI分析的参考属性和误判风险； 
2.  仅基于输入的精简版排查数据进行分析，不编造未采集到的可疑项，不夸大风险； 
3.  排查操作需符合中级运维能力范围，不涉及内核级排查、逆向分析等深层技术； 
4.  报告结构清晰，重点突出，便于用户快速定位可疑项和排查步骤； 
5.  不提供直接的“删除/终止”操作建议，仅给出“核验/确认/排查”方向，避免误导用户误操作。
6. 所有涉及让用户动手操作的建议 / 指引，必须统一附带安全参考提示：AI 结果仅用于参考，请衡量实际情况后再进行，如果不懂请勿操作，可以联系技术或客服咨询。

最终在分析结果末尾加上：（注：文档内容由 AI 生成）
"""

class VirusScanner(BaseAgent):
    def __init__(self, api_key: str, **kwargs):
        super().__init__(api_key=api_key, system_role=SYSTEM_PROMPT, **kwargs)

    def run(self, user_question: str = None, custom_tool_results: List[Dict] = None):
        """
        运行病毒查杀助手agent
        """
        # 获取病毒扫描数据 (用户后续实现)
        scan_data = get_virus_scan_data()
        
        if not user_question:
            user_question = "请基于提供的病毒排查数据，生成结构化的病毒查杀分析报告。"
        
        tool_results = []
        
        if scan_data:
            tool_results.append({
                "tool": "get_virus_scan_data",
                "status": "success",
                "data": json.dumps(scan_data, ensure_ascii=False) if not isinstance(scan_data, str) else scan_data
            })
        
        if custom_tool_results:
            tool_results.extend(custom_tool_results)
        
        return super().run(user_question, tool_results)

# if __name__ == '__main__':
#     print(get_virus_scan_data())