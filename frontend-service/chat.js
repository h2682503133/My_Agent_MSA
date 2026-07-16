// ═══════════════════════════════════════════════════════════════
    // State
    // ═══════════════════════════════════════════════════════════════
    const userId = localStorage.getItem("user_id");
    const baseSessionId = localStorage.getItem("session_id") || (userId ? `web_${userId}` : "");

    if (!userId) { window.location.href = "/login.html"; }

    let conversations = [];           // [{agent_id, message_count, created_at, last_active}]
    let messageCache = {};            // {agent_id: [{role, text, images}]}
    let _restoring = false;           // skip cache sync during restore
    let currentAgentId = null;       // currently selected agent_id
    let eventSource = null;
    let logAutoRefreshTimer = null;

    // DOM refs
    const chatBox = document.getElementById("chat-box");
    const input = document.getElementById("message-input");
    const sendBtn = document.getElementById("send-btn");
    const convList = document.getElementById("conv-list");
    const userBadge = document.getElementById("user-badge");
    const queueStatus = document.getElementById("queue-status");
    const connectionStatus = document.getElementById("connection-status");
    const currentAgentName = document.getElementById("current-agent-name");
    const currentAgentIdEl = document.getElementById("current-agent-id");
    const rightPanel = document.getElementById("right-panel");
    const togglePanelBtn = document.getElementById("toggle-panel-btn");
    const mobileMenuBtn = document.getElementById("mobile-menu-btn");
    const mobilePanelBtn = document.getElementById("mobile-panel-btn");
    const overlay = document.getElementById("overlay");

    // ═══════════════════════════════════════════════════════════════
    // Init
    // ═══════════════════════════════════════════════════════════════
    userBadge.textContent = userId;
    document.getElementById("profile-user-id").textContent = userId;
    initPanelTabs();
    loadConversations().then(() => {
      // Preload cached messages from localStorage for all known conversations
      for (const conv of conversations) {
        loadCacheFromStorage(conv.agent_id);
      }
      // Auto-select first conversation if any
      if (conversations.length > 0) {
        switchConversation(conversations[0].agent_id);
      } else {
        // Create a default "main" conversation
        createConversation("main");
      }
    });
    loadUserProfile();

    // ═══════════════════════════════════════════════════════════════
    // Conversations
    // ═══════════════════════════════════════════════════════════════
    async function loadConversations() {
      try {
        const resp = await fetch(`/api/conversations?user_id=${encodeURIComponent(userId)}`);
        if (!resp.ok) return;
        const data = await resp.json();
        conversations = data.conversations || [];
        renderConvList();
      } catch (e) { console.error("loadConversations failed:", e); addSystemMessage("无法加载对话列表，请检查后端服务"); }
    }

    function renderConvList() {
      convList.innerHTML = "";
      if (conversations.length === 0) {
        convList.innerHTML = '<div style="padding:20px;text-align:center;color:#6c7086;font-size:13px;">暂无对话</div>';
        return;
      }
      for (const conv of conversations) {
        const div = document.createElement("div");
        div.className = "conv-item" + (conv.agent_id === currentAgentId ? " active" : "");
        div.innerHTML = `
          <span class="conv-name">${escHtml(conv.agent_id)}</span>
          <span class="conv-msg-count">${conv.message_count} 条</span>
          <button class="conv-del" onclick="event.stopPropagation();deleteConversation('${escHtml(conv.agent_id)}')">✕</button>
        `;
        div.onclick = () => switchConversation(conv.agent_id);
        convList.appendChild(div);
      }
    }

    async function switchConversation(agentId) {
      if (agentId === currentAgentId) return;

      // Save current messages to cache before switching away
      if (currentAgentId) {
        messageCache[currentAgentId] = collectChatMessages();
      }

      currentAgentId = agentId;
      currentAgentName.textContent = agentId;
      currentAgentIdEl.textContent = `agent: ${agentId}`;
      renderConvList();
      clearChatBox();

      // Restore messages from cache or fetch from backend
      const cached = messageCache[agentId];
      if (cached && cached.length > 0) {
        restoreChatMessages(cached);
      } else {
        await loadConversationMessages(agentId);
      }

      input.disabled = false;
      sendBtn.disabled = false;
      input.focus();
      reconnectEvents();
    }

    function showNewConvModal() {
      document.getElementById("new-conv-modal").classList.remove("hidden");
      document.getElementById("new-agent-id").value = "main";
      document.getElementById("new-agent-id").focus();
    }

    function closeNewConvModal() {
      document.getElementById("new-conv-modal").classList.add("hidden");
    }

    async function confirmNewConv() {
      const agentId = document.getElementById("new-agent-id").value.trim();
      if (!agentId) return;
      closeNewConvModal();
      await createConversation(agentId);
    }

    async function createConversation(agentId) {
      try {
        const resp = await fetch(`/api/conversations?user_id=${encodeURIComponent(userId)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ agent_id: agentId }),
        });
        if (!resp.ok) { addSystemMessage("创建对话失败: HTTP " + resp.status); return; }
        await loadConversations();
        switchConversation(agentId);
      } catch (e) { console.error("createConversation failed:", e); addSystemMessage("创建对话失败，请检查后端服务"); }
    }

    async function deleteConversation(agentId) {
      try {
        const resp = await fetch(`/api/conversations/${encodeURIComponent(agentId)}?user_id=${encodeURIComponent(userId)}`, {
          method: "DELETE",
        });
        if (!resp.ok) return;
        delete messageCache[agentId];
        try { localStorage.removeItem(localStorageKey(agentId)); } catch (_) {}
        if (currentAgentId === agentId) {
          currentAgentId = null;
          currentAgentName.textContent = "-";
          currentAgentIdEl.textContent = "agent: -";
          clearChatBox();
          input.disabled = true;
          sendBtn.disabled = true;
          if (eventSource) { eventSource.close(); eventSource = null; }
          setConnectionStatus(false);
        }
        await loadConversations();
      } catch (e) { console.error("deleteConversation failed:", e); }
    }

    // ═══════════════════════════════════════════════════════════════
    // SSE Events
    // ═══════════════════════════════════════════════════════════════
    function reconnectEvents() {
      if (eventSource) {
        eventSource.onopen = null;
        eventSource.onmessage = null;
        eventSource.onerror = null;
        eventSource.close();
        eventSource = null;
      }
      const url = `/api/events?user_id=${encodeURIComponent(userId)}&agent_id=${encodeURIComponent(currentAgentId || "")}`;
      eventSource = new EventSource(url);
      eventSource.onopen = () => setConnectionStatus(true);
      eventSource.onmessage = (event) => {
        if (!event.data || event.data.startsWith(":")) return;
        try {
          const data = JSON.parse(event.data);
          handleTaskEvent(data);
        } catch (err) {
          // ignore ping comments and malformed data
        }
      };
      eventSource.onerror = () => {
        setConnectionStatus(false);
        // EventSource auto-reconnects, no need to manually retry
      };
    }

    function setConnectionStatus(connected) {
      if (connected) {
        connectionStatus.textContent = "事件流：已连接";
        connectionStatus.className = "status-online";
      } else {
        connectionStatus.textContent = "事件流：重连中";
        connectionStatus.className = "status-offline";
      }
    }

    // ═══════════════════════════════════════════════════════════════
    // Message Cache Helpers
    // ═══════════════════════════════════════════════════════════════
    function collectChatMessages() {
      const msgs = [];
      const rows = chatBox.querySelectorAll('.msg-row');
      for (const row of rows) {
        const bubble = row.querySelector('.msg');
        if (!bubble) continue;
        const role = row.classList.contains('user') ? 'user'
          : row.classList.contains('agent') ? 'agent'
          : 'system';
        const text = bubble.textContent || '';
        const imgs = [];
        bubble.querySelectorAll('img.msg-img').forEach(img => imgs.push(img.src));
        msgs.push({ role, text, images: imgs });
      }
      return msgs;
    }

    function restoreChatMessages(msgs) {
      _restoring = true;
      for (const msg of msgs) {
        if (msg.role === 'system') {
          addSystemMessage(msg.text);
        } else {
          addMsg(msg.role, msg.text, msg.images || []);
        }
      }
      _restoring = false;
    }

    async function loadConversationMessages(agentId) {
      try {
        const resp = await fetch(`/api/conversations/${encodeURIComponent(agentId)}/messages?user_id=${encodeURIComponent(userId)}`);
        if (!resp.ok) return;
        const data = await resp.json();
        const msgs = data.messages || [];
        if (msgs.length > 0) {
          messageCache[agentId] = msgs.map(m => ({
            role: m.role || 'system',
            text: m.content || '',
            images: m.images || [],
          }));
          restoreChatMessages(messageCache[agentId]);
        }
      } catch (e) { console.error("loadConversationMessages failed:", e); }
    }

    const MAX_CACHED_MESSAGES = 200;

    function localStorageKey(agentId) {
      return `chat_cache_${userId}_${agentId}`;
    }

    function saveCacheToStorage(agentId) {
      if (!messageCache[agentId]) return;
      let msgs = messageCache[agentId];
      if (msgs.length > MAX_CACHED_MESSAGES) {
        msgs = msgs.slice(msgs.length - MAX_CACHED_MESSAGES);
        messageCache[agentId] = msgs;
      }
      try {
        localStorage.setItem(localStorageKey(agentId), JSON.stringify(msgs));
      } catch (e) {
        console.warn("localStorage full, trimming cache for", agentId);
        // Keep only the newer half of current agent's messages (min 20)
        const trimmed = msgs.slice(msgs.length - Math.max(Math.floor(msgs.length / 2), 20));
        messageCache[agentId] = trimmed;
        try { localStorage.setItem(localStorageKey(agentId), JSON.stringify(trimmed)); } catch (_) {}
      }
    }

    function loadCacheFromStorage(agentId) {
      try {
        const raw = localStorage.getItem(localStorageKey(agentId));
        if (!raw) return null;
        const msgs = JSON.parse(raw);
        if (!Array.isArray(msgs) || msgs.length === 0) return null;
        messageCache[agentId] = msgs;
        return msgs;
      } catch (_) { return null; }
    }

    function syncMessageToCache(role, text, images) {
      if (_restoring) return;
      if (!currentAgentId) return;
      if (!messageCache[currentAgentId]) {
        messageCache[currentAgentId] = [];
      }
      messageCache[currentAgentId].push({ role, text, images: images || [] });
      saveCacheToStorage(currentAgentId);
    }

    // ═══════════════════════════════════════════════════════════════
    // Messages
    // ═══════════════════════════════════════════════════════════════
    function buildSessionId(agentId) {
      return `web_${userId}_${agentId}`;
    }

    function buildClientMessageId() {
      return `msg-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    async function sendMessage() {
      const text = input.value.trim();
      if (!currentAgentId) {
        addSystemMessage("请先选择一个对话或创建新对话");
        return;
      }
      if (!text) return;

      addMsg("user", text);
      input.value = "";
      sendBtn.disabled = true;

      try {
        const sessionId = buildSessionId(currentAgentId);
        const resp = await fetch("/api/messages", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: userId,
            session_id: sessionId,
            content: text,
            agent_id: currentAgentId,
            client_message_id: buildClientMessageId(),
          }),
        });

        if (!resp.ok) {
          const errText = await resp.text();
          throw new Error(errText || `发送失败：HTTP ${resp.status}`);
        }

        const data = await resp.json();
        if (data.ok === false) throw new Error(data.error || "发送失败");
        if (data.task_id) addSystemMessage(`任务已创建：${data.task_id}`);
        if (typeof data.waiting === "number") queueStatus.textContent = `排队：${data.waiting}`;
      } catch (err) {
        addMsg("agent", `消息发送失败：${err.message || err}`);
      } finally {
        sendBtn.disabled = false;
        input.focus();
      }
    }

    function handleTaskEvent(data) {
      if (!data || typeof data !== "object") return;
      // Only process events for the current agent
      if (data.agent_id && currentAgentId && data.agent_id !== currentAgentId) return;
      switch (data.type) {
        case "task_queued":
          if (typeof data.waiting === "number") queueStatus.textContent = `排队：${data.waiting}`;
          break;
        case "task_started":
          addSystemMessage(`任务开始：${data.task_id || ""}`);
          break;
        case "task_progress":
          if (data.text) addSystemMessage(data.text);
          break;
        case "model_call_started":
          addSystemMessage("正在调用模型...");
          break;
        case "tool_call_started":
          addSystemMessage(data.text || "正在执行工具...");
          break;
        case "tool_call_finished":
          if (data.text) addSystemMessage(data.text);
          break;
        case "assistant_message":
          addMsg("agent", data.text || "", data.images || []);
          if (window.AndroidNotify) AndroidNotify.notifyMsg(data.text || "收到新消息");
          break;
        case "task_waiting_user":
          addMsg("agent", data.text || "需要你补充信息。");
          break;
        case "task_finished":
          if (typeof data.waiting === "number") queueStatus.textContent = `排队：${data.waiting}`;
          loadConversations(); // refresh message count
          break;
        case "task_failed":
          addMsg("agent", `任务失败：${data.error || "未知错误"}`);
          break;
        default:
          if ("waiting" in data) queueStatus.textContent = `排队：${data.waiting}`;
          else if ("text" in data || "images" in data) addMsg("agent", data.text || "", data.images || []);
          break;
      }
    }

    function addMsg(role, text, images) {
      syncMessageToCache(role, text, images);
      const row = document.createElement("div");
      row.className = `msg-row ${role}`;
      const bubble = document.createElement("div");
      bubble.className = `msg ${role}`;
      bubble.textContent = text;
      if (images && images.length > 0) {
        for (const imgUrl of images) {
          const img = document.createElement("img");
          img.className = "msg-img";
          img.src = imgUrl;
          img.loading = "lazy";
          bubble.appendChild(img);
        }
      }
      row.appendChild(bubble);
      chatBox.appendChild(row);
      scrollToBottom();
    }

    function addSystemMessage(text) {
      syncMessageToCache('system', text, []);
      const row = document.createElement("div");
      row.className = "msg-row";
      const bubble = document.createElement("div");
      bubble.className = "msg system";
      bubble.textContent = text;
      row.appendChild(bubble);
      chatBox.appendChild(row);
      scrollToBottom();
    }

    function clearChatBox() {
      chatBox.innerHTML = "";
    }

    function scrollToBottom() {
      chatBox.scrollTop = chatBox.scrollHeight;
    }

    // ═══════════════════════════════════════════════════════════════
    // Right Panel Tabs
    // ═══════════════════════════════════════════════════════════════
    function initPanelTabs() {
      const tabs = document.querySelectorAll(".panel-tab");
      tabs.forEach(tab => {
        tab.onclick = () => {
          document.querySelectorAll(".panel-tab").forEach(t => t.classList.remove("active"));
          document.querySelectorAll(".tab-content").forEach(t => t.classList.remove("active"));
          tab.classList.add("active");
          const target = document.getElementById("tab-" + tab.dataset.tab);
          if (target) target.classList.add("active");
          if (tab.dataset.tab === "user-settings") loadUserProfile();
        };
      });
    }

    let panelVisible = true;
    function toggleRightPanel() {
      panelVisible = !panelVisible;
      rightPanel.classList.toggle("collapsed", !panelVisible);
      togglePanelBtn.textContent = panelVisible ? "面板 ▸" : "面板 ◂";
    }

    // Mobile: sidebar drawer
    function toggleSidebar() {
      const sidebar = document.getElementById("sidebar");
      const isOpen = sidebar.classList.toggle("open");
      overlay.classList.toggle("hidden", !isOpen);
      if (isOpen) {
        // Close right panel if open
        document.getElementById("right-panel").classList.remove("open");
      }
    }

    // Mobile: right panel drawer
    function toggleMobilePanel() {
      const panel = document.getElementById("right-panel");
      const isOpen = panel.classList.toggle("open");
      overlay.classList.toggle("hidden", !isOpen);
      if (isOpen) {
        // Close sidebar if open
        document.getElementById("sidebar").classList.remove("open");
      }
    }

    // Overlay click closes all drawers (mobile only)
    if (overlay) {
      overlay.addEventListener("click", function() {
        document.getElementById("sidebar").classList.remove("open");
        document.getElementById("right-panel").classList.remove("open");
        overlay.classList.add("hidden");
      });
    }

    // ═══════════════════════════════════════════════════════════════
    // User Profile & Channels
    // ═══════════════════════════════════════════════════════════════
    async function loadUserProfile() {
      try {
        const resp = await fetch(`/api/user/profile?user_id=${encodeURIComponent(userId)}`);
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.ok) {
          const chs = data.channels || {};
          const count = Array.isArray(chs) ? chs.length : Object.keys(chs).length;
          document.getElementById("profile-channel-count").textContent = count;
          renderChannels(data.channels || {});
        }
      } catch (e) { console.error("loadUserProfile failed:", e); }
    }

    function renderChannels(channels) {
      // Convert dict to array, preserving channel name as "channel" field
      const arr = [];
      if (channels) {
        if (Array.isArray(channels)) {
          arr.push(...channels);
        } else {
          for (const [chName, chData] of Object.entries(channels)) {
            arr.push({
              channel: chData.channel || chName,
              channel_user_id: chData.channel_user_id || chData.user_id || '',
              priority: (chData.priority != null ? chData.priority : 0),
            });
          }
        }
      }
      const list = document.getElementById("channel-list");
      list.innerHTML = "";
      if (arr.length === 0) {
        list.innerHTML = '<div class="ws-empty">暂无绑定渠道</div>';
        return;
      }
      for (const ch of arr) {
        const div = document.createElement("div");
        div.className = "channel-item";
        div.innerHTML = `
          <div class="channel-info">
            <span class="channel-tag">${escHtml(ch.channel)}</span>
            <span>${escHtml(ch.channel_user_id)}</span>
            <span style="color:#888;font-size:11px;">优先级: ${ch.priority != null ? ch.priority : 0}</span>
          </div>
          <button class="channel-del" onclick="unbindChannel('${escHtml(ch.channel)}')">✕</button>
        `;
        list.appendChild(div);
      }
    }

    async function bindChannel() {
      const channel = document.getElementById("new-channel-type").value;
      const channelUserId = document.getElementById("new-channel-uid").value.trim();
      if (!channelUserId) return;
      try {
        const resp = await fetch(`/api/user/channels?user_id=${encodeURIComponent(userId)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel, channel_user_id: channelUserId, priority: 0 }),
        });
        const data = await resp.json();
        if (data.ok) {
          document.getElementById("new-channel-uid").value = "";
          loadUserProfile();
        } else {
          alert("绑定失败: " + (data.message || data.error || "未知错误"));
        }
      } catch (e) { console.error("bindChannel failed:", e); }
    }

    async function unbindChannel(channel) {
      try {
        const resp = await fetch(`/api/user/channels/${encodeURIComponent(channel)}?user_id=${encodeURIComponent(userId)}`, {
          method: "DELETE",
        });
        if (resp.ok) loadUserProfile();
      } catch (e) { console.error("unbindChannel failed:", e); }
    }

    // ═══════════════════════════════════════════════════════════════
    // Workspace
    // ═══════════════════════════════════════════════════════════════
    let currentWsPath = "/";

    async function loadWorkspaceFiles() {
      const path = document.getElementById("ws-path-input").value.trim() || "/";
      currentWsPath = path;
      try {
        const resp = await fetch(`/api/workspace/files?user_id=${encodeURIComponent(userId)}&path=${encodeURIComponent(path)}`);
        if (!resp.ok) {
          document.getElementById("ws-tree").innerHTML = '<div class="ws-empty">加载失败</div>';
          return;
        }
        const data = await resp.json();
        renderWsTree(data.files || [], path);
      } catch (e) {
        document.getElementById("ws-tree").innerHTML = '<div class="ws-empty">加载失败</div>';
      }
    }

    function renderWsTree(files, currentPath) {
      const tree = document.getElementById("ws-tree");
      tree.innerHTML = "";
      if (files.length === 0) {
        tree.innerHTML = '<div class="ws-empty">空目录</div>';
        return;
      }
      // Parent directory link
      if (currentPath !== "/") {
        const parentPath = currentPath.replace(/\/+$/, "").split("/").slice(0, -1).join("/") || "/";
        const item = document.createElement("div");
        item.className = "ws-tree-item";
        item.innerHTML = '<span class="icon dir">📁</span><span>..</span>';
        item.onclick = () => { document.getElementById("ws-path-input").value = parentPath; loadWorkspaceFiles(); };
        tree.appendChild(item);
      }
      for (const f of files) {
        const item = document.createElement("div");
        item.className = "ws-tree-item";
        const isDir = f.type === "dir";
        const displayName = f.name.split('/').pop() || f.name;
        const isImg = !isDir && /\.(png|jpg|jpeg|gif|webp|svg|bmp|ico)$/i.test(displayName);
        const icon = isDir ? "📁" : (isImg ? "🖼" : "📄");
        item.innerHTML = `<span class="icon ${isDir ? 'dir' : (isImg ? 'img' : 'file')}">${icon}</span><span>${escHtml(displayName)}</span>`;
        if (isDir) {
          item.onclick = () => {
            const newPath = f.path;
            document.getElementById("ws-path-input").value = newPath;
            loadWorkspaceFiles();
          };
        } else {
          item.onclick = () => previewFile(f.path);
        }
        tree.appendChild(item);
      }
    }

    async function previewFile(filePath) {
      const isImg = /\.(png|jpg|jpeg|gif|webp|svg|bmp|ico)$/i.test(filePath);

      if (isImg) {
        // Image preview via raw endpoint
        const url = `/api/workspace/files/raw?user_id=${encodeURIComponent(userId)}&path=${encodeURIComponent(filePath)}`;
        const preview = document.getElementById("ws-preview");
        preview.innerHTML = `
          <div class="preview-toolbar">
            <span class="path-label">${escHtml(filePath)}</span>
          </div>
          <div class="preview-image">
            <div>
              <img src="${url}" alt="${escHtml(fileName)}" onerror="this.parentElement.innerHTML='<span style=color:#c62828>图片加载失败</span>'" />
              <div class="size-hint">点击图片在新标签页打开</div>
            </div>
          </div>
        `;
        preview.querySelector("img").onclick = () => window.open(url, "_blank");
      } else {
        // Text preview with encoding selector
        const encoding = (document.getElementById("preview-encoding") || {}).value || "utf-8";
        const url = `/api/workspace/files/read?user_id=${encodeURIComponent(userId)}&path=${encodeURIComponent(filePath)}&encoding=${encoding}`;
        try {
          const resp = await fetch(url);
          const data = await resp.json();
          const preview = document.getElementById("ws-preview");
          const content = data.ok ? (data.content || "") : ("加载失败: " + (data.error || ""));
          preview.innerHTML = `
            <div class="preview-toolbar">
              <span class="path-label">${escHtml(filePath)}</span>
              <select id="preview-encoding">
                <option value="utf-8" ${encoding === "utf-8" ? "selected" : ""}>UTF-8</option>
                <option value="gbk" ${encoding === "gbk" ? "selected" : ""}>GBK</option>
                <option value="gb2312" ${encoding === "gb2312" ? "selected" : ""}>GB2312</option>
                <option value="latin-1" ${encoding === "latin-1" ? "selected" : ""}>Latin-1</option>
              </select>
            </div>
            <div class="preview-content">${escHtml(content)}</div>
          `;
        } catch (e) {
          document.getElementById("ws-preview").innerHTML = `<div class="preview-toolbar"><span class="path-label">${escHtml(filePath)}</span></div><div class="preview-content" style="color:#c62828;">加载失败</div>`;
        }
      }
    }

    // ═══════════════════════════════════════════════════════════════
    // Logs
    // ═══════════════════════════════════════════════════════════════
    async function loadLogs() {
      const filter = document.getElementById("log-filter").value.trim();
      const maxLines = 200;
      let url = `/api/logs/orchestrator?user_id=${encodeURIComponent(userId)}&lines=${maxLines}`;
      if (currentAgentId) url += `&agent_id=${encodeURIComponent(currentAgentId)}`;

      try {
        const resp = await fetch(url);
        const data = await resp.json();
        const logContent = document.getElementById("log-content");
        if (!data.ok) {
          logContent.innerHTML = `<div class="log-empty">${escHtml(data.error || "加载失败")}</div>`;
          return;
        }
        let logLines = data.lines || [];
        if (filter) logLines = logLines.filter(l => l.includes(filter));
        if (logLines.length === 0) {
          logContent.innerHTML = '<div class="log-empty">无匹配日志</div>';
          return;
        }
        logContent.innerHTML = logLines.map(l => `<div class="log-line">${escHtml(l)}</div>`).join("");
      } catch (e) {
        document.getElementById("log-content").innerHTML = '<div class="log-empty">加载失败</div>';
      }
    }

    function startLogAutoRefresh() {
      if (logAutoRefreshTimer) {
        clearInterval(logAutoRefreshTimer);
        logAutoRefreshTimer = null;
        return;
      }
      loadLogs();
      logAutoRefreshTimer = setInterval(loadLogs, 5000);
    }

    // ═══════════════════════════════════════════════════════════════
    // Logout
    // ═══════════════════════════════════════════════════════════════
    function logout() {
      if (eventSource) { eventSource.close(); eventSource = null; }
      if (logAutoRefreshTimer) { clearInterval(logAutoRefreshTimer); logAutoRefreshTimer = null; }
      localStorage.removeItem("user_id");
      localStorage.removeItem("session_id");
      window.location.href = "/login.html";
    }

    // ═══════════════════════════════════════════════════════════════
    // Utils
    // ═══════════════════════════════════════════════════════════════
    function escHtml(s) {
      if (typeof s !== "string") return "";
      const d = document.createElement("div");
      d.textContent = s;
      return d.innerHTML;
    }

    // Encoding selector change handler
    document.getElementById('panel-body').addEventListener('change', function(e) {
      if (e.target && e.target.id === 'preview-encoding') {
        const preview = document.getElementById('ws-preview');
        const toolbar = preview.querySelector('.preview-toolbar');
        if (toolbar) {
          const pathLabel = toolbar.querySelector('.path-label');
          if (pathLabel) {
            const filePath = pathLabel.textContent;
            previewFile(filePath);
          }
        }
      }
    });

    // Keyboard shortcut
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });

    // Close modal on overlay click
    document.getElementById("new-conv-modal").onclick = function(e) {
      if (e.target === this) closeNewConvModal();
    };
