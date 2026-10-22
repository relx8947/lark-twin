// 网页版飞书。只用只读接口看消息, 看过的仍是未读; 聊天内容一律用 textContent 渲染, 不拼 HTML。
// 能发文字、表情(Unicode emoji)、@、图片和文件, 由后端发出(默认普通发送, 直接发到这个会话)。
// 话题群显示话题列表(每个话题的根消息和回复数), 点开一个话题在右侧看它的回复; 位置(position)都是话题位置。
'use strict';

const $ = id => document.getElementById(id);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function put(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false)
    .map(k => (k instanceof Node ? k : document.createTextNode(String(k)))));
  return el;
}

const ICONS = {
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>',
  mute: '<path d="M9 18.5a3 3 0 0 0 6 0M18 15.5V11a6 6 0 0 0-9.4-4.9M6.3 8.6A6 6 0 0 0 6 11v4.5L4.5 17H16M3 3l18 18"/>',
  send: '<path d="M4 11.5L20 4l-6.5 16-2.5-6.5z"/><path d="M11 13.5l9-9.5"/>',
  reply: '<path d="M9.5 7L4.5 12l5 5"/><path d="M5 12h9a5 5 0 0 1 5 5v1"/>',
  eye: '<path d="M2.5 12s3.5-6.5 9.5-6.5S21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><path d="M4 20L20 4"/>',
  back: '<path d="M15 5l-7 7 7 7"/>',
  chat: '<path d="M4.5 4.5h15a1.5 1.5 0 0 1 1.5 1.5v10a1.5 1.5 0 0 1-1.5 1.5H10l-4.5 3.5v-3.5h-1A1.5 1.5 0 0 1 3 16V6a1.5 1.5 0 0 1 1.5-1.5z"/>',
  file: '<path d="M6 3h8l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M14 3v5h5"/>',
  video: '<rect x="3" y="5.5" width="13" height="13" rx="2"/><path d="M16 10l5-3v10l-5-3z"/>',
  emoji: '<circle cx="12" cy="12" r="8.5"/><path d="M8.5 14a4 4 0 0 0 7 0"/><path d="M9 9.5h.01M15 9.5h.01"/>',
  at: '<circle cx="12" cy="12" r="3.5"/><path d="M15.5 12v1.5a2.5 2.5 0 0 0 5 0V12a8.5 8.5 0 1 0-3.3 6.7"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M20.5 16l-5-5-8.5 8.5"/>',
  attach: '<path d="M20 11.5l-7.8 7.8a5 5 0 0 1-7.1-7.1l8.2-8.2a3.3 3.3 0 0 1 4.7 4.7l-8.2 8.2a1.7 1.7 0 0 1-2.4-2.4l7.4-7.4"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
};
function icon(name, cls) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  el.setAttribute('viewBox', '0 0 24 24');
  if (cls) el.setAttribute('class', cls);
  el.innerHTML = ICONS[name];  // 只用上面固定的图标, 不含外部内容
  return el;
}

function toast(msg) {
  const t = $('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 2400);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || 'GET', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Larkx': '1' },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (r.status === 401) { showLogin(); throw new Error('未登录'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const err = new Error(data.error || r.statusText); err.status = r.status; err.data = data; throw err; }
  return data;
}

async function act(fn) {
  try { return await fn(); } catch (e) { if (e.message !== '未登录') toast(e.message); return null; }
}

// ---- 时间, 和飞书客户端的写法一致 ----

const WEEK = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];
const pad = n => String(n).padStart(2, '0');
function dayDiff(d) {
  const a = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const now = new Date(); const b = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((b - a) / 86400000);
}
function feedTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), diff = dayDiff(d);
  if (diff === 0) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (diff === 1) return '昨天';
  if (diff < 7) return WEEK[d.getDay()];
  if (d.getFullYear() === new Date().getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}
