// 世界书管理（由 index.html 内联 script 拆出）

// ─── 世界书管理 ────────────────────────────────────────────
let wiEditingId = '';
let wiEntries = [];
let wiAgentGroups = {};
let wiFilter = '__all__';

async function wiLoad() {
  try {
    const data = await fetch('/api/world_info').then(r => r.json());
    wiEntries = data.entries || [];
    wiLoadGroups();
    wiBuildFilter();
    wiRender();
  } catch(e) { toast('世界书加载失败: ' + e, false); }
}

function wiBuildFilter() {
  const sel = document.getElementById('wi-filter');
  // 反向映射：群组id -> 组内 agent
  const groupToAgents = {};
  Object.keys(wiAgentGroups).forEach(agent => {
    (wiAgentGroups[agent] || []).forEach(g => {
      if (!groupToAgents[g]) groupToAgents[g] = [];
      groupToAgents[g].push(agent);
    });
  });
  const scopes = new Set(wiEntries.map(e => e.scope || 'main'));
  let html = '<option value="__all__">全部</option>';
  html += '<optgroup label="scope 筛选">';
  html += [...scopes].sort().map(s => `<option value="scope:${esc(s)}">${esc(s)}</option>`).join('');
  html += '</optgroup>';
  html += '<optgroup label="按群组筛选">';
  html += Object.keys(groupToAgents).sort().map(g =>
    `<option value="group:${esc(g)}">${esc(g + ' (' + groupToAgents[g].join(', ') + ')')}</option>`).join('');
  html += '</optgroup>';
  sel.innerHTML = html;
  // 保留原筛选（若选项仍存在）
  const exists = [...sel.options].some(o => o.value === wiFilter);
  sel.value = exists ? wiFilter : '__all__';
  wiFilter = sel.value;
}

function wiRender() {
  wiFilter = document.getElementById('wi-filter').value;
  const list = document.getElementById('wi-list');
  const countEl = document.getElementById('wi-filter-count');
  let entries = wiEntries;
  if (wiFilter.startsWith('scope:')) {
    const s = wiFilter.slice(6);
    entries = wiEntries.filter(e => (e.scope || 'main') === s);
  } else if (wiFilter.startsWith('group:')) {
    const g = wiFilter.slice(6);
    const agents = Object.keys(wiAgentGroups).filter(a => (wiAgentGroups[a] || []).includes(g));
    const scopes = new Set(['group:' + g, ...agents]);
    entries = wiEntries.filter(e => scopes.has(e.scope || 'main'));
  }
  countEl.textContent = `共 ${entries.length} 条`;
  if (!entries.length) {
    list.innerHTML = '<div style="color:var(--dim);padding:12px;">无匹配条目（0 条）</div>';
    return;
  }
  list.innerHTML = entries.map(e => `
    <div style="border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:8px;${e.enabled ? '' : 'opacity:0.5;'}">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
        <span style="font-size:0.75rem;color:var(--dim);">${esc(e.id)} <span style="color:var(--yellow);">scope=${esc(e.scope)}</span></span>
        <span>
          <button class="btn" style="padding:2px 8px;font-size:0.75rem;" onclick="wiEdit('${esc(e.id)}')">编辑</button>
          <button class="btn ${e.enabled ? 'danger' : 'green'}" style="padding:2px 8px;font-size:0.75rem;" onclick="wiToggle('${esc(e.id)}', ${!e.enabled})">${e.enabled ? '禁用' : '启用'}</button>
          <button class="btn danger" style="padding:2px 8px;font-size:0.75rem;" onclick="wiDelete('${esc(e.id)}')">删除</button>
        </span>
      </div>
      <div style="font-size:0.85rem;color:var(--accent-bright);margin-top:4px;font-weight:500;">关键词：${esc((e.keys||[]).join('、'))} ${e.constant ? '<span class="status running">常驻</span>' : ''} ${e.regex ? '<span class="status pending">正则</span>' : ''} ${(e.match_mode||'or')==='and' ? '<span class="status running">AND</span>' : ''} ${(e.exclude||[]).length ? '<span style="color:var(--red);font-weight:bold;">排除:' + esc((e.exclude||[]).join('、')) + '</span>' : ''} ${e.priority ? '<span style="color:var(--dim);">pri=' + e.priority + '</span>' : ''}</div>
      <div style="font-size:0.8rem;margin-top:4px;color:var(--text);white-space:pre-wrap;">${esc(e.content)}</div>
    </div>`).join('');
}

function wiSetMode(mode) {
  document.getElementById('wi-mode-or').classList.toggle('active', mode === 'or');
  document.getElementById('wi-mode-and').classList.toggle('active', mode === 'and');
}
function wiGetMode() {
  return document.getElementById('wi-mode-and').classList.contains('active') ? 'and' : 'or';
}

function wiResetForm() {
  wiEditingId = '';
  document.getElementById('wi-form-title').textContent = '添加条目';
  document.getElementById('wi-scope').value = '';
  document.getElementById('wi-keys').value = '';
  document.getElementById('wi-exclude').value = '';
  document.getElementById('wi-content').value = '';
  document.getElementById('wi-priority').value = '0';
  document.getElementById('wi-constant').checked = false;
  document.getElementById('wi-regex').checked = false;
  wiSetMode('or');
  document.getElementById('wi-enabled').checked = true;
}

