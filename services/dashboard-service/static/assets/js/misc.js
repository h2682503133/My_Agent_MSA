// 杂项设置（由 index.html 内联 script 拆出）

// ═══ 杂项设置 ═══════════════════════════════════════════════

async function loadMiscSettings() {
  const imgCb = document.getElementById('misc-image-receive');
  const idCb = document.getElementById('misc-main-identity');
  const shCb = document.getElementById('misc-shell-restrict');
  const usersEl = document.getElementById('misc-shell-users');
  const mntCb = document.getElementById('misc-mnt-restrict');
  const mntUsersEl = document.getElementById('misc-mnt-users');
  try {
    const data = await fetch('/api/misc/settings').then(r => r.json()) || {};
    imgCb.checked = !!(data.image_receive_enabled !== false);
    idCb.checked = !!(data.main_read_identity !== false);
    shCb.checked = !!data.shell_restriction_enabled;
    usersEl.value = (data.shell_allowed_users || []).join('\n');
    usersEl.disabled = !shCb.checked;
    mntCb.checked = !!data.mnt_write_restriction_enabled;
    mntUsersEl.value = (data.mnt_write_allowed_users || []).join('\n');
    mntUsersEl.disabled = !mntCb.checked;
  } catch(e) {
    imgCb.checked = true;
    idCb.checked = true;
    shCb.checked = false;
    usersEl.disabled = true;
    mntCb.checked = false;
    mntUsersEl.disabled = true;
  }
}

function collectMiscSettings() {
  return {
    image_receive_enabled: document.getElementById('misc-image-receive').checked,
    main_read_identity: document.getElementById('misc-main-identity').checked,
    shell_restriction_enabled: document.getElementById('misc-shell-restrict').checked,
    shell_allowed_users: document.getElementById('misc-shell-users').value.split(/[\n,，]+/).map(s => s.trim()).filter(Boolean),
    mnt_write_restriction_enabled: document.getElementById('misc-mnt-restrict').checked,
    mnt_write_allowed_users: document.getElementById('misc-mnt-users').value.split(/[\n,，]+/).map(s => s.trim()).filter(Boolean),
  };
}

async function saveMiscSettings() {
  const shCb = document.getElementById('misc-shell-restrict');
  document.getElementById('misc-shell-users').disabled = !shCb.checked;
  const mntCb = document.getElementById('misc-mnt-restrict');
  document.getElementById('misc-mnt-users').disabled = !mntCb.checked;
  try {
    const r = await fetch('/api/misc/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectMiscSettings()),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    toast('杂项设置已保存');
  } catch(e) {
    toast('保存失败: ' + e, false);
    loadMiscSettings();
  }
}

async function saveHostUsers() {
  const users = document.getElementById('misc-mnt-users').value.split(/[\n,，]+/).map(s => s.trim()).filter(Boolean);
  try {
    const r = await fetch('/api/misc/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mnt_write_allowed_users: users }),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    toast('宿主机访问白名单已保存');
  } catch(e) {
    toast('保存失败: ' + e, false);
  }
}

async function saveShellUsers() {
  const users = document.getElementById('misc-shell-users').value.split(/[\n,，]+/).map(s => s.trim()).filter(Boolean);
  try {
    const r = await fetch('/api/misc/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shell_allowed_users: users }),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    toast('shell 白名单已保存');
  } catch(e) {
    toast('保存失败: ' + e, false);
  }
}
