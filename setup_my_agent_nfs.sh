#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# setup_my_agent_nfs.sh
#
# 用途：
#   在当前 Linux / WSL 机器上创建 My_Agent MSA 使用的 NFS 共享目录。
#
# 默认 NFS 根目录：
#   /srv/nfs/my-agent
#
# K8s PV YAML 中对应：
#   CHANGE_ME_NFS_EXPORT_ROOT=/srv/nfs/my-agent
#
# 使用方式：
#   chmod +x setup_my_agent_nfs.sh
#   sudo ./setup_my_agent_nfs.sh
#
# 可选：
#   指定 NFS 根目录：
#     sudo ./setup_my_agent_nfs.sh /srv/nfs/my-agent
#
#   跳过 apt 安装：
#     sudo SKIP_INSTALL=1 ./setup_my_agent_nfs.sh
#
#   跳过写入 exports：
#     sudo SKIP_EXPORTS=1 ./setup_my_agent_nfs.sh
#
#   限制允许访问的客户端网段，默认 *：
#     sudo CLIENT_ALLOW="192.168.1.0/24" ./setup_my_agent_nfs.sh
#
# 创建后的目录结构：
#   /srv/nfs/my-agent/
#   ├── config/
#   ├── openviking/
#   ├── assets/
#   ├── workspace/
#   └── timer-tasks/
# ============================================================

NFS_ROOT="${1:-/srv/nfs/my-agent}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"
SKIP_EXPORTS="${SKIP_EXPORTS:-0}"
CLIENT_ALLOW="${CLIENT_ALLOW:-*}"

if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] 请使用 sudo 执行："
  echo "  sudo $0"
  exit 1
fi

echo "[INFO] NFS root     : $NFS_ROOT"
echo "[INFO] CLIENT_ALLOW : $CLIENT_ALLOW"

# 1. 安装 NFS server
if [ "$SKIP_INSTALL" = "0" ]; then
  if command -v apt >/dev/null 2>&1; then
    echo "[INFO] installing nfs-kernel-server..."
    apt update
    apt install -y nfs-kernel-server
  else
    echo "[WARN] 未检测到 apt，跳过自动安装。"
    echo "       请手动安装 nfs server，例如 nfs-kernel-server。"
  fi
else
  echo "[INFO] SKIP_INSTALL=1，跳过安装。"
fi

# 2. 创建目录结构
echo "[INFO] creating NFS directories..."

mkdir -p "$NFS_ROOT/config/orchestrator/config"
mkdir -p "$NFS_ROOT/config/orchestrator/system_prompt/main"
mkdir -p "$NFS_ROOT/config/orchestrator/system_prompt/tool"
mkdir -p "$NFS_ROOT/config/orchestrator/system_prompt/reader"
mkdir -p "$NFS_ROOT/config/model-proxy/config"
mkdir -p "$NFS_ROOT/config/tool-runtime/config"

mkdir -p "$NFS_ROOT/openviking/viking_data"
mkdir -p "$NFS_ROOT/assets"
mkdir -p "$NFS_ROOT/workspace"
mkdir -p "$NFS_ROOT/timer-tasks"
mkdir -p "$NFS_ROOT/user-data"

# 本地开发阶段给宽权限，避免 Pod 写入权限问题。
chmod -R 777 "$NFS_ROOT"

echo "[OK] directories prepared."

# 3. 配置 exports
if [ "$SKIP_EXPORTS" = "0" ]; then
  EXPORT_FILE="/etc/exports.d/my-agent.exports"

  echo "[INFO] writing export file: $EXPORT_FILE"

  cat > "$EXPORT_FILE" <<EOF
$NFS_ROOT $CLIENT_ALLOW(rw,sync,no_subtree_check,no_root_squash,insecure)
EOF

  echo "[INFO] applying exports..."
  exportfs -ra

  # WSL / systemd / 非 systemd 环境兼容处理
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files | grep -q '^nfs-kernel-server'; then
    systemctl enable nfs-kernel-server >/dev/null 2>&1 || true
    systemctl restart nfs-kernel-server || true
  fi

  if command -v service >/dev/null 2>&1; then
    service nfs-kernel-server restart || true
  fi

  echo "[OK] exports applied."
else
  echo "[INFO] SKIP_EXPORTS=1，跳过写入 /etc/exports.d。"
fi

# 4. 输出检查信息
echo
echo "[DONE] NFS for My_Agent MSA is prepared."
echo
echo "NFS root:"
echo "  $NFS_ROOT"
echo
echo "Export check:"
echo "  sudo exportfs -v"
echo
echo "Server IP candidates:"
if command -v hostname >/dev/null 2>&1; then
  hostname -I || true
fi
echo
echo "K8s YAML replacement:"
echo "  CHANGE_ME_NFS_EXPORT_ROOT = $NFS_ROOT"
echo "  CHANGE_ME_NFS_SERVER      = 从上面 hostname -I 选择 K8s 节点可访问的 IP"
echo
echo "Example paths in PV YAML:"
echo "  path: $NFS_ROOT/config"
echo "  path: $NFS_ROOT/openviking/viking_data"
echo "  path: $NFS_ROOT/assets"
echo "  path: $NFS_ROOT/workspace"
echo "  path: $NFS_ROOT/timer-tasks"
echo
echo "Directory tree:"
find "$NFS_ROOT" -maxdepth 4 -type d | sort
