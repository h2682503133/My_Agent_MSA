// LLBot 二维码（由 index.html 内联 script 拆出）

// ═══ 二维码 ═══════════════════════════════════════════════

function refreshQrcode() {
  const img = document.getElementById('qrcode-img');
  if (img) img.src = '/api/qrcode?t=' + Date.now();
  fetch('/api/qrcode/info').then(r => r.json()).then(data => {
    const el = document.getElementById('qrcode-time');
    if (data.updated_at) {
      el.textContent = '最后更新：' + data.updated_at;
    } else {
      el.textContent = '';
    }
  }).catch(() => {});
}
