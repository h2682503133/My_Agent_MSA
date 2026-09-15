// System Prompt：智能体 / 分组 / 文件编辑（由 index.html 内联 script 拆出）

// ─── System Prompt ─────────────────────────────────────────
let spAgent = '', spFile = '';
let spGroups = {}; // {组名: [agent,...]}，仅展示用
let spCollapsedGroups = new Set(JSON.parse(localStorage.getItem('sp_collapsed_groups') || '[]'));
let spCollapsedAgents = new Set(JSON.parse(localStorage.getItem('sp_collapsed_agents') || '[]'));

function spSaveCollapseState() {
  localStorage.setItem('sp_collapsed_groups', JSON.stringify([...spCollapsedGroups]));
  localStorage.setItem('sp_collapsed_agents', JSON.stringify([...spCollapsedAgents]));
}

async function spRefresh() {
  const [data, grp] = await Promise.all([
    fetch('/api/system_prompt').then(r => r.json()),
    fetch('/api/system_prompt/groups').then(r => r.json())
  ]);
  spGroups = grp.groups || {};
  spLastAgents = data.agents || [];
  const list = document.getElementById('sp-agent-list');
  const allAgents = data.agents || [];
  let html = '';
  if (data.global_files && data.global_files.length > 0) {
    html += '<div style="padding:4px 0;border-bottom:1px solid var(--border);margin-bottom:8px;">';
    html += '<span style="color:var(--yellow);font-weight:bold;">全局文件</span>';
    data.global_files.forEach(f => {
      // TOOL.json = 统一工具目录（各智能体 TOOL.md 只写注入选项，清单从这里注入）
      const isTool = f.kind === 'tool_catalog' || f.name.toUpperCase() === 'TOOL.JSON';
      let hint = '';
      if (isTool) {
        const tc = data.tool_catalog || {};
        hint = ` <span class="tp-badge info">工具目录 ${tc.section_count || 0}段/${tc.tool_count || 0}工具</span>`;
        if (tc.json_error) hint += ` <span class="tp-badge err">${esc(tc.json_error)}</span>`;
      }
      html += `<div style="cursor:pointer;padding:2px 0 2px 16px;font-size:0.8rem;color:${isTool ? 'var(--accent-bright)' : 'var(--dim)'}"
                onclick="spLoadGlobal('${esc(f.name)}')">${esc(f.name)} (${f.size}B)${hint}</div>`;
    });
    const hasToolCatalog = data.global_files.some(f => f.name.toUpperCase() === 'TOOL.JSON');
    if (!hasToolCatalog) {
      html += '<div style="padding:2px 0 2px 16px;font-size:0.7rem;color:var(--red);">缺少 TOOL.json：智能体 TOOL.md 的工具清单将无法注入</div>';
    }
    html += '</div>';
  }
  // 组装分组：组名 -> 该组实际存在的智能体
  const grouped = {};
  const inGroup = new Set();
  Object.keys(spGroups).forEach(g => {
    grouped[g] = [];
    (spGroups[g] || []).forEach(a => {
      const agent = allAgents.find(x => x.name === a);
      if (agent) { grouped[g].push(agent); inGroup.add(a); }
    });
  });
  const ungrouped = allAgents.filter(a => !inGroup.has(a.name));
  Object.keys(grouped).forEach(g => { html += spRenderGroup(g, grouped[g]); });
  if (ungrouped.length) html += spRenderGroup('__ungrouped__', ungrouped);
  if (!Object.keys(grouped).length && !ungrouped.length) {
    html += '<div style="color:var(--dim);">暂无智能体</div>';
  }
  list.innerHTML = html;
}

function spRenderGroup(groupKey, agents) {
  const isUngrouped = groupKey === '__ungrouped__';
  const displayName = isUngrouped ? '未分组' : groupKey;
  const collapsed = spCollapsedGroups.has(groupKey);
  const arrow = collapsed ? '▶' : '▼';
  let html = `<div style="padding:4px 0;border-bottom:1px solid var(--border);">
    <div style="display:flex;align-items:center;gap:6px;">
      <span style="cursor:pointer;color:var(--dim);font-size:0.75rem;" onclick="spToggleGroup('${esc(groupKey)}')">${arrow}</span>
      <span style="cursor:pointer;color:${isUngrouped ? 'var(--yellow)' : 'var(--text)'};font-weight:bold;"
            onclick="spToggleGroup('${esc(groupKey)}')">${displayName}</span>
      <span style="color:var(--dim);font-size:0.7rem;">(${agents.length})</span>`;
  if (!isUngrouped) {
    html += `<span style="margin-left:auto;display:flex;gap:4px;">
      <button class="btn" style="padding:1px 6px;font-size:0.7rem;" onclick="spRenameGroup('${esc(groupKey)}')">重命名</button>
      <button class="btn danger" style="padding:1px 6px;font-size:0.7rem;" onclick="spDeleteGroup('${esc(groupKey)}')">删除</button>
    </span>`;
  }
  html += '</div>';
  if (!collapsed) {
    if (agents.length) {
      agents.forEach(a => { html += spRenderAgent(a, groupKey); });
    } else {
      html += '<div style="color:var(--dim);font-size:0.8rem;padding:2px 0 2px 20px;">（空组）</div>';
    }
  }
  html += '</div>';
  return html;
}