function wiEdit(id) {
  fetch('/api/world_info').then(r => r.json()).then(data => {
    const e = (data.entries || []).find(x => x.id === id);
    if (!e) return toast('条目不存在', false);
    wiEditingId = id;
    document.getElementById('wi-form-title').textContent = '编辑条目 ' + id;
    document.getElementById('wi-scope').value = e.scope || '';
    document.getElementById('wi-keys').value = (e.keys || []).join(',');
    document.getElementById('wi-exclude').value = (e.exclude || []).join(',');
    document.getElementById('wi-content').value = e.content || '';
    document.getElementById('wi-priority').value = e.priority || 0;
    document.getElementById('wi-constant').checked = !!e.constant;
    document.getElementById('wi-regex').checked = !!e.regex;
    wiSetMode((e.match_mode === 'and') ? 'and' : 'or');
    document.getElementById('wi-enabled').checked = e.enabled !== false;
    document.getElementById('wi-form-title').scrollIntoView({behavior:'smooth'});
  });
}

async function wiSave() {
  const payload = {
    scope: document.getElementById('wi-scope').value.trim(),
    keys: document.getElementById('wi-keys').value.split(/[,，、]/).map(s => s.trim()).filter(Boolean),
    exclude: document.getElementById('wi-exclude').value.split(/[,，、]/).map(s => s.trim()).filter(Boolean),
    content: document.getElementById('wi-content').value,
    priority: parseInt(document.getElementById('wi-priority').value) || 0,
    constant: document.getElementById('wi-constant').checked,
    regex: document.getElementById('wi-regex').checked,
    match_mode: wiGetMode(),
    enabled: document.getElementById('wi-enabled').checked,
  };
  if (!payload.keys.length) return toast('至少需要一个关键词', false);
  if (!payload.content.trim()) return toast('内容不能为空', false);
  try {
    const url = wiEditingId ? '/api/world_info/' + encodeURIComponent(wiEditingId) : '/api/world_info';
    const method = wiEditingId ? 'PUT' : 'POST';
    const r = await fetch(url, {
      method, headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    }).then(r => r.json());
    if (!r.ok) return toast(r.detail || '保存失败', false);
    toast(wiEditingId ? '已更新条目' : '已添加条目');
    wiResetForm();
    wiLoad();
  } catch(e) { toast('保存失败: ' + e, false); }
}

async function wiToggle(id, enabled) {
  const data = await fetch('/api/world_info').then(r => r.json());
  const e = (data.entries || []).find(x => x.id === id);
  if (!e) return;
  e.enabled = enabled;
  await fetch('/api/world_info/' + encodeURIComponent(id), {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      scope: e.scope, keys: e.keys, content: e.content,
      priority: e.priority, constant: e.constant, regex: e.regex,
      match_mode: e.match_mode || 'or', exclude: e.exclude || [], enabled: enabled
    })
  });
  wiLoad();
}

async function wiDelete(id) {
  if (!confirm('删除世界书条目 ' + id + '?')) return;
  await fetch('/api/world_info/' + encodeURIComponent(id), {method:'DELETE'});
  if (wiEditingId === id) wiResetForm();
  wiLoad();
}

async function wiLoadGroups() {
  try {
    const data = await fetch('/api/world_info/groups').then(r => r.json());
    wiAgentGroups = data.agent_groups || {};
    const box = document.getElementById('wi-groups');
    box.innerHTML = '';
    Object.keys(wiAgentGroups).forEach(agent => wiAppendGroupRow(agent, wiAgentGroups[agent]));
    if (!Object.keys(wiAgentGroups).length) wiAppendGroupRow('', []);
  } catch(e) { toast('群组加载失败: ' + e, false); }
}

function wiAppendGroupRow(agent, groups) {
  const box = document.getElementById('wi-groups');
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:8px;align-items:center;';
  row.innerHTML = `
    <input class="wi-g-agent" placeholder="agent_id" value="${esc(agent)}" style="flex:0 0 160px;background:var(--bg);color:var(--text);border:1px solid var(--border);padding:6px;border-radius:4px;">
    <input class="wi-g-groups" placeholder="群组id（逗号分隔）" value="${esc((groups||[]).join(','))}" style="flex:1;background:var(--bg);color:var(--text);border:1px solid var(--border);padding:6px;border-radius:4px;">
    <button class="btn danger" style="padding:4px 10px;" onclick="this.parentElement.remove()">×</button>`;
  box.appendChild(row);
}

function wiAddGroupRow() {
  wiAppendGroupRow('', []);
}

async function wiSaveGroups() {
  const ag = {};
  document.querySelectorAll('#wi-groups .wi-g-agent').forEach((el, i) => {
    const agent = el.value.trim();
    const groupsEl = document.querySelectorAll('#wi-groups .wi-g-groups')[i];
    const groups = (groupsEl.value || '').split(/[,，、]/).map(s => s.trim()).filter(Boolean);
    if (agent) ag[agent] = groups;
  });
  await fetch('/api/world_info/groups', {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({agent_groups: ag})
  }).then(r => r.json());
  toast('群组已保存');
  wiLoadGroups();
  wiBuildFilter();
  wiRender();
}