function msgTime(ts) {
  const d = new Date(ts * 1000), diff = dayDiff(d), hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (diff === 0) return hm;
  if (diff === 1) return `昨天 ${hm}`;
  if (d.getFullYear() === new Date().getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}

// ---- 头像: 有图用图, 没图按名字生成彩色头像 ----

const AVATAR_COLORS = ['#3370ff', '#7f3bf5', '#00b8a9', '#ff8800', '#f54a45', '#2ea121', '#4954e6', '#e5398a'];
function avatar(url, name, size = 32) {
  const label = (name || '?').replace(/[（(].*$/, '').trim();
  const text = /^[一-龥]/.test(label) ? label.slice(-2) : label.slice(0, 2).toUpperCase();
  let n = 0; for (const ch of label) n = (n * 31 + ch.charCodeAt(0)) >>> 0;
  const color = AVATAR_COLORS[n % AVATAR_COLORS.length];
  const el = h('span', { class: 'avatar', style: `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.38)}px` });
  const fallback = () => { el.style.background = color; put(el, text); };
  if (!url) { fallback(); return el; }
  const img = h('img', { src: url, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer' });
  img.addEventListener('error', fallback);
  put(el, img);
  return el;
}

// ---- 状态 ----

// msgs/start/end/hasMore/readPos/unreadUpTo/fetchedAt/rows 是当前会话的; 离开时连同滚动位置存进 cache,
// 再切回来先立刻显示存下的, 再在后台刷新
const S = { feed: [], cursor: '', filter: 'all', q: '', results: null, latest: null, feedAt: 0, feedDirty: false, feedBusy: false,
  chatId: '', chat: null, msgs: [], start: 0, end: 0, hasMore: false, replyTo: null, seq: 0, poll: 0, me: null,
  readPos: 0, unreadUpTo: 0, lastServer: 0, polling: false, loaded: false, fetchedAt: 0, rows: new Map(),
  cache: new Map(), inflight: new Map(), cards: new Map(), hoverTimer: 0, prefetching: false,
  pending: [], pendingSeq: 0, mentions: [], members: null, sending: false, thread: null, threadSeq: 0 };
const isTopic = () => !!S.chat && S.chat.kind === 'topic';
// 新消息: 每秒先问本地库(daemon 收到推送就入库), 本地没有时最多每 3 秒去服务端拉一次; 页面在后台时放慢
const POLL_MS = 1000, SERVER_MS = 3000, SERVER_HIDDEN_MS = 10000;
// 会话列表: 本地库有新消息时尽快刷新(最多每 3 秒一次), 否则每 8 秒一次, 页面在后台时 30 秒一次
const FEED_MS = 8000, FEED_DIRTY_MS = 3000, FEED_HIDDEN_MS = 30000;
// 记住最近打开过的这么多个会话; 刚取到不到 5 秒的切回来不再刷新; 会话列表前几个和鼠标停留的会话提前取好
const CACHE_MAX = 30, FRESH_MS = 5000, PREFETCH_TOP = 5, PREFETCH_FRESH_MS = 30000, HOVER_MS = 120;
const KIND = { p2p: '单聊', group: '群组', topic: '话题群' };
const FILTERS = [['all', '全部'], ['unread', '未读'], ['p2p', '单聊'], ['group', '群组']];

// ---- 会话列表 ----

function applyFeed(d) {
  const fresh = new Set(d.items.map(i => i.chat_id));
  S.feed = d.items.concat(S.feed.filter(i => !fresh.has(i.chat_id)));
  if (!S.cursor) S.cursor = d.next_cursor;
  renderFeed();
}

async function refreshFeed() {
  if (S.feedBusy) return;
  S.feedBusy = true; S.feedAt = Date.now();
  try {
    applyFeed(await api('/api/im/feed'));
  } finally { S.feedBusy = false; }
  prefetchTop();
}

// 每 2 秒问一次本地库有没有新消息入库, 有就尽快刷新会话列表, 不用等定时刷新; 两种刷新合在这一个定时器里, 不重复请求
async function feedTick() {
  if (!document.hidden) {
    const d = await api('/api/im/changes').catch(() => null);
    if (d) {
      if (S.latest != null && d.latest !== S.latest) S.feedDirty = true;
      S.latest = d.latest;
    }
  }
  const age = Date.now() - S.feedAt;
  if (S.feedBusy || !(age >= (document.hidden ? FEED_HIDDEN_MS : FEED_MS) || (S.feedDirty && age >= FEED_DIRTY_MS))) return;
  S.feedDirty = false;
  refreshFeed().catch(() => {});
}

async function moreFeed() {
  if (!S.cursor) return;
  const d = await api('/api/im/feed?cursor=' + encodeURIComponent(S.cursor));
  const have = new Set(S.feed.map(i => i.chat_id));
  S.feed = S.feed.concat(d.items.filter(i => !have.has(i.chat_id)));
  S.cursor = d.next_cursor;
  renderFeed();
}

function feedCard(i) {
  return h('div', { class: 'feedCard', onclick: () => { location.hash = '#/im/' + i.chat_id; },
    onmouseenter: () => hoverPrefetch(i.chat_id), onmouseleave: () => clearTimeout(S.hoverTimer) },
    avatar(i.avatar, i.name, 32),
    h('div', { class: 'feedCard-main' },
      h('div', { class: 'feedCard-row' }, h('div', { class: 'feedCard-name', title: i.name }, i.name),
        h('div', { class: 'feedCard-time' }, feedTime(i.time))),
      h('div', { class: 'feedCard-row' }, h('div', { class: 'feedCard-preview', title: i.preview }, i.preview || ''),
        i.muted ? icon('mute', 'feedCard-mute') : null,
        i.unread ? h('span', { class: 'badge' + (i.muted ? ' mute' : '') }, i.unread > 99 ? '99+' : i.unread) : null)));
}

function renderFilters() {
  put($('filters'), FILTERS.map(([k, label]) => h('button', { class: S.filter === k ? 'on' : null,
    onclick: () => { S.filter = k; renderFilters(); renderFeed(); } }, label)));
}

function renderFeed() {
  const list = $('feedList');
  const total = S.feed.filter(i => !i.muted).reduce((n, i) => n + i.unread, 0);
  $('unreadAll').hidden = !total; $('unreadAll').textContent = total > 99 ? '99+' : total;
  document.title = (total ? `(${total}) ` : '') + '飞书 · lark-twin';
  if (S.results) {
    put(list, h('div', { class: 'feed-hint' }, '搜索结果',
      h('a', { href: '#', onclick: e => { e.preventDefault(); S.results = null; renderFeed(); } }, '返回')),
    S.results.length ? S.results.map(r => h('div', { class: 'feedCard', onclick: () => openResult(r) },
      avatar('', r.name, 32), h('div', { class: 'feedCard-main' },
        h('div', { class: 'feedCard-row' }, h('div', { class: 'feedCard-name' }, r.name), h('span', { class: 'feed-tag' }, r.kind === 'user' ? '联系人' : '群组')))))
      : h('div', { class: 'feed-hint' }, '没有找到相关结果'));
    return;
  }
  const q = S.q.toLowerCase();
  const rows = S.feed.filter(i => (S.filter === 'all' || (S.filter === 'unread' ? i.unread > 0 : S.filter === 'p2p' ? i.kind === 'p2p' : i.kind !== 'p2p'))
    && (!q || i.name.toLowerCase().includes(q)));
  // 内容没变的会话卡片沿用原来的节点(头像不用重新加载), 只重建变了的
  const cards = new Map();
  for (const i of rows) {
    const sig = JSON.stringify(i), old = S.cards.get(i.chat_id);
    cards.set(i.chat_id, old && old.sig === sig ? old : { sig, node: feedCard(i) });
  }
  S.cards = cards;
  put(list, [...cards.values()].map(c => c.node),
    S.cursor && !q ? h('button', { class: 'feed-more', onclick: () => act(moreFeed) }, '加载更多') : null,
    !rows.length ? h('div', { class: 'feed-hint' }, S.feed.length ? (q ? '回车搜索联系人和群组' : '没有符合条件的会话') : '加载中…') : null);
  markActive();
}

function markActive() {
  for (const [id, c] of S.cards) c.node.classList.toggle('active', id === S.chatId);
}

async function searchAll(q) {
  S.results = (await act(() => api('/api/im/search?q=' + encodeURIComponent(q)))) || [];
  renderFeed();
}

async function openResult(r) {
  if (r.kind !== 'user') { location.hash = '#/im/' + r.id; return; }
  const d = await act(() => api('/api/im/open', { method: 'POST', body: { user_id: r.id } }));
  if (d) location.hash = '#/im/' + d.chat_id;
}

// ---- 聊天窗口 ----

function emptyChat(text) {
  const box = h('div', { class: 'chat-empty' }, icon('chat'), text);
  put($('chat'), box);
}

// 取一个会话最近的消息; 同一个会话已经在取的(预取、上一次打开)就共用那次请求
function fetchChat(chatId) {
  let p = S.inflight.get(chatId);
  if (!p) {
    p = api(`/api/im/chat/${chatId}`).finally(() => S.inflight.delete(chatId));
    S.inflight.set(chatId, p);
  }
  return p;
}

// 已读位置在取到时记下: "以下为新消息"标在那时就未读的第一条前, 之后来的新消息不再挪动它
function entryFrom(d) {
  return { chat: d.chat, msgs: d.messages, start: d.start, end: d.end, hasMore: d.has_more, readPos: d.chat.read_position || 0,
    unreadUpTo: d.end, fetchedAt: Date.now(), rows: new Map(), view: null };
}

function remember(chatId, e) {
  S.cache.delete(chatId); S.cache.set(chatId, e);
  for (const id of S.cache.keys()) {
    if (S.cache.size <= CACHE_MAX) break;
    if (id !== S.chatId) S.cache.delete(id);
  }
}

// 后台刷新到的最近一段并进已有的消息(t 是当前会话 S 或缓存里的一项): 和已有的连得上就替换这一段, 撤回等变化也跟着更新;
// 连不上(离开期间新消息太多)就只留新取到的这段
function mergeFresh(t, d) {
  if (t.msgs.length && d.start >= t.start && d.start <= t.end + 1) {
    t.msgs = t.msgs.filter(m => m.position < d.start).concat(d.messages, t.msgs.filter(m => m.position > d.end));
    t.end = Math.max(t.end, d.end);
  } else {
    Object.assign(t, { msgs: d.messages, start: d.start, end: d.end, hasMore: d.has_more });
  }
  Object.assign(t, { chat: d.chat, readPos: d.chat.read_position || 0, unreadUpTo: Math.max(t.unreadUpTo, d.end), fetchedAt: Date.now() });
}

// 离开当前会话时记下它的消息、已经建好的消息节点和滚动位置
function saveCurrent() {
  if (!S.chatId || !S.loaded) return;
  const box = S.body;
  const view = box ? { bottom: box.scrollHeight - box.scrollTop - box.clientHeight < 80, top: box.scrollTop } : null;
  remember(S.chatId, { chat: S.chat, msgs: S.msgs, start: S.start, end: S.end, hasMore: S.hasMore, readPos: S.readPos,
    unreadUpTo: S.unreadUpTo, fetchedAt: S.fetchedAt, rows: S.rows, view });
}

function useEntry(e) {
  Object.assign(S, { chat: e.chat, msgs: e.msgs, start: e.start, end: e.end, hasMore: e.hasMore, readPos: e.readPos,
    unreadUpTo: e.unreadUpTo, fetchedAt: e.fetchedAt, rows: e.rows || new Map(), loaded: true, lastServer: Date.now(), polling: false });
}

// 预取: 只读接口, 不会标已读。结果放进缓存, 打开时直接显示
async function prefetch(chatId) {
  const e = S.cache.get(chatId);
  if (chatId === S.chatId || S.inflight.has(chatId) || (e && Date.now() - e.fetchedAt < PREFETCH_FRESH_MS)) return;
  const d = await fetchChat(chatId).catch(() => null);
  if (!d || chatId === S.chatId) return;  // 这期间打开了它: 由 openChat 用这次的结果
  const old = S.cache.get(chatId);
  if (old) mergeFresh(old, d); else remember(chatId, entryFrom(d));
}

function hoverPrefetch(chatId) {
  clearTimeout(S.hoverTimer);
  S.hoverTimer = setTimeout(() => prefetch(chatId), HOVER_MS);
}

// 会话列表最前面几个还没取过的会话, 一个接一个地预取
async function prefetchTop() {
  if (S.prefetching || document.hidden) return;
  S.prefetching = true;
  try {
    for (const i of S.feed.slice(0, PREFETCH_TOP)) if (!S.cache.has(i.chat_id)) await prefetch(i.chat_id);
  } finally { S.prefetching = false; }
}

async function openChat(chatId, threadId) {
  saveCurrent();
  clearInterval(S.poll);
  const seq = ++S.seq;
  clearComposer(); closeThread();
  const cached = S.cache.get(chatId), item = S.feed.find(i => i.chat_id === chatId);
  Object.assign(S, { chatId, chat: null, msgs: [], start: 0, end: 0, hasMore: false, replyTo: null, loaded: false, rows: new Map(),
    head: null, body: null, input: null });
  $('tab-im').classList.add('has-chat');
  markActive();
  if (cached) {
    // 打开过的会话: 立刻显示记下的消息和滚动位置, 再在后台刷新
    useEntry(cached);
    renderChat(); renderMsgs(cached.view ? 'restore' : 'unread', cached.view);
    startPoll(seq);
    if (threadId) openThreadId(threadId);
    if (Date.now() - cached.fetchedAt > FRESH_MS) refreshOpen(seq);
    return;
  }
  // 没打开过: 先用会话列表里的名字和头像把窗口搭好(输入框可以先打字), 消息到了再填进去
  if (item) { S.chat = { chat_id: chatId, name: item.name, avatar: item.avatar, kind: item.kind, unread: item.unread }; renderChat(); }
  else emptyChat('加载中…');
  const d = await act(() => fetchChat(chatId));
  if (seq !== S.seq) {
    if (d && chatId !== S.chatId && !S.cache.has(chatId)) remember(chatId, entryFrom(d));  // 已经切走了, 留着下次用
    return;
  }
  if (!d) { emptyChat('打不开这个会话'); return; }
  useEntry(entryFrom(d));
  if (S.body) renderHead(); else renderChat();
  renderMsgs('unread');
  startPoll(seq);
  if (threadId) openThreadId(threadId);
}

async function refreshOpen(seq) {
  const chatId = S.chatId;
  const d = await fetchChat(chatId).catch(() => null);
  if (!d) return;
  if (seq !== S.seq) {
    const e = chatId !== S.chatId && S.cache.get(chatId);
    if (e) mergeFresh(e, d);
    return;
  }
  mergeFresh(S, d);
  renderHead(); renderMsgs('keep-bottom');
}

function startPoll(seq) {
  S.poll = setInterval(() => pollTick(seq), POLL_MS);
}

function closeChat() {
  saveCurrent();
  clearInterval(S.poll); S.seq++;
  clearComposer(); closeThread();
  Object.assign(S, { chatId: '', chat: null, msgs: [], loaded: false, head: null, body: null, input: null });
  $('tab-im').classList.remove('has-chat');
  emptyChat('看消息不会标为已读，对方也看不到');
  markActive();
}

async function pollTick(seq) {
  if (S.polling || seq !== S.seq) return;
  S.polling = true;
  try {
    // 话题群不问本地库: 本地库按会话位置存消息, 和话题位置对不上, 只定时从服务端刷新
    if (!isTopic()) {
      const d = await api(`/api/im/chat/${S.chatId}?after=${S.end}&local=1`).catch(() => null);
      if (seq !== S.seq) return;
      if (d && d.messages.length) {
        addMsgs(d.messages); S.end = Math.max(S.end, d.end); renderMsgs('keep-bottom');
        return;
      }
    }
    if (Date.now() - S.lastServer >= (document.hidden ? SERVER_HIDDEN_MS : SERVER_MS)) await pollChat(seq);
  } finally { if (seq === S.seq) S.polling = false; }
}

async function pollChat(seq) {
  if (!S.loaded || seq !== S.seq) return;  // 消息还没取到(比如加载时就发了消息): 取到后的轮询会带上
  S.lastServer = Date.now();
  const d = await api(`/api/im/chat/${S.chatId}?after=${S.end}`).catch(() => null);
  if (!d || seq !== S.seq) return;
  if (d.chat.kind === 'topic') {
    // 话题群返回的是最近几个话题(回复数会变), 替换这一段; 打开着的话题顺带取新回复
    mergeFresh(S, d); renderHead(); renderMsgs('keep-bottom');
    await pollThread();
    return;
  }
  S.chat = d.chat;
  if (d.messages.length) addMsgs(d.messages);
  S.end = Math.max(S.end, d.end);
  if (d.messages.length) renderMsgs('keep-bottom');
  renderHead();
}

async function olderMsgs() {
  const seq = S.seq;
  const d = await act(() => api(`/api/im/chat/${S.chatId}?before=${S.start}&count=30`));
  if (!d || seq !== S.seq) return;
  addMsgs(d.messages); S.start = d.start; S.hasMore = d.has_more;
  renderMsgs('keep-offset');
}

function addMsgs(list) { addTo(S, list); }
function addTo(t, list) {
  const have = new Set(t.msgs.map(m => m.msg_id));
  t.msgs = t.msgs.concat(list.filter(m => !have.has(m.msg_id))).sort((a, b) => a.position - b.position);
}

// 搭好聊天窗口(标题、消息区、输入框), 每次打开会话搭一次; 之后只更新标题(renderHead)和消息(renderMsgs), 输入框里打的字不会丢
function renderChat() {
  S.head = h('div', { class: 'chat-head' });
  S.body = h('div', { class: 'chat-body' }, S.loaded ? null : h('div', { class: 'sys-msg' }, '加载中…'));
  S.replyBar = h('div', { class: 'composer-reply', hidden: true });
  S.pendingBar = h('div', { class: 'composer-pending', hidden: true });
  S.mentionPop = h('div', { class: 'pop mention-pop', hidden: true });
  S.emojiPop = h('div', { class: 'pop emoji-pop', hidden: true },
    EMOJIS.map(e => h('button', { title: e, onmousedown: ev => ev.preventDefault(), onclick: () => { insertText(e); closeEmoji(); } }, e)));
  const input = h('textarea', { rows: 1, placeholder: `发送给 ${S.chat.name}` });
  const sendBtn = h('button', { class: 'send', title: '发送 (Enter)', disabled: true, onclick: () => sendMsg() }, icon('send'));
  const refresh = () => { sendBtn.disabled = S.sending || (!input.value.trim() && !S.pending.length); };
  const grow = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 200) + 'px'; refresh(); };
  input.addEventListener('input', () => { grow(); updateMention(); });
  input.addEventListener('keydown', onComposerKey);
  input.addEventListener('click', updateMention);
  input.addEventListener('keyup', e => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) updateMention(); });
  input.addEventListener('blur', () => closeMention());
  input.addEventListener('paste', onPaste);
  S.input = input; S.grow = grow; S.refreshSend = refresh;
  const picker = (accept, kind) => h('input', { type: 'file', accept, multiple: true, hidden: true,
    onchange: e => { addFiles(e.target.files, kind); e.target.value = ''; } });
  const imgInput = picker('image/png,image/jpeg,image/gif,image/webp', 'image'), fileInput = picker(null, 'file');
  S.emojiBtn = tool('emoji', '表情', () => { if (S.emojiPop.hidden) openEmoji(); else closeEmoji(); });
  put($('chat'), S.head, S.body,
    h('div', { class: 'composer' }, S.mentionPop, S.emojiPop, S.pendingBar, S.replyBar, input,
      h('div', { class: 'composer-bar' },
        h('div', { class: 'composer-tools' }, S.emojiBtn, tool('at', '@ 提及', () => insertText('@')),
          tool('image', '发送图片（也可以粘贴截图或拖进来）', () => imgInput.click()),
          tool('attach', '发送文件（不超过 100MB）', () => fileInput.click()), imgInput, fileInput),
        h('span', { class: 'composer-hint' }, 'Enter 发送，Shift + Enter 换行'), sendBtn)),
    h('div', { class: 'drop-zone' }, icon('attach'), S.dropText = h('span', {}, `松开发送给 ${S.chat.name}`)));
  renderHead();
  input.focus();
}

