#!/bin/bash
# 构建并部署前端到项目根目录
#
# 部署目标说明：
#   - vite base 配置为 /static/，build 产物在 frontend/dist/，其 index.html 引用
#     /static/assets/index.<hash>.js 与 /static/assets/index.<hash>.css
#   - web_server 通过 StaticFiles 挂载 /static（见 web_server.py:2357），线上入口为
#     根 index.html（同样引用 /static/assets/...）
#   因此正确部署 = 把 frontend/dist/ 整体同步到 /static/，并把 dist/index.html 放到根目录。
#
# 注意：旧版脚本曾错误输出到 /static/vue/，与线上引用不符，已废弃。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
STATIC_DIR="$SCRIPT_DIR/static"
ROOT="$SCRIPT_DIR"
BACKUP_DIR="$ROOT/deploy_backup/$(date +%Y%m%d_%H%M%S)"

cd "$FRONTEND_DIR"

# 1. 构建
echo "==> [1/4] 构建前端 (npm run build)"
npm run build

# 2. 备份当前线上前端（static/assets + 根 index.html），便于回滚
echo "==> [2/4] 备份现有部署到 $BACKUP_DIR"
mkdir -p "$BACKUP_DIR/assets"
[ -f "$ROOT/index.html" ] && cp "$ROOT/index.html" "$BACKUP_DIR/index.html"
[ -d "$STATIC_DIR/assets" ] && cp -n "$STATIC_DIR/assets/"*.js "$STATIC_DIR/assets/"*.css "$BACKUP_DIR/assets/" 2>/dev/null || true

# 3. 同步 dist/ 到 static/（保留 assets/ 子目录结构；--delete 清掉 static/assets 里
#    不再被新构建引用的旧 hash chunk，避免无限累积）
echo "==> [3/4] 同步 dist/ -> $STATIC_DIR/"
mkdir -p "$STATIC_DIR/assets"
# 仅同步本次构建产出的 assets（按 dist/assets 实际文件），并清理 static/assets 中失效旧文件
rsync -a --delete "$FRONTEND_DIR/dist/assets/" "$STATIC_DIR/assets/"

# 4. 部署根 index.html（dist/index.html 已含正确的 /static/assets/... 绝对引用）
echo "==> [4/4] 部署根 index.html"
cp "$FRONTEND_DIR/dist/index.html" "$ROOT/index.html"

echo ""
echo "✅ 部署完成"
echo "   新 JS:  $(grep -o 'static/assets/index\.[A-Za-z0-9_]*\.js' "$ROOT/index.html")"
echo "   新 CSS: $(grep -o 'static/assets/index\.[A-Za-z0-9_]*\.css' "$ROOT/index.html")"
echo "   回滚:   cp $BACKUP_DIR/index.html $ROOT/index.html ; rsync -a $BACKUP_DIR/assets/ $STATIC_DIR/assets/"
