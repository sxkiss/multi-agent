#!/bin/bash
# mem0 记忆 → 知识库自动同步（每10分钟）
cd /home/sxkiss/bt
.venv/bin/python3 scripts/import_mem0_to_kb.py >> mem0_import.log 2>&1
