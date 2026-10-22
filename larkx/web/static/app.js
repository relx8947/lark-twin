// lark-twin 控制台。聊天内容来自外部, 一律用 textContent 渲染, 不拼 HTML。
'use strict';

const $main = document.getElementById('main');
let timers = [];
let chatNames = {};

// ---- 基础工具 ----

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'style') el.style.cssText = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

// replaceChildren 不会展开数组、会把 null 变成文字; 统一走这里
function put(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false)
    .map(k => (k instanceof Node ? k : document.createTextNode(String(k)))));
  return el;
}

function svg(tag, attrs) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json', 'X-Larkx': '1' },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: 'same-origin',
  });
  if (r.status === 401) { showLogin(); throw new Error('未登录'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toDate(t) {
  if (t == null || t === '' || t === 0) return null;
  if (typeof t === 'number') return new Date(t * 1000);
  return new Date(String(t).replace(' ', 'T'));
}
function fmt(t, withDate = true) {
  const d = toDate(t);
  if (!d || isNaN(d)) return '';
  const p = n => String(n).padStart(2, '0');
  const time = `${p(d.getHours())}:${p(d.getMinutes())}`;
  return withDate ? `${p(d.getMonth() + 1)}-${p(d.getDate())} ${time}` : time;
}
function ago(t) {
  const d = toDate(t);
  if (!d) return '';
  const s = (Date.now() - d) / 1000;
  if (s < 60) return '刚刚';
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return fmt(t);
}
function dur(sec) {
  if (!sec) return '';
  if (sec % 86400 === 0) return `${sec / 86400} 天`;
  if (sec % 3600 === 0) return `${sec / 3600} 小时`;
  return `${Math.round(sec / 60)} 分钟`;
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), 2600);
}

async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(typeof ok === 'function' ? ok(r) : ok); return r; }
  catch (e) { toast('失败: ' + e.message); }
}

function every(ms, fn) { fn(); timers.push(setInterval(fn, ms)); }

function page(title, ...right) {
  put($main, h('div', { class: 'head' }, h('h1', {}, title), h('div', { class: 'grow' }), ...right));
}
function card(title, ...kids) {
  const c = h('section', { class: 'card' }, title ? h('h2', {}, title) : null, ...kids);
  $main.append(c);
  return c;
}

async function loadChats() {
  try { for (const c of await api('/api/chats')) chatNames[c.chat_id] = c.name; } catch (e) { /* 登录前忽略 */ }
}
const chatLabel = (id, name) => name || chatNames[id] || (id ? `会话 ${id}` : '');

const KIND = { message: '消息', task: '任务', say: '终端指令', system: '维护', web: '网页' };
// daemon: 不经过数字人、由 daemon 直接发的(没权限的人想唤起时的自动回复)
const ACTOR = { agent: '数字人', daemon: '自动回复' };
const STATUS = { pending: '排队中', running: '处理中', done: '已完成', failed: '失败', cancelled: '已取消', expired: '已过期' };

// ---- 登录 ----

// 登录链接形如 /#login=<令牌>: 片段不会发给服务器, 换成 cookie 后立即从地址栏抹掉
async function login(token) {
  const r = await fetch('/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }), credentials: 'same-origin',
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || '登录失败');
}

