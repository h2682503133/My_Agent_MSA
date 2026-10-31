// 用假 DOM 跑 toolperm.js：验证数据能真正写进各字段（离线，不需要浏览器）
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const JS = fs.readFileSync(path.join(__dirname, '..', 'static', 'assets', 'js', 'toolperm.js'), 'utf8');

// 页面里存在的元素（与 toolperm.html 对齐）
const ids = [
  'tp-levels', 'tp-tool-list', 'tp-tool-search', 'tp-status',
  'tp-blacklist', 'tp-l2', 'tp-l3', 'tp-l4',
  'tp-note-all', 'tp-note-l0', 'tp-note-l1', 'tp-note-l2', 'tp-note-l3', 'tp-note-l4',
  'tp-notes-status', 'tp-blacklist-hint', 'tp-blocked-hint',
];
const elements = {};
for (const id of ids) elements[id] = { id, value: '', innerHTML: '', textContent: '' };

const payload = {
  levels: [{ key: 'L0', name: '公开', mode: '所有用户', desc: '只读' }],
  L1_blacklist_users: ['111'],
  L2_whitelist_users: ['2195239690', 'h268'],
  L3_whitelist_users: ['2195239690'],
  L4_whitelist_users: ['2195239690'],
  level_notes: { ALL: '通用说明', L3: 'L3 说明' },
  blacklist_hint: '提示A',
  blacklist_blocked_hint: '提示B',
  tools: [
    { name: 'shell', args: 'cmd', level: 'L3', levels: ['L3'] },
    { name: 'file-read', args: 'path', level: 'L0', levels: ['L0'] },
    { name: 'weird', args: '' },                    // 故意缺 levels，验证容错
  ],
  known_users: ['111', '2195239690', 'h268'],
  settings_path: '/app/config/orchestrator/config/system_settings.json',
};

const sandbox = {
  console,
  fetch: async () => ({ ok: true, status: 200, json: async () => payload }),
  document: {
    getElementById: id => elements[id] || null,
    querySelector: () => null,
    addEventListener: () => {},
    createElement: () => ({ style: {}, remove() {} }),
    body: { appendChild() {} },
  },
  location: { hash: '' },
  setTimeout: () => 0,
  Array, String, Object, Error, JSON,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const FAILS = [];
const check = (name, cond, extra = '') => {
  if (cond) console.log('  ok    ' + name);
  else { FAILS.push(name); console.log('  FAIL  ' + name + (extra ? '  ' + extra : '')); }
};

(async () => {
  vm.runInContext(JS, sandbox);
  check('tpRefresh 已定义', typeof sandbox.tpRefresh === 'function');
  await sandbox.tpRefresh();

  check('分级说明渲染', elements['tp-levels'].innerHTML.includes('L0'));
  check('工具一览渲染（含缺 levels 的容错）', elements['tp-tool-list'].innerHTML.includes('shell')
        && elements['tp-tool-list'].innerHTML.includes('weird'), elements['tp-tool-list'].innerHTML.slice(0, 120));
  check('L1 黑名单写入', elements['tp-blacklist'].value === '111', elements['tp-blacklist'].value);
  check('L2 名单写入', elements['tp-l2'].value === '2195239690\nh268', JSON.stringify(elements['tp-l2'].value));
  check('L3 名单写入', elements['tp-l3'].value === '2195239690');
  check('L4 名单写入', elements['tp-l4'].value === '2195239690');
  check('ALL 说明写入', elements['tp-note-all'].value === '通用说明', elements['tp-note-all'].value);
  check('L3 说明写入', elements['tp-note-l3'].value === 'L3 说明');
  check('黑名单提示写入', elements['tp-blacklist-hint'].value === '提示A');
  check('受限提示写入', elements['tp-blocked-hint'].value === '提示B');
  check('状态栏显示路径与计数',
        elements['tp-status'].textContent.includes('system_settings.json')
        && elements['tp-status'].textContent.includes('工具 3 个'),
        elements['tp-status'].textContent);
  check('没有"部分区块出错"', !elements['tp-status'].textContent.includes('出错'),
        elements['tp-status'].textContent);

  // 接口报错时：状态栏必须给出原因，而不是留白
  sandbox.fetch = async () => ({ ok: false, status: 403, json: async () => ({}) });
  await sandbox.tpRefresh();
  check('加载失败有提示', elements['tp-status'].textContent.includes('403'), elements['tp-status'].textContent);

  console.log();
  if (FAILS.length) { console.log('FAILED ' + FAILS.length + ': ' + FAILS.join(', ')); process.exit(1); }
  console.log('ALL OK — 14 checks passed');
})();
