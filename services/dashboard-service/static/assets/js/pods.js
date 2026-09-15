// Pod 状态 / Service 列表 / 重启（由 index.html 内联 script 拆出）

// ─── Pod 状态 ─────────────────────────────────────────────
async function refreshPods() {
  try {
    const [pods, svcs] = await Promise.all([
      fetch('/api/pods').then(r => r.json()),
      fetch('/api/services').then(r => r.json())
    ]);
    const tbody = document.querySelector('#pod-table tbody');
    tbody.innerHTML = pods.pods.map(p => `
      <tr>
        <td>${p.name}</td>
        <td><span class="status ${p.status === 'Running' ? 'running' : p.status === 'Pending' ? 'pending' : 'failed'}">${p.status}</span></td>
        <td>${p.containers.map(c => `${c.name}${c.ready ? '✓' : '✗'}`).join(', ')}</td>
        <td>${p.containers.map(c => c.image.split('/').pop()).join(', ')}</td>
        <td>${p.containers.map(c => c.restarts).join(', ')}</td>
        <td><button class="btn" onclick="restartOne('${p.deployment}')">重启</button></td>
      </tr>`).join('') || '<tr><td colspan="6">无数据</td></tr>';

    const stbody = document.querySelector('#svc-table tbody');
    stbody.innerHTML = svcs.services.map(s => `
      <tr><td>${s.name}</td><td>${s.type}</td><td>${s.clusterIP}</td><td>${s.ports.join(', ')}</td></tr>`).join('') || '<tr><td colspan="4">无数据</td></tr>';

    document.getElementById('status').textContent = `Pods: ${pods.pods.length} | Svc: ${svcs.services.length}`;
  } catch(e) { toast('加载失败: ' + e, false); }
}

async function restartOne(name) {
  const r = await fetch(`/api/restart?service=${name}`, { method: 'POST' }).then(r => r.json());
  toast(r.ok ? `${name} 重启中...` : r.error, r.ok);
  if (r.ok) setTimeout(refreshPods, 2000);
}
