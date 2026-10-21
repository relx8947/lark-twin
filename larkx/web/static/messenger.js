// 网页版飞书。只用只读接口看消息, 看过的仍是未读; 聊天内容一律用 textContent 渲染, 不拼 HTML。
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
  if (!r.ok) throw new Error(data.error || r.statusText);
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

const S = { feed: [], cursor: '', filter: 'all', q: '', results: null, latest: null, feedAt: 0, feedDirty: false, feedBusy: false,
  chatId: '', chat: null, msgs: [], start: 0, end: 0, hasMore: false, replyTo: null, seq: 0, poll: 0, me: null,
  readPos: 0, unreadUpTo: 0, lastServer: 0, polling: false };
// 新消息: 每秒先问本地库(daemon 收到推送就入库), 本地没有时最多每 3 秒去服务端拉一次; 页面在后台时放慢
const POLL_MS = 1000, SERVER_MS = 3000, SERVER_HIDDEN_MS = 10000;
const KIND = { p2p: '单聊', group: '群组', topic: '话题群' };
const FILTERS = [['all', '全部'], ['unread', '未读'], ['p2p', '单聊'], ['group', '群组']];

// ---- 会话列表 ----

async function refreshFeed() {
  if (S.feedBusy) return;
  S.feedBusy = true; S.feedAt = Date.now();
  try {
    const d = await api('/api/im/feed');
    const fresh = new Set(d.items.map(i => i.chat_id));
    S.feed = d.items.concat(S.feed.filter(i => !fresh.has(i.chat_id)));
    if (!S.cursor) S.cursor = d.next_cursor;
    renderFeed();
  } finally { S.feedBusy = false; }
}

