#!/usr/bin/env bash
# traework-refresh.sh —— 本机运行，导出 Traework 凭证
# 用法: ./traework-refresh.sh
set -euo pipefail

STORAGE_FILE="${APPDATA}/Trae CN/User/globalStorage/storage.json"
[ -f "$STORAGE_FILE" ] || { echo "找不到 storage.json"; exit 1; }

# 提取 iCubeAuthInfo 节点下的加密 token
TOKEN=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
print(data.get('iCubeAuthInfo://icube.cloudide', ''))
" "$STORAGE_FILE")

echo "TRAE_SESSION=$TOKEN"
echo "TRAE_DEVICE_ID=$(uuidgen)"
echo
echo "→ 请将以上值分别填入 GitHub 仓库 Secret"
echo "→ 不要把本脚本输出粘贴到任何公开渠道"
