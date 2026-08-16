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
mkdir -p "$NFS_ROOT/config/orchestrator/config"
cp "$REPO_ROOT/config/orchestrator/config/agent_list.json" "$NFS_ROOT/config/orchestrator/config/" 2>/dev/null || true
cp "$REPO_ROOT/config/orchestrator/config/system_settings.json" "$NFS_ROOT/config/orchestrator/config/" 2>/dev/null || true

# model-proxy 配置
mkdir -p "$NFS_ROOT/config/model-proxy/config"
cp "$REPO_ROOT/config/model-proxy/config/model_list.json" "$NFS_ROOT/config/model-proxy/config/" 2>/dev/null || true

# system_prompt（每个智能体一个子目录）
mkdir -p "$NFS_ROOT/config/orchestrator/system_prompt"
if [ -d "$REPO_ROOT/config/orchestrator/system_prompt" ]; then
  cp -r "$REPO_ROOT/config/orchestrator/system_prompt/"* "$NFS_ROOT/config/orchestrator/system_prompt/" 2>/dev/null || true
fi

# openviking 配置（ov.conf / api_key / root_api_key，需先手动填写 ov.conf）
mkdir -p "$NFS_ROOT/config/openviking"
if [ -d "$REPO_ROOT/config/openviking" ]; then
  cp -r "$REPO_ROOT/config/openviking/." "$NFS_ROOT/config/openviking/" 2>/dev/null || true
fi

# qq-llbot 配置
mkdir -p "$NFS_ROOT/config/qq-llbot"
cp "$REPO_ROOT/config/qq-llbot/qq_llbot_config.json" "$NFS_ROOT/config/qq-llbot/" 2>/dev/null || true

echo "[OK] 同步完成。"
echo
echo "NFS config 目录结构："
find "$NFS_ROOT/config" -maxdepth 4 -type f | sort