// 本地库有新消息入库就尽快刷新会话列表(最多每 3 秒一次), 不用等 8 秒一次的定时刷新
async function checkChanges() {
  if (document.hidden) return;
  const d = await api('/api/im/changes').catch(() => null);
  if (!d) return;
  if (S.latest != null && d.latest !== S.latest) S.feedDirty = true;
  S.latest = d.latest;
  if (S.feedDirty && !S.feedBusy && Date.now() - S.feedAt >= 3000) {
    S.feedDirty = false;
    refreshFeed().catch(() => {});
  }
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
  return h('div', { class: 'feedCard' + (i.chat_id === S.chatId ? ' active' : ''), onclick: () => { location.hash = '#/im/' + i.chat_id; } },
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
  put(list, rows.map(feedCard),
    S.cursor && !q ? h('button', { class: 'feed-more', onclick: () => act(moreFeed) }, '加载更多') : null,
    !rows.length ? h('div', { class: 'feed-hint' }, S.feed.length ? (q ? '回车搜索联系人和群组' : '没有符合条件的会话') : '加载中…') : null);
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

async function openChat(chatId) {
  clearInterval(S.poll);
  const seq = ++S.seq;
  Object.assign(S, { chatId, chat: null, msgs: [], replyTo: null });
  $('tab-im').classList.add('has-chat');
  renderFeed();
  emptyChat('加载中…');
  const d = await act(() => api(`/api/im/chat/${chatId}`));
  if (seq !== S.seq) return;
  if (!d) { emptyChat('打不开这个会话'); return; }
  // 已读位置只在打开时记一次: "以下为新消息"标在打开时就未读的第一条前, 之后来的新消息不再挪动它
  Object.assign(S, { chat: d.chat, msgs: d.messages, start: d.start, end: d.end, hasMore: d.has_more,
    readPos: d.chat.read_position || 0, unreadUpTo: d.end, lastServer: Date.now(), polling: false });
  renderChat();
  S.poll = setInterval(() => pollTick(seq), POLL_MS);
}

function closeChat() {
  clearInterval(S.poll); S.seq++;
  Object.assign(S, { chatId: '', chat: null, msgs: [] });
  $('tab-im').classList.remove('has-chat');
  emptyChat('看消息不会标为已读，对方也看不到');
  renderFeed();
}

async function pollTick(seq) {
  if (S.polling || seq !== S.seq) return;
  S.polling = true;
  try {
    const d = await api(`/api/im/chat/${S.chatId}?after=${S.end}&local=1`).catch(() => null);
    if (seq !== S.seq) return;
    if (d && d.messages.length) {
      addMsgs(d.messages); S.end = Math.max(S.end, d.end); renderMsgs('keep-bottom');
      return;
    }
    if (Date.now() - S.lastServer >= (document.hidden ? SERVER_HIDDEN_MS : SERVER_MS)) await pollChat(seq);
  } finally { if (seq === S.seq) S.polling = false; }
}

async function pollChat(seq) {
  S.lastServer = Date.now();
  const d = await api(`/api/im/chat/${S.chatId}?after=${S.end}`).catch(() => null);
  if (!d || seq !== S.seq) return;
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

function addMsgs(list) {
  const have = new Set(S.msgs.map(m => m.msg_id));
  S.msgs = S.msgs.concat(list.filter(m => !have.has(m.msg_id))).sort((a, b) => a.position - b.position);
}

function renderChat() {
  S.head = h('div', { class: 'chat-head' });
  S.body = h('div', { class: 'chat-body' });
  S.replyBar = h('div', { class: 'composer-reply', hidden: true });
  const input = h('textarea', { rows: 1, placeholder: `发送给 ${S.chat.name}` });
  const sendBtn = h('button', { class: 'send', title: '发送 (Enter)', disabled: true, onclick: () => sendMsg() }, icon('send'));
  const grow = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 200) + 'px'; sendBtn.disabled = !input.value.trim(); };
  input.addEventListener('input', grow);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMsg(); }
  });
  S.input = input; S.grow = grow;
  put($('chat'), S.head, S.body,
    h('div', { class: 'composer' }, S.replyBar, input,
      h('div', { class: 'composer-bar' }, h('span', { class: 'composer-hint' }, 'Enter 发送，Shift + Enter 换行；匿名发送：对方看不到已读，你在这个会话的未读也保留'), sendBtn)));
  renderHead(); renderMsgs('unread');
  input.focus();
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
    h('span', { class: 'incognito', title: '在这里看消息不会标为已读' }, icon('eye'), c.unread ? `${c.unread} 条未读 · 不会标为已读` : '不会标为已读'));
}

function msgRow(m, prev) {
  if (m.kind === 'system' || m.kind === 'recalled') return h('div', { class: 'sys-msg' + (m.kind === 'recalled' ? ' recalled' : '') }, m.content);
  // 同一个人 5 分钟内的连续消息不重复显示头像和名字, 和客户端一样
  const grouped = prev && prev.kind !== 'system' && prev.kind !== 'recalled' && prev.from_id === m.from_id && m.time - prev.time < 300;
  return h('div', { class: 'msg' + (m.me ? ' me' : '') },
    grouped ? h('span', { class: 'msg-avatar-gap' }) : avatar(m.avatar, m.sender, 32),
    h('div', { class: 'msg-main' },
      grouped ? null : h('div', { class: 'msg-meta' }, m.me ? null : h('span', {}, m.sender || '未知'), h('time', {}, msgTime(m.time))),
      m.quote ? h('div', { class: 'msg-quote', title: (m.quote.sender ? m.quote.sender + '：' : '') + m.quote.text },
        m.quote.sender ? h('span', { class: 'msg-quote-who' }, m.quote.sender + '：') : null, m.quote.text) : null,
      msgBody(m)),
    h('div', { class: 'msg-ops' }, h('button', { title: '回复', onclick: () => setReply(m) }, icon('reply'), '回复')));
}

