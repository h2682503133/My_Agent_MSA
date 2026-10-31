// 工具权限：分级说明 + L1 黑名单 / L2-L4 白名单
let tpTools = [];

function tpParse(text) {
  return (text || '')
    .split(/[\s,，;；\n]+/)
    .map(item => item.trim())
    .filter(Boolean);
}

function tpJoin(list) {
  return (list || []).join('\n');
}

function tpRenderTools() {
  const box = document.getElementById('tp-tool-list');
  if (!box) return;
  const search = ((document.getElementById('tp-tool-search') || {}).value || '').trim().toLowerCase();
  const rows = tpTools.filter(tool => !search || tool.name.toLowerCase().includes(search));
  if (!rows.length) {
    box.innerHTML = '<div style="color:var(--dim);">没有匹配的工具</div>';
    return;
  }
  const color = level => ({ L0: '#6c8ebf', L1: '#4caf50', L2: '#e8b04b', L3: '#e2843b', L4: '#e05252' }[level] || 'var(--dim)');
  box.innerHTML = rows.map(tool =>
    `<div style="display:flex;gap:8px;padding:1px 0;">
       <span style="flex:0 0 46px;color:${color(tool.level)};">${tool.levels.join('/')}</span>
       <span style="flex:0 0 190px;">${tool.name}</span>
       <span style="color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${tool.args}</span>
     </div>`).join('');
}

async function tpRefresh() {
  try {
    const data = await fetch('/api/tool_permissions').then(r => r.json());
    const box = document.getElementById('tp-levels');
    if (box) {
      box.innerHTML = (data.levels || []).map(level => `
        <div style="margin-bottom:6px;">
          <b>${level.key} ${level.name}</b>
          <span style="color:var(--dim);">（${level.mode}）</span><br>
          <span style="color:var(--dim);font-size:0.8rem;">${level.desc}</span>
        </div>`).join('');
    }
    tpTools = data.tools || [];
    tpRenderTools();
    const set = (id, value) => { const el = document.getElementById(id); if (el) el.value = tpJoin(value); };
    set('tp-blacklist', data.L1_blacklist_users);
    set('tp-l2', data.L2_whitelist_users);
    set('tp-l3', data.L3_whitelist_users);
    set('tp-l4', data.L4_whitelist_users);

    const notes = data.level_notes || {};
    ['ALL', 'L0', 'L1', 'L2', 'L3', 'L4'].forEach(level => {
      const el = document.getElementById(`tp-note-${level.toLowerCase()}`);
      if (el) el.value = notes[level] || '';
    });

    const status = document.getElementById('tp-status');
    if (status) {
      const known = (data.known_users || []).length;
      status.textContent = `配置：${data.settings_path}　名单内用户 ${known} 个`;
    }
  } catch (err) {
    const status = document.getElementById('tp-status');
    if (status) status.textContent = `加载失败：${err}`;
  }
}

async function tpSave() {
  const value = id => tpParse((document.getElementById(id) || {}).value);
  const payload = {
    L1_blacklist_users: value('tp-blacklist'),
    L2_whitelist_users: value('tp-l2'),
    L3_whitelist_users: value('tp-l3'),
    L4_whitelist_users: value('tp-l4'),
  };
  try {
    const resp = await fetch('/api/tool_permissions', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.ok) {
      toast('名单已保存（立即生效）');
      tpRefresh();
    } else {
      toast(data.detail || '保存失败', false);
    }
  } catch (err) {
    toast('保存失败：' + err, false);
  }
}

async function tpSaveNotes() {
  const level_notes = {};
  ['ALL', 'L0', 'L1', 'L2', 'L3', 'L4'].forEach(level => {
    const el = document.getElementById(`tp-note-${level.toLowerCase()}`);
    const text = (el ? el.value : '').trim();
    if (text) level_notes[level] = text;
  });
  try {
    const resp = await fetch('/api/tool_permissions', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level_notes }),
    });
    const data = await resp.json();
    if (data.ok) {
      toast('权限说明已保存（下一轮对话生效）');
      const status = document.getElementById('tp-notes-status');
      if (status) status.textContent = `已保存 ${Object.keys(level_notes).length} 段说明（含通用）`;
    } else {
      toast(data.detail || '保存失败', false);
    }
  } catch (err) {
    toast('保存失败：' + err, false);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const button = document.querySelector('nav button[data-tab="toolperm"]');
  if (button) button.addEventListener('click', () => tpRefresh());
});
