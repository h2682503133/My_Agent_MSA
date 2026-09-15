// 启动：首次刷新与定时器（由 index.html 内联 script 拆出）

// ─── 初始化 ────────────────────────────────────────────────
refreshPods();
setInterval(refreshPods, 15000);
