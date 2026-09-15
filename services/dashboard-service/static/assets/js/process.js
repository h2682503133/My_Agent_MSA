// 长期事件管理（PROCESS）（由 index.html 内联 script 拆出）

// ═══ 长期事件管理（PROCESS）═══

let procPendingAlias = null; // 别名解析结果 {alias: storeKey}，待追加到模板

async function procLoadUsers() {
  const sel = document.getElementById('proc-user-select');
  const tplSel = document.getElementById('proc-tpl-agent-select');
  sel.innerHTML = '<option value="">-- 加载中 --</option>';
  try {
    const data = await fetch('/api/process/users').then(r => r.json());
    sel.innerHTML = '<option value="">-- 选择 user_id --</option>' +
      (data.users || []).map(u => `<option value="${escapeHtml(u)}">${escapeHtml(u)}</option>`).join('');
    // 模板侧的 agent 下拉复用"每个 user 下出现过的 agent + 模板已有 agent"，
    // 简单起见加载第一个有数据的 user 的 agent 列表
    await procRefreshTplAgents();
  } catch(e) { sel.innerHTML = '<option value="">-- 加载失败 --</option>'; }
}

async function procReloadUsers() {
  await procLoadUsers();
  toast('用户列表已刷新');
}

async function procLoadAgents() {
  const user = document.getElementById('proc-user-select').value;
  const sel = document.getElementById('proc-agent-select');
  sel.innerHTML = '<option value="">-- 加载中 --</option>';
  document.getElementById('proc-editor').value = '';
  document.getElementById('proc-path-info').textContent = '';
  if (!user) { sel.innerHTML = '<option value="">-- 选择 agent_id --</option>'; return; }
  try {
    const data = await fetch('/api/process/agents?user=' + encodeURIComponent(user)).then(r => r.json());
    sel.innerHTML = '<option value="">-- 选择 agent_id --</option>' +
      (data.agents || []).map(a => `<option value="${escapeHtml(a)}">${escapeHtml(a)}</option>`).join('');
  } catch(e) { sel.innerHTML = '<option value="">-- 加载失败 --</option>'; }
}

async function procLoadFile() {
  const user = document.getElementById('proc-user-select').value;
  const agent = document.getElementById('proc-agent-select').value;
  const ed = document.getElementById('proc-editor');
  ed.value = '';
  document.getElementById('proc-path-info').textContent = '';
  if (!user || !agent) return;
  try {
    const data = await fetch(`/api/process/file?user=${encodeURIComponent(user)}&agent=${encodeURIComponent(agent)}`).then(r => r.json());
    ed.value = data.content;
    document.getElementById('proc-path-info').textContent = `process/${user}/${agent}.json`;
  } catch(e) { toast('读取失败: ' + e, false); }
}

async function procSaveFile() {
  const user = document.getElementById('proc-user-select').value;
  const agent = document.getElementById('proc-agent-select').value;
  if (!user || !agent) { toast('请先选择 user_id 和 agent_id', false); return; }
  try {
    const r = await fetch(`/api/process/file?user=${encodeURIComponent(user)}&agent=${encodeURIComponent(agent)}`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: document.getElementById('proc-editor').value})
    });
    const data = await r.json();
    if (data.ok) { toast('已保存 ' + data.path); procReloadUsers(); }
    else toast('保存失败', false);
  } catch(e) { toast('保存失败: ' + e, false); }
}

// ─── 模板 ───

async function procRefreshTplAgents() {
  const sel = document.getElementById('proc-tpl-agent-select');
  // 从第一个有 process 数据的 user 收集 agent 名；模板可能独立存在，也补上模板目录里的 agent
  const users = await fetch('/api/process/users').then(r => r.json()).catch(() => ({users: []}));
  const agents = new Set();
  for (const u of (users.users || []).slice(0, 5)) {
    const d = await fetch('/api/process/agents?user=' + encodeURIComponent(u)).then(r => r.json()).catch(() => ({agents: []}));
    (d.agents || []).forEach(a => agents.add(a));
  }
  sel.innerHTML = '<option value="">-- 选择 agent_id --</option>' +
    [...agents].sort().map(a => `<option value="${escapeHtml(a)}">${escapeHtml(a)}</option>`).join('');
}

async function procLoadTemplate() {
  const agent = document.getElementById('proc-tpl-agent-select').value;
  const ed = document.getElementById('proc-tpl-editor');
  ed.value = '';
  if (!agent) return;
  try {
    const data = await fetch('/api/process/template?agent=' + encodeURIComponent(agent)).then(r => r.json());
    ed.value = data.content;
  } catch(e) { toast('读取模板失败: ' + e, false); }
}

async function procSaveTemplate() {
  const agent = document.getElementById('proc-tpl-agent-select').value;
  if (!agent) { toast('请先选择 agent_id', false); return; }
  try {
    const r = await fetch('/api/process/template?agent=' + encodeURIComponent(agent), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: document.getElementById('proc-tpl-editor').value})
    });
    const data = await r.json();
    if (data.ok) { toast('模板已保存 ' + data.path); }
    else toast('模板保存失败', false);
  } catch(e) { toast('模板保存失败: ' + e, false); }
}

// ─── 别名解析工具 ───

function procAliasParse() {
  const key = document.getElementById('proc-ali-key').value.trim();
  const raw = document.getElementById('proc-ali-values').value;
  const aliases = raw.split(/[,，、\s]+/).map(s => s.trim()).filter(Boolean);
  const out = document.getElementById('proc-ali-output');
  const status = document.getElementById('proc-ali-status');
  if (!key) { out.textContent = '请先输入存储 key'; procPendingAlias = null; return; }
  if (!aliases.length) { out.textContent = '请输入至少一个别名'; procPendingAlias = null; return; }
  const map = {};
  for (const a of aliases) map[a] = key;
  procPendingAlias = map;
  out.textContent = JSON.stringify(map, null, 2);
  status.textContent = `${aliases.length} 个别名 → 存储 key「${key}」`;
}

async function procAliasAppend() {
  if (!procPendingAlias) { toast('请先生成映射', false); return; }
  const agent = document.getElementById('proc-tpl-agent-select').value;
  if (!agent) { toast('请先选择 agent_id', false); return; }
  let data;
  try {
    const r = await fetch('/api/process/template?agent=' + encodeURIComponent(agent)).then(r => r.json());
    data = JSON.parse(r.content);
  } catch(e) { toast('模板读取失败: ' + e, false); return; }
  if (!data.aliases) data.aliases = {};
  Object.assign(data.aliases, procPendingAlias);
  document.getElementById('proc-tpl-editor').value = JSON.stringify(data, null, 2);
  toast('已追加到模板 aliases（点击「保存模板」生效）');
}
