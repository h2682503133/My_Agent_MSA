// 日志查看（含 SSE 实时跟踪）（由 index.html 内联 script 拆出）

// ─── 日志查看 ──────────────────────────────────────────────
let logStream = null;

async function quickLogs(name) {
  const data = await fetch('/api/pods').then(r => r.json());
  const pod = data.pods.find(p => p.name.startsWith(name));
  if (pod) {
    document.getElementById('log-pod-select').value = pod.name;
    updateLogContainers(pod.containers);
    loadLogs();
  } else {
    toast('未找到 ' + name + ' Pod', false);
  }
}

async function refreshLogPods() {
  const data = await fetch('/api/pods').then(r => r.json());
  const sel = document.getElementById('log-pod-select');
  sel.innerHTML = data.pods.map(p => `<option value="${p.name}">${p.name}</option>`).join('');
  if (data.pods.length > 0) {
    updateLogContainers(data.pods[0].containers);
  }
}

function updateLogContainers(containers) {
  const sel = document.getElementById('log-container-select');
  sel.innerHTML = '<option value="">默认容器</option>' +
    containers.map(c => `<option value="${c.name}">${c.name}</option>`).join('');
}

async function loadLogs() {
  const pod = document.getElementById('log-pod-select').value;
  const container = document.getElementById('log-container-select').value;
  const tail = document.getElementById('log-tail').value;
  if (!pod) return;
  const params = new URLSearchParams({ pod, tail, container });
  const data = await fetch('/api/logs?' + params).then(r => r.json());
  document.getElementById('log-output').textContent = data.logs || data.error || '(无日志)';
}

async function streamLogs() {
  stopStream();
  const pod = document.getElementById('log-pod-select').value;
  const container = document.getElementById('log-container-select').value;
  const tail = document.getElementById('log-tail').value;
  if (!pod) return;
  const params = new URLSearchParams({ pod, tail, container });
  const out = document.getElementById('log-output');
  out.textContent = '';

  let ended = false;
  logStream = new EventSource('/api/logs/stream?' + params);
  logStream.onopen = () => { if (out.textContent) out.textContent += '\n--- 已重新连接 ---\n'; };
  logStream.onmessage = (e) => {
    if (e.data === '[end]') { ended = true; return; }
    out.textContent += e.data + '\n';
    out.scrollTop = out.scrollHeight;
  };
  // 正常结束（服务端显式发了 end 帧）
  logStream.addEventListener('end', () => {
    ended = true;
    out.textContent += '\n--- 实时追踪结束 ---';
    stopStream();
  });
  // 异常失联：EventSource 自己会重连，只有在彻底关闭时才提示并停掉
  logStream.onerror = () => {
    if (ended) return;
    if (!logStream || logStream.readyState === EventSource.CLOSED) {
      stopStream();
      out.textContent += '\n--- 流已断开（服务端无响应；可点「加载」看当前日志） ---';
    } else {
      out.textContent += '\n--- 连接中断，正在重连… ---';
    }
  };
}

function stopStream() {
  if (logStream) { logStream.close(); logStream = null; }
}
