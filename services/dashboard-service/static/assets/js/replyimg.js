// 表情绑定管理：列表 + 图片预览 + 删除 + 把某个用户的规则「加入全局」
// 数据源：
//   GET    /api/reply_images            列出全局 + 各用户规则
//   DELETE /api/reply_images            按关键词删除一条
//   POST   /api/reply_images/promote    把某用户的规则并入全局（按关键词去重）
let riRules = [];

async function riRefresh() {
  try {
    const data = await fetch('/api/reply_images').then(r => r.json());
    riRules = data.rules || [];
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
  return `用户 ${rule.owner}`;
}

function riVisibleRules() {
  const filter = (document.getElementById('ri-filter') || {}).value || '';
  const search = ((document.getElementById('ri-search') || {}).value || '').trim().toLowerCase();
  return riRules.filter(rule => {
    if (filter && rule.scope !== filter) return false;
    if (!search) return true;
    const hay = `${(rule.keywords || []).join(' ')} ${rule.owner || ''} ${rule.agent || ''}`.toLowerCase();
    return hay.includes(search);
  });
}

function riRender() {
  const list = document.getElementById('ri-list');
  if (!list) return;
  const rows = riVisibleRules();

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
    const promoteBtn = rule.scope === 'user'
      ? `<button onclick="riPromote(${index})">加入全局</button>`
      : '';
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
        <div style="flex:0 0 auto;display:flex;flex-direction:column;gap:6px;">
          ${promoteBtn}
          <button onclick="riDelete(${index})">删除</button>
        </div>
      </div>`;
  }).join('');
}

async function riPromote(rowIndex) {
  const rule = riVisibleRules()[rowIndex];
  if (!rule || rule.scope !== 'user') return;
  const keyword = (rule.keywords || [])[0] || '';
  try {
    const resp = await fetch('/api/reply_images/promote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ owner: rule.owner, agent: rule.agent, keyword }),
    });
    const data = await resp.json();
    if (data.ok) {
      toast(data.message || '已加入全局');
      riRefresh();
    } else {
      toast(data.detail || '加入全局失败', false);
    }
  } catch (err) {
    toast('加入全局失败：' + err, false);
  }
}

async function riDelete(rowIndex) {
  const rule = riVisibleRules()[rowIndex];
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
