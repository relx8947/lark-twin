"""lark-twin 网页控制台: 看数字人在做什么、管理任务/接管/记忆, 必要时打断它。

只监听 127.0.0.1, 由系统 Nginx 反向代理出去。页面会展示聊天内容和记忆, 所以所有数据接口都要令牌:
打开 `larkx web url` 给出的链接后, 页面把 # 后面的令牌 POST 给 /login 换成 HttpOnly cookie
(令牌放在 # 片段里, 不会进 nginx 访问日志); 改动类请求还要带 X-Larkx 请求头, 防跨站伪造。
"""
import hmac
import json
import re
import secrets
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import service, state
from ..agent.home import commit_agent_home
from ..config import DATA_DIR, load_config
from ..storage import Storage
from ..storage.models import Action, InboxItem, Message

STATIC = Path(__file__).parent / 'static'
TOKEN_PATH = DATA_DIR / 'web_token'
COOKIE = 'larkx_token'
MEMORY_NAME = re.compile(r'^[\w\-.一-鿿]+\.md$')
_login_cache = {'at': 0, 'value': None}


def get_token(reset: bool = False) -> str:
    if TOKEN_PATH.exists() and not reset:
        return TOKEN_PATH.read_text(encoding='utf-8').strip()
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    TOKEN_PATH.write_text(token + '\n', encoding='utf-8')
    TOKEN_PATH.chmod(0o600)
    return token


def login_url(cfg: dict) -> str:
    base = (cfg.get('web_url') or f'http://127.0.0.1:{cfg["web_port"]}').rstrip('/')
    return f'{base}/#login={get_token()}'


def _json(data, status: int = 200) -> Response:
    return Response(json.dumps(data, ensure_ascii=False, default=str), status_code=status, media_type='application/json')


def _authorized(request: Request) -> bool:
    token = request.cookies.get(COOKIE) or request.headers.get('authorization', '').removeprefix('Bearer ').strip()
    return bool(token) and hmac.compare_digest(token, get_token())


def api(fn):
    """数据接口统一做鉴权; 改动类请求要求自定义请求头, 浏览器跨站提交带不上它。"""
    def endpoint(request: Request):
        if not _authorized(request):
            return _json({'error': '未登录'}, 401)
        if request.method != 'GET' and request.headers.get('x-larkx') != '1':
            return _json({'error': '缺少 X-Larkx 请求头'}, 403)
        try:
            return fn(request)
        except ValueError as e:
            return _json({'error': str(e)}, 400)
    return endpoint


# ---- 页面与登录 ----

def index(request: Request):
    return Response((STATIC / 'index.html').read_text(encoding='utf-8'), media_type='text/html',
                    headers={'Cache-Control': 'no-cache'})


def login(request: Request):
    token = str(request.state.body.get('token') or '')
    if not token or not hmac.compare_digest(token, get_token()):
        return _json({'error': '令牌无效。在服务器上运行 larkx web url 获取登录链接。'}, 401)
    resp = _json({'ok': True})
    resp.set_cookie(COOKIE, token, max_age=180 * 86400, httponly=True, samesite='strict')
    return resp


# ---- 只读接口 ----

OWNER_CHAT_NAME = '我自己（私聊）'


def _owner_chat() -> str:
    from ..notify import _load
    return _load().get('owner_chat', '')


def _chat_names(st: Storage) -> dict:
    names = {c.chat_id: c.name for c in st.list_chats(limit=500)}
    owner = _owner_chat()
    if owner and not names.get(owner):
        names[owner] = OWNER_CHAT_NAME
    return names


def _login_state():
    if time.time() - _login_cache['at'] > 300:
        from ..protocol import LarkAuth
        auth = LarkAuth()
        ok, msg = auth.validate(fetch_profile=False)
        _login_cache.update(at=time.time(), value={'ok': ok, 'message': msg, 'user_id': auth.user_id})
    return _login_cache['value']


def _hourly(st: Storage, hours: int = 24) -> list:
    """最近 24 小时每小时: 交给数字人的消息数、它发出的消息数。"""
    start = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)
    buckets = {start + timedelta(hours=i): [0, 0] for i in range(hours)}
    s = st.Session()
    try:
        for (ts,) in s.query(InboxItem.created_at).filter(InboxItem.created_at >= start):
            buckets[ts.replace(minute=0, second=0, microsecond=0)][0] += 1
        for (ts,) in s.query(Action.created_at).filter(Action.created_at >= start, Action.actor == 'agent',
                                                       Action.action == 'send', Action.ok == 1):
            buckets[ts.replace(minute=0, second=0, microsecond=0)][1] += 1
    finally:
        s.close()
    return [{'hour': k.strftime('%H:00'), 'received': v[0], 'sent': v[1]} for k, v in buckets.items()]


