#!/bin/bash
# bt-agent 重启脚本（2026-10-15）
# 用途：安全重启 web_server（9876）。因当前会话可能就跑在该服务上，
# 本脚本设计为「分离执行」：调用方用 setsid nohup 启动它，它自己 sleep 后
# 再 kill 旧进程 + 拉起新进程，避免把调用者一起杀掉导致重启中断。
#
# 用法：
#   setsid nohup bash restart_bt.sh [delay_seconds] > logs/restart.log 2>&1 < /dev/null &
#
# 行为：
#   1. 等待 delay 秒（默认 5，给调用方留出响应时间）
#   2. 优雅停止 9876 上的旧进程（SIGTERM，超时后 SIGKILL）
#   3. 用 nohup 拉起新进程（cwd=BASE_DIR，venv python）
#   4. 轮询 /api/config 直到健康（最多 60s）
#   5. 结果写入 logs/restart.log

set -u

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$BASE_DIR/.venv/bin/python"
PORT=9876
DELAY="${1:-5}"
LOG_DIR="$BASE_DIR/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

echo "[$STAMP] === 重启任务启动（delay=${DELAY}s）==="

sleep "$DELAY"

# 1. 找到监听 9876 的进程并停止
OLD_PIDS="$(ss -tlnp 2>/dev/null | grep ":$PORT " | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)"
if [ -z "$OLD_PIDS" ]; then
  # 回退：按命令行匹配
  OLD_PIDS="$(pgrep -f "uvicorn web_server:app" || true)"
fi

if [ -n "$OLD_PIDS" ]; then
  echo "[$(date '+%H:%M:%S')] 停止旧进程: $OLD_PIDS"
  for pid in $OLD_PIDS; do
    kill "$pid" 2>/dev/null || true
  done
  # 等待最多 15s 优雅退出
  for i in $(seq 1 15); do
    if ! ss -tlnp 2>/dev/null | grep -q ":$PORT "; then break; fi
    sleep 1
  done
  # 仍未退出则强杀
  for pid in $OLD_PIDS; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "[$(date '+%H:%M:%S')] 强杀 $pid"
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
  sleep 1
else
  echo "[$(date '+%H:%M:%S')] 未发现旧进程，直接启动"
fi

# 2. 拉起新进程
cd "$BASE_DIR" || exit 1
echo "[$(date '+%H:%M:%S')] 启动新进程..."
setsid nohup "$VENV_PY" -m uvicorn web_server:app --host 0.0.0.0 --port "$PORT" \
  > "$LOG_DIR/server.out" 2>&1 < /dev/null &
NEW_PID=$!
echo "[$(date '+%H:%M:%S')] 新进程 PID=$NEW_PID"

# 3. 健康检查
OK=0
for i in $(seq 1 30); do
  sleep 2
  CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$PORT/" 2>/dev/null || echo 000)"
  if [ "$CODE" = "200" ]; then
    OK=1
    echo "[$(date '+%H:%M:%S')] ✅ 健康检查通过（HTTP $CODE，第 ${i} 次）"
    break
  fi
done

if [ "$OK" = "1" ]; then
  echo "[$(date '+%H:%M:%S')] === 重启完成 ==="
  exit 0
else
  echo "[$(date '+%H:%M:%S')] ❌ 重启后健康检查失败，最近日志："
  tail -20 "$LOG_DIR/server.out" 2>/dev/null
  exit 1
fi