function spRenderAgent(a, currentGroup) {
  const collapsed = spCollapsedAgents.has(a.name);
  const files = a.files || [];
  const arrow = collapsed ? '▶' : '▼';
  const hasToolMd = files.some(f => f.name.toLowerCase() === 'tool.md');
  let toolHint = '';
  if (hasToolMd) {
    const tf = files.find(f => f.name.toLowerCase() === 'tool.md');
    toolHint = tf.kind === 'tool_options'
      ? '<span class="tp-badge ok">TOOL 选项式</span>'
      : '<span class="tp-badge warn">TOOL 原文式</span>';
  } else if (a.declares_tool_md) {
    toolHint = '<span class="tp-badge err">缺 TOOL.md</span>';
  }
  const moveOptions = ['<option value="">移动▾</option>']
    .concat(Object.keys(spGroups).map(g =>
      `<option value="${esc(g)}" ${g === currentGroup ? 'selected' : ''}>${esc(g)}</option>`))
    .concat(`<option value="__ungrouped__" ${currentGroup === '__ungrouped__' ? 'selected' : ''}>未分组</option>`)
    .join('');
  let html = `<div style="padding:2px 0;">
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
      <span style="cursor:pointer;color:var(--dim);font-size:0.75rem;" onclick="spToggleAgent('${esc(a.name)}')">${arrow}</span>
      <span style="cursor:pointer;color:var(--accent-bright);font-weight:bold;" onclick="spSelectAgent('${esc(a.name)}')">${esc(a.name)}</span>
      <span style="color:var(--dim);font-size:0.7rem;">(${files.length}文件)</span>
      ${toolHint}
      <select style="margin-left:auto;font-size:0.7rem;padding:1px 4px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;max-width:110px;"
              onchange="spMoveAgent('${esc(a.name)}', this.value)">
        ${moveOptions}
      </select>
      <button class="btn danger" style="padding:1px 6px;font-size:0.7rem;" onclick="spDeleteAgent('${esc(a.name)}')">删除</button>
    </div>`;
  if (!collapsed) {
    if (!hasToolMd && a.declares_tool_md) {
      html += `<div style="cursor:pointer;padding:2px 0 2px 20px;font-size:0.78rem;color:var(--yellow);"
        onclick="tpOpenForAgent('${esc(a.name)}', true)">＋ 生成 TOOL.md（按 TOOL.json 勾选）</div>`;
    }
    html += files.map(f =>
      `<div style="cursor:pointer;padding:2px 0 2px 20px;font-size:0.8rem;color:var(--dim)"
            onclick="spLoad('${esc(a.name)}','${esc(f.name)}')">${esc(f.name)} (${f.size}B)</div>`).join('');
  }
  html += '</div>';
  return html;
}

function spToggleGroup(name) {
  if (spCollapsedGroups.has(name)) spCollapsedGroups.delete(name); else spCollapsedGroups.add(name);
  spSaveCollapseState();
  spRefresh();
}

function spToggleAgent(name) {
  if (spCollapsedAgents.has(name)) spCollapsedAgents.delete(name); else spCollapsedAgents.add(name);
  spSaveCollapseState();
  spRefresh();
}

async function spSaveGroups() {
  await fetch('/api/system_prompt/groups', {
    method: 'PUT', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({groups: spGroups})
  });
  spRefresh();
}

async function spAddGroup() {
  const name = document.getElementById('sp-group-input').value.trim();
  if (!name) return;
  if (spGroups[name]) { toast('分组已存在', false); return; }
  spGroups[name] = [];
  document.getElementById('sp-group-input').value = '';
  await spSaveGroups();
}

async function spRenameGroup(name) {
  const nv = prompt('新的分组名称：', name);
  if (!nv) return;
  const nn = nv.trim();
  if (!nn || nn === name) return;
  if (spGroups[nn]) { toast('分组名已存在', false); return; }
  spGroups[nn] = spGroups[name] || [];
  delete spGroups[name];
  if (spCollapsedGroups.has(name)) {
    spCollapsedGroups.delete(name);
    spCollapsedGroups.add(nn);
    spSaveCollapseState();
  }
  await spSaveGroups();
}

