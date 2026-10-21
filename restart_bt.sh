#!/bin/bash
# bt-agent 重启脚本（2026-10-21 改进版）
# 用途：安全重启 web_server（9876）。
#
# 改进点（对比 2026-10-15 版）：
#   1. 优先使用 systemd 重启 multi-agent.service（真实单元名，旧文档误写 bt-agent.service）
#   2. 无 sudo 权限时：kill 监听 9876 的进程，利用 systemd Restart=always 自动拉起，
#      不再自己 nohup 拉起 uvicorn —— 避免脚本进程与 systemd 单元争抢端口
#   3. 增加 fallback：若进程不是 systemd 托管（手动 nohup 场景），才自行拉起新进程
#   4. 统一日志到 logs/restart.log，含健康检查结果
#
# 用法：
#   setsid nohup bash restart_bt.sh [delay_seconds] > logs/restart.log 2>&1 < /dev/null &
#
# 行为：
#   1. 等待 delay 秒（默认 2，给调用方留出响应时间）
#   2. 优先 sudo systemctl restart multi-agent.service（有 sudo 时）
#      否则 kill 9876 监听进程，等待 systemd 5s 后自动拉起（无 sudo 时）
#   3. 轮询健康检查（最多 90s）
#   4. 结果写入 logs/restart.log

set -u

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=9876
DELAY="${1:-2}"
LOG_DIR="$BASE_DIR/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
UNIT="multi-agent.service"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

echo "[$STAMP] === 重启任务启动（delay=${DELAY}s，unit=${UNIT}）==="
sleep "$DELAY"

# ---------- 1. 优先 systemd 重启 ----------
if sudo -n systemctl restart "$UNIT" 2>/dev/null; then
  log "已通过 systemd 重启 ${UNIT}"
  RESTART_MODE="systemd"
else
  # ---------- 2. 无 sudo：kill 监听进程，依赖 Restart=always ----------
  log "无 sudo 权限，改用 kill+systemd 自动拉起"
  OLD_PIDS="$(ss -tlnp 2>/dev/null | grep ":$PORT " | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)"
  if [ -z "$OLD_PIDS" ]; then
    OLD_PIDS="$(pgrep -f "uvicorn web_server:app" || true)"
  fi

  if [ -n "$OLD_PIDS" ]; then
    for pid in $OLD_PIDS; do
      log "停止旧进程: $pid"
      kill "$pid" 2>/dev/null || true
    done
    # 等待优雅退出（最多 15s）
    for i in $(seq 1 15); do
      if ! ss -tlnp 2>/dev/null | grep -q ":$PORT "; then break; fi
      sleep 1
    done
    # 仍未退出则强杀
    for pid in $OLD_PIDS; do
      if kill -0 "$pid" 2>/dev/null; then
        log "强杀 $pid"
        kill -9 "$pid" 2>/dev/null || true
      fi
    done
  else
    log "未发现监听 9876 的进程"
  fi

  # 判断是否为 systemd 托管：若 kill 后 systemd 会自动拉起，则不自行启动
  SYSTEMD_PID="$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null || echo 0)"
  if [ "$SYSTEMD_PID" != "0" ] && [ -n "$SYSTEMD_PID" ]; then
    log "systemd 托管中（MainPID=${SYSTEMD_PID}），等待自动拉起..."
    RESTART_MODE="systemd-kill"
  else
    # ---------- 3. fallback：手动进程场景，自行拉起 ----------
    log "进程非 systemd 托管，自行拉起新进程"
    cd "$BASE_DIR" || exit 1
    setsid nohup "$BASE_DIR/.venv/bin/python" -m uvicorn web_server:app \
      --host 0.0.0.0 --port "$PORT" > "$LOG_DIR/server.out" 2>&1 < /dev/null &
    log "新进程已拉起 PID=$!"
    RESTART_MODE="manual"
  fi
fi

# ---------- 4. 健康检查 ----------
OK=0
for i in $(seq 1 45); do
  sleep 2
  CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$PORT/" 2>/dev/null || echo 000)"
  if [ "$CODE" = "200" ]; then
    OK=1
    log "健康检查通过（HTTP $CODE，第 ${i} 次）"
    break
  fi
done

if [ "$OK" = "1" ]; then
  log "=== 重启完成（mode=${RESTART_MODE}）==="
  exit 0
else
  log "=== 重启失败（mode=${RESTART_MODE}），最近日志：==="
  if [ "$RESTART_MODE" = "manual" ]; then
    tail -20 "$LOG_DIR/server.out" 2>/dev/null
  else
    journalctl -u "$UNIT" -n 20 --no-pager 2>/dev/null || true
  fi
  exit 1
fi
