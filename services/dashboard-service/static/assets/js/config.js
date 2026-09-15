// 配置管理（由 index.html 内联 script 拆出）

// ─── 配置管理 ──────────────────────────────────────────────
let currentConfigPath = '';

async function refreshConfigs() {
  const data = await fetch('/api/config').then(r => r.json());
  const list = document.getElementById('config-list');
  list.innerHTML = data.files.map(f => `
    <label class="file-item">
      <input type="radio" name="config-file" value="${f.path}" onchange="loadConfig('${f.path}')">
      ${f.path} <span style="color:var(--dim);font-size:0.7rem;">(${f.size}B)</span>
    </label>`).join('');
}

async function loadConfig(path) {
  currentConfigPath = path;
  document.getElementById('config-filename').textContent = path;
  const data = await fetch('/api/config/' + path).then(r => r.json());
  document.getElementById('config-editor').value = data.content;
}

async function reloadConfig() {
  if (currentConfigPath) loadConfig(currentConfigPath);
}

async function saveConfig() {
  if (!currentConfigPath) return toast('请先选择文件', false);
  const content = document.getElementById('config-editor').value;
  const r = await fetch('/api/config/' + currentConfigPath, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: currentConfigPath, content })
  }).then(r => r.json());
  toast(r.ok ? '保存成功' : '保存失败', r.ok);
}