function tool(name, title, onclick) {
  return h('button', { class: 'tool', title, 'aria-label': title, onmousedown: e => e.preventDefault(), onclick }, icon(name));
}

function renderHead() {
  const c = S.chat;
  if (!c || !S.head) return;
  put(S.head,
    h('button', { class: 'chat-back', title: '返回', onclick: () => { location.hash = '#/im'; } }, icon('back')),
    avatar(c.avatar, c.name, 32),
    h('div', { class: 'chat-title', title: c.name }, c.name),
    c.kind !== 'p2p' && c.member_count ? h('span', { class: 'chat-sub' }, `${c.member_count} 人`) : null,
    h('div', { class: 'chat-grow' }),
    h('span', { class: 'incognito', title: '在这里看消息不会标为已读，对方也看不到' }, icon('eye'), c.unread ? `${c.unread} 条未读 · 看消息不标已读` : '看消息不标已读'));
  if (S.input) S.input.placeholder = `发送给 ${c.name}`;
  if (S.dropText) S.dropText.textContent = `松开发送给 ${c.name}`;
}

// 同一个人 5 分钟内的连续消息不重复显示头像和名字, 和客户端一样
function isGrouped(m, prev) {
  return !!prev && prev.kind !== 'system' && prev.kind !== 'recalled' && prev.from_id === m.from_id && m.time - prev.time < 300;
}

