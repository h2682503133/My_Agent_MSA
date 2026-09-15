// 工具目录 TOOL.json → 智能体 TOOL.md 勾选生成（由 index.html 内联 script 拆出）

// ─── 工具目录（TOOL.json）→ 智能体 TOOL.md 勾选生成 ─────────
// 勾选规则（段 / 工具 include·exclude、默认关闭段需点名）由后端
// app/tool_catalog.py 统一实现，前端只传勾选结果，不重复规则。
let tpCatalog = null;        // /api/tool/catalog 结果
let tpTargetAgent = '';      // 面板对应的智能体（'' = 全局 TOOL.json 视图）
let tpMode = '';             // catalog / options / legacy / missing
let tpSections = new Set();  // 勾选的段 id
let tpTools = new Set();     // 勾选的工具名
let tpShowTitle = true;
let tpBodyPos = 'after';
let tpPreview = '';
let tpWarnings = [];
let tpDeclares = null;
let tpFilter = '';
let tpBody = '';             // 正文补充（单一来源：输入时同步，渲染时不再读 DOM）
let spLastAgents = [];

async function tpLoadCatalog(force) {
  if (tpCatalog && !force) return tpCatalog;
  tpCatalog = await fetch('/api/tool/catalog').then(r => r.json()).catch(() => null);
  return tpCatalog;
}

function tpHidePanel() {
  const panel = document.getElementById('sp-tool-panel');
  if (panel) { panel.style.display = 'none'; panel.innerHTML = ''; }
  tpTargetAgent = ''; tpMode = ''; tpPreview = ''; tpWarnings = [];
}

function tpSummary() {
  return (tpCatalog && tpCatalog.summary) || { sections: [], section_count: 0, tool_count: 0 };
}

function tpToolNamesIn(sections) {
  const names = [];
  (tpSummary().sections || []).forEach(s => {
    if (!sections.has(s.id)) return;
    (s.tools || []).forEach(t => names.push(t.name));
  });
  return names;
}

// 打开某智能体的 TOOL.md 生成器；forceNew=true 表示文件缺失、要新生成
async function tpOpenForAgent(agent, forceNew) {
  await tpLoadCatalog();
  const info = await fetch('/api/tool/options?agent=' + encodeURIComponent(agent))
    .then(r => r.json()).catch(() => null);
  if (!info) { tpHidePanel(); return toast('读取 TOOL.md 状态失败', false); }

  tpTargetAgent = agent;
  tpMode = forceNew && info.mode !== 'options' && info.mode !== 'legacy' ? 'missing' : info.mode;
  tpDeclares = info.declares_tool_md;
  tpPreview = ''; tpWarnings = info.warnings || [];
  tpFilter = '';
  tpBody = info.body || '';

  if (tpMode === 'options' && info.options) {
    tpSections = new Set(info.picked_sections || []);
    tpTools = new Set(info.picked_tools || []);
    tpShowTitle = info.options.show_title !== false;
    tpBodyPos = String(info.options.body_position || 'after').toLowerCase() === 'before' ? 'before' : 'after';
  } else {
    // 老式原文 / 尚未创建：默认勾选目录里默认可注入的段与它们的全部工具
    const def = info.default_options || {};
    tpSections = new Set(def.sections || []);
    tpTools = new Set(tpToolNamesIn(tpSections));
    tpShowTitle = true; tpBodyPos = 'after';
  }

  // 面板要能保存，编辑器需要指向该智能体的 TOOL.md
  spAgent = agent; spFile = 'TOOL.md';
  document.getElementById('sp-editor-title').textContent = agent + ' / TOOL.md（勾选生成）';
  if (forceNew && tpMode === 'missing') document.getElementById('sp-editor').value = '';
  tpRenderPanel();
}

async function tpOpenForCatalog() {
  await tpLoadCatalog(true);
  tpTargetAgent = ''; tpMode = 'catalog'; tpPreview = ''; tpWarnings = [];
  tpRenderPanel();
}