async function loginFromHash() {
  const m = location.hash.match(/^#login=(.+)$/);
  if (!m) return;
  history.replaceState(null, '', location.pathname + '#overview');
  await login(decodeURIComponent(m[1])).catch(e => toast(e.message));
}

function showLogin() {
  timers.forEach(clearInterval); timers = [];
  const input = h('input', { type: 'password', placeholder: '令牌', style: 'flex:1' });
  const go = () => { if (input.value.trim()) login(input.value.trim()).then(loadChats).then(route, e => toast(e.message)); };
  input.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
  put($main, h('div', { class: 'login card' },
    h('h2', {}, '🔒 需要登录'),
    h('p', { class: 'sec' }, '在服务器上运行 ', h('span', { class: 'mono' }, 'larkx web url'), ' 获取登录链接直接打开；或者把令牌粘贴到这里。'),
    h('div', { class: 'row' }, input, h('button', { class: 'btn primary', onclick: go }, '登录'))));
}

// ---- 总览 ----

function statusPills(o) {
  const d = (o.services || {}).daemon || {};
  const pills = [h('span', { class: 'pill ' + (d.active ? 'good' : 'bad') }, h('i'), d.active ? '数字人运行中' : '数字人未运行')];
  if (o.login && !o.login.ok) pills.push(h('span', { class: 'pill bad' }, h('i'), '飞书登录失效'));
  pills.push(h('span', { class: 'pill' }, o.backend === 'codex' ? 'Codex' : 'Claude Code'));
  return pills;
}

function hourlyChart(data) {
  const wrap = h('div', { class: 'chart' });
  const tip = h('div', { class: 'tip', hidden: true });
  const table = h('table', { hidden: true },
    h('thead', {}, h('tr', {}, h('th', {}, '时段'), h('th', { class: 'num' }, '交给数字人'), h('th', { class: 'num' }, '它发出的'))),
    h('tbody', {}, data.filter(d => d.received || d.sent).map(d =>
      h('tr', {}, h('td', {}, d.hour), h('td', { class: 'num' }, d.received), h('td', { class: 'num' }, d.sent)))));
  const graph = h('div');
  wrap.append(graph, tip);

  function draw() {
    const W = Math.max(graph.clientWidth || wrap.clientWidth || 700, 320), H = 200, L = 30, B = 22, T = 8;
    const peak = Math.max(1, ...data.map(d => Math.max(d.received, d.sent)));
    const step = Math.max(1, Math.ceil(peak / 4));
    const top = step * Math.ceil(peak / step);
    const y = v => T + (H - T - B) * (1 - v / top);
    const band = (W - L) / data.length;
    const bw = Math.max(3, Math.min(10, (band - 8) / 2));
    const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': '最近 24 小时每小时的消息数' });
    for (let v = 0; v <= top; v += step) {
      s.append(svg('line', { class: 'gridline', x1: L, x2: W, y1: y(v), y2: y(v) }));
      const t = svg('text', { class: 'axis', x: L - 6, y: y(v) + 4, 'text-anchor': 'end' }); t.textContent = v; s.append(t);
    }
    const bar = (x, v, color) => {
      if (!v) return null;
      const yy = y(v), hh = H - B - yy, r = Math.min(4, bw / 2, hh);
      return svg('path', { fill: color, d: `M${x},${yy + hh}V${yy + r}Q${x},${yy} ${x + r},${yy}H${x + bw - r}Q${x + bw},${yy} ${x + bw},${yy + r}V${yy + hh}Z` });
    };
    data.forEach((d, i) => {
      const x0 = L + i * band + (band - (2 * bw + 2)) / 2;
      const g = svg('g');
      [bar(x0, d.received, 'var(--series-1)'), bar(x0 + bw + 2, d.sent, 'var(--series-2)')].forEach(b => b && g.append(b));
      const hit = svg('rect', { class: 'band', x: L + i * band, y: T, width: band, height: H - T - B, tabindex: 0 });
      const show = () => {
        g.classList.add('hot');
        put(tip, h('div', { class: 't' }, d.hour), h('div', {}, `交给数字人 ${d.received}`), h('div', {}, `它发出的 ${d.sent}`));
        tip.hidden = false;
        const px = (L + (i + 0.5) * band) / W * wrap.clientWidth;
        tip.style.left = Math.min(Math.max(px - 60, 0), wrap.clientWidth - 140) + 'px';
        tip.style.top = '0px';
      };
      const hide = () => { g.classList.remove('hot'); tip.hidden = true; };
      hit.addEventListener('pointermove', show); hit.addEventListener('focus', show);
      hit.addEventListener('pointerleave', hide); hit.addEventListener('blur', hide);
      s.append(hit, g);
      if (i % 3 === 0) {
        const t = svg('text', { class: 'axis', x: L + (i + 0.5) * band, y: H - 6, 'text-anchor': 'middle' });
        t.textContent = d.hour; s.append(t);
      }
    });
    put(graph, s);
  }
  requestAnimationFrame(draw);
  window.addEventListener('resize', draw);
  const toggle = h('button', { class: 'btn small', onclick: () => {
    table.hidden = !table.hidden; graph.hidden = !table.hidden; toggle.textContent = table.hidden ? '表格' : '图表';
  } }, '表格');
  return { node: h('div', {}, h('div', { class: 'row', style: 'margin-bottom:8px' },
    h('div', { class: 'legend' }, h('span', { style: '--c:var(--series-1)' }, '交给数字人的消息'), h('span', { style: '--c:var(--series-2)' }, '它发出的消息')),
    h('div', { style: 'flex:1' }), toggle), wrap, table) };
}

function turnCard(o) {
  const t = o.turn;
  if (!t) return h('div', { class: 'row' }, h('span', { class: 'pill' }, h('i'), '空闲'), h('span', { class: 'muted' }, o.inbox_pending ? `排队 ${o.inbox_pending} 条` : '没有排队的消息'));
  return h('div', { class: 'row' },
    h('span', { class: 'pill warn' }, h('i'), `正在处理${KIND[t.kind] || t.kind}`),
    h('span', {}, chatLabel(t.chat_id)), h('span', { class: 'tag' + (t.is_owner ? ' owner' : '') }, t.is_owner ? '本人发起' : '访客'),
    h('span', { class: 'muted' }, `已 ${Math.round(t.elapsed)} 秒`), h('div', { style: 'flex:1' }),
    h('button', { class: 'btn danger', onclick: () => confirm('打断当前任务并清空排队的消息?') && act(() => api('/api/control', { method: 'POST', body: { action: 'interrupt' } }), r => r.result) }, '打断'));
}

async function viewOverview() {
  page('总览');
  const head = $main.querySelector('.head');
  const tiles = h('div', { class: 'tiles' });
  $main.append(tiles);
  const now = card('现在');
  const chartCard = card('最近 24 小时');
  const recent = card('最近的对话');
  let chartDrawn = false;
  every(5000, async () => {
    const o = await api('/api/overview');
    put(head, h('h1', {}, '总览'), h('div', { class: 'grow' }), ...statusPills(o));
    const tile = (v, k) => h('div', { class: 'tile' }, h('div', { class: 'v' }, v), h('div', { class: 'k' }, k));
    put(tiles, tile(o.today.handled, '今天交给它'), tile(o.today.sent, '今天它发出'), tile(o.today.denied, '今天被拦截'),
      tile(o.tasks_pending, '待执行任务'), tile(o.takeovers.length, '接管中'), tile(o.memory_files.length, '记忆文件'));
    put(now, h('h2', {}, '现在'), turnCard(o),
      o.takeovers.length ? h('div', { class: 'meta', style: 'margin-top:10px' }, '接管中:', o.takeovers.map(t => h('span', { class: 'tag' }, `${chatLabel(t.chat_id)} 到 ${fmt(t.until, false)}`))) : null);
    if (!chartDrawn) { chartCard.append(hourlyChart(o.hourly).node); chartDrawn = true; }
  });
  every(15000, async () => {
    const items = (await api('/api/conversations?limit=6'));
    put(recent, h('h2', {}, '最近的对话', h('span', { class: 'grow' }), h('a', { href: '#conversations' }, '全部')),
      items.length ? h('div', { class: 'list' }, items.map(convItem)) : h('div', { class: 'empty' }, '还没有交给数字人的消息'));
  });
}

// ---- 对话 / 动态 ----

// 长内容默认折叠成几行, 点一下展开
function clamp(text) {
  const el = h('div', { class: 'body clamp' }, text);
  // 真的超出了才显示渐隐和"点击展开"
  requestAnimationFrame(() => { if (el.scrollHeight > el.clientHeight + 2) { el.classList.add('more'); el.title = '点击展开'; } });
  el.addEventListener('click', () => el.classList.contains('more') && el.classList.toggle('open'));
  return el;
}

// 已撤回的显示灰色标签; 没撤回的给一个悬停才出现的撤回按钮, 免得满屏红字看着像状态
function recallCtl(r) {
  if (r.recalled) return h('span', { class: 'tag' }, '已撤回');
  if (!r.ok || !r.msg_id) return null;
  return h('button', { class: 'btn small recall', title: '在飞书里撤回这条消息', onclick: () => confirm('撤回这条消息?') &&
    act(() => api('/api/recall', { method: 'POST', body: { msg_id: r.msg_id } }), '已撤回').then(route) }, '撤回');
}

function convItem(c) {
  const who = c.is_owner ? h('span', { class: 'tag owner' }, '本人') : h('span', { class: 'tag' }, c.takeover ? '接管中·对方' : '访客');
  return h('div', { class: 'item' },
    h('div', { class: 'meta' }, fmt(c.time), h('b', { class: 'sec' }, chatLabel(c.chat_id, c.chat_name)), c.sender_name, who,
      c.status !== 'done' ? h('span', { class: 'tag' + (c.status === 'failed' ? ' bad' : '') }, STATUS[c.status] || c.status) : null),
    h('div', { class: 'body' }, c.body),
    c.replies.map(r => h('div', { class: 'reply' },
      h('div', { class: 'meta' }, '↳ ' + fmt(r.time, false), r.chat_id !== c.chat_id ? h('span', { class: 'tag' }, '发到 ' + chatLabel(r.chat_id, r.chat_name)) : null,
        r.ok ? null : h('span', { class: 'tag bad' }, '被拦截'), recallCtl(r)),
      h('div', { class: 'body' + (r.ok && !r.recalled ? '' : ' muted') }, r.ok ? r.content : `${r.content}\n(${r.error})`))));
}

async function viewConversations() {
  page('对话', h('span', { class: 'muted' }, '交给数字人的每条消息，以及它在那一轮里的回复'));
  const c = card(null);
  every(10000, async () => {
    const items = await api('/api/conversations?limit=60');
    put(c, items.length ? h('div', { class: 'list' }, items.map(convItem)) : h('div', { class: 'empty' }, '还没有交给数字人的消息'));
  });
}

async function viewActivity() {
  page('动态', h('span', { class: 'muted' }, '通过 larkx 对外做过的每个动作'));
  const c = card(null);
  every(10000, async () => {
    const rows = await api('/api/activity?limit=150');
    put(c, rows.length ? h('table', {},
      h('thead', {}, h('tr', {}, ['时间', '谁', '动作', '会话', '内容', '结果', ''].map(t => h('th', {}, t)))),
      h('tbody', {}, rows.map(r => h('tr', { class: 'act' },
        h('td', { class: 'muted nowrap' }, fmt(r.time)),
        h('td', { class: 'nowrap' }, ACTOR[r.actor] || '本人', r.turn_kind ? h('div', { class: 'muted', style: 'font-size:11.5px' }, KIND[r.turn_kind] || r.turn_kind) : null),
        h('td', { class: 'nowrap' }, { send: '发消息', recall: '撤回', whitelist: '白名单', deny_reply: '没权限回复' }[r.action] || r.action),
        h('td', { style: 'min-width:120px;max-width:220px' }, h('div', { class: 'lines2', title: chatLabel(r.chat_id, r.chat_name) }, chatLabel(r.chat_id, r.chat_name))),
        h('td', { style: 'width:50%', class: r.recalled ? 'muted' : null }, clamp(r.content)),
        h('td', { class: 'nowrap' }, r.ok ? '✓' : h('span', { class: 'tag bad', title: r.error }, '✗ ' + (r.error || '').slice(0, 40))),
        h('td', { class: 'nowrap' }, r.action === 'send' || r.action === 'deny_reply' ? recallCtl(r) : null))))) : h('div', { class: 'empty' }, '还没有记录'));
  });
}

// ---- 任务 / 接管 ----

function chatSelect(allowEmpty) {
  const sel = h('select', {}, allowEmpty ? h('option', { value: '' }, '(不关联会话)') : null,
    Object.entries(chatNames).map(([id, name]) => h('option', { value: id }, name || `会话 ${id}`)));
  return sel;
}

async function viewTasks() {
  page('任务');
  const list = card('待执行');
  let showAll = false;
  const prompt = h('textarea', { rows: 3, placeholder: '到点时让数字人做什么，例如：提醒我给张三回电话（在我和自己的私聊里提醒）' });
  const mode = h('select', {}, h('option', { value: 'in' }, '多久之后'), h('option', { value: 'at' }, '指定时间'));
  const when = h('input', { type: 'text', placeholder: '30m / 2h / 1d', style: 'width:150px' });
  mode.addEventListener('change', () => { when.placeholder = mode.value === 'in' ? '30m / 2h / 1d' : '10-22 09:30 或 09:30'; });
  const evry = h('input', { type: 'text', placeholder: '不重复；或 1d / 1w', style: 'width:150px' });
  const chat = chatSelect(true);
  card('新建任务', prompt, h('div', { class: 'row', style: 'margin-top:10px' },
    h('div', { class: 'field' }, h('label', {}, '时间'), h('div', { class: 'row' }, mode, when)),
    h('div', { class: 'field' }, h('label', {}, '周期'), evry),
    h('div', { class: 'field' }, h('label', {}, '关联会话（到点在这里通知）'), chat),
    h('div', { style: 'flex:1' }),
    h('button', { class: 'btn primary', style: 'align-self:flex-end', onclick: () => act(async () => {
      const body = { prompt: prompt.value, every: evry.value.trim() || undefined, chat_id: chat.value || undefined };
      body[mode.value] = when.value.trim();
      const r = await api('/api/tasks', { method: 'POST', body });
      prompt.value = ''; refresh(); return r;
    }, r => `已安排任务 #${r.id}`) }, '安排')));

  async function refresh() {
    const rows = await api('/api/tasks' + (showAll ? '?all=1' : ''));
    const toggle = h('button', { class: 'btn small', onclick: () => { showAll = !showAll; refresh(); } }, showAll ? '只看待执行' : '包括已完成');
    put(list, h('h2', {}, showAll ? '全部任务' : '待执行', h('span', { class: 'grow' }), toggle),
      rows.length ? h('div', { class: 'list' }, rows.map(t => h('div', { class: 'item' },
        h('div', { class: 'meta' }, `#${t.id}`, h('span', { class: 'tag' }, t.kind === 'say' ? '终端指令' : '任务'),
          h('span', { class: 'tag' + (t.status === 'failed' ? ' bad' : '') }, STATUS[t.status] || t.status),
          t.due_at ? fmt(t.due_at) : '', t.every_sec ? `每 ${dur(t.every_sec)}` : '', t.chat_id ? '→ ' + chatLabel(t.chat_id) : '',
          t.creator === 'agent' ? h('span', { class: 'tag' }, t.by_owner ? '数字人代本人安排' : '访客让它安排') : null,
          h('span', { style: 'flex:1' }),
          t.status === 'pending' ? h('button', { class: 'btn small danger', onclick: () => confirm(`取消任务 #${t.id}?`) &&
            act(async () => { await api(`/api/tasks/${t.id}`, { method: 'DELETE' }); refresh(); }, '已取消') }, '取消') : null),
        h('div', { class: 'body' }, t.prompt),
        t.result ? h('div', { class: 'reply' }, h('div', { class: 'body sec' }, t.result)) : null))) : h('div', { class: 'empty' }, '没有任务'));
  }
  every(15000, refresh);
}

async function viewTakeovers() {
  page('接管');
  const list = card('接管中');
  const chat = chatSelect(false);
  const forv = h('input', { type: 'text', value: '1h', style: 'width:100px' });
  const goal = h('input', { type: 'text', placeholder: '交代给数字人的目标，例如：帮我和他约下周二下午开会', style: 'flex:1;min-width:260px' });
  card('开始接管',
    h('p', { class: 'sec', style: 'margin-top:0' }, '接管期间，对方发来的每条消息都会由数字人以你的身份回复（按访客规则），它发出的每条都会抄送到你的私聊；结束后它会在你的私聊发一份总结。'),
    h('div', { class: 'row' }, h('div', { class: 'field' }, h('label', {}, '会话'), chat), h('div', { class: 'field' }, h('label', {}, '时长'), forv),
      h('div', { class: 'field', style: 'flex:1' }, h('label', {}, '目标'), goal),
      h('button', { class: 'btn primary', style: 'align-self:flex-end', onclick: () => confirm(`接管「${chat.selectedOptions[0]?.textContent}」${forv.value}?`) &&
        act(async () => { await api('/api/takeovers', { method: 'POST', body: { chat_id: chat.value, for: forv.value, goal: goal.value } }); refresh(); }, '已开始接管') }, '接管')));
  async function refresh() {
    const rows = await api('/api/takeovers');
    put(list, h('h2', {}, '接管中'), rows.length ? h('div', { class: 'list' }, rows.map(t => h('div', { class: 'item' },
      h('div', { class: 'meta' }, h('b', { class: 'sec' }, chatLabel(t.chat_id, t.chat_name)), `到 ${fmt(t.until)}`, h('span', { style: 'flex:1' }),
        h('button', { class: 'btn small danger', onclick: () => act(async () => { await api(`/api/takeovers/${encodeURIComponent(t.chat_id)}`, { method: 'DELETE' }); refresh(); }, '已结束，总结稍后发到你的私聊') }, '结束')),
      t.goal ? h('div', { class: 'body' }, t.goal) : null))) : h('div', { class: 'empty' }, '当前没有接管中的会话'));
  }
  every(10000, refresh);
}

// ---- 白名单 ----

// 头像来自飞书的公开 CDN; 加载不了时退回名字的第一个字
function avatar(p) {
  const initial = h('span', { class: 'avatar ph', 'aria-hidden': 'true' }, (p.name || '?').slice(0, 1));
  if (!p.avatar) return initial;
  return h('img', { class: 'avatar', src: p.avatar, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer',
    onerror: e => e.target.replaceWith(initial) });
}

function personRow(p, attrs, ...right) {
  return h('div', Object.assign({ class: 'person' }, attrs), avatar(p),
    h('div', { class: 'who' },
      h('div', { class: 'name' }, h('b', {}, p.name || '(无名字)'), p.department ? h('span', { class: 'sec' }, p.department) : null),
      h('div', { class: 'muted mono' }, 'user_id ' + p.user_id)),
    h('div', { style: 'flex:1' }), ...right);
}

async function viewWhitelist() {
  page('白名单', h('span', { class: 'muted' }, '除了你，还有谁能唤起数字人'));
  const rule = card(null);
  const q = h('input', { type: 'text', placeholder: '输入名字搜人，例如：张伟', autocomplete: 'off', style: 'flex:1;min-width:220px' });
  const results = h('div', { class: 'people' });
  card('添加', h('p', { class: 'sec', style: 'margin-top:0' }, '搜到同名的人时，看头像和部门分清是谁，点一下就加进白名单。'),
    h('div', { class: 'row' }, q), results);
  const list = card('白名单');
  let st = { entries: [] }, found = null, seq = 0, debounce = null;
  const listed = () => new Set(st.entries.map(e => e.user_id));

  function draw() {
    put(rule, h('div', { class: 'rule' }, st.rule),
      h('p', { class: 'muted', style: 'margin:8px 0 0' },
        st.entries.length ? '白名单里的人和其他人一样按访客对待，不会拿到你的权限。'
          : `白名单是空的，按 LARKX_TRIGGER_OWNER_ONLY（现在${st.owner_only ? '打开' : '没打开'}）决定。加了人之后，就只有你和白名单里的人能唤起。`,
        '改动立即生效，不用重启；数字人自己改不了白名单。'),
      st.deny ? h('p', { class: 'muted', style: 'margin:6px 0 0' }, st.deny) : null);
    put(list, h('h2', {}, `白名单（${st.entries.length}）`),
      st.entries.length ? h('div', { class: 'people' }, st.entries.map(p => personRow(p, {},
        h('span', { class: 'muted', style: 'font-size:12px' }, fmt(p.added_at) + (p.added_by === 'cli' ? ' 命令行添加' : ' 网页添加')),
        h('button', { class: 'btn small danger', onclick: () => confirm(`把 ${p.name || p.user_id} 移出白名单?`) && act(async () => {
          st = await api(`/api/whitelist/${encodeURIComponent(p.user_id)}`, { method: 'DELETE' }); draw();
        }, `已把 ${p.name || p.user_id} 移出白名单`) }, '移除'))))
        : h('div', { class: 'empty' }, '还没有人。在上面搜名字添加。'));
    if (found == null) { put(results); return; }
    const ids = listed();
    put(results, found.people.length ? found.people.map(p => {
      if (p.is_owner) return personRow(p, {}, h('span', { class: 'tag owner' }, '你自己，一直能唤起'));
      if (ids.has(p.user_id)) return personRow(p, {}, h('span', { class: 'tag' }, '已在白名单'));
      return personRow(p, { class: 'person pick', title: `把 ${p.name} 加进白名单`, onclick: () => add(p) },
        h('button', { class: 'btn small primary' }, '添加'));
    }) : h('div', { class: 'empty' }, `没有搜到「${found.query}」`));
  }

  const add = p => act(async () => {
    st = await api('/api/whitelist', { method: 'POST', body: { user_id: p.user_id, name: p.name, department: p.department, avatar_key: p.avatar_key } });
    draw();
  }, `已把 ${p.name} 加进白名单`);

  async function search() {
    const text = q.value.trim(), my = ++seq;
    if (!text) { found = null; draw(); return; }
    put(results, h('div', { class: 'empty' }, '搜索中…'));
    try {
      const r = await api('/api/people?q=' + encodeURIComponent(text));
      if (my === seq) { found = r; draw(); }
    } catch (e) { if (my === seq) put(results, h('div', { class: 'empty' }, '搜索失败: ' + e.message)); }
  }
  q.addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(search, 400); });
  q.addEventListener('keydown', e => { if (e.key === 'Enter') { clearTimeout(debounce); search(); } });
  // 命令行那边改了, 这里过一会儿也会跟上
  every(20000, async () => { st = await api('/api/whitelist'); draw(); });
}

