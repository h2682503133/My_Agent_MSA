// QQ 设置：LLBot 扫码（沿用 qrcode.js 的 refreshQrcode） + 官方 bot openid 绑定

function qqSwitch(pane) {
  const llbot = document.getElementById('qq-pane-llbot');
  const official = document.getElementById('qq-pane-official');
  if (!llbot || !official) return;
  const isLlb = pane === 'llbot';
  llbot.style.display = isLlb ? '' : 'none';
  official.style.display = isLlb ? 'none' : '';
  document.querySelectorAll('.qq-sub').forEach(btn => {
    const active = btn.dataset.qq === pane;
    btn.classList.toggle('green', active);
    btn.classList.toggle('active', active);
  });
  if (isLlb) refreshQrcode();
  else { qqLoadApp(); qqLoadBindings(); }
}

async function qqLoadBindings() {
  const tbody = document.getElementById('qq-bind-rows');
  if (!tbody) return;
  try {
    const r = await fetch('/api/qq/channels');
    const data = await r.json();
    const rows = (data && data.bindings) || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" style="color:var(--dim);">还没有绑定。' +
        '让对方在 QQ 里给官方机器人发 <code>/bind &lt;QQ号&gt;</code> 即可。</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(row => `
      <tr>
        <td>${row.qq_user_id ? esc(row.qq_user_id) : '<span style="color:var(--dim);">未绑 qq 渠道</span>'}</td>
        <td style="word-break:break-all;">${esc(row.channel_user_id)}</td>
        <td>${esc(row.user_id)}</td>
        <td>${esc(row.priority)}</td>
        <td style="text-align:right;">
          <button class="btn danger" onclick="qqUnbind('${esc(row.user_id)}')">解绑</button>
        </td>
      </tr>`).join('');
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--red);">读取失败：' + esc(e.message) + '</td></tr>';
  }
}

async function qqBind() {
  const userId = (document.getElementById('qq-bind-user').value || '').trim();
  const openid = (document.getElementById('qq-bind-openid').value || '').trim();
  if (!userId || !openid) {
    toast('user_id 和 qqofficial id 都要填', false);
    return;
  }
  try {
    const r = await fetch('/api/qq/bind', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, channel_user_id: openid }),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      toast('绑定成功');
      document.getElementById('qq-bind-openid').value = '';
      qqLoadBindings();
    } else {
      toast(data.detail || data.message || '绑定失败', false);
    }
  } catch (e) {
    toast('请求失败：' + e.message, false);
  }
}

async function qqUnbind(userId) {
  if (!confirm('确定解绑 ' + userId + ' 的 qqofficial 渠道？')) return;
  try {
    const r = await fetch('/api/qq/unbind', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId }),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      toast('已解绑');
      qqLoadBindings();
    } else {
      toast(data.detail || data.message || '解绑失败', false);
    }
  } catch (e) {
    toast('请求失败：' + e.message, false);
  }
}

async function qqLoadApp() {
  const el = document.getElementById('qq-app-status');
  const idInput = document.getElementById('qq-app-id');
  if (!el) return;
  try {
    const data = await fetch('/api/qq/app').then(r => r.json());
    if (data.app_id && idInput && !idInput.value) idInput.value = data.app_id;
    el.innerHTML = data.configured
      ? '已配置：AppID <code>' + esc(data.app_id) + '</code>，AppSecret 已保存（' + esc(data.channel) + '）'
      : '未配置：请填写 AppID 与 AppSecret 后点「保存并重启」';
    el.style.color = data.configured ? 'var(--green)' : 'var(--dim)';
  } catch (e) {
    el.textContent = '读取失败：' + e.message;
    el.style.color = 'var(--red)';
  }
}

async function qqSaveApp() {
  const appId = (document.getElementById('qq-app-id').value || '').trim();
  const secret = (document.getElementById('qq-app-secret').value || '').trim();
  if (!appId) { toast('AppID 不能为空', false); return; }
  try {
    const r = await fetch('/api/qq/app', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ app_id: appId, client_secret: secret }),
    });
    const data = await r.json();
    if (!(r.ok && data.ok)) {
      toast(data.detail || '保存失败', false);
      return;
    }
    document.getElementById('qq-app-secret').value = '';
    const rr = await fetch('/api/restart?service=' + encodeURIComponent(data.deployment || 'qq-official-service'), { method: 'POST' });
    const rd = await rr.json();
    if (rd.ok) toast('已保存并重启 ' + (data.deployment || 'qq-official-service'));
    else toast('配置已保存，但重启失败：' + (rd.error || '未知错误'), false);
    qqLoadApp();
  } catch (e) {
    toast('请求失败：' + e.message, false);
  }
}