function msgRow(m, grouped, inThread) {
  if (m.kind === 'system' || m.kind === 'recalled') return h('div', { class: 'sys-msg' + (m.kind === 'recalled' ? ' recalled' : '') }, m.content);
  return h('div', { class: 'msg' + (m.me ? ' me' : '') },
    grouped ? h('span', { class: 'msg-avatar-gap' }) : avatar(m.avatar, m.sender, 32),
    h('div', { class: 'msg-main' },
      grouped ? null : h('div', { class: 'msg-meta' }, m.me ? null : h('span', {}, m.sender || '未知'), h('time', {}, msgTime(m.time))),
      m.quote ? h('div', { class: 'msg-quote', title: (m.quote.sender ? m.quote.sender + '：' : '') + m.quote.text },
        m.quote.sender ? h('span', { class: 'msg-quote-who' }, m.quote.sender + '：') : null, m.quote.text) : null,
      msgBody(m, inThread)),
    h('div', { class: 'msg-ops' }, h('button', { title: '回复', onclick: () => setReply(m) }, icon('reply'), '回复')));
}

function msgBody(m, inThread) {
  if (m.kind === 'image') return h('div', { class: 'bubble img' }, chatImage(m, m.image, 260, true));
  if (m.kind === 'sticker') return h('div', { class: 'bubble img sticker' }, chatImage(m, m.image, 140, false));
  if (m.kind === 'file') return fileCard(m);
  if (m.kind === 'card') return cardBubble(m);
  return h('div', { class: 'bubble' },
    m.root_id && !m.quote && !inThread ? h('div', { class: 'bubble-reply' }, '回复话题') : null,
    m.title ? h('div', { class: 'bubble-title' }, m.title) : null,
    segments(m.segments, m));
}

// 后端整理好的片段: 文字一律走 textContent, 链接只认 http/https, 新窗口打开且不带 opener/referrer
const SEG_STYLES = ['bold', 'italic', 'underline', 'strike', 'code'];
const SAFE_HREF = /^https?:\/\//i;
function segments(list, m) {
  return (list || []).map(s => {
    if (s.type === 'link' && SAFE_HREF.test(s.href || '')) return h('a', { href: s.href, target: '_blank', rel: 'noopener noreferrer', title: s.href }, s.text || s.href);
    if (s.type === 'at') return h('span', { class: 'at' + (s.me ? ' at-me' : '') }, s.name);
    if (s.type === 'image') return chatImage(m, s, 260, true, 'inline-img');
    const cls = SEG_STYLES.filter(k => s[k]).map(k => 'seg-' + k).join(' ');
    return cls ? h('span', { class: cls }, s.text || '') : document.createTextNode(s.text || '');
  });
}

function mediaUrl(m, key) {
  return `/api/im/media?chat=${S.chatId}&msg=${encodeURIComponent(m.msg_id)}&key=${encodeURIComponent(key)}`;
}

// 按原图比例先占好位置, 图片加载完成前后高度不变, 定位到"以下为新消息"时不会被挤开
function fitStyle(w, hgt, max) {
  if (!(w > 0 && hgt > 0)) return null;
  const k = Math.min(1, max / Math.max(w, hgt));
  return `width:${Math.max(1, Math.round(w * k))}px;aspect-ratio:${w}/${hgt}`;
}

function chatImage(m, img, max, zoom, cls) {
  const el = h('img', { src: mediaUrl(m, img.key), alt: m.kind === 'sticker' ? '表情包' : '图片', loading: 'lazy',
    class: cls || null, style: fitStyle(img.w, img.h, max) });
  if (zoom) el.addEventListener('click', () => window.open(el.src, '_blank', 'noopener'));
  el.addEventListener('error', () => el.replaceWith(document.createTextNode(m.kind === 'sticker' ? '[表情包]' : '[图片]')));
  return el;
}

function fmtSize(n) {
  if (!n) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0; while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + ' ' + units[i];
}
function fmtDuration(ms) {
  const sec = Math.round((ms || 0) / 1000);
  return sec ? `${Math.floor(sec / 60)}:${pad(sec % 60)}` : '';
}

function fileCard(m) {
  const f = m.file;
  const meta = [fmtSize(f.size), f.video ? fmtDuration(f.duration) : ''].filter(Boolean).join(' · ');
  return h('a', { class: 'bubble file', href: mediaUrl(m, f.key) + '&dl=1&name=' + encodeURIComponent(f.name), download: f.name,
    title: '下载 ' + f.name },
    icon(f.video ? 'video' : 'file', 'file-icon'),
    h('span', { class: 'file-main' }, h('span', { class: 'file-name' }, f.name),
      h('span', { class: 'file-meta' }, meta ? `${meta} · 点击下载` : '点击下载')));
}