@api
def overview(request: Request):
    cfg = load_config()
    st = Storage()
    today = datetime.combine(date.today(), datetime.min.time())
    s = st.Session()
    try:
        inbox_pending = s.query(InboxItem).filter(InboxItem.status == 'pending').count()
        handled_today = s.query(InboxItem).filter(InboxItem.created_at >= today).count()
        denied_today = s.query(Action).filter(Action.actor == 'agent', Action.ok == 0, Action.created_at >= today).count()
        messages_today = s.query(Message).filter(Message.create_time >= today.timestamp()).count()
    finally:
        s.close()
    key = 'main:codex' if cfg['agent_backend'] == 'codex' else 'main'
    sess = st.session_get(key)
    turn = state.read_turn()
    home = Path(cfg['agent_home'])
    memory_files = sorted(p.name for p in (home / 'memory').glob('*.md'))
    units = {k: service.status(k) for k in service.UNITS} if service.available() else {}
    return _json({
        'now': datetime.now(),
        'backend': cfg['agent_backend'],
        'signature': cfg.get('reply_signature'),
        'trigger_prefix': (cfg.get('triggers') or {}).get('prefix'),
        'services': units,
        'login': _login_state(),
        'session': {'id': sess.agent_session_id if sess else '', 'turns': sess.msg_count if sess else 0,
                    'last_active': sess.last_active if sess else None},
        'turn': turn and dict(turn, elapsed=time.time() - turn['started_at']),
        'inbox_pending': inbox_pending,
        'today': {'handled': handled_today, 'sent': st.count_actions('agent', 'send', today), 'denied': denied_today,
                  'messages': messages_today},
        'tasks_pending': len([t for t in st.list_tasks() if t.status == 'pending']),
        'takeovers': [{'chat_id': t.chat_id, 'until': t.until, 'goal': t.goal} for t in st.takeover_list() if t.until > time.time()],
        'memory_files': memory_files,
        'hourly': _hourly(st),
    })


@api
def activity(request: Request):
    st = Storage()
    names = _chat_names(st)
    rows = st.list_actions(limit=int(request.query_params.get('limit', 100)))
    rows.reverse()
    return _json([{'id': r.id, 'time': r.created_at, 'actor': r.actor, 'action': r.action, 'chat_id': r.chat_id,
                   'chat_name': names.get(r.chat_id, ''), 'msg_id': r.msg_id, 'content': r.content, 'ok': bool(r.ok),
                   'error': r.error, 'turn_kind': r.turn_kind, 'turn_ref': r.turn_ref} for r in rows])


@api
def conversations(request: Request):
    """交给数字人的每条消息, 以及它在那一轮里发出的回复。"""
    st = Storage()
    names = _chat_names(st)
    items = st.list_inbox(limit=int(request.query_params.get('limit', 40)))
    out = []
    for it in items:
        try:
            p = json.loads(it.payload)
        except json.JSONDecodeError:
            continue
        m = p.get('msg') or {}
        out.append({'id': it.id, 'msg_id': it.msg_id, 'status': it.status, 'time': it.created_at,
                    'chat_id': it.chat_id, 'chat_name': p.get('chat_name') or names.get(it.chat_id, ''),
                    'sender_name': p.get('sender_name', ''), 'is_owner': p.get('is_owner', False),
                    'body': p.get('body', ''), 'takeover': bool(p.get('note') and '接管' in p.get('note', '')),
                    'content': m.get('content', ''), 'replies': []})
    by_ref = {c['msg_id']: c for c in out}
    for a in st.actions_for_turns(list(by_ref)):
        if a.action == 'send':
            by_ref[a.turn_ref]['replies'].append({'time': a.created_at, 'chat_id': a.chat_id, 'chat_name': names.get(a.chat_id, ''),
                                                  'content': a.content, 'ok': bool(a.ok), 'error': a.error, 'msg_id': a.msg_id})
    return _json(out)


@api
def chats(request: Request):
    owner = _owner_chat()
    return _json([{'chat_id': c.chat_id, 'name': c.name or (OWNER_CHAT_NAME if c.chat_id == owner else ''),
                   'chat_type': c.chat_type, 'updated_at': c.updated_at} for c in Storage().list_chats(limit=100)])


@api
def mentions(request: Request):
    from ..protocol import LarkAuth
    since = time.time() - state.parse_duration(request.query_params.get('since', '24h'))
    st = Storage()
    uid = LarkAuth().user_id
    return _json(st.mentions(uid, st.owner_name(uid), int(since)))


