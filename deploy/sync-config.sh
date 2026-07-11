#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# sync-config.sh
#
# 用途：
#   将仓库 config/ 目录同步到 NFS 共享目录。
#
# 使用方式：
#   chmod +x sync-config.sh
#   bash sync-config.sh
#
# 默认 NFS 根目录：
#   /srv/nfs/my-agent
#
# 可选指定 NFS 根目录：
#   NFS_ROOT=/srv/nfs/my-agent bash sync-config.sh
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
NFS_ROOT="${NFS_ROOT:-/srv/nfs/my-agent}"

echo "[INFO] 同步 config 到 NFS: $NFS_ROOT/config/"

# orchestrator 配置
cp "$REPO_ROOT/config/agent_list.json" "$NFS_ROOT/config/orchestrator/config/" 2>/dev/null || true
cp "$REPO_ROOT/config/model_list.json" "$NFS_ROOT/config/model-proxy/config/" 2>/dev/null || true

# system_prompt
if [ -d "$REPO_ROOT/config/system_prompt" ]; then
  cp -r "$REPO_ROOT/config/system_prompt/"* "$NFS_ROOT/config/orchestrator/system_prompt/" 2>/dev/null || true
fi

echo "[OK] 同步完成。"
echo
echo "NFS config 目录结构："
find "$NFS_ROOT/config" -maxdepth 4 -type f | sort