function tpRenderPanel() {
  const panel = document.getElementById('sp-tool-panel');
  if (!panel) return;
  if (tpMode === 'catalog') return tpRenderCatalogPanel(panel);
  if (!tpMode) return tpHidePanel();

  const catalog = tpCatalog || {};
  let html = '<div class="tp-head"><b>TOOL.md 生成器</b>';
  html += `<span class="tp-badge info">${esc(tpTargetAgent)}</span>`;
  if (tpMode === 'options') html += '<span class="tp-badge ok">当前：选项式</span>';
  if (tpMode === 'legacy') html += '<span class="tp-badge warn">当前：手写原文式</span>';
  if (tpMode === 'missing') html += '<span class="tp-badge warn">当前：没有 TOOL.md</span>';
  if (catalog.json_error) html += `<span class="tp-badge err">TOOL.json ${esc(catalog.json_error)}</span>`;
  else if (!catalog.exists) html += '<span class="tp-badge err">缺少 TOOL.json</span>';
  html += `<span class="tp-badge info">目录 ${tpSummary().section_count} 段 / ${tpSummary().tool_count} 工具</span>`;
  html += '</div>';

  if (tpMode === 'legacy') {
    html += '<div class="tp-note">原手写内容已放进下方「正文补充」，转换后作为补充说明继续注入；不需要就清空它。</div>';
  }
  if (tpDeclares === false) {
    html += '<div class="tp-warn">agent_list.json 里该智能体的 files 没有声明 TOOL.md，保存后不会生效。</div>';
  }

  // 勾选工具栏
  html += '<div class="tp-bar">';
  html += '<button class="btn" style="padding:2px 8px;font-size:0.75rem;" onclick="tpSetAll(true)">全选</button>';
  html += '<button class="btn" style="padding:2px 8px;font-size:0.75rem;" onclick="tpSetAll(false)">全不选</button>';
  html += '<button class="btn" style="padding:2px 8px;font-size:0.75rem;" onclick="tpInvert()">反选</button>';
  html += '<span class="tp-note" id="tp-count"></span>';
  html += `<input type="text" id="tp-filter" placeholder="过滤工具名 / 说明" value="${esc(tpFilter)}" oninput="tpApplyFilter(this.value)">`;
  html += '</div>';

  const groups = (tpCatalog && tpCatalog.groups) || [];
  if (groups.length) {
    html += '<div class="tp-bar"><span class="tp-note">按分组整组勾选：</span>';
    groups.forEach(g => {
      html += `<button class="btn" style="padding:1px 8px;font-size:0.72rem;" onclick="tpToggleGroup('${esc(g)}')">${esc(g)}</button>`;
    });
    html += '</div>';
  }

  // 段 / 工具勾选列表
  html += '<div class="tp-list">';
  (tpSummary().sections || []).forEach(s => {
    const on = tpSections.has(s.id);
    html += `<div class="tp-sec ${on ? '' : 'off'}" data-sec="${esc(s.id)}">
      <div class="tp-sec-head" onclick="tpToggleSection('${esc(s.id)}')">
        <input type="checkbox" ${on ? 'checked' : ''} onclick="event.stopPropagation();tpToggleSection('${esc(s.id)}')">
        <span class="tp-sec-title">${esc(s.title || s.id)}</span>
        <span class="tp-note">${esc(s.id)}</span>
        ${s.enabled ? '' : '<span class="tp-badge warn">目录默认关闭</span>'}
        ${s.kind === 'text' ? '<span class="tp-badge info">文字段</span>' : `<span class="tp-badge info">${s.tools.length} 工具</span>`}
      </div>`;
    if (s.tools && s.tools.length) {
      html += '<div class="tp-tools">';
      s.tools.forEach(t => {
        const gs = (t.groups || []).join(' ');
        html += `<label class="tp-tool ${tpTools.has(t.name) ? '' : 'off'}" data-tool="${esc(t.name)}" data-groups="${esc(gs)}">
          <input type="checkbox" data-toolbox="${esc(t.name)}" ${tpTools.has(t.name) ? 'checked' : ''} ${on ? '' : 'disabled'}
                 onchange="tpToggleTool('${esc(t.name)}', this.checked)">
          <span><span class="nm">${esc(t.name)}</span>${t.args ? `<span class="ar">|${esc(t.args)}</span>` : ''}<span class="desc">${t.desc ? ' - ' + esc(t.desc) : ''}</span>
          ${(t.groups || []).map(g => `<span class="tp-chip">${esc(g)}</span>`).join('')}
          ${t.enabled ? '' : '<span class="tp-badge warn">目录默认关闭</span>'}</span>
        </label>`;
      });
      html += '</div>';
    }
    html += '</div>';
  });
  html += '</div>';

  // 正文补充 + 注入位置
  html += '<div class="tp-grid"><div>';
  html += '<div class="tp-note">正文补充（选项块之外的内容，作为该智能体自己的补充说明）</div>';
  html += `<textarea id="tp-body" style="min-height:70px;" placeholder="可留空" oninput="tpBody=this.value;tpSyncCounts()">${esc(tpBody)}</textarea>`;
  html += '</div><div>';
  html += '<div class="tp-note">注入位置</div>';
  html += `<select id="tp-bodypos" onchange="tpBodyPos=this.value">
    <option value="after" ${tpBodyPos === 'after' ? 'selected' : ''}>正文在工具清单之后</option>
    <option value="before" ${tpBodyPos === 'before' ? 'selected' : ''}>正文在工具清单之前</option>
  </select>`;
  html += `<label class="tp-note" style="display:block;margin-top:8px;"><input type="checkbox" id="tp-showtitle" ${tpShowTitle ? 'checked' : ''}
    onchange="tpShowTitle=this.checked"> 注入目录标题</label>`;
  html += '</div></div>';

  html += '<div class="tp-bar" style="border-top:none;padding-bottom:0;">';
  html += '<button class="btn" onclick="tpGenerate(\'preview\')">只预览</button>';
  html += '<button class="btn green" onclick="tpGenerate(\'fill\')">生成到编辑器</button>';
  html += '<button class="btn green" onclick="tpGenerate(\'save\')">生成并保存</button>';
  html += '<span class="tp-note">生成后写入下方编辑器，可再手动微调</span>';
  html += '</div>';

  if (tpWarnings.length) html += `<div class="tp-warn">${tpWarnings.map(esc).join('<br>')}</div>`;
  if (tpPreview) {
    html += `<div class="tp-note" style="margin-top:8px;">注入预览（orchestrator 实际发给模型的工具段，${tpPreview.length} 字）</div>`;
    html += `<pre class="tp-preview">${esc(tpPreview)}</pre>`;
  }
  panel.innerHTML = html;
  panel.style.display = 'block';
  tpApplyFilter(tpFilter);
  tpSyncCounts();
}

