#!/bin/bash
# Build Vue frontend and deploy to plugin directory

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

# Copy build output to plugin root
if [ -f "dist/index.html" ]; then
  cp "dist/index.html" "../index.html"
  echo "Updated: ../index.html"
fi

if [ -d "dist/assets" ]; then
  mkdir -p "../static/frontend"
  cp -r "dist/assets/"* "../static/frontend/"
  echo "Updated: ../static/frontend/"
fi

echo "Build complete!"