const CARD_THEMES = new Set(['blue', 'wathet', 'turquoise', 'green', 'yellow', 'orange', 'red', 'carmine', 'violet', 'purple', 'indigo', 'grey']);
function cardBubble(m) {
  const theme = CARD_THEMES.has(m.theme) ? ' theme-' + m.theme : '';
  return h('div', { class: 'bubble card' },
    m.title ? h('div', { class: 'card-head' + theme }, h('div', { class: 'card-title' }, m.title),
      m.subtitle ? h('div', { class: 'card-sub' }, m.subtitle) : null) : null,
    m.segments && m.segments.length ? h('div', { class: 'card-body' }, segments(m.segments, m)) : null);
}

function setReply(m) {
  // 引用回复: parent 是点的那条(上方显示的引用), root 是它所在那串回复最早的一条; 话题群里 root 就是话题的根消息
  S.replyTo = { root_id: m.root_id || m.msg_id, parent_id: m.msg_id, sender: m.me ? '我' : m.sender, content: (m.content || '').slice(0, 80) };
  renderReply(); S.input && S.input.focus();
  // 被回复的那条(话题的根消息)会被服务端标为已读; 回复自己发的不涉及别人
  if (!m.me || m.root_id) toast('回复某条消息会让那一条对对方显示已读，其他消息不受影响');
}

function renderReply() {
  const r = S.replyTo;
  S.replyBar.hidden = !r;
  put(S.replyBar, r ? [h('span', {}, `回复 ${r.sender}：${r.content}`),
    h('button', { title: '取消回复', onclick: () => { S.replyTo = null; renderReply(); } }, '×')] : null);
}

// 打开时就未读的第一条(别人发的): 在它前面画"以下为新消息"。只是显示, 不会标已读
function isFirstUnread(m) {
  return S.readPos > 0 && m.position > S.readPos && m.position <= S.unreadUpTo && !m.me && m.kind !== 'system';
}

// 消息内容的签名: 同一条消息内容没变(签名相同)就沿用已经建好的节点
const SIGS = new WeakMap();
function sigOf(m) {
  let s = SIGS.get(m);
  if (s === undefined) { s = JSON.stringify(m); SIGS.set(m, s); }
  return s;
}

// 增量更新: 内容和分组都没变的消息沿用原来的节点(图片不重新加载、不闪), 只新建新来的和变了的(比如被撤回)。
// mode: unread 定位到"以下为新消息"(没有就到底部), bottom 到底部, keep-bottom 原来在底部就留在底部,
// keep-offset 往前加载后保持看到的位置不动, restore 恢复切走时的位置(view)
function renderMsgs(mode, view) {
  const box = S.body;
  if (!box || !S.loaded) return;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  const fromBottom = box.scrollHeight - box.scrollTop;
  // 消息上的时间按"今天/昨天"显示, 过了零点就重建
  // 话题群: 每个话题一行(发起人、时间、根消息、回复数), 不合并同一个人的连续话题, 也不插时间分隔
  const topic = isTopic();
  const rows = [], built = new Map(), day = new Date().toDateString();
  let prev = null, divider = null;
  for (const m of S.msgs) {
    if (m.kind === 'system' && !m.content) continue;  // 没有提示文字的系统消息
    if (!divider && isFirstUnread(m)) {
      divider = h('div', { class: 'unread-divider' }, h('span', {}, topic ? '以下为新话题' : '以下为新消息'));
      rows.push(divider); prev = null;
    }
    if (!topic && (!prev || m.time - prev.time > 300)) { rows.push(h('div', { class: 'time-divider' }, msgTime(m.time))); prev = null; }
    const key = m.msg_id || 'p' + m.position, sig = sigOf(m), grouped = !topic && isGrouped(m, prev), old = S.rows.get(key);
    const row = old && old.sig === sig && old.grouped === grouped && old.day === day ? old
      : { sig, grouped, day, node: topic ? topicRow(m) : msgRow(m, grouped) };
    built.set(key, row);
    rows.push(row.node);
    prev = m;
  }
  S.rows = built;
  const empty = topic ? (S.hasMore ? '最近没有话题' : '还没有话题') : '还没有消息';
  put(box, S.hasMore ? h('div', { class: 'chat-older' }, h('button', { onclick: olderMsgs }, topic ? '查看更早的话题' : '查看更早的消息')) : null,
    rows.length ? rows : h('div', { class: 'sys-msg' }, empty));
  if (topic) markTopic();
  if (mode === 'restore' && view) box.scrollTop = view.bottom ? box.scrollHeight : view.top;
  else if (mode === 'unread' && divider) box.scrollTop = Math.max(0, divider.offsetTop - 12);
  else if (mode === 'bottom' || mode === 'unread' || (mode === 'keep-bottom' && nearBottom)) box.scrollTop = box.scrollHeight;
  else if (mode === 'keep-offset') box.scrollTop = box.scrollHeight - fromBottom;
}

// ---- 话题群: 话题列表和右侧的话题面板 ----

// 话题的根消息: 和消息列表一样的头像、名字、时间和气泡, 一律靠左(本人发的也显示名字); foot 是下面的"N 条回复"
function postRow(m, cls, foot, onclick) {
  if (m.kind === 'system' || m.kind === 'recalled') {
    return h('div', { class: 'topic-sys' }, h('div', { class: 'sys-msg' + (m.kind === 'recalled' ? ' recalled' : '') }, m.content), foot);
  }
  return h('div', { class: 'msg ' + cls, onclick },
    avatar(m.avatar, m.sender, 32),
    h('div', { class: 'msg-main' },
      h('div', { class: 'msg-meta' }, h('span', {}, m.me ? (S.me && S.me.name) || '我' : m.sender || '未知'), h('time', {}, msgTime(m.time))),
      msgBody(m, true), foot),
    h('div', { class: 'msg-ops' }, h('button', { title: '回复这个话题', onclick: e => { e.stopPropagation(); setReply(m); } }, icon('reply'), '回复')));
}

function topicRow(m) {
  const t = m.thread || {}, n = t.reply_count || 0;
  const foot = h('button', { class: 'topic-foot', onclick: e => { e.stopPropagation(); openThread(m); } }, icon('chat'),
    n ? `${n} 条回复` : '查看话题', n && t.update_time ? h('span', { class: 'topic-last' }, '最后回复 ' + msgTime(t.update_time)) : null);
  // 点话题的空白处也能打开; 点链接、图片、文件、按钮或者选中文字时不打开
  const row = postRow(m, 'topic', m.kind === 'system' || m.kind === 'recalled' ? (n ? foot : null) : foot, e => {
    if (e.target.closest('a, img, button') || String(window.getSelection() || '')) return;
    openThread(m);
  });
  row.dataset.thread = t.id || '';
  return row;
}

// 话题列表里标出正在右侧看的那个
function markTopic() {
  if (!S.body) return;
  const id = S.thread ? S.thread.id : '';
  S.body.querySelectorAll('[data-thread]').forEach(el => el.classList.toggle('active', !!id && el.dataset.thread === id));
}

function openThreadId(id) {
  if (!isTopic() || !/^\d+$/.test(id)) return;
  openThread(S.msgs.find(m => m.thread && m.thread.id === id) || { thread: { id, reply_count: 0, last_position: -1 } });
}

