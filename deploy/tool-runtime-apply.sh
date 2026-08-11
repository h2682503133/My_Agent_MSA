#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-agent}"
TOOL_RUNTIME_IMAGE="${TOOL_RUNTIME_IMAGE:-agent/tool-runtime-service:v30}"

OPENVIKING_SERVER_URL="${OPENVIKING_SERVER_URL:-http://openviking.agent.svc.cluster.local:1933}"
OPENVIKING_API_KEY="${OPENVIKING_API_KEY:-/app/system_prompts/openviking/api_key}"
OPENVIKING_ACCOUNT="${OPENVIKING_ACCOUNT:-my-agent}"
OPENVIKING_USER="${OPENVIKING_USER:-system}"
OPENVIKING_AGENT="${OPENVIKING_AGENT:-skills}"

CLAW_DOWNLOAD_MODE="${CLAW_DOWNLOAD_MODE:-external-vm}"
CLAW_EXTERNAL_VM_PORT="${CLAW_EXTERNAL_VM_PORT:-22}"
CLAW_EXTERNAL_VM_USER="${CLAW_EXTERNAL_VM_USER:-$(id -un)}"
CLAW_EXTERNAL_VM_SKILL_ROOT_DIR="${CLAW_EXTERNAL_VM_SKILL_ROOT_DIR:-/srv/nfs/my-agent/workspace/skill}"
CLAW_EXTERNAL_VM_STRICT_HOST_KEY_CHECKING="${CLAW_EXTERNAL_VM_STRICT_HOST_KEY_CHECKING:-false}"

# 处理 sudo 场景：保存真实用户信息，避免 root 环境下找不到 nvm/ssh key
REAL_USER="${SUDO_USER:-$(id -un)}"
REAL_HOME="$(eval echo ~"$REAL_USER")"

MY_AGENT_SSH_KEY_FILE="${MY_AGENT_SSH_KEY_FILE:-$REAL_HOME/.ssh/my_agent_tool_runtime_ed25519}"
MY_AGENT_CLAWHUB_WRAPPER="${MY_AGENT_CLAWHUB_WRAPPER:-$REAL_HOME/.local/bin/my-agent-clawhub}"

# 图床外部 URL：通过 host.docker.internal 解析宿主机 IP
export IMAGE_BASE_URL="http://localhost:5102/assets"

sudo_cmd() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

require_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "缺少命令: $name"
    exit 1
  fi
}

detect_host_ip() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 route get 1.1.1.1 2>/dev/null | awk '
      {
        for (i = 1; i <= NF; i++) {
          if ($i == "src") {
            print $(i + 1)
            exit
          }
        }
      }
    '
    return
  fi
  hostname -I 2>/dev/null | awk '{print $1}'
}

detect_clawhub_real_bin() {
  if [ -n "${CLAW_REAL_CLAWHUB_BIN:-}" ] && [ -x "${CLAW_REAL_CLAWHUB_BIN}" ]; then
    echo "${CLAW_REAL_CLAWHUB_BIN}"
    return
  fi
  # 优先用真实用户的 PATH 查找（sudo 下 command -v 找不到 nvm 里的命令）
  if [ "$(id -un)" != "$REAL_USER" ] && sudo -u "$REAL_USER" command -v clawhub >/dev/null 2>&1; then
    sudo -u "$REAL_USER" command -v clawhub
    return
  fi
  if command -v clawhub >/dev/null 2>&1; then
    command -v clawhub
    return
  fi
  if [ -d "$REAL_HOME/.nvm/versions/node" ]; then
    find "$REAL_HOME/.nvm/versions/node" -path "*/bin/clawhub" -type f -perm -u+x 2>/dev/null | sort -V | tail -n 1
    return
  fi
}

