#!/usr/bin/env bash
set -euo pipefail

# 推荐放在 openviking-context-service 的上一级目录。
#
# parent-dir/
# ├── init_openviking_host_pv_parent.sh
# └── openviking-context-service/
#
# 用途：
#   创建宿主机 /data/my-agent/openviking-context/viking_data
#
# 如果你是 kind / Docker Desktop 节点容器环境，hostPath 目录仍需存在于实际节点中。
# 可参考：
#   docker exec -it desktop-worker mkdir -p /data/my-agent/openviking-context/viking_data

TARGET_DIR="${1:-/data/my-agent/openviking-context/viking_data}"

echo "[INFO] target dir: $TARGET_DIR"
mkdir -p "$TARGET_DIR"
chmod 755 "$TARGET_DIR"

echo "[DONE] openviking host PV dir prepared:"
echo "  $TARGET_DIR"