async function openThread(m) {
  const info = m.thread;
  if (!info || !info.id || (S.thread && S.thread.id === info.id)) return;
  const seq = ++S.threadSeq, chatId = S.chatId;
  S.thread = { id: info.id, chatId, seq, root: m.msg_id ? m : null, info, msgs: [], start: 0, end: -1, hasMore: false, loaded: false,
    error: false, rows: new Map(), rootRow: null };
  $('tab-im').classList.add('has-thread');
  history.replaceState(null, '', `#/im/${chatId}/${info.id}`);
  renderThread(); markTopic();
  // 带上话题列表里的最后一条回复的位置, 后端据此先猜着拉回复, 和查话题信息同时进行
  const q = info.last_position >= 0 ? `?last=${info.last_position}` : '';
  const d = await act(() => api(`/api/im/chat/${chatId}/thread/${info.id}${q}`));
  const t = S.thread;
  if (!t || t.seq !== seq) return;
  if (!d) { t.error = true; renderThreadBody(); return; }
  Object.assign(t, { root: d.root, info: d.thread, msgs: d.messages, start: d.start, end: d.end, hasMore: d.has_more, loaded: true });
  renderThreadBody('top');
}

// byUser: 点了关闭(地址退回到会话); 切换、关闭会话时地址已经由导航改好了, 不动它
function closeThread(byUser) {
  if (!S.thread) return;
  if (byUser === true && S.chatId) history.replaceState(null, '', `#/im/${S.chatId}`);
  S.thread = null; S.threadSeq++;
  $('tab-im').classList.remove('has-thread');
  put($('thread')); $('thread').hidden = true;
  markTopic();
}

function renderThread() {
  const t = S.thread;
  if (!t) return;
  t.sub = h('span', { class: 'chat-sub' });
  t.body = h('div', { class: 'thread-body' });
  put($('thread'), h('div', { class: 'thread-head' }, h('div', { class: 'thread-title' }, '话题'), t.sub, h('div', { class: 'chat-grow' }),
    h('button', { class: 'thread-close', title: '关闭', onclick: () => closeThread(true) }, icon('close'))), t.body);
  $('thread').hidden = false;
  renderThreadBody('top');
}

// 话题面板: 根消息、"N 条回复"、(更早的回复)、回复; 回复和消息列表一样按时间分隔、合并同一个人的连续消息, 没变的沿用原来的节点
function renderThreadBody(mode) {
  const t = S.thread;
  if (!t || !t.body) return;
  const box = t.body, n = t.info.reply_count || 0;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80, fromBottom = box.scrollHeight - box.scrollTop;
  t.sub.textContent = n ? `${n} 条回复` : '';
  // 从地址直接打开时, 根消息要等接口返回
  const rootSig = t.root ? sigOf(t.root) : '';
  if (t.root && (!t.rootRow || t.rootRow.sig !== rootSig)) t.rootRow = { sig: rootSig, node: postRow(t.root, 'thread-root', null) };
  const rows = [], built = new Map(), day = new Date().toDateString();
  let prev = null;
  for (const m of t.msgs) {
    if (m.kind === 'system' && !m.content) continue;
    if (!prev || m.time - prev.time > 300) { rows.push(h('div', { class: 'time-divider' }, msgTime(m.time))); prev = null; }
    const key = m.msg_id || 'p' + m.position, sig = sigOf(m), grouped = isGrouped(m, prev), old = t.rows.get(key);
    const row = old && old.sig === sig && old.grouped === grouped && old.day === day ? old : { sig, grouped, day, node: msgRow(m, grouped, true) };
    built.set(key, row);
    rows.push(row.node);
    prev = m;
  }
  t.rows = built;
  const status = t.error ? '加载失败' : !t.loaded ? '加载中…' : !n && !rows.length ? '还没有回复' : null;
  put(box, t.rootRow ? t.rootRow.node : null, h('div', { class: 'thread-divider' }, h('span', {}, n ? `${n} 条回复` : '回复')),
    t.hasMore ? h('div', { class: 'chat-older' }, h('button', { onclick: olderReplies }, '查看更早的回复')) : null,
    status ? h('div', { class: 'sys-msg' }, status) : null, rows);
  if (mode === 'top') box.scrollTop = 0;
  else if (mode === 'keep-bottom' && nearBottom) box.scrollTop = box.scrollHeight;
  else if (mode === 'keep-offset') box.scrollTop = box.scrollHeight - fromBottom;
}

async function olderReplies() {
  const t = S.thread;
  if (!t) return;
  const d = await act(() => api(`/api/im/chat/${t.chatId}/thread/${t.id}?before=${t.start}&count=30`));
  if (!d || S.thread !== t) return;
  addTo(t, d.messages); Object.assign(t, { start: d.start, hasMore: d.has_more, info: d.thread });
  renderThreadBody('keep-offset');
}

// 打开着的话题: 随话题列表的定时刷新取新回复, 回复数和根消息(比如被撤回)也一起更新
async function pollThread() {
  const t = S.thread;
  if (!t || !t.loaded) return;
  const d = await api(`/api/im/chat/${t.chatId}/thread/${t.id}?after=${t.end}`).catch(() => null);
  if (!d || S.thread !== t) return;
  Object.assign(t, { root: d.root, info: d.thread, end: Math.max(t.end, d.end) });
  if (d.messages.length) addTo(t, d.messages);
  renderThreadBody('keep-bottom');
}

// ---- 发送: 图片、文件、@、表情 ----

const MAX_IMAGE = 20 * 1024 * 1024, MAX_FILE = 100 * 1024 * 1024, MAX_PENDING = 9;
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
// 表情用 Unicode emoji: 它就是普通文字, 飞书各端、通知和这里都原样显示。[微笑] 这类文字代码要作为飞书内部的
// 表情元素(带表情 key)发出才会显示成表情, 当纯文字发出去对方看到的就是"[微笑]"几个字
const EMOJIS = ['😀', '😄', '😁', '😂', '🤣', '😊', '🙂', '😉', '😍', '😘', '😋', '😎', '🤔', '🤨', '😐', '🙄',
  '😏', '😮', '😴', '😅', '😓', '😭', '😢', '😤', '😡', '😱', '🥳', '🤗', '🤝', '👍', '👎', '👏',
  '🙏', '💪', '👌', '✌️', '👋', '🙌', '👀', '❤️', '💔', '🔥', '🎉', '✨', '💯', '✅', '❌', '⚠️',
  '❓', '❗', '⭐', '🌹', '☕', '🍺', '🎂', '🧧', '💰', '📌', '📎', '🚀', '⏰', '🌙', '☀️', '🆗'];

function clearComposer() {
  for (const p of S.pending) if (p.url) URL.revokeObjectURL(p.url);
  Object.assign(S, { pending: [], mentions: [], members: null });
  closeMention(); closeEmoji();
}

function insertText(text) {
  const el = S.input;
  if (!el) return;
  el.focus();
  el.setRangeText(text, el.selectionStart, el.selectionEnd, 'end');
  S.grow(); updateMention();
}

function openEmoji() { closeMention(); S.emojiPop.hidden = false; S.emojiBtn.classList.add('on'); }
function closeEmoji() {
  if (S.emojiPop) S.emojiPop.hidden = true;
  if (S.emojiBtn) S.emojiBtn.classList.remove('on');
}