detect_codex_real_bin() {
  if [ -n "${CODEX_REAL_BIN:-}" ] && [ -x "${CODEX_REAL_BIN}" ]; then
    echo "${CODEX_REAL_BIN}"
    return
  fi
  # 优先用真实用户的 PATH 查找（sudo 下 command -v 找不到 nvm 里的命令）
  if [ "$(id -un)" != "$REAL_USER" ] && sudo -u "$REAL_USER" command -v codex >/dev/null 2>&1; then
    sudo -u "$REAL_USER" command -v codex
    return
  fi
  if command -v codex >/dev/null 2>&1; then
    command -v codex
    return
  fi
  if [ -d "$REAL_HOME/.nvm/versions/node" ]; then
    find "$REAL_HOME/.nvm/versions/node" -path "*/bin/codex" -type f -perm -u+x 2>/dev/null | sort -V | tail -n 1
    return
  fi
}

detect_node_bin() {
  if [ "$(id -un)" != "$REAL_USER" ] && sudo -u "$REAL_USER" command -v node >/dev/null 2>&1; then
    sudo -u "$REAL_USER" command -v node
    return
  fi
  if command -v node >/dev/null 2>&1; then
    command -v node
    return
  fi
  if [ -d "$REAL_HOME/.nvm/versions/node" ]; then
    find "$REAL_HOME/.nvm/versions/node" -path "*/bin/node" -type f -perm -u+x 2>/dev/null | sort -V | tail -n 1
    return
  fi
}

ensure_clawhub_wrapper() {
  echo "检查 clawhub 远程执行包装器..."

  local real_clawhub
  local node_bin
  local wrapper_dir

  real_clawhub="$(detect_clawhub_real_bin || true)"
  node_bin="$(detect_node_bin || true)"

  if [ -z "$real_clawhub" ] || [ ! -x "$real_clawhub" ]; then
    echo "未找到可执行的 clawhub"
    echo "请先在当前 WSL / VM 安装 clawhub，并确认 command -v clawhub 有输出"
    exit 1
  fi

  if [ -z "$node_bin" ] || [ ! -x "$node_bin" ]; then
    echo "未找到可执行的 node"
    echo "clawhub 是 Node 脚本，远程 SSH 执行时必须能找到 node"
    exit 1
  fi

  wrapper_dir="$(dirname "$MY_AGENT_CLAWHUB_WRAPPER")"
  mkdir -p "$wrapper_dir"

  cat > "$MY_AGENT_CLAWHUB_WRAPPER" <<EOF
#!/usr/bin/env bash
set -euo pipefail

exec "${node_bin}" "${real_clawhub}" "\$@"
EOF

  chmod +x "$MY_AGENT_CLAWHUB_WRAPPER"

  CLAW_EXTERNAL_VM_CLAWHUB_BIN="$MY_AGENT_CLAWHUB_WRAPPER"
  export CLAW_EXTERNAL_VM_CLAWHUB_BIN

  echo "clawhub 包装器: $CLAW_EXTERNAL_VM_CLAWHUB_BIN"
  echo "真实 clawhub: $real_clawhub"
  echo "node: $node_bin"
}


MY_AGENT_CODEX_WRAPPER="${MY_AGENT_CODEX_WRAPPER:-$REAL_HOME/.local/bin/my-agent-codex}"

ensure_codex_wrapper() {
  echo "检查 codex 远程执行包装器..."

  local real_codex
  local node_bin
  local wrapper_dir

  real_codex="$(detect_codex_real_bin || true)"
  node_bin="$(detect_node_bin || true)"

  if [ -z "$real_codex" ] || [ ! -x "$real_codex" ]; then
    echo "未找到可执行的 codex"
    echo "请先在当前 WSL / VM 安装 codex，并确认 command -v codex 有输出"
    exit 1
  fi

  if [ -z "$node_bin" ] || [ ! -x "$node_bin" ]; then
    echo "未找到可执行的 node"
    echo "codex 是 Node 脚本，远程 SSH 执行时必须能找到 node"
    exit 1
  fi

  wrapper_dir="$(dirname "$MY_AGENT_CODEX_WRAPPER")"
  mkdir -p "$wrapper_dir"

  cat > "$MY_AGENT_CODEX_WRAPPER" <<EOF
#!/usr/bin/env bash
set -euo pipefail

exec "${node_bin}" "${real_codex}" "$@"
EOF

  chmod +x "$MY_AGENT_CODEX_WRAPPER"

  CODEX_BIN_PATH="$MY_AGENT_CODEX_WRAPPER"
  export CODEX_BIN_PATH

  echo "codex 包装器: $CODEX_BIN_PATH"
  echo "真实 codex: $real_codex"
  echo "node: $node_bin"
}