async function spDeleteGroup(name) {
  const count = (spGroups[name] || []).length;
  const msg = '删除分组「' + name + '」？' + (count ? ' 组内 ' + count + ' 个智能体将回到「未分组」。' : '');
  if (!confirm(msg)) return;
  delete spGroups[name];
  spCollapsedGroups.delete(name);
  spSaveCollapseState();
  await spSaveGroups();
}

async function spMoveAgent(name, targetGroup) {
  if (!targetGroup) return;
  Object.keys(spGroups).forEach(g => {
    spGroups[g] = (spGroups[g] || []).filter(a => a !== name);
  });
  if (targetGroup !== '__ungrouped__') {
    if (!spGroups[targetGroup]) spGroups[targetGroup] = [];
    spGroups[targetGroup].push(name);
  }
  await spSaveGroups();
}

async function spLoadGlobal(file) {
  spAgent = ''; spFile = file;
  document.getElementById('sp-editor-title').textContent = '全局 / ' + file;
  const data = await fetch('/api/system_prompt/global/' + file).then(r => r.json());
  document.getElementById('sp-editor').value = data.content;
  if (file.toUpperCase() === 'TOOL.JSON') await tpOpenForCatalog();
  else tpHidePanel();
}

async function spSelectAgent(name) {
  spAgent = name;
  document.getElementById('sp-editor-title').textContent = '文件 - ' + name;
  spFile = '';
  document.getElementById('sp-editor').value = '';
  tpHidePanel();
}

async function spLoad(agent, file) {
  spAgent = agent; spFile = file;
  document.getElementById('sp-editor-title').textContent = agent + ' / ' + file;
  const data = await fetch('/api/system_prompt/' + agent + '/' + file).then(r => r.json());
  document.getElementById('sp-editor').value = data.content;
  if (file.toLowerCase() === 'tool.md') await tpOpenForAgent(agent);
  else tpHidePanel();
}

async function spAddAgent() {
  const name = document.getElementById('sp-agent-input').value.trim();
  if (!name) return;
  await fetch('/api/system_prompt/agent?name=' + name, {method:'POST'});
  document.getElementById('sp-agent-input').value = '';
  spRefresh();
}

async function spDeleteAgent(name) {
  if (!confirm('删除 ' + name + ' 及其所有文件?')) return;
  await fetch('/api/system_prompt/agent/' + name, {method:'DELETE'});
  // 同步清理分组引用
  let changed = false;
  Object.keys(spGroups).forEach(g => {
    const before = spGroups[g].length;
    spGroups[g] = (spGroups[g] || []).filter(a => a !== name);
    if (spGroups[g].length !== before) changed = true;
  });
  if (spAgent === name) { spAgent = ''; spFile = ''; document.getElementById('sp-editor').value = ''; }
  if (changed) await spSaveGroups(); else spRefresh();
}

async function spAddFile() {
  const name = document.getElementById('sp-file-input').value.trim();
  if (!name) return;
  if (!name.endsWith('.md') && !name.endsWith('.json')) return toast('文件名需以 .md 或 .json 结尾', false);
  // 未选智能体 → 作为全局文件创建（TOOL.json / GLOBAL_SETTING.md 这类）
  const agent = spAgent || '';
  if (!agent && !confirm('未选择智能体，将作为全局文件创建：' + name + '\n继续？')) return;
  await fetch('/api/system_prompt/file', {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({agent: agent || 'global', filename:name, content:''})
  });
  document.getElementById('sp-file-input').value = '';
  // 添加文件后自动展开该智能体，便于看到新文件
  if (agent) spCollapsedAgents.delete(agent);
  spSaveCollapseState();
  spRefresh();
  if (agent) spLoad(agent, name); else spLoadGlobal(name);
}

async function spSave() {
  if (!spFile) return toast('请先选择文件', false);
  const agent = spAgent || 'global';
  const resp = await fetch('/api/system_prompt/file', {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({agent:agent, filename:spFile, content: document.getElementById('sp-editor').value})
  });
  const data = await resp.json().catch(() => ({}));
  // .json 文件（如 TOOL.json）：已写入磁盘，但格式错误会让 orchestrator 降级，明确提示
  if (data.json_error) return toast('已保存，但 ' + data.json_error, false);
  // 保存了工具目录 → 刷新勾选器用的目录缓存
  if (spFile.toUpperCase() === 'TOOL.JSON') {
    await tpLoadCatalog(true);
    if (tpMode === 'catalog') tpRenderPanel();
  }
  toast('保存成功');
}

async function spDeleteFile() {
  if (!spFile) return toast('请先选择文件', false);
  const agent = spAgent || 'global';
  if (!confirm('删除 ' + agent + '/' + spFile + '?')) return;
  await fetch('/api/system_prompt/file/' + agent + '/' + spFile, {method:'DELETE'});
  spFile = '';
  document.getElementById('sp-editor').value = '';
  spRefresh();
}
