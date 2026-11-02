// 工具权限：分级说明 + L1 黑名单 / L2-L4 白名单
// 设计要点：每个区块独立 try/catch —— 任何一段数据异常都不能让后面的名单填空，
// 并且失败原因直接写在页面上（而不是留白，让人以为"设置被清掉了"）。
let tpPermTools = [];        // 注意：不能叫 tpPermTools，tool_panel.js 已有同名顶层变量

function tpParse(text) {
  return (text || '')
    .split(/[\s,，;；\n]+/)
    .map(item => item.trim())
    .filter(Boolean);
}

function tpJoin(list) {
  return (Array.isArray(list) ? list : []).join('\n');
}

function tpSetStatus(msg) {
  const status = document.getElementById('tp-status');
  if (status) status.textContent = msg;
}

function tpRenderLevels(levels) {
  const box = document.getElementById('tp-levels');
  if (!box) return;
  box.innerHTML = (Array.isArray(levels) ? levels : []).map(level => `
    <div style="margin-bottom:6px;">
      <b>${level.key} ${level.name}</b>
      <span style="color:var(--dim);">（${level.mode}）</span><br>
      <span style="color:var(--dim);font-size:0.8rem;">${level.desc}</span>
    </div>`).join('');
}

function tpRenderTools() {
  const box = document.getElementById('tp-tool-list');
  if (!box) return;
  const searchEl = document.getElementById('tp-tool-search');
  const search = ((searchEl && searchEl.value) || '').trim().toLowerCase();
  const rows = (Array.isArray(tpPermTools) ? tpPermTools : []).filter(tool => {
    const name = String((tool && tool.name) || '');
    return !search || name.toLowerCase().includes(search);
  });
  if (!rows.length) {
    box.innerHTML = '<div style="color:var(--dim);">没有匹配的工具</div>';
    return;
  }
  const color = level => ({ L0: '#6c8ebf', L1: '#4caf50', L2: '#e8b04b', L3: '#e2843b', L4: '#e05252' }[level] || 'var(--dim)');
  box.innerHTML = rows.map(tool => {
    const levels = Array.isArray(tool.levels) && tool.levels.length ? tool.levels : [tool.level || 'L4'];
    return `<div style="display:flex;gap:8px;padding:1px 0;">
       <span style="flex:0 0 46px;color:${color(levels[0])};">${levels.join('/')}</span>
       <span style="flex:0 0 190px;">${String(tool.name || '')}</span>
       <span style="color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${String(tool.args || '')}</span>
     </div>`;
  }).join('');
}

function tpSetField(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = tpJoin(value);
}

async function tpRefresh() {
  const errors = [];
  let data = null;
  try {
    const resp = await fetch('/api/tool_permissions', { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    tpSetStatus(`加载失败：${err}`);
    return;
  }

  // ① 分级说明
  try {
    tpRenderLevels(data.levels);
  } catch (err) { errors.push(`分级说明 ${err}`); }

  // ② 工具档位一览
  try {
    tpPermTools = Array.isArray(data.tools) ? data.tools : [];
    tpRenderTools();
  } catch (err) { errors.push(`工具一览 ${err}`); }

  // ③ 名单（最关键：必须无条件写入，不能被上面任何异常带崩）
  try {
    tpSetField('tp-blacklist', data.L1_blacklist_users);
    tpSetField('tp-l2', data.L2_whitelist_users);
    tpSetField('tp-l3', data.L3_whitelist_users);
    tpSetField('tp-l4', data.L4_whitelist_users);
  } catch (err) { errors.push(`名单 ${err}`); }

  // ④ 权限说明
  try {
    const notes = data.level_notes || {};
    ['ALL', 'L0', 'L1', 'L2', 'L3', 'L4'].forEach(level => {
      const el = document.getElementById(`tp-note-${level.toLowerCase()}`);
      if (el) el.value = notes[level] || '';
    });
    // 注入名单：* = 全部（默认），空 = 都不注入
    const agents = document.getElementById('tp-prompt-agents');
    if (agents) agents.value = data.prompt_agents === undefined || data.prompt_agents === null
      ? '*' : data.prompt_agents;
  } catch (err) { errors.push(`说明 ${err}`); }

  // ⑤ 黑名单提示词
  try {
    const hint = document.getElementById('tp-blacklist-hint');
    if (hint) hint.value = data.blacklist_hint || data.blacklist_hint_default || '';
    const blocked = document.getElementById('tp-blocked-hint');
    if (blocked) blocked.value = data.blacklist_blocked_hint || data.blacklist_blocked_hint_default || '';
  } catch (err) { errors.push(`黑名单提示 ${err}`); }

  const known = (data.known_users || []).length;
  tpSetStatus(
    (errors.length ? `部分区块出错：${errors.join('；')}　` : '') +
    `配置：${data.settings_path || '?'}　名单内用户 ${known} 个　工具 ${tpPermTools.length} 个`
  );
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
  const agentsEl = document.getElementById('tp-prompt-agents');
  const payload = { level_notes };
  // 只有输入框存在时才带上这一项（避免旧页面把它清空）
  if (agentsEl) payload.prompt_agents = (agentsEl.value || '').trim();
  try {
    const resp = await fetch('/api/tool_permissions', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.ok) {
      toast('权限说明已保存（下一轮对话生效）');
      const status = document.getElementById('tp-notes-status');
      if (status) {
        const who = data.prompt_agents === '*' || data.prompt_agents === ''
          ? (data.prompt_agents === '*' ? '全部智能体' : '都不注入')
          : data.prompt_agents;
        status.textContent = `已保存 ${Object.keys(level_notes).length} 段说明（含通用）；注入给：${who}`;
      }
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
  if ((location.hash || '').includes('toolperm')) tpRefresh();
});