ensure_sshd_installed() {
  if command -v sshd >/dev/null 2>&1 || [ -x /usr/sbin/sshd ]; then
    return 0
  fi

  echo "未检测到 sshd，正在安装 openssh-server..."
  if command -v apt-get >/dev/null 2>&1; then
    sudo_cmd apt-get update
    sudo_cmd apt-get install -y openssh-server
  else
    echo "当前系统未找到 apt-get，请手动安装 openssh-server"
    exit 1
  fi
}

ensure_sshd_running() {
  echo "检查并启动 sshd..."

  sudo_cmd mkdir -p /run/sshd

  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files ssh.service >/dev/null 2>&1; then
    sudo_cmd systemctl enable ssh >/dev/null 2>&1 || true
    sudo_cmd systemctl start ssh || true
  fi

  if command -v service >/dev/null 2>&1; then
    sudo_cmd service ssh start || true
  fi

  if ! ss -lnt 2>/dev/null | grep -q ":${CLAW_EXTERNAL_VM_PORT} "; then
    if [ -x /usr/sbin/sshd ]; then
      sudo_cmd /usr/sbin/sshd || true
    else
      sudo_cmd sshd || true
    fi
  fi

  if ! ss -lnt 2>/dev/null | grep -q ":${CLAW_EXTERNAL_VM_PORT} "; then
    echo "sshd 未能在端口 ${CLAW_EXTERNAL_VM_PORT} 启动"
    echo "请检查: sudo service ssh status"
    exit 1
  fi
}

ensure_ssh_key_authorized() {
  echo "检查 My_Agent 专用 SSH key..."

  mkdir -p "$REAL_HOME/.ssh"
  chmod 700 "$REAL_HOME/.ssh"

  if [ ! -f "$MY_AGENT_SSH_KEY_FILE" ]; then
    echo "生成 SSH key: $MY_AGENT_SSH_KEY_FILE"
    ssh-keygen -t ed25519 -f "$MY_AGENT_SSH_KEY_FILE" -N "" -C "my-agent-tool-runtime"
  fi

  if [ ! -f "${MY_AGENT_SSH_KEY_FILE}.pub" ]; then
    ssh-keygen -y -f "$MY_AGENT_SSH_KEY_FILE" > "${MY_AGENT_SSH_KEY_FILE}.pub"
  fi

  touch "$REAL_HOME/.ssh/authorized_keys"

  if ! grep -qxF "$(cat "${MY_AGENT_SSH_KEY_FILE}.pub")" "$REAL_HOME/.ssh/authorized_keys"; then
    echo "写入 authorized_keys"
    cat "${MY_AGENT_SSH_KEY_FILE}.pub" >> "$REAL_HOME/.ssh/authorized_keys"
  fi

  chmod 600 "$REAL_HOME/.ssh/authorized_keys"
  chmod 600 "$MY_AGENT_SSH_KEY_FILE"
  chmod 644 "${MY_AGENT_SSH_KEY_FILE}.pub"
}

ensure_skill_root_dir() {
  echo "检查外部 VM / WSL skill 目录..."

  mkdir -p "$CLAW_EXTERNAL_VM_SKILL_ROOT_DIR" || {
    echo "无法创建目录: $CLAW_EXTERNAL_VM_SKILL_ROOT_DIR"
    echo "请检查当前用户是否有权限，或先手动创建 NFS 共享目录"
    exit 1
  }

  if [ ! -w "$CLAW_EXTERNAL_VM_SKILL_ROOT_DIR" ]; then
    echo "当前用户对 skill 目录没有写权限: $CLAW_EXTERNAL_VM_SKILL_ROOT_DIR"
    echo "请执行类似命令修复："
    echo "sudo chown -R ${CLAW_EXTERNAL_VM_USER}:${CLAW_EXTERNAL_VM_USER} ${CLAW_EXTERNAL_VM_SKILL_ROOT_DIR}"
    exit 1
  fi
}

