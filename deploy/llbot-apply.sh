#!/usr/bin/env bash
# LLBot（有头 QQ）部署脚本：敏感值一律从这里输入，不写进仓库文件。
#
# 用法：
#   export LLBOT_QQ=3559594073
#   export LLBOT_AUTH_TOKEN='<auth.luckylillia.com 的 Auth Token>'
#   export LLBOT_WEBUI_TOKEN='<WebUI 登录密码>'
#   export LLBOT_SATORI_TOKEN='<satori 侧给 qq-llbot-service 用的 token>'
#   bash deploy/llbot-apply.sh
#
# 也可以不 export，脚本会交互式提示（token 用 read -s 不回显）。
#
# 做的事：
#   1. 把 llbot 运行所需文件写进 NFS 数据目录（config_<QQ>.json / auth_token.txt / webui_token.txt）
#   2. 用同一个 Auth Token 建/更新 k8s Secret `llbot-secrets`（pmhq 用它登录 QQ）
#   3. apply deploy/services/llbot.yaml 与 deploy/services/llbot-pmhq.yaml
#
# 机密只存在于：NFS 数据目录（运行必需）+ k8s Secret。仓库里只有占位符。
set -euo pipefail

NAMESPACE="${NAMESPACE:-agent}"
# llbot 的 data 目录（挂到容器 /app/llbot/data）与 pmhq 的 QQ 数据目录
LLBOT_DATA_DIR="${LLBOT_DATA_DIR:-/srv/nfs/my-agent/llbot}"
LLBOT_PMHQ_DIR="${LLBOT_PMHQ_DIR:-/srv/nfs/my-agent/llbot-pmhq}"

prompt() {
  # $1=变量名 $2=提示语 $3=是否静默(1/0)
  local __var="$1" __hint="$2" __quiet="${3:-0}" __value=""
  while [ -z "$__value" ]; do
    if [ "$__quiet" = "1" ]; then
      read -r -s -p "${__hint}: " __value
      echo
    else
      read -r -p "${__hint}: " __value
    fi
  done
  printf -v "$__var" '%s' "$__value"
}

LLBOT_QQ="${LLBOT_QQ:-}"
[ -z "$LLBOT_QQ" ] && prompt LLBOT_QQ "QQ 号（机器人自己的）" 0
[[ "$LLBOT_QQ" =~ ^[0-9]+$ ]] || { echo "QQ 号必须是数字"; exit 1; }

LLBOT_AUTH_TOKEN="${LLBOT_AUTH_TOKEN:-}"
[ -z "$LLBOT_AUTH_TOKEN" ] && prompt LLBOT_AUTH_TOKEN "Auth Token（auth.luckylillia.com）" 1

LLBOT_WEBUI_TOKEN="${LLBOT_WEBUI_TOKEN:-}"
[ -z "$LLBOT_WEBUI_TOKEN" ] && prompt LLBOT_WEBUI_TOKEN "WebUI 登录密码" 1

LLBOT_SATORI_TOKEN="${LLBOT_SATORI_TOKEN:-}"
[ -z "$LLBOT_SATORI_TOKEN" ] && prompt LLBOT_SATORI_TOKEN "satori token（qq-llbot-service 用它连）" 1

SATORI_PORT="${SATORI_PORT:-5600}"
LLBOT_IMAGE="${LLBOT_IMAGE:-hub.rat.dev/linyuchen/llbot:latest}"
PMHQ_IMAGE="${PMHQ_IMAGE:-hub.rat.dev/linyuchen/pmhq:latest}"

echo
echo "== 目标 =="
echo "  namespace      : ${NAMESPACE}"
echo "  QQ             : ${LLBOT_QQ}"
echo "  llbot data     : ${LLBOT_DATA_DIR}"
echo "  pmhq QQ data   : ${LLBOT_PMHQ_DIR}"
echo "  satori         : 0.0.0.0:${SATORI_PORT}（token 已设置，不显示）"
echo "  images         : ${LLBOT_IMAGE} / ${PMHQ_IMAGE}"
echo

mkdir -p "${LLBOT_DATA_DIR}" "${LLBOT_PMHQ_DIR}"

# llbot 的账号配置：satori 开启、webui 开启；不写任何 token 到仓库文件
cat > "${LLBOT_DATA_DIR}/config_${LLBOT_QQ}.json" <<JSON
{
  "webui": { "enable": true, "host": "", "port": 3080 },
  "milky": {
    "enable": false,
    "reportSelfMessage": false,
    "http": { "host": "127.0.0.1", "port": 3010, "prefix": "", "accessToken": "" },
    "webhook": { "urls": [], "accessToken": "" }
  },
  "satori": { "enable": true, "host": "", "port": ${SATORI_PORT}, "token": "${LLBOT_SATORI_TOKEN}" },
  "ob11": { "enable": false, "connect": [] },
  "enableLocalFile2Url": false,
  "log": true,
  "autoDeleteFile": false,
  "autoDeleteFileSecond": 60,
  "musicSignUrl": "https://ss.xingzhige.com/music_card/card",
  "msgCacheExpire": 120,
  "ffmpeg": "/usr/bin/ffmpeg"
}
JSON

printf '%s' "${LLBOT_AUTH_TOKEN}" > "${LLBOT_DATA_DIR}/auth_token.txt"
printf '%s' "${LLBOT_WEBUI_TOKEN}" > "${LLBOT_DATA_DIR}/webui_token.txt"
chmod 600 "${LLBOT_DATA_DIR}/auth_token.txt" "${LLBOT_DATA_DIR}/webui_token.txt"
echo "[OK] 运行文件已写入 ${LLBOT_DATA_DIR}"

# 敏感值进 Secret，清单里只引用 secretKeyRef
kubectl -n "${NAMESPACE}" create secret generic llbot-secrets \
  --from-literal=pmhq-auth-token="${LLBOT_AUTH_TOKEN}" \
  --from-literal=auto-login-qq="${LLBOT_QQ}" \
  --dry-run=client -o yaml | kubectl apply -f -
echo "[OK] Secret llbot-secrets 已更新"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NFS_SERVER="${NFS_SERVER:-}"
if [ -z "${NFS_SERVER}" ]; then
  read -r -p "NFS 服务器地址（例如 10.0.0.10，仓库里不写死）: " NFS_SERVER
fi
[ -z "${NFS_SERVER}" ] && { echo "必须提供 NFS_SERVER"; exit 1; }
NFS_PATH_LLBOT="${NFS_PATH_LLBOT:-/srv/nfs/my-agent/llbot}"
NFS_PATH_PMHQ="${NFS_PATH_PMHQ:-/srv/nfs/my-agent/llbot-pmhq}"
export NAMESPACE LLBOT_IMAGE PMHQ_IMAGE LLBOT_DATA_DIR LLBOT_PMHQ_DIR \
       NFS_SERVER NFS_PATH_LLBOT NFS_PATH_PMHQ

for manifest in "${SCRIPT_DIR}/services/llbot.yaml" "${SCRIPT_DIR}/services/llbot-pmhq.yaml"; do
  # 清单里用 ${VAR} 占位（镜像、NFS 路径），其余靠 secretKeyRef，不放明文
  envsubst < "${manifest}" | kubectl apply -f -
done

echo
echo "[OK] 已应用。查看登录二维码："
echo "  kubectl -n ${NAMESPACE} logs deploy/llbot -c llbot -f | grep -A2 'txz.qq.com'"
