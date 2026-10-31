// 杂项设置（由 index.html 内联 script 拆出）
// 只剩两个开关：接收图像、main 读取 IDENTITY.md。
// shell / Windows 宿主机权限已并入「工具权限」页（L3/L4 白名单），这里不再读写。

async function loadMiscSettings() {
  const imgCb = document.getElementById('misc-image-receive');
  const idCb = document.getElementById('misc-main-identity');
  try {
    const data = await fetch('/api/misc/settings').then(r => r.json()) || {};
    if (imgCb) imgCb.checked = !!(data.image_receive_enabled !== false);
    if (idCb) idCb.checked = !!(data.main_read_identity !== false);
  } catch (e) {
    if (imgCb) imgCb.checked = true;
    if (idCb) idCb.checked = true;
  }
}

function collectMiscSettings() {
  return {
    image_receive_enabled: !!document.getElementById('misc-image-receive').checked,
    main_read_identity: !!document.getElementById('misc-main-identity').checked,
  };
}

async function saveMiscSettings() {
  try {
    const r = await fetch('/api/misc/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectMiscSettings()),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    toast('杂项设置已保存');
  } catch (e) {
    toast('保存失败: ' + e, false);
    loadMiscSettings();
  }
}