verify_local_ssh_login() {
  echo "验证本机 SSH 登录..."

  local ssh_log
  ssh_log="$(mktemp)" || ssh_log="/tmp/my_agent_ssh_check_$$.log"

  # 以真实用户身份执行 SSH 测试（避免 root 的 known_hosts 干扰）
  if [ "$(id -un)" != "$REAL_USER" ]; then
    sudo -u "$REAL_USER" ssh       -i "$MY_AGENT_SSH_KEY_FILE"       -o StrictHostKeyChecking=no       -o UserKnownHostsFile=/dev/null       -o PasswordAuthentication=no       -o ConnectTimeout=5       -p "$CLAW_EXTERNAL_VM_PORT"       "${CLAW_EXTERNAL_VM_USER}@127.0.0.1"       "whoami && ${CLAW_EXTERNAL_VM_CLAWHUB_BIN} -V && ${CODEX_BIN_PATH} --version" >"$ssh_log" 2>&1
  else
    ssh       -i "$MY_AGENT_SSH_KEY_FILE"       -o StrictHostKeyChecking=no       -o UserKnownHostsFile=/dev/null       -o PasswordAuthentication=no       -o ConnectTimeout=5       -p "$CLAW_EXTERNAL_VM_PORT"       "${CLAW_EXTERNAL_VM_USER}@127.0.0.1"       "whoami && ${CLAW_EXTERNAL_VM_CLAWHUB_BIN} -V && ${CODEX_BIN_PATH} --version" >"$ssh_log" 2>&1
  fi

  local ssh_rc=$?
  if [ $ssh_rc -ne 0 ]; then
    echo "SSH 登录或 clawhub/codex 检测失败（退出码: $ssh_rc）"
    echo "详细输出："
    cat "$ssh_log"
    rm -f "$ssh_log"
    echo
    echo "常见原因："
    echo "  1. sshd 未启动：sudo service ssh start"
    echo "  2. 密钥未授权：确保 ${MY_AGENT_SSH_KEY_FILE}.pub 在 ${REAL_HOME}/.ssh/authorized_keys 中"
    echo "  3. 防火墙阻止：检查端口 ${CLAW_EXTERNAL_VM_PORT}"
    echo "  4. PasswordAuthentication 未关闭导致密钥认证失败"
    exit 1
  fi

  rm -f "$ssh_log"
  echo "本机 SSH 登录验证通过"
}