function tpApplyFilter(value) {
  tpFilter = value || '';
  const q = tpFilter.trim().toLowerCase();
  document.querySelectorAll('#sp-tool-panel .tp-tool').forEach(el => {
    const name = (el.dataset.tool || '').toLowerCase();
    const text = el.textContent.toLowerCase();
    el.style.display = (!q || name.includes(q) || text.includes(q)) ? '' : 'none';
  });
  document.querySelectorAll('#sp-tool-panel .tp-sec').forEach(sec => {
    const rows = sec.querySelectorAll('.tp-tool');
    if (!rows.length) return;
    let visible = 0;
    rows.forEach(r => { if (r.style.display !== 'none') visible++; });
    sec.style.display = (q && visible === 0) ? 'none' : '';
  });
}

function tpSyncCounts() {
  const el = document.getElementById('tp-count');
  if (!el) return;
  let picked = 0;
  (tpSummary().sections || []).forEach(s => {
    if (!tpSections.has(s.id)) return;
    (s.tools || []).forEach(t => { if (tpTools.has(t.name)) picked++; });
  });
  el.textContent = `已选 ${tpSections.size} 段 / ${picked} 工具`;
}

function tpToggleSection(id) {
  if (tpSections.has(id)) tpSections.delete(id); else {
    tpSections.add(id);
    // 勾上段时默认带上该段全部工具
    (tpSummary().sections || []).forEach(s => {
      if (s.id !== id) return;
      (s.tools || []).forEach(t => tpTools.add(t.name));
    });
  }
  tpRenderPanel();
}