def _task_dict(t) -> dict:
    return {'id': t.id, 'kind': t.kind, 'status': t.status, 'due_at': t.due_at, 'every_sec': t.every_sec, 'chat_id': t.chat_id,
            'by_owner': bool(t.by_owner), 'creator': t.creator, 'run_count': t.run_count, 'last_run_at': t.last_run_at,
            'prompt': t.prompt, 'result': t.result, 'created_at': t.created_at}


@api
def tasks(request: Request):
    st = Storage()
    if request.method == 'POST':
        b = request.state.body
        prompt = (b.get('prompt') or '').strip()
        if not prompt:
            raise ValueError('任务内容为空')
        due = state.parse_at(b['at']) if b.get('at') else time.time() + state.parse_duration(b.get('in') or '')
        every = state.parse_duration(b['every']) if b.get('every') else 0
        task_id = st.add_task(kind='task', due_at=int(due), every_sec=every, prompt=prompt, chat_id=b.get('chat_id') or '',
                              by_owner=1, creator='human')
        return _json({'ok': True, 'id': task_id})
    show_all = request.query_params.get('all') == '1'
    rows = st.list_tasks(include_finished=show_all, limit=200)
    return _json([_task_dict(t) for t in rows if show_all or t.kind == 'task'])


@api
def task_item(request: Request):
    st = Storage()
    t = st.get_task(int(request.path_params['task_id']))
    if not t:
        return _json({'error': '没有这个任务'}, 404)
    if request.method == 'DELETE':
        st.update_task(t.id, status='cancelled')
        return _json({'ok': True})
    return _json(_task_dict(t))


@api
def say(request: Request):
    text = (request.state.body.get('text') or '').strip()
    if not text:
        raise ValueError('内容为空')
    task_id = Storage().add_task(kind='say', due_at=0, prompt=text, by_owner=1, creator='human')
    return _json({'ok': True, 'id': task_id})


@api
def takeovers(request: Request):
    st = Storage()
    names = _chat_names(st)
    if request.method == 'POST':
        b = request.state.body
        if not b.get('chat_id'):
            raise ValueError('需要 chat_id')
        seconds = state.parse_duration(b['for']) if b.get('for') else load_config()['takeover_default']
        st.takeover_start(b['chat_id'], time.time() + seconds, b.get('goal') or '', 'human')
        return _json({'ok': True})
    return _json([{'chat_id': t.chat_id, 'chat_name': names.get(t.chat_id, ''), 'until': t.until, 'goal': t.goal,
                   'creator': t.creator, 'created_at': t.created_at} for t in st.takeover_list() if t.until > time.time()])


@api
def takeover_item(request: Request):
    ok = Storage().takeover_expire(request.path_params['chat_id'], time.time())
    return _json({'ok': ok})


def _agent_home() -> Path:
    return Path(load_config()['agent_home'])


def _git(home: Path, *args) -> str:
    return subprocess.run(['git', '-C', str(home), *args], capture_output=True, text=True).stdout


@api
def memory(request: Request):
    home = _agent_home()
    files = [{'name': p.name, 'content': p.read_text(encoding='utf-8'), 'mtime': datetime.fromtimestamp(p.stat().st_mtime)}
             for p in sorted((home / 'memory').glob('*.md'), key=lambda p: (p.name != 'MEMORY.md', p.name))]
    return _json(files)


@api
def memory_item(request: Request):
    name = request.path_params['name']
    if not MEMORY_NAME.match(name):
        raise ValueError('文件名不合法')
    home = _agent_home()
    path = home / 'memory' / name
    if request.method == 'DELETE':
        path.unlink(missing_ok=True)
        commit_agent_home(home, f'网页删除 memory/{name}')
        return _json({'ok': True})
    path.write_text(request.state.body.get('content', ''), encoding='utf-8')
    commit_agent_home(home, f'网页编辑 memory/{name}')
    return _json({'ok': True})


@api
def persona(request: Request):
    home = _agent_home()
    path = home / 'CLAUDE.md'
    if request.method == 'PUT':
        path.write_text(request.state.body.get('content', ''), encoding='utf-8')
        commit_agent_home(home, '网页编辑 CLAUDE.md')
        return _json({'ok': True, 'note': '新会话才一定会读到新的人设: 需要时在"指挥"里重置会话'})
    return _json({'content': path.read_text(encoding='utf-8')})


@api
def history(request: Request):
    """数字人目录的提交记录: 记忆和人设每次是怎么变的。"""
    home = _agent_home()
    out = []
    log = _git(home, 'log', '-n', '60', '--date=format:%Y-%m-%d %H:%M:%S', '--format=%h%x1f%ad%x1f%s', '--shortstat')
    for block in log.strip().split('\n\n'):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        for line in lines:
            if '\x1f' in line:
                sha, when, subject = line.split('\x1f', 2)
                out.append({'sha': sha, 'time': when, 'subject': subject, 'stat': ''})
            elif out:
                out[-1]['stat'] = line.strip()
    return _json(out)


