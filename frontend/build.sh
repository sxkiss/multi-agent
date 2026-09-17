#!/bin/bash
# Build Vue frontend and deploy to static directory
#
# Vite base is '/static/', so index.html references /static/assets/...
# Correct deploy = copy dist/ contents to static/ (not static/frontend/)
# Root index.html is the server entry; static/index.html is the standalone entry.

set -e

cd "$(dirname "$0")"

echo "Building Vue frontend..."
npm run build

# Backup existing index.html
if [ -f "../index.html" ]; then
  BACKUP="../index.html.bak.$(date +%Y%m%d%H%M%S)"
  cp "../index.html" "$BACKUP"
  echo "Backed up existing index.html to $BACKUP"
fi

# Deploy root index.html (references /static/assets/...)
if [ -f "dist/index.html" ]; then
  cp "dist/index.html" "../index.html"
  echo "Updated: ../index.html"
fi

# Deploy to static/ (rsync with --delete to clean stale hash chunks)
if [ -d "dist/assets" ]; then
  mkdir -p "../static/assets"
  rsync -a --delete "dist/assets/" "../static/assets/"
  echo "Updated: ../static/assets/"
fi

# Deploy standalone static/index.html
if [ -f "dist/index.html" ]; then
  cp "dist/index.html" "../static/index.html"
  echo "Updated: ../static/index.html"
fi

echo "Build complete!"
