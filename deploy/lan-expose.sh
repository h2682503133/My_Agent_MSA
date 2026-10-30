#!/usr/bin/env bash
# 局域网暴露工具：把集群里的 Web 界面按**原端口**发布到宿主机，局域网设备可直接访问。
#
# 为什么不用 kubectl port-forward：
#   port-forward 是普通用户进程，入站要过 Windows 防火墙（Domain profile 常拦）；
#   而 Docker 发布的端口走 Docker 自己的放行规则 —— 这也是 80 端口天然可达的原因。
#   所以这里给每个界面建一个 NodePort，再用 socat 容器把宿主端口转发到 NodePort。
#
# 用法：
#   bash deploy/lan-expose.sh                # 建/更新 NodePort + 起 socat 容器（幂等）
#   bash deploy/lan-expose.sh --check        # 只探测本机各端口是否可用
#   bash deploy/lan-expose.sh --status       # 列出容器与 Service 状态
#   bash deploy/lan-expose.sh --down         # 拆掉 socat 容器与 NodePort Service
#
# 可覆盖的环境变量：
#   NAMESPACE      默认 agent
#   SOCAT_IMAGE    默认 alpine/socat:latest；国内可给镜像前缀，如 hub.rat.dev/alpine/socat:latest
#   KIND_NETWORK   默认 kind（socat 容器加入这个网络才能访问节点 IP）
#   NODE_IP        默认自动探测控制节点的 INTERNAL-IP
set -euo pipefail

NAMESPACE="${NAMESPACE:-agent}"
SOCAT_IMAGE="${SOCAT_IMAGE:-alpine/socat:latest}"
KIND_NETWORK="${KIND_NETWORK:-kind}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="${SCRIPT_DIR}/services/lan-expose.yaml"

# host_port:node_port:名称:健康检查路径
EXPOSES=(
  "5210:30521:gateway-api:/api/health"
  "5700:30570:dashboard:/"
  "3080:30080:llbot-webui:/"
  "5102:30510:image-assets:/assets/"
)

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "缺少命令：$1"; exit 1; }
}

detect_node_ip() {
  if [ -n "${NODE_IP:-}" ]; then
    echo "${NODE_IP}"
    return
  fi
  local ip
  ip=$(kubectl get nodes -o wide --no-headers 2>/dev/null |
    awk '$3 == "control-plane" { print $6; exit }')
  if [ -z "${ip}" ]; then
    ip=$(kubectl get nodes -o wide --no-headers 2>/dev/null | awk 'NR == 1 { print $6 }')
  fi
  if [ -z "${ip}" ]; then
    echo "无法探测节点 IP，请显式设置 NODE_IP" >&2
    exit 1
  fi
  echo "${ip}"
}

container_name() {
  echo "lan-$1"
}

do_up() {
  require kubectl
  require docker

  local node_ip
  node_ip="$(detect_node_ip)"
  echo "节点 IP: ${node_ip}  网络: ${KIND_NETWORK}  镜像: ${SOCAT_IMAGE}"
  echo

  echo "== 1) 应用 NodePort Service =="
  kubectl apply -f "${MANIFEST}"

  echo
  echo "== 2) 拉取 socat 镜像 =="
  docker pull "${SOCAT_IMAGE}" >/dev/null
  echo "ok"

  echo
  echo "== 3) 起转发容器（幂等：先删后建）=="
  for item in "${EXPOSES[@]}"; do
    local host_port node_port name health
    IFS=':' read -r host_port node_port name health <<<"${item}"
    local cname
    cname="$(container_name "${name}")"
    docker rm -f "${cname}" >/dev/null 2>&1 || true
    # 兼容旧命名（llbot-lan / dashboard-lan / gateway-lan）以及任何占着该宿主端口的容器：
    # 不先清掉会报 "port is already allocated"。
    for legacy in "${name}-lan"; do
      docker rm -f "${legacy}" >/dev/null 2>&1 || true
    done
    while read -r holder; do
      [ -n "${holder}" ] && docker rm -f "${holder}" >/dev/null 2>&1 || true
    done < <(docker ps --filter "publish=${host_port}" --format '{{.Names}}')
    docker run -d --name "${cname}" --restart unless-stopped \
      --network "${KIND_NETWORK}" \
      -p "${host_port}:${host_port}" \
      "${SOCAT_IMAGE}" \
      tcp-listen:"${host_port}",fork,reuseaddr tcp-connect:"${node_ip}:${node_port}" >/dev/null
    echo "  ${cname}: 宿主 ${host_port} → ${node_ip}:${node_port}"
  done

  echo
  do_check
}

do_check() {
  echo "== 探测（本机回环）=="
  local failed=0
  for item in "${EXPOSES[@]}"; do
    local host_port _node name health
    IFS=':' read -r host_port _node name health <<<"${item}"
    health="${health:-/}"
    if command -v curl >/dev/null 2>&1; then
      local code
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${host_port}${health}" || true)
      echo "  ${name} (${host_port}${health}): HTTP ${code}"
      case "${code}" in
        000|"") failed=1 ;;
      esac
    else
      echo "  ${name} (${host_port}): 无 curl，跳过"
    fi
  done
  [ "${failed}" -eq 0 ] || { echo "有端口不通，检查容器：docker logs lan-<name>"; return 1; }
}

do_status() {
  echo "== NodePort Service =="
  kubectl -n "${NAMESPACE}" get svc gateway-lan dashboard-lan llbot-webui-np \
    -o custom-columns=NAME:.metadata.name,TYPE:.spec.type,PORT:.spec.ports[*].port,NODEPORT:.spec.ports[*].nodePort 2>/dev/null || true
  echo
  echo "== socat 容器 =="
  docker ps --filter "name=lan-" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
}

do_down() {
  echo "== 拆 socat 容器 =="
  for item in "${EXPOSES[@]}"; do
    local name
    IFS=':' read -r _ _ name _ <<<"${item}"
    docker rm -f "$(container_name "${name}")" >/dev/null 2>&1 && echo "  已删除 $(container_name "${name}")" || echo "  不存在 $(container_name "${name}")"
  done
  echo
  echo "== 删 NodePort Service =="
  kubectl -n "${NAMESPACE}" delete svc gateway-lan dashboard-lan llbot-webui-np --ignore-not-found
}

case "${1:-up}" in
  up|"") do_up ;;
  --check|check) do_check ;;
  --status|status) do_status ;;
  --down|down) do_down ;;
  -h|--help|help)
    sed -n '2,20p' "$0"
    ;;
  *)
    echo "未知参数：$1（up / --check / --status / --down）"
    exit 1
    ;;
esac