// ---- 记忆 / 人设 ----

function diffView(text) {
  return h('pre', { class: 'diff' }, text.split('\n').map(l => h('span', {
    class: l.startsWith('+') && !l.startsWith('+++') ? 'add' : l.startsWith('-') && !l.startsWith('---') ? 'del' : l.startsWith('@@') ? 'hunk' : null,
  }, l + '\n')));
}

async function viewMemory() {
  page('记忆', h('span', { class: 'muted' }, '数字人的长期记忆，每次变化都有记录'));
  const wrap = h('div', { class: 'split' });
  const files = h('div', { class: 'card files' });
  const editorCard = h('div', { class: 'card' });
  wrap.append(files, editorCard);
  $main.append(wrap);
  const hist = card('变化记录');
  let current = null;

  async function load() {
    const list = await api('/api/memory');
    if (!current && list.length) current = list[0].name;
    put(files, h('h2', {}, '文件'), list.map(f => h('a', { href: '#memory', class: f.name === current ? 'on' : null,
      onclick: e => { e.preventDefault(); current = f.name; load(); } }, f.name)),
      h('button', { class: 'btn small', style: 'margin-top:10px', onclick: () => {
        const name = prompt('新文件名（以 .md 结尾）', 'notes.md');
        if (name) act(async () => { await api(`/api/memory/${encodeURIComponent(name)}`, { method: 'PUT', body: { content: '' } }); current = name; load(); }, '已新建');
      } }, '新建'));
    const f = list.find(x => x.name === current);
    if (!f) { put(editorCard, h('div', { class: 'empty' }, '还没有记忆')); return; }
    const ta = h('textarea', { class: 'editor' }); ta.value = f.content;
    put(editorCard, h('h2', {}, f.name, h('span', { class: 'grow' }), h('span', { class: 'muted', style: 'font-weight:400' }, '更新于 ' + fmt(f.mtime)),
      f.name !== 'MEMORY.md' ? h('button', { class: 'btn small danger', onclick: () => confirm(`删除 ${f.name}?`) &&
        act(async () => { await api(`/api/memory/${encodeURIComponent(f.name)}`, { method: 'DELETE' }); current = null; load(); }, '已删除') }, '删除') : null,
      h('button', { class: 'btn small primary', onclick: () => act(async () => { await api(`/api/memory/${encodeURIComponent(f.name)}`, { method: 'PUT', body: { content: ta.value } }); loadHist(); }, '已保存') }, '保存')), ta);
  }
  async function loadHist() {
    const rows = await api('/api/history');
    const detail = h('div');
    put(hist, h('h2', {}, '变化记录'), h('table', {}, h('tbody', {}, rows.slice(0, 40).map(r => h('tr', {},
      h('td', { class: 'muted', style: 'white-space:nowrap' }, r.time.slice(5, 16)), h('td', {}, r.subject), h('td', { class: 'muted' }, r.stat),
      h('td', {}, h('button', { class: 'btn small', onclick: async () => put(detail, diffView((await api(`/api/history/${r.sha}`)).diff)) }, '查看')))))), detail);
  }
  await load(); await loadHist();
}

