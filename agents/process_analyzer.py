import json
import subprocess
import shutil
import time
import re
import os
from typing import List, Dict
from .base import BaseAgent

# --- Helper Functions ---

def run_cmd(cmd, timeout=5):
    try:
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return result.stdout.decode('utf-8', errors='ignore').strip()
    except Exception as e:
        return f"Error: {str(e)}"

def get_cmd_output_lines(cmd, timeout=5):
    output = run_cmd(cmd, timeout)
    if output.startswith("Error:"):
        return []
    return output.split('\n')

def parse_kv(text, separator=':'):
    data = {}
    for line in text.split('\n'):
        if separator in line:
            key, val = line.split(separator, 1)
            data[key.strip()] = val.strip()
    return data

def parse_free_m(output):
    """Parse free -m output to structured dict with units"""
    lines = output.strip().split('\n')
    result = {}
    try:
        # Headers: total used free shared buff/cache available
        mem_line = next((l for l in lines if l.startswith("Mem:")), None)
        if mem_line:
            parts = mem_line.split()
            if len(parts) >= 7:
                total = int(parts[1])
                used = int(parts[2])
                free = int(parts[3])
                buff = int(parts[5])
                avail = int(parts[6])
                
                result['memory_summary'] = {
                    "total": f"{total}MB",
                    "used": f"{used}MB ({used/total*100:.1f}%)" if total > 0 else "0MB",
                    "free": f"{free}MB ({free/total*100:.1f}%)" if total > 0 else "0MB",
                    "buff_cache": f"{buff}MB",
                    "available": f"{avail}MB"
                }
        
        swap_line = next((l for l in lines if l.startswith("Swap:")), None)
        if swap_line:
            parts = swap_line.split()
            if len(parts) >= 4:
                total = int(parts[1])
                used = int(parts[2])
                free = int(parts[3])
                result['swap_summary'] = {
                    "total": f"{total}MB",
                    "used": f"{used}MB ({used/total*100:.1f}%)" if total > 0 else "0MB",
                    "free": f"{free}MB"
                }
    except:
        pass
    return result

# --- Data Collectors ---

def collect_basic_info():
    info = {}
    info['uname'] = run_cmd('uname -a')
    info['os_release'] = parse_kv(run_cmd('cat /etc/os-release'), '=')
    info['hostname'] = run_cmd('hostname')
    info['uptime'] = run_cmd('uptime')
    info['loadavg'] = run_cmd('cat /proc/loadavg')
    info['who'] = run_cmd('who -b')
    return info

def collect_cpu_info():
    data = {}
    # Lscpu
    data['cpu_architecture_info'] = parse_kv(run_cmd('lscpu'))
    
    # Top (batch mode)
    top_out = run_cmd('top -bn1 | head -n 20')
    data['cpu_load_overview'] = top_out
    
    # Mpstat
    if shutil.which('mpstat'):
        data['cpu_per_core_usage'] = run_cmd('mpstat -P ALL 1 1')
    
    # Top Processes
    ps_cmd = "ps aux --sort=-%cpu | head -11"
    data['top_cpu_processes'] = []
    lines = get_cmd_output_lines(ps_cmd)
    if lines:
        headers = lines[0].split()
        for line in lines[1:]:
            parts = line.split(None, 10)
            if len(parts) == 11:
                process = dict(zip(headers, parts))
                # Add human readable units
                try:
                    if 'RSS' in process:
                        process['RSS_MB'] = f"{int(process['RSS'])/1024:.1f}MB"
                except:
                    pass
                data['top_cpu_processes'].append(process)
                
    return data

def collect_memory_info():
    data = {}
    free_out = run_cmd('free -m')
    data['raw_free_m'] = free_out
    
    # Parse memory info with units
    data.update(parse_free_m(free_out))
    
    data['swap_usage_details'] = run_cmd('swapon -s')
        
    # Top Memory Processes
    ps_cmd = "ps aux --sort=-%mem | head -11"
    data['top_memory_processes'] = []
    lines = get_cmd_output_lines(ps_cmd)
    if lines:
        headers = lines[0].split()
        for line in lines[1:]:
            parts = line.split(None, 10)
            if len(parts) == 11:
                process = dict(zip(headers, parts))
                # Add human readable units for RSS (KB -> MB)
                try:
                    if 'RSS' in process:
                        process['RSS_MB'] = f"{int(process['RSS'])/1024:.1f}MB"
                except:
                    pass
                data['top_memory_processes'].append(process)
                
    return data

