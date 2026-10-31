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

  const mcp = data.probe?.mcp || {};
  const cdp = data.probe?.cdp || {};
  const line = (label, probe) => {
    const ok = probe.ok;
    const color = ok ? '#4caf50' : '#e8b04b';
    const extra = ok
      ? (probe.browser ? `（${probe.browser}）` : '')
      : (probe.reason ? `　原因：${probe.reason}` : '');
    return `<div><span style="color:${color};">●</span> ${label}：${ok ? '可达' : '不可达'}${extra}</div>`
      + (probe.url ? `<div style="color:var(--dim);font-size:0.78rem;margin-left:18px;">${probe.url}</div>` : '');
  };

  status.innerHTML =
    line('Playwright MCP（推荐）', mcp) +
    line('CDP 兼容模式', cdp) +
    `<div style="color:var(--dim);font-size:0.8rem;margin-top:6px;">
       当前生效：${data.probe?.mode ? data.probe.mode.toUpperCase() : '无'}　
       启动/停止请用宿主上的 MyAgentAdminPanel（「🌐 浏览器环境」页）
     </div>`;

  if (queued) queued.textContent = data.queued ? `队列中还有 ${data.queued} 条` : '';

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