@api
def history_item(request: Request):
    sha = request.path_params['sha']
    if not re.fullmatch(r'[0-9a-f]{4,40}', sha):
        raise ValueError('提交号不合法')
    diff = _git(_agent_home(), 'show', '--format=%s%n%ad', '--date=format:%Y-%m-%d %H:%M:%S', sha, '--', 'memory', 'CLAUDE.md')
    return _json({'sha': sha, 'diff': diff[:60000]})


@api
def logs(request: Request):
    from ..daemon import LOG_PATH
    n = min(int(request.query_params.get('lines', 300)), 2000)
    if not LOG_PATH.exists():
        return _json({'lines': []})
    with open(LOG_PATH, 'rb') as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 400 * n))
        lines = f.read().decode('utf-8', errors='replace').splitlines()[-n:]
    return _json({'lines': lines})


@api
def control(request: Request):
    action = request.state.body.get('action')
    if action not in ('interrupt', 'clear'):
        raise ValueError('不支持的操作')
    st = Storage()
    cid = st.control_add(action)
    deadline = time.time() + 15
    while time.time() < deadline:
        c = st.control_get(cid)
        if c.status != 'pending':
            return _json({'ok': True, 'result': c.result})
        time.sleep(0.5)
    st.control_done(cid, '超时', status='expired')
    return _json({'ok': False, 'result': 'daemon 15 秒内没有响应, 它在运行吗?'})


@api
def restart(request: Request):
    r = service.systemctl('restart', service.UNITS['daemon'])
    return _json({'ok': r.returncode == 0, 'result': (r.stderr or '已重启').strip()})


@api
def recall(request: Request):
    from ..protocol import LarkAuth, LarkClient
    msg_id = str(request.state.body.get('msg_id') or '')
    if not msg_id.isdigit():
        raise ValueError('msg_id 不合法')
    st = Storage()
    sent = st.find_action('agent', 'send', msg_id) or st.find_action('human', 'send', msg_id)
    try:
        LarkClient(LarkAuth()).api('messages.DeleteMessageRequest', {'id': msg_id})
    except Exception as e:
        st.log_action(actor='human', action='recall', chat_id=sent.chat_id if sent else '', msg_id=msg_id,
                      content=f'网页撤回 {msg_id}', ok=0, error=str(e)[:300])
        return _json({'ok': False, 'error': '撤回失败: 只能撤回自己账号发的消息, 且要在飞书允许的撤回时限内'}, 400)
    st.log_action(actor='human', action='recall', chat_id=sent.chat_id if sent else '', msg_id=msg_id, content=f'网页撤回 {msg_id}')
    return _json({'ok': True})


class _JSONBody:
    """把请求体解析成 dict 放进 request.state.body, 供同步接口使用。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] in ('GET', 'HEAD'):
            return await self.app(scope, receive, send)
        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg.get('body', b''))
            more = msg.get('more_body', False)
        raw = b''.join(chunks)
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        scope.setdefault('state', {})['body'] = body if isinstance(body, dict) else {}

        async def replay():
            return {'type': 'http.request', 'body': raw, 'more_body': False}
        return await self.app(scope, replay, send)


def create_app() -> Starlette:
    routes = [
        Route('/', index), Route('/login', login, methods=['POST']),
        Route('/api/overview', overview), Route('/api/activity', activity), Route('/api/conversations', conversations),
        Route('/api/chats', chats), Route('/api/mentions', mentions),
        Route('/api/tasks', tasks, methods=['GET', 'POST']), Route('/api/tasks/{task_id:int}', task_item, methods=['GET', 'DELETE']),
        Route('/api/say', say, methods=['POST']),
        Route('/api/takeovers', takeovers, methods=['GET', 'POST']),
        Route('/api/takeovers/{chat_id}', takeover_item, methods=['DELETE']),
        Route('/api/memory', memory), Route('/api/memory/{name}', memory_item, methods=['PUT', 'DELETE']),
        Route('/api/persona', persona, methods=['GET', 'PUT']),
        Route('/api/history', history), Route('/api/history/{sha}', history_item),
        Route('/api/logs', logs), Route('/api/control', control, methods=['POST']),
        Route('/api/restart', restart, methods=['POST']), Route('/api/recall', recall, methods=['POST']),
        Mount('/static', StaticFiles(directory=str(STATIC)), name='static'),
    ]
    app = Starlette(routes=routes)
    return _JSONBody(app)


def run_web():
    import uvicorn
    cfg = load_config()
    get_token()
    uvicorn.run(create_app(), host='127.0.0.1', port=cfg['web_port'], log_level='warning', proxy_headers=True)