function tpToggleTool(name, checked) {
  if (checked) tpTools.add(name); else tpTools.delete(name);
  const row = document.querySelector(`#sp-tool-panel .tp-tool[data-tool="${CSS.escape ? CSS.escape(name) : name}"]`);
  if (row) row.classList.toggle('off', !checked);
  tpSyncCounts();
}

function tpSetAll(on) {
  tpSections = new Set();
  tpTools = new Set();
  if (on) {
    (tpSummary().sections || []).forEach(s => {
      tpSections.add(s.id);
      (s.tools || []).forEach(t => tpTools.add(t.name));
    });
  }
  tpRenderPanel();
}

function tpInvert() {
  // 段与工具各自独立取反（未勾选段里的工具本来就不参与注入）
  const nextSections = new Set();
  const nextTools = new Set();
  (tpSummary().sections || []).forEach(s => {
    if (!tpSections.has(s.id)) nextSections.add(s.id);
    (s.tools || []).forEach(t => { if (!tpTools.has(t.name)) nextTools.add(t.name); });
  });
  tpSections = nextSections; tpTools = nextTools;
  tpRenderPanel();
}

function tpToggleGroup(group) {
  // 先找出该分组下的工具，判断是否已全选 → 决定整组勾上还是整组取消
  const targets = [];
  (tpSummary().sections || []).forEach(s => {
    (s.tools || []).forEach(t => { if ((t.groups || []).includes(group)) targets.push([s.id, t.name]); });
  });
  if (!targets.length) return;
  const allOn = targets.every(([sid, name]) => tpSections.has(sid) && tpTools.has(name));
  targets.forEach(([sid, name]) => {
    if (allOn) tpTools.delete(name);
    else { tpSections.add(sid); tpTools.add(name); }
  });
  tpRenderPanel();
}

async function tpGenerate(mode) {
  const payload = {
    sections: Array.from(tpSections),
    tools: Array.from(tpTools),
    body: tpBody,
    show_title: tpShowTitle,
    body_position: tpBodyPos,
  };
  const resp = await fetch('/api/tool/generate', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  }).then(r => r.json()).catch(() => null);
  if (!resp) return toast('生成失败：接口不可用', false);
  tpPreview = resp.preview || '';
  tpWarnings = resp.warnings || [];
  tpBody = payload.body;

  if (mode === 'fill' || mode === 'save') {
    document.getElementById('sp-editor').value = resp.content;
    tpMode = 'options';
    if (mode === 'save') {
      await spSave();
      toast('已生成并保存 TOOL.md');
      tpRenderPanel();
      spRefresh();
      return;
    }
    toast('已生成到编辑器，确认后点「保存」');
  } else if (resp.json_error) {
    toast('TOOL.json ' + resp.json_error, false);
  }
  tpRenderPanel();
}

