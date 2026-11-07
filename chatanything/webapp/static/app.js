'use strict';

/* =========================================================
 * ChatAnything 控制台前端
 * 原生 JS：fetch 封装、Hash 路由、五个视图的加载与 CRUD。
 * 所有用户数据均通过 textContent / 文本节点渲染，避免 XSS。
 * ========================================================= */

const VIEWS = ['chat', 'models', 'mcp', 'skills', 'tools'];
const SESSION_STATUS_CLASS = {
  created: 'info',
  running: 'good',
  waiting: 'warn',
  reported: 'info',
  closed: 'neutral',
  failed: 'bad'
};

const state = {
  view: 'chat',
  health: null,
  sessions: [],
  selectedSessionId: null,
  models: [],
  editingModel: null,
  mcpServers: [],
  editingServer: null,
  selectedServer: null,
  entries: [],
  skills: [],
  editingSkill: null,
  tools: [],
  selectedTool: null
};

/* 当前打开的 SSE 连接（同一时间只保留一个对话流）。 */
let activeStream = null;

/* ---------------- 基础工具 ---------------- */

function byId(id) {
  return document.getElementById(id);
}

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function setText(id, text) {
  const node = byId(id);
  if (node) node.textContent = text === undefined || text === null ? '—' : String(text);
}

function encodeId(value) {
  return encodeURIComponent(String(value));
}

function fmtTime(timestamp) {
  if (timestamp === undefined || timestamp === null || timestamp === '') return '—';
  const date = new Date(Number(timestamp) * 1000);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

function statusClass(status) {
  return SESSION_STATUS_CLASS[String(status || '').toLowerCase()] || 'neutral';
}

function parseJsonObject(raw, label) {
  const text = String(raw || '').trim();
  if (!text) return {};
  let value;
  try {
    value = JSON.parse(text);
  } catch (err) {
    throw new Error(label + ' JSON 解析失败：' + (err && err.message ? err.message : err));
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(label + ' 必须是 JSON 对象');
  }
  return value;
}

function parseJsonArray(raw, label) {
  const text = String(raw || '').trim();
  if (!text) return [];
  let value;
  try {
    value = JSON.parse(text);
  } catch (err) {
    throw new Error(label + ' JSON 解析失败：' + (err && err.message ? err.message : err));
  }
  if (!Array.isArray(value)) {
    throw new Error(label + ' 必须是 JSON 数组');
  }
  return value;
}

/* ---------------- fetch 封装 ---------------- */

async function api(path, options = {}) {
  const method = options.method || 'GET';
  const init = {
    method,
    cache: 'no-store',
    headers: { Accept: 'application/json' }
  };
  if (options.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }

  let response;
  try {
    response = await fetch(path, init);
  } catch (err) {
    throw new Error('网络请求失败：' + (err && err.message ? err.message : err));
  }

  const raw = await response.text();
  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch (err) {
      if (response.ok) throw new Error('服务端响应不是有效 JSON');
    }
  }

  if (!response.ok) {
    const detail = data && typeof data === 'object' && data.error ? data.error : 'HTTP ' + response.status;
    throw new Error(detail);
  }
  return data;
}

/* ---------------- 提示 / 状态 ---------------- */

function toast(message, type = 'info') {
  const stack = byId('toastStack');
  if (!stack) return;
  const node = make('div', 'toast ' + type, message);
  node.setAttribute('role', 'status');
  stack.appendChild(node);
  while (stack.children.length > 4) {
    stack.removeChild(stack.firstChild);
  }
  setTimeout(() => {
    node.classList.add('leaving');
    setTimeout(() => node.remove(), 220);
  }, 3200);
}

function run(handler) {
  Promise.resolve()
    .then(handler)
    .catch((err) => toast(err && err.message ? err.message : String(err), 'error'));
}

function setBusy(button, busy, label) {
  if (!button) return;
  if (busy) {
    if (!button.dataset.originalLabel) button.dataset.originalLabel = button.textContent;
    button.textContent = label || '处理中…';
    button.disabled = true;
  } else {
    if (button.dataset.originalLabel) {
      button.textContent = button.dataset.originalLabel;
      delete button.dataset.originalLabel;
    }
    button.disabled = false;
  }
}

/* ---------------- 健康状态 ---------------- */

async function loadHealth() {
  const badge = byId('statusBadge');
  const dot = byId('healthDot');
  try {
    const health = await api('/api/health');
    state.health = health;
    const store = health.store || {};
    const caps = Array.isArray(health.capabilities)
      ? health.capabilities.length
      : Object.keys(health.capabilities || {}).length;
    const plugins = Array.isArray(health.plugins) ? health.plugins.length : 0;
    dot.className = 'status-dot online';
    badge.classList.remove('offline');
    setText('healthState', health.state || 'unknown');
    setText('healthMeta', '会话 ' + (store.sessions ?? 0) + ' · 触发器 ' + (store.triggers ?? 0));
    badge.title = '能力 ' + caps + ' · 插件 ' + plugins + ' · 线程 ' + (store.threads ?? 0) + ' · 工具 ' + (Array.isArray(health.tools) ? health.tools.length : 0);
  } catch (err) {
    dot.className = 'status-dot offline';
    badge.classList.add('offline');
    setText('healthState', '离线');
    setText('healthMeta', err.message);
    badge.title = '后端健康检查失败';
  }
}

/* ---------------- Hash 路由 ---------------- */

