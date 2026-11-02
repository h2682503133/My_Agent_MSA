// 杂项设置（由 index.html 内联 script 拆出）
// 只剩两个开关：接收图像、main 读取 IDENTITY.md。
// shell / Windows 宿主机权限已并入「工具权限」页（L3/L4 白名单），这里不再读写。


async function loadCapabilities() {
  const summary = document.getElementById('cap-summary');
  const list = document.getElementById('cap-list');
  if (!summary || !list) return;
  try {
    const data = await fetch('/api/capabilities').then(r => r.json());
    if (!data.exists) {
      summary.textContent = '尚未生成 capability.json（未通过部署程序安装时正常）；当前按"全部可用"处理。';
    } else {
      summary.textContent = `档位 ${data.tier || '未知'}　系统 ${data.os || '-'}　生成于 ${data.generated_at || '-'}`;
    }
    const color = item => (item.enabled ? '#4caf50' : '#e8b04b');
    list.innerHTML = (data.items || []).map(item =>
      `<div><span style="color:${color(item)};">●</span> ${item.label}：${item.value}</div>`).join('');
    const degraded = (data.degraded || []).filter(d => (d.level || 'degrade') !== 'info');
    const notes = (data.degraded || []).filter(d => d.level === 'info');
    if (degraded.length) {
      list.innerHTML += '<div style="margin-top:6px;color:#e8b04b;">降级 ' + degraded.length + ' 项：' +
        degraded.map(d => `${d.feature}（${d.reason}）`).join('；') + '</div>';
    }
    if (notes.length) {
      // 说明项：例如"单体模式没 Docker"，是正常形态，不算降级
      list.innerHTML += '<div style="margin-top:6px;color:#888;">说明：' +
        notes.map(d => `${d.feature}（${d.reason}）`).join('；') + '</div>';
    }
  } catch (err) {
    summary.textContent = `读取运行环境失败：${err}`;
  }
}

async function loadMiscSettings() {
  const imgCb = document.getElementById('misc-image-receive');
  const idCb = document.getElementById('misc-main-identity');
  try {
    const data = await fetch('/api/misc/settings').then(r => r.json()) || {};
    if (imgCb) imgCb.checked = !!(data.image_receive_enabled !== false);
    if (idCb) idCb.checked = !!(data.main_read_identity !== false);
    loadCapabilities();
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