function msgBody(m) {
  if (m.kind === 'image') return h('div', { class: 'bubble img' }, chatImage(m, m.image, 260, true));
  if (m.kind === 'sticker') return h('div', { class: 'bubble img sticker' }, chatImage(m, m.image, 140, false));
  if (m.kind === 'file') return fileCard(m);
  if (m.kind === 'card') return cardBubble(m);
  return h('div', { class: 'bubble' },
    m.root_id && !m.quote ? h('div', { class: 'bubble-reply' }, '回复话题') : null,
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
  S.replyTo = { root_id: m.root_id || m.msg_id, sender: m.me ? '我' : m.sender, content: (m.content || '').slice(0, 80) };
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

function renderMsgs(mode) {
  const box = S.body;
  if (!box) return;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  const fromBottom = box.scrollHeight - box.scrollTop;
  const rows = [];
  let prev = null, divider = null;
  for (const m of S.msgs) {
    if (m.kind === 'system' && !m.content) continue;  // 没有提示文字的系统消息
    if (!divider && isFirstUnread(m)) {
      divider = h('div', { class: 'unread-divider' }, h('span', {}, '以下为新消息'));
      rows.push(divider); prev = null;
    }
    if (!prev || m.time - prev.time > 300) { rows.push(h('div', { class: 'time-divider' }, msgTime(m.time))); prev = null; }
    rows.push(msgRow(m, prev));
    prev = m;
  }
  put(box, S.hasMore ? h('div', { class: 'chat-older' }, h('button', { onclick: olderMsgs }, '查看更早的消息')) : null,
    rows.length ? rows : h('div', { class: 'sys-msg' }, '还没有消息'));
  if (mode === 'unread' && divider) box.scrollTop = Math.max(0, divider.offsetTop - 12);
  else if (mode === 'bottom' || mode === 'unread' || (mode === 'keep-bottom' && nearBottom)) box.scrollTop = box.scrollHeight;
  else if (mode === 'keep-offset') box.scrollTop = box.scrollHeight - fromBottom;
}

async function sendMsg() {
  const input = S.input;
  const text = input.value.trim();
  if (!text || !S.chat) return;
  input.disabled = true;
  const r = await act(() => api(`/api/im/chat/${S.chatId}/send`, { method: 'POST', body: { text, root_id: S.replyTo ? S.replyTo.root_id : null } }));
  input.disabled = false;
  if (!r) return;
  if (r.kept_unread === false) toast(r.newly_read ? `已发出，但有 ${r.newly_read} 条别人的消息变成了已读` : '已发出，但这次没能匿名发送：你在这个会话的未读被清掉了，对方看不到已读');
  else if (r.kept_unread == null) toast('已发出，但没能核对别人的消息是否仍是未读');
  input.value = ''; S.grow(); S.replyTo = null; renderReply(); input.focus();
  await pollChat(S.seq);
  S.body.scrollTop = S.body.scrollHeight;
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
  const me = await act(() => api('/api/im/me'));
  if (!me) return;
  if (S.me) { route(); return; }  // 登录过期后重新登录: 定时器已经在跑
  S.me = me;
  put($('me'), avatar(me.avatar, me.name, 36));
  $('me').title = me.name;
  renderFilters();
  renderFeed();
  act(refreshFeed);
  setInterval(() => refreshFeed().catch(() => {}), 8000);
  setInterval(checkChanges, 2000);
  route();
}

function route() {
  const [, tab, arg] = (location.hash || '#/im').split('/');
  if (tab === 'twin') { showTab('twin'); return; }
  showTab('im');
  if (arg && arg !== S.chatId) openChat(arg);
  else if (!arg && S.chatId !== '') closeChat();
  else if (!arg) emptyChat('看消息不会标为已读，对方也看不到');
}

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

(async () => {
  // 登录链接形如 /#login=<令牌>: 片段不会发给服务器, 换成 cookie 后立即从地址栏抹掉
  const m = location.hash.match(/^#login=(.+)$/);
  if (m) {
    history.replaceState(null, '', location.pathname + '#/im');
    await login(decodeURIComponent(m[1])).catch(e => toast(e.message));
  }
  start();
})();