async function viewPersona() {
  page('人设', h('span', { class: 'muted' }, '数字人的身份和行为准则（CLAUDE.md）'));
  const r = await api('/api/persona');
  const ta = h('textarea', { class: 'editor', style: 'min-height:60vh' }); ta.value = r.content;
  card(null, h('div', { class: 'row', style: 'margin-bottom:10px' }, h('span', { class: 'sec' }, '改完保存后，新会话一定会读到新内容；需要时在"指挥"里开新会话。'), h('div', { style: 'flex:1' }),
    h('button', { class: 'btn primary', onclick: () => act(() => api('/api/persona', { method: 'PUT', body: { content: ta.value } }), '已保存') }, '保存')), ta);
}

// ---- 日志 / 指挥 ----

async function viewLogs() {
  page('日志');
  const auto = h('input', { type: 'checkbox', checked: true });
  const pre = h('pre', { class: 'log' });
  card(null, h('div', { class: 'row', style: 'margin-bottom:10px' }, h('label', { class: 'sec' }, auto, ' 自动刷新')), pre);
  every(3000, async () => {
    if (!auto.checked) return;
    const { lines } = await api('/api/logs?lines=400');
    const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
    put(pre, ...lines.map(l => h('span', { class: /\| (ERROR|CRITICAL)/.test(l) ? 'err' : /\| WARNING/.test(l) ? 'warn' : null }, l + '\n')));
    if (stick) pre.scrollTop = pre.scrollHeight;
  });
}