// 全局 TOOL.json 视图：结构化展示目录 + 各智能体 TOOL.md 状态
function tpRenderCatalogPanel(panel) {
  const info = tpCatalog || {};
  const s = tpSummary();
  let html = '<div class="tp-head"><b>TOOL.json 工具目录</b>';
  html += `<span class="tp-badge ${info.exists ? 'ok' : 'err'}">${info.exists ? '已就绪' : '缺失'}</span>`;
  if (info.json_error) html += `<span class="tp-badge err">${esc(info.json_error)}</span>`;
  html += `<span class="tp-badge info">${s.section_count || 0} 段 / ${s.tool_count || 0} 工具</span>`;
  html += `<span class="tp-note">${esc(info.path || '')}</span>`;
  html += '</div>';
  html += '<div class="tp-note">工具清单只在 TOOL.json 维护；智能体 TOOL.md 只写注入选项。'
       + '点左侧智能体下的 TOOL.md，或用下面的「生成」按钮按勾选生成。</div>';

  html += '<div class="tp-list">';
  if (s.title) html += `<div class="tp-sec"><div class="tp-sec-head"><span class="tp-sec-title">${esc(s.title)}</span></div></div>`;
  (s.sections || []).forEach(sec => {
    html += `<div class="tp-sec">
      <div class="tp-sec-head">
        <span class="tp-sec-title">${esc(sec.title || sec.id)}</span>
        <span class="tp-note">${esc(sec.id)}</span>
        ${sec.enabled ? '<span class="tp-badge ok">默认注入</span>' : '<span class="tp-badge warn">默认关闭</span>'}
        ${sec.kind === 'text' ? '<span class="tp-badge info">文字段</span>' : `<span class="tp-badge info">${sec.tools.length} 工具</span>`}
      </div>`;
    if (sec.tools && sec.tools.length) {
      html += '<div class="tp-tools">' + sec.tools.map(t =>
        `<span class="tp-tool" style="display:inline-flex;"><span class="nm">${esc(t.name)}</span>${t.args ? `<span class="ar">|${esc(t.args)}</span>` : ''}`
        + ((t.groups || []).map(g => `<span class="tp-chip">${esc(g)}</span>`).join(''))
        + (t.enabled ? '' : '<span class="tp-badge warn">默认关闭</span>')
        + `</span>`).join('') + '</div>';
    }
    if (sec.note) html += `<div class="tp-note" style="padding-left:22px;">${esc(sec.note)}</div>`;
    html += '</div>';
  });
  html += '</div>';

  html += '<div class="tp-note" style="margin-top:6px;">各智能体 TOOL.md 状态</div>';
  html += '<div class="tp-list" style="max-height:180px;">';
  (spLastAgents || []).forEach(a => {
    const tf = (a.files || []).find(f => f.name.toLowerCase() === 'tool.md');
    let state, action;
    if (tf && tf.kind === 'tool_options') { state = '<span class="tp-badge ok">选项式</span>'; }
    else if (tf) { state = '<span class="tp-badge warn">原文式</span>'; }
    else if (a.declares_tool_md) { state = '<span class="tp-badge err">缺 TOOL.md</span>'; }
    else { state = '<span class="tp-badge info">未声明</span>'; }
    action = tf
      ? `<button class="btn" style="padding:0 8px;font-size:0.7rem;" onclick="spLoad('${esc(a.name)}','TOOL.md')">编辑/勾选</button>`
      : `<button class="btn green" style="padding:0 8px;font-size:0.7rem;" onclick="tpOpenForAgent('${esc(a.name)}', true)">生成</button>`;
    html += `<div class="tp-tool" style="padding:3px 6px;"><span class="nm">${esc(a.name)}</span>${state}
      <span style="margin-left:auto;">${action}</span></div>`;
  });
  html += '</div>';

  html += '<div class="tp-bar"><button class="btn" onclick="tpFormatCatalog()">格式化 JSON 到编辑器</button>';
  html += '<button class="btn" onclick="tpOpenForCatalog()">重新加载目录</button>';
  if (info.json_error) html += '<span class="tp-note">修好 JSON 后点「重新加载目录」刷新勾选器</span>';
  html += '</div>';
  panel.innerHTML = html;
  panel.style.display = 'block';
}

function tpFormatCatalog() {
  if (!tpCatalog || !tpCatalog.catalog) return toast('目录未加载', false);
  document.getElementById('sp-editor').value = JSON.stringify(tpCatalog.catalog, null, 2);
  toast('已格式化到编辑器，确认后点「保存」');
}
