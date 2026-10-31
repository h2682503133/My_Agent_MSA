// 浏览器环境：探活 / 下发启动命令 / 显示宿主执行结果
let brTimer = null;

async function brRefresh() {
  try {
    const data = await fetch('/api/browser/state').then(r => r.json());
    brRender(data);
  } catch (err) {
    const box = document.getElementById('br-status');
    if (box) box.textContent = `状态获取失败：${err}`;
  }
}

function brRender(data) {
  const status = document.getElementById('br-status');
  const last = document.getElementById('br-last');
  const queued = document.getElementById('br-queued');
  if (!status) return;

  const probe = data.probe || {};
  const ok = probe.ok;
  const color = ok ? '#4caf50' : '#e8b04b';
  status.innerHTML = `
    <div><span style="color:${color};">●</span> ${ok ? '浏览器可达' : '浏览器不可达'}
      ${probe.browser ? `（${probe.browser}）` : ''}</div>
    <div style="color:var(--dim);font-size:0.8rem;">探测地址：${probe.url || '（缺少 NODE_IP）'}</div>
    ${ok ? '' : `<div style="color:var(--dim);font-size:0.8rem;">原因：${probe.reason || '-'}</div>`}
  `;

  if (queued) {
    queued.textContent = data.queued ? `队列中还有 ${data.queued} 条待宿主执行` : '';
  }

  if (last) {
    const record = data.last || {};
    if (!record.at) {
      last.textContent = '（还没有执行记录）';
    } else {
      const when = new Date(record.at * 1000).toLocaleString();
      const state = record.state || {};
      const lines = [
        `时间：${when}　动作：#${record.id}　结果：${record.ok ? '成功' : '失败'}`,
        record.message ? `说明：${record.message}` : '',
        state.edge ? `Edge: ${state.edge}` : '',
        state.relay ? `中继: ${state.relay}` : '',
        state.container ? `容器: ${state.container}` : '',
      ].filter(Boolean);
      last.textContent = lines.join('\n');
    }
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const button = document.querySelector('nav button[data-tab="browser"]');
  if (button) {
    button.addEventListener('click', () => {
      brRefresh();
      if (brTimer) clearInterval(brTimer);
      brTimer = setInterval(brRefresh, 5000);
    });
  }
});