def collect_disk_info():
    data = {}
    data['filesystem_usage'] = run_cmd("df -hT | grep -v -E 'overlay|overlay2'")
    data['block_device_map'] = run_cmd('lsblk -f')
    data['inode_usage'] = run_cmd("df -i | grep -v -E 'overlay|overlay2'")
    
    if shutil.which('iostat'):
        data['io_statistics'] = run_cmd('iostat -x 1 1')
        
    if shutil.which('iotop'):
        # iotop usually requires root and interactive, batch mode -b
        data['top_io_processes'] = run_cmd('iotop -o -b -n 1 | head -n 10')
        
    return data

def collect_network_info():
    data = {}
    
    # Connection counts
    data['connection_state_counts'] = run_cmd("netstat -ant | awk '{print $6}' | sort | uniq -c")
    
    # Bandwidth usage (simple estimate via /proc/net/dev)
    try:
        def read_net_dev():
            with open('/proc/net/dev', 'r') as f:
                lines = f.readlines()
            ifaces = {}
            for line in lines[2:]:
                if ':' in line:
                    parts = line.split(':')
                    name = parts[0].strip()
                    values = parts[1].split()
                    if len(values) >= 9:
                        ifaces[name] = {
                            'rx': int(values[0]),
                            'tx': int(values[8])
                        }
            return ifaces
            
        t1 = time.time()
        net1 = read_net_dev()
        time.sleep(1)
        t2 = time.time()
        net2 = read_net_dev()
        
        diff = {}
        duration = t2 - t1
        for name, stats in net2.items():
            if name in net1:
                rx_rate = (stats['rx'] - net1[name]['rx']) / duration
                tx_rate = (stats['tx'] - net1[name]['tx']) / duration
                if rx_rate > 0 or tx_rate > 0: # Only show active interfaces
                    diff[name] = {
                        'rx_bps': rx_rate * 8,
                        'tx_bps': tx_rate * 8,
                        'rx_human': f"{rx_rate/1024:.2f} KB/s",
                        'tx_human': f"{tx_rate/1024:.2f} KB/s"
                    }
        data['network_bandwidth_usage'] = diff
    except:
        data['network_bandwidth_usage'] = {}
        
    # Top connection processes
    try:
        ss_out = run_cmd('ss -tunp')
        proc_conns = []
        lines = ss_out.split('\n')
        if len(lines) > 1:
            # Skip header
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 6:
                    proto = parts[0]
                    state = parts[1]
                    local_addr = parts[4]
                    peer_addr = parts[5]
                    # Parse process info
                    process_info = parts[6] if len(parts) > 6 else ""
                    
                    pid = "Unknown"
                    proc_name = "Unknown"
                    match = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
                    if match:
                        proc_name = match.group(1)
                        pid = match.group(2)
                    
                    proc_conns.append({
                        "pid": pid,
                        "process_name": proc_name,
                        "protocol": proto,
                        "local_address": local_addr,
                        "remote_address": peer_addr,
                        "state": state
                    })
        
        # Group by process and count
        proc_stats = {}
        for conn in proc_conns:
            key = f"{conn['process_name']}({conn['pid']})"
            if key not in proc_stats:
                proc_stats[key] = {
                    "pid": conn['pid'],
                    "process_name": conn['process_name'],
                    "count": 0,
                    "details": []
                }
            proc_stats[key]["count"] += 1
            # Keep first 5 connections as sample
            if len(proc_stats[key]["details"]) < 5:
                proc_stats[key]["details"].append(conn)

        # Sort by count
        sorted_stats = sorted(proc_stats.values(), key=lambda x: x['count'], reverse=True)[:5]
        data['top_network_processes'] = sorted_stats
    except:
        data['top_network_processes'] = []
    
    return data

def collect_services_logs():
    data = {}
    data['failed_systemd_units'] = run_cmd('systemctl list-units --state=failed')
    data['active_systemd_services'] = run_cmd('systemctl list-units --type=service --state=running | head -n 20')
    data['recent_login_history'] = run_cmd('last | head -n 5')
    data['scheduled_tasks'] = run_cmd('crontab -l')
    data['kernel_ring_buffer_tail'] = run_cmd('dmesg -T | tail -10')
    
    # Check for errors in messages (requires readable log file)
    log_file = '/var/log/messages' if os.path.exists('/var/log/messages') else '/var/log/syslog'
    if os.path.exists(log_file):
        data['system_log_errors'] = run_cmd(f'grep -iE "error|fail|panic|oom" {log_file} | tail -n 10')
    
    return data

def get_system_info():
    raw_data = {
        "basic": collect_basic_info(),
        "cpu": collect_cpu_info(),
        "memory": collect_memory_info(),
        "disk": collect_disk_info(),
        "network": collect_network_info(),
        "services": collect_services_logs()
    }
    
    return json.dumps({
        "raw_data": raw_data,
    }, ensure_ascii=False, indent=2)


