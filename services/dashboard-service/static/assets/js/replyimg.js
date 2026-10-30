// 表情绑定管理：列表 + 图片预览 + 删除（数据源 /api/reply_images）
let riRules = [];
let riSourceUser = '';

async function riRefresh() {
  try {
    const data = await fetch('/api/reply_images').then(r => r.json());
    riRules = data.rules || [];
    riSourceUser = data.source_user || '';
    await riLoadSourceCandidates();
    riRender();
  } catch (err) {
    const list = document.getElementById('ri-list');
    if (list) list.innerHTML = `<div style="color:var(--dim);">加载失败：${err}</div>`;
  }
}

function riScopeLabel(rule) {
  if (rule.scope === 'global') {
    const owner = rule.owner ? `（来自用户 ${rule.owner}）` : '';
    return `<span style="color:#e8b04b;">全局</span>${owner}`;
  }
  const isSource = riSourceUser && rule.owner === riSourceUser;
  const suffix = isSource ? ' <span style="color:#e8b04b;">· 全局来源</span>' : '';
  return `用户 ${rule.owner}${suffix}`;
}

async function riLoadSourceCandidates() {
  try {
    const data = await fetch('/api/reply_images/source').then(r => r.json());
    const select = document.getElementById('ri-source');
    const now = document.getElementById('ri-source-now');
    if (now) now.textContent = data.source_user ? `当前来源：${data.source_user}` : '当前未设置（只有各自规则生效）';
    if (!select) return;
    const current = data.source_user || '';
    const options = [`<option value="">（不设置）</option>`].concat(
      (data.candidates || []).map(user =>
        `<option value="${user}" ${user === current ? 'selected' : ''}>${user}</option>`)
    );
    select.innerHTML = options.join('');
  } catch (err) { /* 忽略：来源接口不存在时页面仍可用 */ }
}

async function riSetSource() {
  const select = document.getElementById('ri-source');
  const user = select ? select.value : '';
  if (!user) { toast('请选择一个用户', false); return; }
  const resp = await fetch('/api/reply_images/source', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user }),
  });
  const data = await resp.json();
  if (data.ok) { toast(`已把 ${user} 的规则设为全局`); riRefresh(); }
  else toast(data.detail || '设置失败', false);
}

async function riClearSource() {
  const resp = await fetch('/api/reply_images/source', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user: '' }),
  });
  const data = await resp.json();
  if (data.ok) { toast('已清除全局来源'); riRefresh(); }
  else toast(data.detail || '清除失败', false);
}

function riRender() {
  const list = document.getElementById('ri-list');
  if (!list) return;
  const filter = (document.getElementById('ri-filter') || {}).value || '';
  const search = ((document.getElementById('ri-search') || {}).value || '').trim().toLowerCase();

  const rows = riRules.filter(rule => {
    if (filter && rule.scope !== filter) return false;
    if (!search) return true;
    const hay = `${(rule.keywords || []).join(' ')} ${rule.owner || ''} ${rule.agent || ''}`.toLowerCase();
    return hay.includes(search);
  });

  const countEl = document.getElementById('ri-count');
  if (countEl) countEl.textContent = `${rows.length} / ${riRules.length} 条`;

  if (!rows.length) {
    list.innerHTML = '<div style="color:var(--dim);padding:8px;">还没有规则。让智能体调用 reply-image 添加，或检查 config/.reply_images。</div>';
    return;
  }

  list.innerHTML = rows.map((rule, index) => {
    const preview = rule.preview
      ? `<img src="${rule.preview}" alt="preview" style="max-width:120px;max-height:120px;border-radius:6px;border:1px solid var(--border);" loading="lazy">`
      : `<span style="color:var(--dim);font-size:0.75rem;">（非图床地址，无法预览）</span>`;
    const keywords = (rule.keywords || []).map(k => `<code>${k}</code>`).join(' ');
    const note = rule.note ? `<div style="color:var(--dim);font-size:0.75rem;">备注：${rule.note}</div>` : '';
    return `
      <div style="display:flex;gap:12px;padding:10px;border-bottom:1px solid var(--border);align-items:flex-start;">
        <div style="flex:0 0 120px;">${preview}</div>
        <div style="flex:1;min-width:0;">
          <div style="margin-bottom:4px;">${keywords}</div>
          <div style="color:var(--dim);font-size:0.75rem;word-break:break-all;">${rule.image || ''}</div>
          <div style="color:var(--dim);font-size:0.75rem;margin-top:4px;">
            ${riScopeLabel(rule)} · 智能体 ${rule.agent} · 更新 ${rule.updated_at || '-'}
          </div>
          ${note}
        </div>
        <div style="flex:0 0 auto;">
          <button onclick="riDelete(${index})">删除</button>
        </div>
      </div>`;
  }).join('');
}

async function riDelete(rowIndex) {
  const filter = (document.getElementById('ri-filter') || {}).value || '';
  const search = ((document.getElementById('ri-search') || {}).value || '').trim().toLowerCase();
  const rows = riRules.filter(rule => {
    if (filter && rule.scope !== filter) return false;
    if (!search) return true;
    const hay = `${(rule.keywords || []).join(' ')} ${rule.owner || ''} ${rule.agent || ''}`.toLowerCase();
    return hay.includes(search);
  });
  const rule = rows[rowIndex];
  if (!rule) return;
  const keyword = (rule.keywords || [])[0] || '';
  if (!keyword) return;
  try {
    const resp = await fetch('/api/reply_images', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scope: rule.scope,
        owner: rule.owner || '',
        agent: rule.agent,
        keyword,
      }),
    });
    const data = await resp.json();
    if (data.ok) {
      toast(`已删除（关键词 ${keyword}）`);
      riRefresh();
    } else {
      toast(data.detail || '删除失败', false);
    }
  } catch (err) {
    toast('删除失败：' + err, false);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const button = document.querySelector('nav button[data-tab="replyimg"]');
  if (button) button.addEventListener('click', () => { if (!riRules.length) riRefresh(); });
});
