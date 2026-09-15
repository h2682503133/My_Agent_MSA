// 公共：登录/改密、Tab 切换、Toast、HTML 转义（由 index.html 内联 script 拆出）

// ═══ 登录 ═══════════════════════════════════════════════

let loggedIn = false;

async function doLogin() {
  const pwd = document.getElementById('login-pwd').value;
  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: pwd})
    });
    if (r.ok) {
      loggedIn = true;
      document.getElementById('login-overlay').classList.add('hidden');
      document.getElementById('login-err').textContent = '';
      refreshPods();
    } else {
      document.getElementById('login-err').textContent = '密码错误';
    }
  } catch(e) {
    document.getElementById('login-err').textContent = '连接失败';
  }
}

function openPwdModal() {
  document.getElementById('pwd-modal').classList.remove('hidden');
  document.getElementById('pwd-old').value = '';
  document.getElementById('pwd-new').value = '';
  document.getElementById('pwd-confirm').value = '';
  document.getElementById('pwd-err').textContent = '';
}

function closePwdModal() {
  document.getElementById('pwd-modal').classList.add('hidden');
}

async function changePwd() {
  const oldPwd = document.getElementById('pwd-old').value;
  const newPwd = document.getElementById('pwd-new').value;
  const confirm = document.getElementById('pwd-confirm').value;
  const errEl = document.getElementById('pwd-err');
  if (newPwd !== confirm) { errEl.textContent = '两次新密码不一致'; return; }
  if (!newPwd) { errEl.textContent = '新密码不能为空'; return; }
  try {
    const r = await fetch('/api/auth/change-password', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({old_password: oldPwd, new_password: newPwd})
    });
    const data = await r.json();
    if (r.ok) {
      closePwdModal();
      toast('密码修改成功');
    } else {
      errEl.textContent = data.detail || '修改失败';
    }
  } catch(e) {
    errEl.textContent = '请求失败';
  }
}

// ─── Tab 切换 ────────────────────────────────────────────
document.querySelectorAll('nav button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'pods') refreshPods();
    if (btn.dataset.tab === 'config') refreshConfigs();
    if (btn.dataset.tab === 'logs') refreshLogPods();
    if (btn.dataset.tab === 'deploy') refreshDeployTab();
    if (btn.dataset.tab === 'session') sessLoadUsers();
    if (btn.dataset.tab === 'sprompt') spRefresh();
    if (btn.dataset.tab === 'worldinfo') wiLoad();
    if (btn.dataset.tab === 'process') procLoadUsers();
    if (btn.dataset.tab === 'qrcode') refreshQrcode();
    if (btn.dataset.tab === 'misc') loadMiscSettings();
  });
});

// ─── Toast ────────────────────────────────────────────────
function toast(msg, ok = true) {
  const el = document.createElement('div');
  el.className = 'toast ' + (ok ? 'ok' : 'err');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2500);
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