async function viewCommand() {
  page('指挥');
  const ta = h('textarea', { rows: 4, placeholder: '以本人身份直接给数字人下指令，例如：帮我看一下今天各个群里有没有人找我，整理成要点' });
  const out = h('div', { class: 'body sec', style: 'margin-top:12px' });
  const send = h('button', { class: 'btn primary', onclick: async () => {
    const text = ta.value.trim(); if (!text) return;
    send.disabled = true; out.textContent = '已交给数字人，等它处理…';
    const r = await act(() => api('/api/say', { method: 'POST', body: { text } }));
    if (!r) { send.disabled = false; return; }
    const started = Date.now();
    const poll = setInterval(async () => {
      const t = await api(`/api/tasks/${r.id}`).catch(() => null);
      if (t && (t.status === 'done' || t.status === 'failed')) { clearInterval(poll); send.disabled = false; out.textContent = t.result || '(数字人没有文字回答)'; }
      else if (Date.now() - started > 600000) { clearInterval(poll); send.disabled = false; out.textContent = '等了 10 分钟还没处理完，稍后在"任务"里看结果。'; }
    }, 1500);
    timers.push(poll);
  } }, '发送');
  card('和数字人说', ta, h('div', { class: 'row', style: 'margin-top:10px' }, h('div', { style: 'flex:1' }), send), out);
  const ctl = (label, action, msg) => h('button', { class: 'btn', onclick: () => confirm(msg) &&
    act(() => api('/api/control', { method: 'POST', body: { action } }), r => r.result) }, label);
  card('控制', h('div', { class: 'row' },
    ctl('打断当前任务', 'interrupt', '打断当前任务并清空排队的消息?'),
    ctl('开新会话', 'clear', '开一个新会话? 对话上下文会清空, 长期记忆保留。'),
    h('button', { class: 'btn danger', onclick: () => confirm('重启数字人服务?') && act(() => api('/api/restart', { method: 'POST' }), r => r.result) }, '重启数字人')),
    h('p', { class: 'muted', style: 'margin-bottom:0' }, '这些操作也可以在飞书里发 /ai /stop、/ai /clear 完成。'));
}

// ---- 路由 ----

const VIEWS = { overview: viewOverview, conversations: viewConversations, activity: viewActivity, tasks: viewTasks,
  takeovers: viewTakeovers, whitelist: viewWhitelist, memory: viewMemory, persona: viewPersona, logs: viewLogs, command: viewCommand };

async function route() {
  timers.forEach(clearInterval); timers = [];
  const name = (location.hash || '#overview').slice(1);
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.getAttribute('href') === '#' + name));
  try { await (VIEWS[name] || viewOverview)(); } catch (e) { if (e.message !== '未登录') toast('加载失败: ' + e.message); }
}

document.getElementById('theme').addEventListener('click', e => {
  e.preventDefault();
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === 'dark'
    : matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = dark ? 'light' : 'dark';
  localStorage.setItem('larkx-theme', document.documentElement.dataset.theme);
});
if (localStorage.getItem('larkx-theme')) document.documentElement.dataset.theme = localStorage.getItem('larkx-theme');
const start = () => loginFromHash().then(loadChats).then(route);
// 页面已经开着时再打开登录链接, 只有 # 后面变了, 页面不会重新加载: 这时也要换 cookie
window.addEventListener('hashchange', () => (location.hash.startsWith('#login=') ? start() : route()));
start();