function route() {
  let view = (location.hash || '#chat').slice(1);
  if (!VIEWS.includes(view)) {
    view = 'chat';
    history.replaceState(null, '', '#chat');
  }
  state.view = view;

  VIEWS.forEach((name) => {
    const section = byId('view-' + name);
    if (section) section.hidden = name !== view;
  });

  document.querySelectorAll('.nav-link').forEach((link) => {
    const active = link.getAttribute('data-view') === view;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });

  const loaders = {
    chat: loadSessions,
    models: loadModels,
    mcp: loadMcpServers,
    skills: loadSkills,
    tools: loadTools
  };
  const loader = loaders[view];
  if (loader) run(loader);
}

/* ---------------- 聊天与会话 ---------------- */

function fmtChatTime(timestamp) {
  if (timestamp === undefined || timestamp === null || timestamp === '') return '';
  const date = new Date(Number(timestamp) * 1000);
  if (Number.isNaN(date.getTime())) return '';
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  return date.toLocaleDateString();
}

function truncateText(text, limit) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  return value.length > limit ? value.slice(0, limit - 1) + '…' : value;
}

function avatarLabel(role) {
  if (role === 'user') return '你';
  if (role === 'assistant') return 'CA';
  if (role === 'tool') return 'T';
  return 'S';
}

async function loadSessions() {
  try {
    const data = await api('/api/sessions?limit=30&kind=worker');
    state.sessions = Array.isArray(data) ? data : (data && data.sessions) || [];
    renderSessions();
  } catch (err) {
    const box = byId('sessionList');
    box.replaceChildren(make('p', 'empty', '加载失败：' + err.message));
    toast('加载会话列表失败：' + err.message, 'error');
  }
}

function renderSessions() {
  const box = byId('sessionList');
  box.replaceChildren();
  if (!state.sessions.length) {
    box.appendChild(make('p', 'empty', '还没有对话，发送一条消息开始吧。'));
    return;
  }
  state.sessions.forEach((item) => {
    const button = make('button', 'session-item');
    button.type = 'button';
    if (item.id === state.selectedSessionId) button.classList.add('active');

    const top = make('div', 'session-item-top');
    top.appendChild(make('strong', 'session-title', item.title || item.id || '未命名对话'));
    top.appendChild(make('span', 'session-dot ' + statusClass(item.status)));

    button.appendChild(top);
    button.appendChild(
      make(
        'div',
        'session-time',
        (item.message_count ?? 0) + ' 条消息 · ' + (fmtChatTime(item.last_activity || item.updated_at) || '—')
      )
    );
    button.title = (item.kind || 'worker') + ' · ' + (item.status || 'unknown') + ' · ' + item.id;
    button.addEventListener('click', () => run(() => loadSession(item.id)));
    box.appendChild(button);
  });
}

async function loadSession(id) {
  closeChatStream();
  try {
    const data = await api('/api/sessions/' + encodeId(id));
    state.selectedSessionId = id;
    renderSessionDetail(data);
    renderSessions();
  } catch (err) {
    toast('加载会话详情失败：' + err.message, 'error');
  }
}

function renderSessionDetail(data) {
  const messages = Array.isArray(data.messages) ? data.messages : [];
  setText('chatTitle', data.title || data.id || '对话');
  setText(
    'chatSubtitle',
    (data.kind || 'session') + ' · ' + messages.length + ' 条消息 · 更新 ' +
    (fmtChatTime(data.last_activity || data.updated_at) || '—')
  );

  const thread = byId('chatThread');
  thread.replaceChildren();
  if (!messages.length) {
    thread.appendChild(make('p', 'empty', '该会话还没有消息'));
  } else {
    messages.forEach((message) => thread.appendChild(renderMessage(message)));
  }
  scrollThread();

  const status = byId('chatStreamStatus');
  if (status) {
    status.textContent = '历史记录';
    status.classList.remove('streaming');
  }
  const meta = byId('chatMeta');
  if (meta) {
    meta.hidden = true;
    meta.replaceChildren();
  }
}

function renderMessage(message) {
  const role = String(message.role || 'unknown');
  const row = make('div', 'chat-msg role-' + role);
  row.appendChild(make('div', 'chat-avatar role-' + role, avatarLabel(role)));

  const body = make('div', 'chat-body');
  const head = make('div', 'chat-msg-head');
  head.appendChild(make('span', 'chat-role', role === 'user' ? '你' : (message.name || role)));
  const when = fmtChatTime(message.created_at);
  if (when) head.appendChild(make('span', '', when));
  body.appendChild(head);

  let content = message.content === undefined || message.content === null ? '' : String(message.content);
  if (!content && Array.isArray(message.tool_calls) && message.tool_calls.length) {
    content = '[tool_calls]\n' + JSON.stringify(message.tool_calls, null, 2);
  }
  body.appendChild(make('div', 'chat-bubble', content || '（空消息）'));
  if (message.tool_call_id) {
    body.appendChild(make('div', 'chat-note', 'tool_call_id: ' + message.tool_call_id));
  }

  row.appendChild(body);
  return row;
}