// 附件: 先放在输入框上方(图片显示缩略图), 可以取消, 按发送时依次上传并发出
function addFiles(list, prefer) {
  if (!S.chat || S.sending) return;
  for (const f of Array.from(list || [])) {
    if (!f.size) { toast(`「${f.name || '文件'}」是空文件，不能发送`); continue; }
    let kind = prefer === 'file' ? 'file' : IMAGE_TYPES.has(f.type) ? 'image' : 'file';
    if (prefer === 'image' && kind === 'file') toast('不是 PNG、JPEG、GIF、WebP 图片，将作为文件发送');
    if (kind === 'image' && f.size > MAX_IMAGE) { kind = 'file'; toast('图片超过 20MB，将作为文件发送'); }
    if (kind === 'file' && f.size > MAX_FILE) { toast(`「${f.name}」超过 100MB，不能发送`); continue; }
    if (S.pending.length >= MAX_PENDING) { toast(`一次最多发 ${MAX_PENDING} 个图片或文件`); break; }
    S.pending.push({ id: ++S.pendingSeq, kind, file: f, url: kind === 'image' ? URL.createObjectURL(f) : '', status: '' });
  }
  renderPending(); S.refreshSend(); S.input.focus();
}

function removePending(id) {
  const p = S.pending.find(x => x.id === id);
  if (p && p.url) URL.revokeObjectURL(p.url);
  S.pending = S.pending.filter(x => x.id !== id);
  renderPending(); S.refreshSend(); S.input.focus();
}

function renderPending() {
  const bar = S.pendingBar;
  if (!bar) return;
  bar.hidden = !S.pending.length;
  put(bar, S.pending.map(p => h('div', { class: `pending ${p.kind}`, title: `${p.file.name || ''} ${fmtSize(p.file.size)}`.trim() },
    // 上传进度: 图片盖在缩略图上, 文件写在文件名下面
    p.kind === 'image' ? [h('img', { src: p.url, alt: '待发送的图片' }), p.status ? h('span', { class: 'pending-status' }, p.status) : null]
      : [icon('file', 'pending-icon'), h('span', { class: 'pending-text' }, h('span', { class: 'pending-name' }, p.file.name || '文件'),
        h('span', { class: 'pending-meta' + (p.status ? ' busy' : '') }, [fmtSize(p.file.size), p.status].filter(Boolean).join(' · ')))],
    S.sending ? null : h('button', { class: 'pending-x', title: '取消', onclick: () => removePending(p.id) }, icon('close')))));
}

function onPaste(e) {
  const files = Array.from((e.clipboardData && e.clipboardData.files) || []);
  if (!files.length) return;  // 普通文字照常粘贴
  e.preventDefault();
  addFiles(files);
}

// 拖进聊天窗口: 图片按图片发, 其他按文件发; 窗口外松开不让浏览器打开文件
const hasFiles = e => Array.from((e.dataTransfer && e.dataTransfer.types) || []).includes('Files');
let dragDepth = 0;
function setupDrop() {
  const box = $('chat');
  const off = () => { dragDepth = 0; box.classList.remove('dragging'); };
  box.addEventListener('dragenter', e => {
    if (!S.chat || !hasFiles(e)) return;
    e.preventDefault(); dragDepth++; box.classList.add('dragging');
  });
  box.addEventListener('dragover', e => { if (S.chat && hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
  box.addEventListener('dragleave', () => { if (--dragDepth <= 0) off(); });
  box.addEventListener('drop', e => { if (!S.chat || !hasFiles(e)) return; e.preventDefault(); off(); addFiles(e.dataTransfer.files); });
  window.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', e => { if (hasFiles(e)) { e.preventDefault(); off(); } });
}

// ---- @ 提及: 输入 @ 弹出候选, 上下键选择, 回车或 Tab 确认 ----

const M = { open: false, start: 0, query: '', items: [], index: 0, seq: 0, hint: '', timer: 0 };

function mentionContext() {
  const el = S.input;
  if (!el || el.selectionStart !== el.selectionEnd) return null;
  const before = el.value.slice(0, el.selectionStart);
  // @ 前面是开头、空白或标点才算(邮箱里的 @ 不算)
  const m = before.match(/(?:^|[^\w@.])@([^\s@]{0,30})$/);
  return m ? { start: el.selectionStart - m[1].length - 1, query: m[1] } : null;
}

function loadMembers() {
  if (S.members && S.members.chatId === S.chatId) return S.members.promise;
  const chatId = S.chatId;
  const promise = api(`/api/im/chat/${chatId}/members`).then(d => ({ list: d.members || [], complete: !!d.complete }), () => null);
  S.members = { chatId, promise };
  promise.then(r => { if (!r && S.members && S.members.chatId === chatId) S.members = null; });  // 失败了下次再试
  return promise;
}

function filterMembers(list, q) {
  q = q.trim().toLowerCase();
  if (!q) return list.slice(0, 8);
  const scored = [];
  for (const m of list) {
    const name = m.name.toLowerCase(), alias = (m.alias || '').toLowerCase(), en = (m.en || '').toLowerCase();
    const py = (m.pinyin || '').toLowerCase().trim(), joined = py.replace(/\s+/g, ''), initials = py.split(/\s+/).map(w => w[0] || '').join('');
    let score = 0;
    if (name.startsWith(q) || alias.startsWith(q)) score = 4;
    else if (name.includes(q) || alias.includes(q)) score = 3;
    else if (joined.startsWith(q) || initials.startsWith(q) || en.startsWith(q)) score = 2;
    else if (joined.includes(q) || en.includes(q)) score = 1;
    if (score) scored.push([score, m]);
  }
  return scored.sort((a, b) => b[0] - a[0]).slice(0, 8).map(x => x[1]);
}

async function updateMention() {
  const ctx = mentionContext();
  if (!ctx || !S.chat) { closeMention(); return; }
  closeEmoji();
  const seq = ++M.seq;
  clearTimeout(M.timer);
  Object.assign(M, { open: true, start: ctx.start, query: ctx.query });
  if (!M.items.length) { M.hint = '加载成员…'; renderMention(); }
  const members = await loadMembers();
  if (seq !== M.seq) return;
  Object.assign(M, { items: filterMembers(members ? members.list : [], ctx.query), index: 0 });
  M.hint = members ? (ctx.query ? '没有找到这个人' : '这个会话里没有可以 @ 的人') : (ctx.query ? '' : '查不到会话成员，输入名字搜索联系人');
  // 成员里找得少(或大群没取全、成员查不到)时, 再按名字搜联系人补上
  const search = ctx.query && (!members || !members.complete || M.items.length < 3);
  if (search && !M.items.length) M.hint = '搜索联系人…';
  renderMention();
  if (!search) return;
  M.timer = setTimeout(async () => {
    const found = await api('/api/im/search?q=' + encodeURIComponent(ctx.query)).catch(() => []);
    if (seq !== M.seq) return;
    const have = new Set(M.items.map(i => i.user_id));
    const extra = found.filter(x => x.kind === 'user' && !have.has(x.id) && x.id !== (S.me && S.me.user_id))
      .map(x => ({ user_id: x.id, name: x.name, avatar: '', contact: true }));
    M.items = M.items.concat(extra).slice(0, 8);
    M.hint = '没有找到这个人';
    renderMention();
  }, 300);
}

function renderMention() {
  const pop = S.mentionPop;
  if (!pop) return;
  pop.hidden = !M.open;
  if (!M.open) return;
  put(pop, M.items.length ? M.items.map((m, i) => h('div', { class: 'mention-item' + (i === M.index ? ' on' : ''),
    onmousedown: e => { e.preventDefault(); chooseMention(i); }, onmouseenter: () => { M.index = i; highlightMention(); } },
  avatar(m.avatar, m.name, 24), h('span', { class: 'mention-name' }, m.name),
  m.alias ? h('span', { class: 'mention-sub' }, m.alias) : null,
  m.bot ? h('span', { class: 'mention-tag' }, '机器人') : m.contact ? h('span', { class: 'mention-tag' }, '联系人') : null))
    : h('div', { class: 'mention-empty' }, M.hint));
}

function highlightMention() {
  if (!S.mentionPop) return;
  S.mentionPop.querySelectorAll('.mention-item').forEach((el, j) => {
    el.classList.toggle('on', j === M.index);
    if (j === M.index) el.scrollIntoView({ block: 'nearest' });
  });
}

function chooseMention(i) {
  const m = M.items[i], el = S.input;
  if (!m || !el) return;
  el.setRangeText(`@${m.name} `, M.start, el.selectionStart, 'end');
  S.mentions.push({ user_id: m.user_id, name: m.name });
  closeMention(); S.grow(); el.focus();
}

function closeMention() {
  M.seq++; clearTimeout(M.timer);
  Object.assign(M, { open: false, items: [], index: 0 });
  if (S.mentionPop) S.mentionPop.hidden = true;
}

function onComposerKey(e) {
  if (e.isComposing) return;
  if (M.open && M.items.length) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      M.index = (M.index + (e.key === 'ArrowDown' ? 1 : -1) + M.items.length) % M.items.length;
      highlightMention(); return;
    }
    if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); chooseMention(M.index); return; }
  }
  if (e.key === 'Escape' && (M.open || !S.emojiPop.hidden)) { e.preventDefault(); closeMention(); closeEmoji(); return; }
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
}