main() {
  require_command kubectl
  require_command envsubst
  require_command ssh
  require_command ssh-keygen
  require_command ss

  CLAW_EXTERNAL_VM_HOST="${CLAW_EXTERNAL_VM_HOST:-$(detect_host_ip)}"

  if [ -z "$CLAW_EXTERNAL_VM_HOST" ]; then
    echo "无法自动获取当前 WSL / VM 的 IPv4"
    echo "可以手动执行：export CLAW_EXTERNAL_VM_HOST=<你的WSL或VM IP>"
    exit 1
  fi

  ensure_sshd_installed
  ensure_sshd_running
  ensure_ssh_key_authorized
  ensure_skill_root_dir
  ensure_clawhub_wrapper
  ensure_codex_wrapper
  verify_local_ssh_login

  echo
  echo "将应用以下配置："
  echo "  NAMESPACE: ${NAMESPACE}"
  echo "  TOOL_RUNTIME_IMAGE: ${TOOL_RUNTIME_IMAGE}"
  echo "  OPENVIKING_SERVER_URL: ${OPENVIKING_SERVER_URL}"
  echo "  OPENVIKING_API_KEY: ${OPENVIKING_API_KEY}"
  echo "  OPENVIKING_ACCOUNT: ${OPENVIKING_ACCOUNT}"
  echo "  OPENVIKING_USER: ${OPENVIKING_USER}"
  echo "  OPENVIKING_AGENT: ${OPENVIKING_AGENT}"
  echo "  CLAW_DOWNLOAD_MODE: ${CLAW_DOWNLOAD_MODE}"
  echo "  CLAW_EXTERNAL_VM_HOST: ${CLAW_EXTERNAL_VM_HOST}"
  echo "  CLAW_EXTERNAL_VM_USER: ${CLAW_EXTERNAL_VM_USER}"
  echo "  CLAW_EXTERNAL_VM_PORT: ${CLAW_EXTERNAL_VM_PORT}"
  echo "  CLAW_EXTERNAL_VM_SSH_KEY_FILE: ${MY_AGENT_SSH_KEY_FILE}"
  echo "  CLAW_EXTERNAL_VM_SKILL_ROOT_DIR: ${CLAW_EXTERNAL_VM_SKILL_ROOT_DIR}"
  echo "  CLAW_EXTERNAL_VM_CLAWHUB_BIN: ${CLAW_EXTERNAL_VM_CLAWHUB_BIN}"
  echo "  CODEX_BIN_PATH: ${CODEX_BIN_PATH}"
  echo

  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

  kubectl -n "${NAMESPACE}" create secret generic claw-external-vm-ssh \
    --from-file=id_rsa="${MY_AGENT_SSH_KEY_FILE}" \
    --dry-run=client -o yaml | kubectl apply -f -

  export \
    NAMESPACE \
    TOOL_RUNTIME_IMAGE \
    OPENVIKING_SERVER_URL \
    OPENVIKING_API_KEY \
    OPENVIKING_ACCOUNT \
    OPENVIKING_USER \
    OPENVIKING_AGENT \
    CLAW_DOWNLOAD_MODE \
    CLAW_EXTERNAL_VM_HOST \
    CLAW_EXTERNAL_VM_USER \
    CLAW_EXTERNAL_VM_PORT \
    CLAW_EXTERNAL_VM_SKILL_ROOT_DIR \
    CLAW_EXTERNAL_VM_CLAWHUB_BIN \
    CODEX_BIN_PATH 
    CLAW_EXTERNAL_VM_STRICT_HOST_KEY_CHECKING

  cat <<'YAML' | envsubst | kubectl apply -f -
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: searxng-config
  namespace: ${NAMESPACE}
data:
  settings.yml: |
    use_default_settings: true
    search:
      formats:
        - html
        - json
    server:
      secret_key: "my-agent-searxng-internal-key"
      bind_address: "0.0.0.0"
      limiter: false
      image_proxy: false
    ui:
      static_use_hash: true
  limiter.yml: |
    botdetection:
      ip_limit:
        filter_link_token: false
        link_token: false
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: tool-runtime-service
  namespace: ${NAMESPACE}
  labels:
    app: tool-runtime-service
spec:
  replicas: 1
  selector:
    matchLabels:
      app: tool-runtime-service
  template:
    metadata:
      labels:
        app: tool-runtime-service
      annotations:
        sidecar.istio.io/inject: "false"
    spec:
      containers:
        - name: tool-runtime-service
          image: ${TOOL_RUNTIME_IMAGE}
          imagePullPolicy: Always
          ports:
            - name: grpc
              containerPort: 5303
          env:
            - name: TOOL_RUNTIME_HOST
              value: "0.0.0.0"
            - name: TOOL_RUNTIME_PORT
              value: "5303"
            - name: WORKSPACE_DIR
              value: "/app/workspace"
            - name: ENABLE_SHELL_TOOLS
              value: "true"

            - name: OPENVIKING_SERVER_URL
              value: "${OPENVIKING_SERVER_URL}"
            - name: OPENVIKING_API_KEY
              value: "${OPENVIKING_API_KEY}"
            - name: OPENVIKING_ACCOUNT
              value: "${OPENVIKING_ACCOUNT}"
            - name: OPENVIKING_USER
              value: "${OPENVIKING_USER}"
            - name: OPENVIKING_AGENT
              value: "${OPENVIKING_AGENT}"

            - name: CLAW_DOWNLOAD_MODE
              value: "${CLAW_DOWNLOAD_MODE}"
            - name: CLAW_EXTERNAL_VM_HOST
              value: "${CLAW_EXTERNAL_VM_HOST}"
            - name: CLAW_EXTERNAL_VM_USER
              value: "${CLAW_EXTERNAL_VM_USER}"
            - name: CLAW_EXTERNAL_VM_PORT
              value: "${CLAW_EXTERNAL_VM_PORT}"
            - name: CLAW_EXTERNAL_VM_SSH_KEY
              value: "/app/secrets/claw-external-vm/id_rsa"
            - name: CLAW_EXTERNAL_VM_SKILL_ROOT_DIR
              value: "${CLAW_EXTERNAL_VM_SKILL_ROOT_DIR}"
            - name: CLAW_EXTERNAL_VM_CLAWHUB_BIN
              value: "${CLAW_EXTERNAL_VM_CLAWHUB_BIN}"
            - name: CODEX_BIN_PATH
              value: "${CODEX_BIN_PATH}"
            - name: SEARXNG_URL
              value: "http://localhost:8080"
            - name: CLAW_EXTERNAL_VM_STRICT_HOST_KEY_CHECKING
              value: "${CLAW_EXTERNAL_VM_STRICT_HOST_KEY_CHECKING}"
            - name: IMAGE_ASSET_DIR
              value: "/app/assets/images"
            - name: IMAGE_BASE_URL
              value: "${IMAGE_BASE_URL}"

          volumeMounts:
            - name: workspace
              mountPath: /app/workspace
            - name: system-prompts
              mountPath: /app/system_prompts
            - name: claw-external-vm-ssh
              mountPath: /app/secrets/claw-external-vm
              readOnly: true
            - name: assets
              mountPath: /app/assets
        - name: searxng
          image: searxng/searxng:latest
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8080
          volumeMounts:
            - name: searxng-config
              mountPath: /etc/searxng/settings.yml
              subPath: settings.yml
            - name: searxng-config
              mountPath: /etc/searxng/limiter.yml
              subPath: limiter.yml
      volumes:
        - name: workspace
          persistentVolumeClaim:
            claimName: my-agent-workspace-pvc
        - name: system-prompts
          persistentVolumeClaim:
            claimName: my-agent-config-pvc
        - name: assets
          persistentVolumeClaim:
            claimName: my-agent-assets-pvc
        - name: claw-external-vm-ssh
          secret:
            secretName: claw-external-vm-ssh
            defaultMode: 0400
        - name: searxng-config
          configMap:
            name: searxng-config
---
apiVersion: v1
kind: Service
metadata:
  name: tool-runtime-service
  namespace: ${NAMESPACE}
  labels:
    app: tool-runtime-service
spec:
  type: ClusterIP
  selector:
    app: tool-runtime-service
  ports:
    - name: grpc
      port: 5303
      targetPort: 5303
YAML

  echo
  echo "tool-runtime-service external-vm + OpenViking 配置已应用"
  echo
  echo "验证 Pod 到当前 WSL / VM 的 clawhub："
  echo "kubectl -n ${NAMESPACE} exec deploy/tool-runtime-service -- ssh -i /app/secrets/claw-external-vm/id_rsa -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p ${CLAW_EXTERNAL_VM_PORT} ${CLAW_EXTERNAL_VM_USER}@${CLAW_EXTERNAL_VM_HOST} '${CLAW_EXTERNAL_VM_CLAWHUB_BIN} -V'"
  echo "kubectl -n ${NAMESPACE} exec deploy/tool-runtime-service -- ssh -i /app/secrets/claw-external-vm/id_rsa -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p ${CLAW_EXTERNAL_VM_PORT} ${CLAW_EXTERNAL_VM_USER}@${CLAW_EXTERNAL_VM_HOST} '${CODEX_BIN_PATH} --version'"
  echo
  echo "验证 OpenViking 环境变量："
  echo "kubectl -n ${NAMESPACE} exec deploy/tool-runtime-service -- sh -lc 'echo account=\$OPENVIKING_ACCOUNT user=\$OPENVIKING_USER agent=\$OPENVIKING_AGENT; test -n \"\$OPENVIKING_API_KEY\" && echo OPENVIKING_API_KEY is set'"
}

main "$@"