async function sendChat(event) {
  if (event) event.preventDefault();
  const textarea = byId('chatText');
  const text = textarea.value.trim();
  if (!text) {
    toast('请输入消息内容', 'warn');
    textarea.focus();
    return;
  }

  const button = byId('chatSend');
  setBusy(button, true, '发送中…');
  try {
    const body = { text };
    const threadId = byId('chatThreadId').value.trim();
    if (threadId) body.thread_id = threadId;

    const result = await api('/api/chat', { method: 'POST', body });
    renderChatMeta(result);
    if (result.accepted) {
      textarea.value = '';
      autoGrowChatInput();
      if (!state.selectedSessionId) {
        setText('chatTitle', truncateText(text, 30));
      }
      setText('chatSubtitle', '正在由 Worker 处理…');
      appendChatMessage('user', text);
      openChatStream(result.trigger_id);
      toast('消息已受理，正在流式输出…', 'success');
    } else {
      toast('消息未受理：' + (result.reason || '未知原因'), 'warn');
    }
    await loadSessions();
  } catch (err) {
    toast('发送失败：' + err.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

function renderChatMeta(result) {
  const meta = byId('chatMeta');
  if (!meta) return;
  meta.hidden = false;
  meta.replaceChildren();
  meta.appendChild(
    make('span', 'badge ' + (result.accepted ? 'good' : 'bad'), result.accepted ? '已受理' : '未受理')
  );
  if (result.reason) meta.appendChild(make('span', '', result.reason));
  if (result.trigger_id) meta.appendChild(make('span', '', 'trigger：' + result.trigger_id));
  if (result.thread_id) meta.appendChild(make('span', '', 'thread：' + result.thread_id));
}

/* ---------------- 聊天线程与新建对话 ---------------- */

function hideWelcome() {
  const welcome = byId('chatWelcome');
  if (welcome) welcome.remove();
}

function scrollThread() {
  const thread = byId('chatThread');
  if (thread) thread.scrollTop = thread.scrollHeight;
}

function appendChatMessage(role, text) {
  hideWelcome();
  const thread = byId('chatThread');
  const row = make('div', 'chat-msg role-' + role);
  row.appendChild(make('div', 'chat-avatar role-' + role, avatarLabel(role)));

  const body = make('div', 'chat-body');
  const head = make('div', 'chat-msg-head');
  head.appendChild(make('span', 'chat-role', role === 'user' ? '你' : 'assistant'));
  body.appendChild(head);

  const content = make('div', 'chat-bubble', text || '');
  body.appendChild(content);
  row.appendChild(body);
  thread.appendChild(row);
  scrollThread();
  return { item: row, content };
}

function autoGrowChatInput() {
  const input = byId('chatText');
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
}

function resetThread() {
  const thread = byId('chatThread');
  thread.replaceChildren();

  const welcome = make('div', 'chat-welcome');
  welcome.id = 'chatWelcome';
  welcome.appendChild(make('div', 'chat-welcome-mark', 'CA'));
  welcome.appendChild(make('h3', '', '今天想让 Agent 做什么？'));
  welcome.appendChild(make('p', 'muted', '消息会经过 Router 路由，工具调用与上下文都会持久化到 SQLite。'));

  const suggestions = make('div', 'chat-suggestions');
  [
    '请介绍一下这个服务的架构？',
    '帮我检查当前工作区文件结构',
    '实现一个 SSE 流式接口'
  ].forEach((text) => {
    const chip = make('button', 'chat-suggestion', text);
    chip.type = 'button';
    suggestions.appendChild(chip);
  });
  welcome.appendChild(suggestions);
  thread.appendChild(welcome);
}

function newChat() {
  closeChatStream();
  state.selectedSessionId = null;
  renderSessions();
  resetThread();
  setText('chatTitle', '新对话');
  setText('chatSubtitle', '消息先经过 Router，再交由 Worker 处理。');

  const status = byId('chatStreamStatus');
  if (status) {
    status.textContent = '就绪';
    status.classList.remove('streaming');
  }
  const meta = byId('chatMeta');
  if (meta) {
    meta.hidden = true;
    meta.replaceChildren();
  }
  const input = byId('chatText');
  if (input) input.focus();
}

/* ---------------- SSE 逐字输出 ---------------- */

function closeChatStream() {
  if (activeStream && activeStream.source) {
    try { activeStream.source.close(); } catch (err) { /* ignore */ }
  }
  activeStream = null;
}

function appendStreamTool(label, text) {
  const thread = byId('chatThread');
  const row = make('div', 'chat-note-row');
  row.appendChild(make('span', 'chat-note-icon', label));
  row.appendChild(make('span', 'chat-note', text));
  thread.appendChild(row);
  scrollThread();
}

function appendStreamNote(text) {
  const thread = byId('chatThread');
  const row = make('div', 'chat-note-row');
  row.appendChild(make('span', 'chat-note-icon', '·'));
  row.appendChild(make('span', 'chat-note', text));
  thread.appendChild(row);
  scrollThread();
}

function openChatStream(triggerId) {
  if (!triggerId) return;
  closeChatStream();

  const status = byId('chatStreamStatus');
  if (status) {
    status.textContent = '正在生成…';
    status.classList.add('streaming');
  }
  const bubble = appendChatMessage('assistant', '');
  bubble.item.classList.add('streaming');

  const source = new EventSource('/api/chat/stream?trigger_id=' + encodeId(triggerId));
  activeStream = { source };
  let settled = false;

  const finish = (label) => {
    settled = true;
    bubble.item.classList.remove('streaming');
    if (status) {
      status.textContent = label;
      status.classList.remove('streaming');
    }
    try { source.close(); } catch (err) { /* ignore */ }
    if (activeStream && activeStream.source === source) activeStream = null;
    setText('chatSubtitle', '等待下一条消息');
  };

  source.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    const type = data && data.type;

    if (type === 'delta') {
      bubble.content.textContent += data.delta || '';
      scrollThread();
    } else if (type === 'reset') {
      bubble.content.textContent = '';
    } else if (type === 'tool_call') {
      let args = '';
      try { args = JSON.stringify(data.arguments || {}); } catch (err) { args = ''; }
      appendStreamTool('→ 调用', (data.name || 'tool') + ' ' + args);
    } else if (type === 'tool_result') {
      const content = String(data.content || '').slice(0, 400);
      appendStreamTool(data.is_error ? '✕ 失败' : '← 结果', (data.name || 'tool') + ': ' + content);
    } else if (type === 'start') {
      if (status) status.textContent = '正在生成…';
    } else if (type === 'done') {
      if (data.content) {
        bubble.content.textContent = data.content;
      } else {
        bubble.item.remove();
      }
      if (data.reason) appendStreamNote('Router：' + data.reason);
      finish('完成');
      loadSessions();
    } else if (type === 'error') {
      if (data.content) bubble.content.textContent = data.content;
      appendStreamNote('错误：' + (data.message || '未知错误'));
      finish('失败');
      loadSessions();
    }
  };

  source.onerror = () => {
    if (settled) return;
    finish('连接中断');
    loadSessions();
  };
}

/* ---------------- 模型 ---------------- */

async function loadModels() {
  try {
    const data = await api('/api/models');
    state.models = Array.isArray(data) ? data : (data && data.models) || [];
    if (state.editingModel && !state.models.some((item) => item.id === state.editingModel)) {
      resetModelForm();
    }
    renderModels();
  } catch (err) {
    state.models = [];
    renderModels();
    toast('加载模型列表失败：' + err.message, 'error');
  }
}

function renderModels() {
  const tbody = byId('modelTableBody');
  tbody.replaceChildren();
  if (!state.models.length) {
    tbody.appendChild(emptyRow(6, '暂无数据'));
    return;
  }

  state.models.forEach((item) => {
    const tr = document.createElement('tr');
    tr.appendChild(make('td', null, item.name || '—'));
    tr.appendChild(make('td', null, item.provider || '—'));
    tr.appendChild(make('td', 'cell-payload', item.model || '—'));

    const enabledCell = document.createElement('td');
    enabledCell.appendChild(make('span', 'badge ' + (item.enabled === false ? 'neutral' : 'good'), item.enabled === false ? '停用' : '启用'));
    tr.appendChild(enabledCell);

    const defaultCell = document.createElement('td');
    defaultCell.appendChild(make('span', 'badge ' + (item.is_default ? 'info' : 'neutral'), item.is_default ? '默认' : '—'));
    tr.appendChild(defaultCell);

    const actionCell = document.createElement('td');
    const actions = make('div', 'row-actions');
    actions.appendChild(actionButton('编辑', 'ghost', () => editModel(item)));
    actions.appendChild(actionButton('删除', 'danger', () => deleteModel(item)));
    actions.appendChild(actionButton(item.is_default ? '已默认' : '设为默认', 'accent', () => setDefaultModel(item), item.is_default));
    actionCell.appendChild(actions);
    tr.appendChild(actionCell);
    tbody.appendChild(tr);
  });
}

function editModel(item) {
  state.editingModel = item.id;
  byId('modelId').value = item.id || '';
  setText('modelFormTitle', '编辑模型 · ' + (item.name || item.id));
  byId('modelName').value = item.name || '';
  byId('modelProvider').value = item.provider || '';
  byId('modelModel').value = item.model || '';
  byId('modelBaseUrl').value = item.base_url || '';
  byId('modelApiKey').value = item.api_key || '';
  byId('modelParams').value = JSON.stringify(item.params || {}, null, 2);
  byId('modelEnabled').checked = item.enabled !== false;
  byId('modelDefault').checked = !!item.is_default;
  toast('已载入模型配置', 'info');
}

function resetModelForm() {
  state.editingModel = null;
  byId('modelId').value = '';
  setText('modelFormTitle', '新建模型');
  byId('modelName').value = '';
  byId('modelProvider').value = 'openai';
  byId('modelModel').value = '';
  byId('modelBaseUrl').value = '';
  byId('modelApiKey').value = '';
  byId('modelParams').value = '{}';
  byId('modelEnabled').checked = true;
  byId('modelDefault').checked = false;
}

function modelPayload() {
  const name = byId('modelName').value.trim();
  const model = byId('modelModel').value.trim();
  if (!name) throw new Error('请填写模型名称');
  if (!model) throw new Error('请填写 Model');
  return {
    name,
    provider: byId('modelProvider').value.trim() || 'openai',
    model,
    base_url: byId('modelBaseUrl').value.trim(),
    api_key: byId('modelApiKey').value.trim(),
    params: parseJsonObject(byId('modelParams').value, 'params'),
    enabled: byId('modelEnabled').checked,
    is_default: byId('modelDefault').checked
  };
}

async function saveModel(event) {
  if (event) event.preventDefault();
  let payload;
  try {
    payload = modelPayload();
  } catch (err) {
    toast(err.message, 'error');
    return;
  }

  const creating = !state.editingModel;
  const button = byId('modelSave');
  setBusy(button, true, '保存中…');
  try {
    if (creating) {
      await api('/api/models', { method: 'POST', body: payload });
      resetModelForm();
    } else {
      await api('/api/models/' + encodeId(state.editingModel), { method: 'PUT', body: payload });
    }
    toast('模型已保存', 'success');
    await loadModels();
  } catch (err) {
    toast('保存模型失败：' + err.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

async function deleteModel(item) {
  if (!window.confirm('确定删除模型“' + (item.name || item.id) + '”？')) return;
  try {
    await api('/api/models/' + encodeId(item.id), { method: 'DELETE' });
    if (state.editingModel === item.id) resetModelForm();
    toast('模型已删除', 'success');
    await loadModels();
  } catch (err) {
    toast('删除模型失败：' + err.message, 'error');
  }
}

async function setDefaultModel(item) {
  try {
    await api('/api/models/' + encodeId(item.id) + '/default', { method: 'POST' });
    toast('已设为默认模型', 'success');
    await loadModels();
  } catch (err) {
    toast('设为默认失败：' + err.message, 'error');
  }
}

/* ---------------- MCP ---------------- */

async function loadMcpServers() {
  try {
    const data = await api('/api/mcp/servers');
    state.mcpServers = Array.isArray(data) ? data : (data && data.servers) || [];
    if (state.selectedServer && !state.mcpServers.some((item) => item.id === state.selectedServer)) {
      clearServerSelection();
    } else {
      updateMcpSelectionLabel();
    }
    renderMcpServers();
  } catch (err) {
    state.mcpServers = [];
    renderMcpServers();
    toast('加载 MCP Server 失败：' + err.message, 'error');
  }
}

function renderMcpServers() {
  const tbody = byId('mcpTableBody');
  tbody.replaceChildren();
  if (!state.mcpServers.length) {
    tbody.appendChild(emptyRow(6, '暂无数据'));
    return;
  }

  state.mcpServers.forEach((item) => {
    const tr = document.createElement('tr');
    tr.appendChild(make('td', null, item.name || '—'));
    tr.appendChild(make('td', null, item.transport || '—'));
    tr.appendChild(make('td', 'cell-payload', item.command || '—'));
    tr.appendChild(make('td', 'cell-payload', item.url || '—'));

    const enabledCell = document.createElement('td');
    enabledCell.appendChild(make('span', 'badge ' + (item.enabled === false ? 'neutral' : 'good'), item.enabled === false ? '停用' : '启用'));
    tr.appendChild(enabledCell);

    const actionCell = document.createElement('td');
    const actions = make('div', 'row-actions');
    actions.appendChild(actionButton('编辑', 'ghost', () => editServer(item)));
    actions.appendChild(actionButton('条目', 'accent', () => selectServer(item)));
    actions.appendChild(actionButton('删除', 'danger', () => deleteServer(item)));
    actionCell.appendChild(actions);
    tr.appendChild(actionCell);
    tbody.appendChild(tr);
  });
}

function editServer(item) {
  state.editingServer = item.id;
  byId('mcpId').value = item.id || '';
  setText('mcpFormTitle', '编辑 Server · ' + (item.name || item.id));
  byId('mcpName').value = item.name || '';
  byId('mcpTransport').value = item.transport || 'stdio';
  byId('mcpCommand').value = item.command || '';
  byId('mcpUrl').value = item.url || '';
  byId('mcpArgs').value = JSON.stringify(item.args || [], null, 2);
  byId('mcpEnv').value = JSON.stringify(item.env || {}, null, 2);
  byId('mcpEnabled').checked = item.enabled !== false;
  toast('已载入 Server 配置', 'info');
}

function resetMcpForm() {
  state.editingServer = null;
  byId('mcpId').value = '';
  setText('mcpFormTitle', '新建 Server');
  byId('mcpName').value = '';
  byId('mcpTransport').value = 'stdio';
  byId('mcpCommand').value = '';
  byId('mcpUrl').value = '';
  byId('mcpArgs').value = '[]';
  byId('mcpEnv').value = '{}';
  byId('mcpEnabled').checked = true;
}

function serverPayload() {
  const name = byId('mcpName').value.trim();
  if (!name) throw new Error('请填写 Server 名称');
  return {
    name,
    transport: byId('mcpTransport').value,
    command: byId('mcpCommand').value.trim(),
    url: byId('mcpUrl').value.trim(),
    args: parseJsonArray(byId('mcpArgs').value, 'args'),
    env: parseJsonObject(byId('mcpEnv').value, 'env'),
    enabled: byId('mcpEnabled').checked
  };
}

async function saveServer(event) {
  if (event) event.preventDefault();
  let payload;
  try {
    payload = serverPayload();
  } catch (err) {
    toast(err.message, 'error');
    return;
  }

  const creating = !state.editingServer;
  const button = byId('mcpSave');
  setBusy(button, true, '保存中…');
  try {
    let saved;
    if (creating) {
      saved = await api('/api/mcp/servers', { method: 'POST', body: payload });
      resetMcpForm();
    } else {
      saved = await api('/api/mcp/servers/' + encodeId(state.editingServer), { method: 'PUT', body: payload });
    }
    toast('Server 已保存', 'success');
    state.selectedServer = saved && saved.id ? saved.id : (creating ? null : state.selectedServer);
    state.entries = [];
    updateMcpSelectionLabel();
    await loadMcpServers();
    if (state.selectedServer) await loadEntries();
  } catch (err) {
    toast('保存 Server 失败：' + err.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

async function deleteServer(item) {
  if (!window.confirm('确定删除 Server“' + (item.name || item.id) + '”及其全部条目？')) return;
  try {
    await api('/api/mcp/servers/' + encodeId(item.id), { method: 'DELETE' });
    if (state.selectedServer === item.id) clearServerSelection();
    if (state.editingServer === item.id) resetMcpForm();
    toast('Server 已删除', 'success');
    await loadMcpServers();
  } catch (err) {
    toast('删除 Server 失败：' + err.message, 'error');
  }
}

function selectServer(item) {
  state.selectedServer = item.id;
  state.entries = [];
  updateMcpSelectionLabel();
  resetEntryForm();
  run(loadEntries);
}

function clearServerSelection() {
  state.selectedServer = null;
  state.entries = [];
  updateMcpSelectionLabel();
  renderEntries();
}

function updateMcpSelectionLabel() {
  const label = byId('mcpSelectedServer');
  const hint = byId('mcpEntriesEmpty');
  const formWrap = byId('mcpEntryFormWrap');
  if (!state.selectedServer) {
    label.textContent = '未选择 Server';
    hint.hidden = false;
    formWrap.hidden = true;
    return;
  }
  const server = state.mcpServers.find((item) => item.id === state.selectedServer);
  label.textContent = server ? server.name + '（' + server.id + '）' : state.selectedServer;
  hint.hidden = true;
  formWrap.hidden = false;
}

function normalizeEntries(data) {
  const out = [];
  const push = (kind, item) => {
    if (!item || typeof item !== 'object') return;
    out.push({
      kind: item.kind || kind || 'tool',
      name: item.name || item.id || '',
      payload: item.payload || {},
      updated_at: item.updated_at
    });
  };

  if (Array.isArray(data)) {
    data.forEach((item) => push(item && item.kind, item));
    return out;
  }
  if (!data || typeof data !== 'object') return out;
  if (Array.isArray(data.entries)) {
    data.entries.forEach((item) => push(item && item.kind, item));
    return out;
  }
  ['tool', 'resource', 'prompt'].forEach((kind) => {
    [kind, kind + 's'].forEach((key) => {
      if (Array.isArray(data[key])) data[key].forEach((item) => push(kind, item));
    });
  });
  return out;
}

async function loadEntries() {
  if (!state.selectedServer) {
    state.entries = [];
    renderEntries();
    return;
  }
  try {
    const data = await api('/api/mcp/servers/' + encodeId(state.selectedServer) + '/entries');
    state.entries = normalizeEntries(data);
    renderEntries();
  } catch (err) {
    state.entries = [];
    renderEntries();
    toast('加载 MCP 条目失败：' + err.message, 'error');
  }
}

function renderEntries() {
  const tbody = byId('mcpEntriesBody');
  tbody.replaceChildren();
  if (!state.selectedServer) {
    tbody.appendChild(emptyRow(5, '请先选择 Server'));
    return;
  }
  if (!state.entries.length) {
    tbody.appendChild(emptyRow(5, '暂无数据'));
    return;
  }

  state.entries.forEach((entry) => {
    const tr = document.createElement('tr');
    tr.appendChild(make('td', null, entry.kind || '—'));
    tr.appendChild(make('td', 'cell-payload', entry.name || '—'));

    let payloadText = '—';
    try {
      payloadText = JSON.stringify(entry.payload || {}, null, 0);
    } catch (err) {
      payloadText = String(entry.payload);
    }
    tr.appendChild(make('td', 'cell-payload', payloadText));
    tr.appendChild(make('td', 'muted small', fmtTime(entry.updated_at)));

    const actionCell = document.createElement('td');
    const actions = make('div', 'row-actions');
    actions.appendChild(actionButton('载入', 'ghost', () => fillEntryForm(entry)));
    actions.appendChild(actionButton('删除', 'danger', () => deleteEntry(entry), !entry.name));
    actionCell.appendChild(actions);
    tr.appendChild(actionCell);
    tbody.appendChild(tr);
  });
}

function fillEntryForm(entry) {
  byId('entryKind').value = entry.kind || 'tool';
  byId('entryName').value = entry.name || '';
  byId('entryPayload').value = JSON.stringify(entry.payload || {}, null, 2);
}

function resetEntryForm() {
  byId('entryKind').value = 'tool';
  byId('entryName').value = '';
  byId('entryPayload').value = '{}';
}

async function saveEntry(event) {
  if (event) event.preventDefault();
  if (!state.selectedServer) {
    toast('请先选择 Server', 'warn');
    return;
  }
  const kind = byId('entryKind').value;
  const name = byId('entryName').value.trim();
  if (!name) {
    toast('请填写条目名称', 'warn');
    return;
  }

  let payload;
  try {
    payload = parseJsonObject(byId('entryPayload').value, 'payload');
  } catch (err) {
    toast(err.message, 'error');
    return;
  }

  const button = byId('entrySave');
  setBusy(button, true, '保存中…');
  try {
    await api('/api/mcp/servers/' + encodeId(state.selectedServer) + '/entries', {
      method: 'PUT',
      body: { kind, name, payload }
    });
    toast('条目已保存', 'success');
    resetEntryForm();
    await loadEntries();
  } catch (err) {
    toast('保存条目失败：' + err.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

async function deleteEntry(entry) {
  if (!state.selectedServer) return;
  if (!window.confirm('确定删除条目“' + entry.kind + '/' + entry.name + '”？')) return;
  try {
    await api(
      '/api/mcp/servers/' + encodeId(state.selectedServer) +
      '/entries/' + encodeId(entry.kind) + '/' + encodeId(entry.name),
      { method: 'DELETE' }
    );
    toast('条目已删除', 'success');
    await loadEntries();
  } catch (err) {
    toast('删除条目失败：' + err.message, 'error');
  }
}

/* ---------------- 技能 ---------------- */

async function loadSkills() {
  try {
    const data = await api('/api/skills');
    state.skills = Array.isArray(data) ? data : (data && data.skills) || [];
    if (state.editingSkill && !state.skills.some((item) => item.id === state.editingSkill)) {
      resetSkillForm();
    }
    renderSkills();
  } catch (err) {
    state.skills = [];
    renderSkills();
    toast('加载 Skill 列表失败：' + err.message, 'error');
  }
}

function renderSkills() {
  const tbody = byId('skillTableBody');
  tbody.replaceChildren();
  if (!state.skills.length) {
    tbody.appendChild(emptyRow(7, '暂无数据'));
    return;
  }

  state.skills.forEach((item) => {
    const tr = document.createElement('tr');
    tr.appendChild(make('td', null, item.name || '—'));
    tr.appendChild(make('td', 'cell-payload', item.description || '—'));
    tr.appendChild(make('td', 'cell-payload', item.path || '—'));
    tr.appendChild(make('td', null, item.version || '—'));

    const enabledCell = document.createElement('td');
    enabledCell.appendChild(make('span', 'badge ' + (item.enabled === false ? 'neutral' : 'good'), item.enabled === false ? '停用' : '启用'));
    tr.appendChild(enabledCell);

    const tags = Array.isArray(item.tags) ? item.tags.join(', ') : '';
    tr.appendChild(make('td', 'muted small', tags || '—'));

    const actionCell = document.createElement('td');
    const actions = make('div', 'row-actions');
    actions.appendChild(actionButton('编辑', 'ghost', () => editSkill(item)));
    actions.appendChild(actionButton('删除', 'danger', () => deleteSkill(item)));
    actionCell.appendChild(actions);
    tr.appendChild(actionCell);
    tbody.appendChild(tr);
  });
}

function editSkill(item) {
  state.editingSkill = item.id;
  byId('skillId').value = item.id || '';
  setText('skillFormTitle', '编辑 Skill · ' + (item.name || item.id));
  byId('skillName').value = item.name || '';
  byId('skillVersion').value = item.version || '';
  byId('skillPath').value = item.path || '';
  byId('skillTags').value = Array.isArray(item.tags) ? item.tags.join(', ') : '';
  byId('skillDescription').value = item.description || '';
  byId('skillMetadata').value = JSON.stringify(item.metadata || {}, null, 2);
  byId('skillEnabled').checked = item.enabled !== false;
  toast('已载入 Skill 配置', 'info');
}

function resetSkillForm() {
  state.editingSkill = null;
  byId('skillId').value = '';
  setText('skillFormTitle', '新建 Skill');
  byId('skillName').value = '';
  byId('skillVersion').value = '';
  byId('skillPath').value = '';
  byId('skillTags').value = '';
  byId('skillDescription').value = '';
  byId('skillMetadata').value = '{}';
  byId('skillEnabled').checked = true;
}

function parseTags(raw) {
  return String(raw || '')
    .split(/[,，]/)
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function skillPayload() {
  const name = byId('skillName').value.trim();
  if (!name) throw new Error('请填写 Skill 名称');
  return {
    name,
    description: byId('skillDescription').value.trim(),
    path: byId('skillPath').value.trim(),
    version: byId('skillVersion').value.trim(),
    enabled: byId('skillEnabled').checked,
    tags: parseTags(byId('skillTags').value),
    metadata: parseJsonObject(byId('skillMetadata').value, 'metadata')
  };
}

async function saveSkill(event) {
  if (event) event.preventDefault();
  let payload;
  try {
    payload = skillPayload();
  } catch (err) {
    toast(err.message, 'error');
    return;
  }

  const creating = !state.editingSkill;
  const button = byId('skillSave');
  setBusy(button, true, '保存中…');
  try {
    if (creating) {
      await api('/api/skills', { method: 'POST', body: payload });
      resetSkillForm();
    } else {
      await api('/api/skills/' + encodeId(state.editingSkill), { method: 'PUT', body: payload });
    }
    toast('Skill 已保存', 'success');
    await loadSkills();
  } catch (err) {
    toast('保存 Skill 失败：' + err.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

async function deleteSkill(item) {
  if (!window.confirm('确定删除 Skill“' + (item.name || item.id) + '”？')) return;
  try {
    await api('/api/skills/' + encodeId(item.id), { method: 'DELETE' });
    if (state.editingSkill === item.id) resetSkillForm();
    toast('Skill 已删除', 'success');
    await loadSkills();
  } catch (err) {
    toast('删除 Skill 失败：' + err.message, 'error');
  }
}

/* ---------------- 工具 ---------------- */

async function loadTools() {
  try {
    const data = await api('/api/tools');
    state.tools = Array.isArray(data) ? data : (data && data.tools) || [];
    renderToolSelect();
  } catch (err) {
    state.tools = [];
    renderToolSelect();
    toast('加载工具列表失败：' + err.message, 'error');
  }
}

function renderToolSelect() {
  const select = byId('toolSelect');
  const invokeButton = byId('toolInvoke');
  select.replaceChildren();

  if (!state.tools.length) {
    const option = make('option', null, '暂无数据');
    option.value = '';
    select.appendChild(option);
    select.disabled = true;
    invokeButton.disabled = true;
    setText('toolDescription', '暂无数据');
    setText('toolParameters', '—');
    state.selectedTool = null;
    return;
  }

  select.disabled = false;
  invokeButton.disabled = false;
  state.tools.forEach((tool) => {
    const option = make('option', null, tool.name || '未命名工具');
    option.value = tool.name || '';
    select.appendChild(option);
  });

  if (!state.selectedTool || !state.tools.some((tool) => tool.name === state.selectedTool)) {
    state.selectedTool = state.tools[0].name;
  }
  select.value = state.selectedTool;
  renderToolDetail(state.tools.find((tool) => tool.name === state.selectedTool));
}

function renderToolDetail(tool) {
  if (!tool) {
    setText('toolDescription', '—');
    setText('toolParameters', '—');
    return;
  }
  setText('toolDescription', tool.description || '（无描述）');
  let parameters = '—';
  try {
    parameters = JSON.stringify(tool.parameters === undefined ? {} : tool.parameters, null, 2);
  } catch (err) {
    parameters = String(tool.parameters);
  }
  setText('toolParameters', parameters);
}

function selectTool() {
  state.selectedTool = byId('toolSelect').value;
  const tool = state.tools.find((item) => item.name === state.selectedTool);
  renderToolDetail(tool);
  byId('toolResult').hidden = true;
}

async function invokeTool() {
  const name = state.selectedTool;
  if (!name) {
    toast('请选择工具', 'warn');
    return;
  }

  let args;
  try {
    args = parseJsonObject(byId('toolArguments').value, 'arguments');
  } catch (err) {
    toast(err.message, 'error');
    return;
  }

  const button = byId('toolInvoke');
  setBusy(button, true, '执行中…');
  try {
    const result = await api('/api/tools/' + encodeId(name) + '/invoke', {
      method: 'POST',
      body: { arguments: args }
    });
    renderToolResult(result);
    toast(result.is_error ? '工具返回错误结果' : '工具执行完成', result.is_error ? 'error' : 'success');
  } catch (err) {
    toast('工具执行失败：' + err.message, 'error');
  } finally {
    setBusy(button, false);
  }
}

function renderToolResult(result) {
  const box = byId('toolResult');
  box.hidden = false;
  const failed = !!result.is_error;

  const status = byId('toolStatus');
  status.textContent = (failed ? '执行失败' : '执行成功') + (result.name ? ' · ' + result.name : '');
  status.className = 'badge ' + (failed ? 'bad' : 'good');

  setText('toolCallId', result.call_id || '—');

  const content = byId('toolContent');
  content.textContent = result.content === undefined || result.content === null ? '（无内容）' : String(result.content);
  content.classList.toggle('error-text', failed);

  const meta = byId('toolMetadata');
  if (result.metadata && typeof result.metadata === 'object' && Object.keys(result.metadata).length) {
    meta.textContent = 'metadata: ' + JSON.stringify(result.metadata);
  } else {
    meta.textContent = '';
  }
}

/* ---------------- 通用渲染辅助 ---------------- */

function emptyRow(colspan, text) {
  const tr = document.createElement('tr');
  const cell = make('td', 'empty', text);
  cell.colSpan = colspan;
  tr.appendChild(cell);
  return tr;
}

function actionButton(label, variant, handler, disabled) {
  const button = make('button', 'btn ' + variant + ' small', label);
  button.type = 'button';
  if (disabled) button.disabled = true;
  else button.addEventListener('click', () => run(handler));
  return button;
}

/* ---------------- 事件绑定 ---------------- */

function on(id, event, handler) {
  const node = byId(id);
  if (!node) return;
  node.addEventListener(event, (eventObj) => run(() => handler(eventObj)));
}

function bindEvents() {
  on('chatForm', 'submit', sendChat);
  on('chatNew', 'click', newChat);
  on('sessionsRefresh', 'click', loadSessions);

  const chatInput = byId('chatText');
  if (chatInput) {
    chatInput.addEventListener('input', autoGrowChatInput);
    chatInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        run(sendChat);
      }
    });
  }

  const chatThread = byId('chatThread');
  if (chatThread) {
    chatThread.addEventListener('click', (event) => {
      const chip = event.target.closest('.chat-suggestion');
      if (!chip) return;
      const input = byId('chatText');
      input.value = chip.textContent;
      autoGrowChatInput();
      run(sendChat);
    });
  }

  on('modelForm', 'submit', saveModel);
  on('modelReset', 'click', resetModelForm);
  on('modelsRefresh', 'click', loadModels);

  on('mcpForm', 'submit', saveServer);
  on('mcpReset', 'click', resetMcpForm);
  on('mcpRefresh', 'click', loadMcpServers);
  on('mcpEntryForm', 'submit', saveEntry);
  on('entryReset', 'click', resetEntryForm);

  on('skillForm', 'submit', saveSkill);
  on('skillReset', 'click', resetSkillForm);
  on('skillsRefresh', 'click', loadSkills);

  on('toolsRefresh', 'click', loadTools);
  on('toolSelect', 'change', selectTool);
  on('toolInvoke', 'click', invokeTool);

  window.addEventListener('hashchange', () => route());
}

/* ---------------- 启动 ---------------- */

bindEvents();
route();
loadHealth();
setInterval(loadHealth, 5000);