// 上传一个图片或文件并发出: 浏览器到这里的进度用 XHR 的上传进度显示, 之后是这里上传到飞书和发送
function sendAttachment(p, chatId, reply, show) {
  return new Promise((resolve, reject) => {
    const q = new URLSearchParams({ kind: p.kind, name: p.file.name || (p.kind === 'image' ? 'image.png' : 'file'), type: p.file.type || '' });
    if (reply) { q.set('root_id', reply.root_id); q.set('parent_id', reply.parent_id); }
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/im/chat/${chatId}/upload?${q}`);
    xhr.setRequestHeader('X-Larkx', '1');
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    show('上传中 0%');
    xhr.upload.onprogress = e => { if (e.lengthComputable) show(`上传中 ${Math.floor(e.loaded / e.total * 100)}%`); };
    xhr.upload.onload = () => show('发送中…');
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* 不是 JSON */ }
      if (xhr.status === 401) { showLogin(); reject(new Error('未登录')); } else if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.error || `发送失败（HTTP ${xhr.status}）`));
    };
    xhr.onerror = () => reject(new Error('网络错误，没有发出'));
    xhr.send(p.file);
  });
}

async function sendMsg() {
  const input = S.input;
  if (!input || !S.chat || S.sending) return;
  const text = input.value.trim();
  if (!text && !S.pending.length) return;
  const seq = S.seq, chatId = S.chatId, queue = S.pending, reply = S.replyTo ? { root_id: S.replyTo.root_id, parent_id: S.replyTo.parent_id } : null;
  const current = () => seq === S.seq;
  const results = [];
  S.sending = true; input.disabled = true; S.refreshSend(); renderPending(); closeMention(); closeEmoji();
  try {
    // 先按添加的顺序发图片和文件, 再发文字
    while (queue.length) {
      const p = queue[0];
      results.push(await sendAttachment(p, chatId, reply, s => { p.status = s; if (current()) renderPending(); }).finally(() => { p.status = ''; }));
      if (p.url) URL.revokeObjectURL(p.url);
      queue.shift();
      if (current()) renderPending();
    }
    if (text) {
      const body = { text, ...(reply || {}), mentions: current() ? S.mentions : [] };
      results.push(await api(`/api/im/chat/${chatId}/send`, { method: 'POST', body }));
      if (current()) { input.value = ''; S.mentions = []; }
    }
    if (current() && !queue.length && !input.value.trim()) { S.replyTo = null; renderReply(); }
  } catch (e) {
    if (e.message !== '未登录') toast(e.message);
  } finally {
    S.sending = false; input.disabled = false;
    if (current()) { S.grow(); renderPending(); input.focus(); }
  }
  if (current() && results.length) {
    await pollChat(seq);
    S.body.scrollTop = S.body.scrollHeight;
  }
}

// ---- 数字人(控制台)标签页 ----

function showTab(tab) {
  document.querySelectorAll('.rail-item[data-tab]').forEach(a => a.classList.toggle('on', a.dataset.tab === tab));
  $('tab-im').hidden = tab !== 'im';
  $('tab-twin').hidden = tab !== 'twin';
  if (tab === 'twin' && !$('twinFrame').src) $('twinFrame').src = '/console?embed=1#overview';
}

// ---- 登录、路由、主题 ----

function showLogin() { $('login').hidden = false; $('token').focus(); }

async function login(token) {
  const r = await fetch('/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }), credentials: 'same-origin' });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || '登录失败');
}

async function start() {
  $('login').hidden = true;
  // 会话列表和本人信息同时取; 会话列表先要服务端记下的上一次结果, 立刻显示, 再正常刷新一次
  const first = S.me ? null : api('/api/im/feed?cached=1').catch(() => null);
  const me = await act(() => api('/api/im/me'));
  if (!me) return;
  if (S.me) { route(); return; }  // 登录过期后重新登录: 定时器已经在跑
  S.me = me;
  put($('me'), avatar(me.avatar, me.name, 36));
  $('me').title = me.name;
  renderFilters();
  renderFeed();
  route();
  const d = await first;
  if (d) applyFeed(d);
  if (!d || d.cached) await act(refreshFeed);
  else { S.feedAt = Date.now(); prefetchTop(); }
  setInterval(feedTick, 2000);
}

// 地址: #/im/<会话> 打开会话, 话题群再加 /<话题 id> 同时打开右侧的话题面板
function route() {
  const [, tab, arg, sub] = (location.hash || '#/im').split('/');
  if (tab === 'twin') { showTab('twin'); return; }
  showTab('im');
  if (arg && arg !== S.chatId) openChat(arg, sub);
  else if (arg && S.loaded && (sub || '') !== (S.thread ? S.thread.id : '')) { if (sub) openThreadId(sub); else closeThread(); }
  else if (!arg && S.chatId !== '') closeChat();
  else if (!arg) emptyChat('看消息不会标为已读，对方也看不到');
}

setupDrop();
document.addEventListener('mousedown', e => {
  if (S.emojiPop && !S.emojiPop.hidden && !S.emojiPop.contains(e.target) && !(S.emojiBtn && S.emojiBtn.contains(e.target))) closeEmoji();
});
$('q').addEventListener('input', e => { S.q = e.target.value.trim(); S.results = null; renderFeed(); });
$('q').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing && S.q) searchAll(S.q); });
$('loginBtn').addEventListener('click', () => login($('token').value.trim()).then(start, e => toast(e.message)));
$('token').addEventListener('keydown', e => { if (e.key === 'Enter') $('loginBtn').click(); });
$('theme').addEventListener('click', () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  root.dataset.theme = dark ? 'light' : 'dark';
  localStorage.setItem('larkx-theme', root.dataset.theme);  // 控制台监听同一个键, 一起切换
});
if (localStorage.getItem('larkx-theme')) document.documentElement.dataset.theme = localStorage.getItem('larkx-theme');
window.addEventListener('hashchange', route);
document.addEventListener('visibilitychange', () => { if (!document.hidden && S.me) feedTick(); });

(async () => {
  // 登录链接形如 /#login=<令牌>: 片段不会发给服务器, 换成 cookie 后立即从地址栏抹掉
  const m = location.hash.match(/^#login=(.+)$/);
  if (m) {
    history.replaceState(null, '', location.pathname + '#/im');
    await login(decodeURIComponent(m[1])).catch(e => toast(e.message));
  }
  start();
})();
