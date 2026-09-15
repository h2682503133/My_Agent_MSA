// Session 管理与长期记忆查询（由 index.html 内联 script 拆出）

// ═══ Session 管理 ═════════════════════════════════════════

let sessUser = '', sessName = '', sessMessages = [];

async function sessLoadUsers() {
  const sel = document.getElementById('sess-user-select');
  sel.innerHTML = '<option value="">-- 加载中 --</option>';
  try {
    const data = await fetch('/api/session/users').then(r => r.json());
    sel.innerHTML = '<option value="">-- 选择用户 --</option>' +
      (data.users || []).map(u => `<option value="${u.user_id}">${escapeHtml(u.user_id)} (${u.session_count}个session)</option>`).join('');
    sessUser = sel.value;
    sessLoadSessions();
  } catch(e) { sel.innerHTML = '<option value="">-- 加载失败 --</option>'; }
  memLoadUsers();
}

async function sessLoadSessions() {
  sessUser = document.getElementById('sess-user-select').value;
  sessName = '';
  sessMessages = [];
  document.getElementById('sess-msg-card').style.display = 'none';
  document.getElementById('sess-info').textContent = '';
  const sel = document.getElementById('sess-select');
  sel.innerHTML = '<option value="">-- 选择 Session --</option>';
  if (!sessUser) return;
  sel.innerHTML = '<option value="">-- 加载中 --</option>';
  try {
    const data = await fetch('/api/session/sessions?user=' + encodeURIComponent(sessUser)).then(r => r.json());
    sel.innerHTML = '<option value="">-- 选择 Session --</option>' +
      (data.sessions || []).map(s => `<option value="${s.name}">${s.name} (${s.msg_count}条, ${s.has_history?'有历史':'无历史'})</option>`).join('');
  } catch(e) { sel.innerHTML = '<option value="">-- 加载失败 --</option>'; }
}

async function sessLoadMessages() {
  sessName = document.getElementById('sess-select').value;
  sessMessages = [];
  document.getElementById('sess-msg-card').style.display = 'none';
  if (!sessName) return;
  document.getElementById('sess-info').textContent = '加载中...';
  try {
    const data = await fetch('/api/session/' + encodeURIComponent(sessUser) + '/' + encodeURIComponent(sessName) + '/messages').then(r => r.json());
    sessMessages = data.messages || [];
    document.getElementById('sess-msg-count').textContent = '共 ' + sessMessages.length + ' 条';
    document.getElementById('sess-info').textContent = '';
    sessRenderMessages();
    document.getElementById('sess-msg-card').style.display = 'block';
  } catch(e) { document.getElementById('sess-info').textContent = '加载失败'; toast('加载消息失败', false); }
}

function sessRenderMessages() {
  const tbody = document.getElementById('sess-msg-tbody');
  if (!sessMessages.length) { tbody.innerHTML = '<tr><td colspan="5" style="color:var(--dim);">暂无消息</td></tr>'; return; }
  tbody.innerHTML = sessMessages.map((m, i) => {
    const role = m.role || 'unknown';
    const roleCls = role === 'user' ? 'user' : 'assistant';
    let preview = '';
    if (m.parts && m.parts.length > 0) preview = (m.parts[0].text || '').replace(/\n/g, ' ').substring(0, 80);
    const time = m.created_at ? m.created_at.replace('T', ' ').substring(0, 19) : '';
    return `<tr class="msg-row" data-line="${m._line}" onclick="sessToggleRow(this, ${m._line})">
      <td><input type="checkbox" class="sess-cb" data-line="${m._line}" onclick="event.stopPropagation();"></td>
      <td>${m._line}</td>
      <td><span class="role-badge ${roleCls}">${role}</span></td>
      <td class="msg-preview">${preview}</td>
      <td style="font-size:0.75rem;color:var(--dim);">${time}</td>
    </tr>`;
  }).join('');
}

function sessToggleRow(tr, line) {
  const cb = tr.querySelector('.sess-cb');
  cb.checked = !cb.checked;
  tr.classList.toggle('selected', cb.checked);
}

function sessGetSelectedLines() {
  return [...document.querySelectorAll('.sess-cb:checked')].map(cb => parseInt(cb.dataset.line));
}

function sessToggleAll() {
  const all = document.querySelectorAll('.sess-cb');
  const anyUnchecked = [...all].some(cb => !cb.checked);
  all.forEach(cb => { cb.checked = anyUnchecked; });
  document.querySelectorAll('.msg-row').forEach(tr => tr.classList.toggle('selected', anyUnchecked));
}

async function sessDeleteMessages() {
  const lines = sessGetSelectedLines();
  if (!lines.length) return toast('请先选择要删除的消息', false);
  if (!confirm('确定删除选中的 ' + lines.length + ' 条消息？此操作不可撤销。')) return;
  try {
    const data = await fetch('/api/session/' + encodeURIComponent(sessUser) + '/' + encodeURIComponent(sessName) + '/messages', {
      method: 'DELETE', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lines: lines})
    }).then(r => r.json());
    toast('已删除 ' + data.deleted + ' 条，剩余 ' + data.remaining + ' 条');
    sessLoadMessages();
  } catch(e) { toast('删除失败', false); }
}

async function sessDeleteAgent() {
  if (!confirm('确定删除 ' + sessName + ' 整个文件夹？\n\n这将删除所有消息和历史记录，不可撤销！')) return;
  try {
    await fetch('/api/session/' + encodeURIComponent(sessUser) + '/' + encodeURIComponent(sessName), {method: 'DELETE'}).then(r => r.json());
    toast('已删除 ' + sessUser + '/' + sessName);
    sessLoadSessions();
    document.getElementById('sess-msg-card').style.display = 'none';
  } catch(e) { toast('删除失败', false); }
}

async function memLoadUsers() {
  const sel = document.getElementById('mem-user-select');
  try {
    const data = await fetch('/api/session/users').then(r => r.json());
    sel.innerHTML = '<option value="">-- 选择用户 --</option>' +
      (data.users || []).map(u => `<option value="${u.user_id}">${escapeHtml(u.user_id)}</option>`).join('');
  } catch(e) { sel.innerHTML = '<option value="">-- 选择用户 --</option>'; }
}

async function memSearch() {
  const user = document.getElementById('mem-user-select').value;
  const q = document.getElementById('mem-query').value.trim();
  const tbody = document.getElementById('mem-tbody');
  const info = document.getElementById('mem-info');
  if (!user) { info.textContent = '请先选择用户'; return; }
  if (!q) { info.textContent = '请输入问题/关键词'; return; }
  info.textContent = '搜索中...';
  try {
    const data = await fetch('/api/memory/search?user=' + encodeURIComponent(user)
      + '&query=' + encodeURIComponent(q) + '&limit=20').then(r => r.json());
    const hits = data.hits || [];
    info.textContent = '共 ' + hits.length + ' 条记忆匹配';
    if (!hits.length) { tbody.innerHTML = '<tr><td colspan="3" style="color:var(--dim);">无匹配记忆</td></tr>'; return; }
    tbody.innerHTML = hits.map(h => `<tr>
      <td style="font-size:0.8rem;">${(h.score * 100).toFixed(1)}%</td>
      <td style="font-size:0.8rem;color:var(--dim);">${escapeHtml(h.uri || h.session || '-')}</td>
      <td class="msg-preview" style="white-space:pre-wrap;">${escapeHtml(h.content)}</td>
    </tr>`).join('');
  } catch(e) { info.textContent = '搜索失败'; toast('搜索失败', false); }
}