SYSTEM_PROMPT = """
# 进程分析助手配置
## 身份设定
运维分析助手：专为中初级运维设计，用大白话解读服务器数据，输出“健康结论+可复制操作”，突出AI自动分析优势，不堆砌专业术语，核心聚焦服务器核心硬件资源的关键状态判断。

## 核心任务
1. 根据前置工具输入的原始数据生成“直观文字+图形化”报告，明确告知用户“服务器好不好、哪里有问题、该怎么处理（纯文字步骤）”，不涉及任何命令操作。

## 报告要求（必含模块）
1. **服务器概况**：基础信息、运行时长、负载状态、安全提醒（用✅正常/⚠️警告/❌危险标注）；
2. **核心资源检查**（CPU/物理内存<核心>/磁盘/网络）：各核心资源健康状态+Top5耗资源进程表（含PID、程序名、小白建议），**物理内存分析需结合buff/cache等缓存占用情况综合判定，不单独以空闲数值作为判断依据**；swap仅作为物理内存的辅助参考指标，不单独列耗资源相关统计；
3. **关键服务状态**：常用服务（nginx/mysql等）运行情况+异常处理指引（纯文字步骤，无命令）；
4. **风险清单**：按高/中风险排序，每项含“问题+影响+步骤化文字指引”（不用命令，用通俗文字说明操作步骤），swap相关异常不单独列风险，仅依附于物理内存问题标注；
5. **总结建议**：1句话概括服务器整体状态，重点问题优先处理提示，物理内存相关问题为核心优先级。

## 分析规则
1. 专业指标自动转大白话（如“物理内存使用率90%+且buff/cache低”→“核心物理内存真不够用，服务器运行会卡顿/崩机”；“buff/cache高、实际可用内存充足”→“服务器缓存了常用数据，是正常状态，不是内存不够”；swap有占用→“物理内存资源紧张，临时补充内存已启用”）；
2. 自动关联问题根源（如CPU高→检查是否磁盘读写慢导致；物理内存高→先区分是进程实际占用还是缓存占用，再定位处理）；
3. 按“影响服务器运行优先级”排序问题（崩机风险＞卡顿＞优化），**物理内存相关问题为核心资源最高优先级**；
4. 优先聚焦物理内存健康状态分析，swap仅作为物理内存不足的辅助判断指标，不单独作为核心问题判定依据，不夸大swap占用的影响；
5. 核心资源判断以物理内存为核心，所有内存相关分析均围绕物理内存展开，swap仅作补充说明；
6. **分析物理内存时必须综合考量总占用、进程实际占用、buff/cache缓存等全部情况，严禁仅通过free相关单一数值判定内存是否充足，避免误导用户**；
7. 所有涉及让用户动手操作的建议 / 指引，必须统一附带安全参考提示：AI 结果仅用于参考，请衡量实际情况后再进行，如果不懂请勿操作，可以联系技术或客服咨询。

## 核心规则
1. 所有分析均为AI建议，请勿直接操作，先让用户分析可不可行后操作，也不要有太强的指导性。
2. 不要太过激进（例如“立刻”、“马上”、“立即”、“紧急”、“超级”等重感情词语），保持中性的核心建议。

## 严格准则
1. 仅输出直观的文字说明和图形化反馈，不提及、不生成任何命令；
2. 所有操作指引均为通俗文字步骤，确保非技术用户能看懂；
3. 不单独将swap作为核心资源分析，无独立的swap健康状态判定，其状态仅随物理内存问题同步呈现；
4. 安全参考提示为强制添加项，任何动手操作相关内容均不可遗漏该提示，无操作建议的内容可无需添加；
5. **物理内存状态判定为硬性要求：必须综合计算进程实际占用、buff/cache可释放缓存空间，得出真实可用内存情况，禁止单独依据空闲数值下结论，杜绝误导用户**。

仅输出直观的文字说明和图形化反馈，不提及、不生成任何命令；所有操作指引均为通俗文字步骤，确保非技术用户能看懂。
最终在分析结果末尾加上：（注：文档内容由 AI 生成）
"""

class ProcessAnalyzer(BaseAgent):
    def __init__(self, api_key: str, **kwargs):
        super().__init__(api_key=api_key, system_role=SYSTEM_PROMPT, **kwargs)

    def run(self, user_question: str = None, custom_tool_results: List[Dict] = None):
        """
        运行系统分析agent
        """
        
        if not user_question:
            user_question="请基于提供的系统信息，生成详细的服务器体检报告，包含各项指标分析、风险评估与优化建议。"
        
        if not custom_tool_results:
            sys_info = get_system_info()
            custom_tool_results = [
                {
                    "tool": "get_system_info",
                    "status": "success",
                    "data": sys_info
                }
            ]
        
        return super().run(user_question, custom_tool_results)
