"""网页版飞书: 在浏览器里看会话、收发消息, 看过的消息仍是未读。

会话列表用 feed.PullFeedCardsRequest, 消息用 PullMessagesByPositionsRequest, 都是只读接口;
这里从不调用 PutReadMessagesRequest / EnterChatRequest, 所以对方看不到已读, 你自己的未读提醒也还在。
发消息也不改已读: 普通发送不会给别人的消息产生已读回执, 但会清掉本人在这个会话的未读, 所以有未读时经本人私聊
转发过去(见 LarkClient.send_msg 的 keep_unread), 对方看到的是一条普通消息, 本人的已读位置不动。回复某条(话题回复)
会让被回复的那条对对方显示已读。发出后再核对已读位置和别人的未读消息, 有变化的如实告诉页面。

消息怎么显示(富文本片段、卡片、图片、文件)由 render.message_view 整理成纯数据, 前端只用 textContent 渲染。
打开会话时带上本人的已读位置(readPosition), 页面据此画"以下为新消息", 只是显示, 不会改已读。
新消息优先从本地库取(daemon 收到的 WS 推送都会入库), 取不到再去服务端拉。
"""
import json
import re
import threading
import time
from urllib.parse import quote

from loguru import logger
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from .. import state
from ..config import CREDENTIALS_PATH
from ..protocol import LarkAuth, LarkClient
from ..protocol.client import MODERN_COMMAND_VERSION
from ..protocol.media import download, message_resource_url, open_stream
from ..protocol.proto import decoders
from ..storage import Storage
from . import render

OWNER_CHAT_NAME = '我自己（私聊）'
MAX_TEXT = 10000
# 已读状态要过一会儿才在服务端更新(实测 0.3 秒内), 发出后等这么久再核对
READ_CHECK_DELAY = 1.5
_lock = threading.Lock()
_client = {'obj': None, 'mtime': 0.0}
# 名字和头像只缓存在内存里: 写进 chats 表会刷新 updated_at, 打乱数字人看到的"最近会话"
_profiles = {}   # user_id -> {'name', 'avatar'}
_p2p_names = {}
_p2p_peers = {}  # 私聊 chat_id -> 对方 user_id
_quotes = {}     # 被引用的消息 id -> {'from_id', 'text'}, 消息内容不会变, 查过就记住
MAX_QUOTES = 5000
# 打开会话时除了最近 count 条, 还往前多带到第一条未读(最多这么多条), 好在页面上定位到"以下为新消息"
MAX_INITIAL = 100
# 图片文件头: 有的图片(比如表情包)服务端按 application/octet-stream 返回, 认出来的才按图片显示
IMAGE_MAGIC = ((b'\x89PNG\r\n\x1a\n', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'), (b'GIF87a', 'image/gif'),
               (b'GIF89a', 'image/gif'))


class UpstreamError(Exception):
    """飞书接口请求失败。"""


def client() -> LarkClient:
    """共用一个客户端; daemon 刷新了登录凭证就换新的。"""
    mtime = CREDENTIALS_PATH.stat().st_mtime if CREDENTIALS_PATH.exists() else 0.0
    with _lock:
        if _client['obj'] is None or mtime != _client['mtime']:
            _client.update(obj=LarkClient(LarkAuth()), mtime=mtime)
        return _client['obj']


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning(f'飞书接口请求失败: {e}')
        raise UpstreamError(f'飞书接口请求失败: {e}') from e


def _owner_chat() -> str:
    from ..notify import _load
    return _load().get('owner_chat', '')


def _digits(value, what: str) -> str:
    value = str(value or '')
    if not value.isdigit():
        raise ValueError(f'{what} 不合法')
    return value


def _display_name(chatter: dict, locale: str) -> str:
    """和客户端一样: 有备注名用备注名, 否则用当前语言的显示名。"""
    for field in ('alias', 'displayName', 'name'):
        v = chatter.get(field)
        if isinstance(v, dict):
            v = (v.get('i18nVals') or {}).get(locale.lower()) or v.get('defaultVal')
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ''


def _people(c: LarkClient, user_ids, st: Storage):
    """批量查好这些人的名字(有备注名用备注名)和头像; 查不到的退回消息库里见过的名字。"""
    missing = sorted({str(u) for u in user_ids if u} - set(_profiles))
    for i in range(0, len(missing), 50):
        batch = missing[i:i + 50]
        try:
            r = c.api('chatters.PullChattersByIdsRequest', {'chatterIds': batch, 'needBotInfo': True}) or {}
        except Exception as e:
            logger.warning(f'批量查联系人失败: {e}')
            r = {}
        for uid, ch in (r.get('chatters') or {}).items():
            _profiles[str(uid)] = {'name': _display_name(ch, c.auth.region.locale),
                                   'avatar': (ch.get('avatar') or {}).get('key') or ''}
    unnamed = [u for u in missing if not (_profiles.get(u) or {}).get('name')]
    known = st.sender_names(unnamed)
    for u in unnamed:
        _profiles[u] = {'name': known.get(u, ''), 'avatar': (_profiles.get(u) or {}).get('avatar', '')}


def _profile(c: LarkClient, user_id, chat_id) -> dict:
    uid = str(user_id or '')
    if not uid:
        return {'name': '', 'avatar': ''}
    p = _profiles.get(uid)
    if p is None or not p['name']:
        # 批量接口没查到(比如部分机器人), 单个再查一次名字
        p = _profiles[uid] = {'name': c.get_user_name(uid, chat_id) or '', 'avatar': (p or {}).get('avatar', '')}
    return p


def _user_name(c: LarkClient, user_id, chat_id) -> str:
    if str(user_id or '') == str(c.auth.user_id):
        return '我'
    return _profile(c, user_id, chat_id)['name']


def _avatar(c: LarkClient, key: str) -> str:
    return c.auth.region.avatar_url(key)


def _badge(chat: dict) -> int:
    return max(0, int(chat.get('lastMessagePositionBadgeCount') or 0) - int(chat.get('readPositionBadgeCount') or 0))


def _kind(chat: dict) -> str:
    if decoders.enum_to_int(chat.get('type', 0)) == 1:
        return 'p2p'
    return 'topic' if decoders.is_thread_chat(chat) else 'group'


def _chat_name(c: LarkClient, chat_id: str, chat: dict, known: dict, last_from: str = '') -> str:
    """群直接有名字; 私聊要找出对方是谁。"""
    if chat.get('name'):
        return chat['name']
    if chat_id == _owner_chat():
        return OWNER_CHAT_NAME
    if chat_id in _p2p_names:
        return _p2p_names[chat_id]
    name = known.get(chat_id, '')
    me = str(c.auth.user_id)
    peer = last_from if last_from and last_from != me else ''
    if not peer:
        last = int(chat.get('lastMessagePosition') or 0)
        if last:
            for m in reversed(_call(c.pull_history, chat_id, range(max(1, last - 9), last + 1))):
                if m.get('from_id') and str(m['from_id']) != me:
                    peer = str(m['from_id'])
                    break
    if peer:
        _p2p_peers[chat_id] = peer
        name = name or _user_name(c, peer, chat_id)
    _p2p_names[chat_id] = name = name or '私聊'
    return name


def _chat_avatar(c: LarkClient, chat_id: str, chat: dict) -> str:
    """会话自己的头像; 私聊没有的话用对方的头像。"""
    key = (chat.get('avatar') or {}).get('key', '')
    if not key and chat_id in _p2p_peers:
        key = _profile(c, _p2p_peers[chat_id], chat_id)['avatar']
    return _avatar(c, key)


def _preview(c: LarkClient, chat_id: str, chat: dict, msg: dict) -> str:
    if not msg:
        return ''
    summary, _ = decoders.decode_message_content(decoders.enum_to_int(msg.get('type', 0)), msg.get('content', b''))
    summary = _clean(summary)
    if _kind(chat) != 'p2p' and msg.get('fromId'):
        who = _user_name(c, msg['fromId'], chat_id)
        if who:
            return f'{who}: {summary}'
    return summary


def _clean(summary: str) -> str:
    """去掉摘要里给 agent 用的 key、尺寸等字段, 只留给人看的部分。"""
    summary = summary or ''
    # 服务端对它认为"旧版客户端看不了"的新消息类型只返回这句英文
    if "current version doesn't support viewing this message" in summary:
        return '[暂不支持显示这类消息，请在飞书客户端查看]'
    summary = re.sub(r'^\[(图片|表情包)\].*$', r'[\1]', summary, flags=re.S)
    summary = re.sub(r'^\[语音\] 时长([\d.]+)s.*$', r'[语音] \1 秒', summary, flags=re.S)
    # 系统消息(入群、改群名等)只留提示文字, 没有文字的显示成 [系统消息]
    summary = re.sub(r'^\[系统消息\]\s*(?:type=\S*)?\s*(.*)$', lambda m: m.group(1).strip() or '[系统消息]', summary, flags=re.S)
    return re.sub(r'^(\[(?:文件|视频)\] [^(]*?) \(.*$', r'\1', summary, flags=re.S)


# ---- 接口 ----

def feed(request: Request):
    """会话列表, 顺序和未读数与飞书客户端一致。"""
    c = client()
    cursor = request.query_params.get('cursor')
    body = {'type': 1, 'pullType': 2 if cursor else 1, 'count': 40, 'pullFeedType': 1, 'businessType': 1}
    if cursor:
        body['cursor'] = int(_digits(cursor, 'cursor'))
    r = _call(c.api, 'feed.PullFeedCardsRequest', body) or {}
    chats, msgs = r.get('chats') or {}, r.get('messages') or {}
    st = Storage()
    known = {ch.chat_id: ch.name for ch in st.list_chats(limit=2000) if ch.name}
    _people(c, [m.get('fromId') for m in msgs.values()], st)
    items = []
    for card in r.get('cards') or []:
        if decoders.enum_to_int(card.get('type', 0)) != 1 or card.get('isHide'):
            continue
        chat_id = str(card.get('id'))
        chat = chats.get(chat_id) or {}
        last_msg = msgs.get(str(chat.get('lastMessageId') or '')) or {}
        items.append({'chat_id': chat_id, 'name': _chat_name(c, chat_id, chat, known, str(last_msg.get('fromId') or '')),
                      'kind': _kind(chat), 'unread': _badge(chat), 'muted': chat.get('isRemind') is False,
                      'time': int(card.get('rankTime') or card.get('updateTime') or 0),
                      'avatar': _chat_avatar(c, chat_id, chat),
                      'preview': _preview(c, chat_id, chat, last_msg)})
    return {'items': items, 'next_cursor': str(r['nextCursor']) if r.get('nextCursor') else ''}


def _message_out(c: LarkClient, m: dict, chat_id: str) -> dict:
    me = str(c.auth.user_id)
    who = _profile(c, m.get('from_id'), chat_id)
    content = _clean(m.get('content', ''))
    view = render.message_view(m, me, content)
    if view['kind'] == 'recalled':
        remover = m.get('remover_id') or m.get('from_id')
        name = '你' if str(remover) == me else (_profile(c, remover, chat_id)['name'] or '对方')
        content = f'{name} 撤回了一条消息'
    elif view['kind'] == 'system' and content == '[系统消息]':
        content = ''  # 没有提示文字的系统消息, 页面上不显示
    parent = str(m.get('parent_id') or '')
    quote = None
    if parent and parent != '0' and view['kind'] not in ('recalled', 'system'):
        q = _quotes.get(parent)
        quote = {'sender': _user_name(c, q['from_id'], chat_id) if q and q['from_id'] else '',
                 'text': q['text'] if q else '引用的消息'}
    return {'msg_id': str(m.get('msg_id') or ''), 'position': int(m.get('position') or 0),
            'from_id': str(m.get('from_id') or ''), 'sender': who['name'], 'avatar': _avatar(c, who['avatar']),
            'me': str(m.get('from_id')) == me, 'time': int(m.get('create_time') or 0),
            'type': m.get('msg_type_name', ''), 'content': content, 'root_id': str(m.get('root_id') or ''),
            'parent_id': parent if parent != '0' else '', 'quote': quote, **view}


def _load_quotes(c: LarkClient, msgs: list, st: Storage):
    """查好这批消息引用的那些消息的摘要和发送人: 同一批里有的直接用, 其余用 PullMessagesByIdsRequest 一次批量查。"""
    me = str(c.auth.user_id)
    by_id = {str(m.get('msg_id')): m for m in msgs}
    wanted = {str(m.get('parent_id')) for m in msgs if m.get('parent_id') and str(m.get('parent_id')) != '0'}
    missing = [i for i in wanted if i not in _quotes and i not in by_id]
    found = list(by_id[i] for i in wanted if i in by_id and i not in _quotes)
    if missing:
        try:
            found += c.pull_messages(missing, command_version=MODERN_COMMAND_VERSION)
        except Exception as e:
            logger.warning(f'查被引用的消息失败: {e}')
    if len(_quotes) > MAX_QUOTES:
        _quotes.clear()
    for m in found:
        content = _clean(m.get('content', ''))
        _quotes[str(m.get('msg_id'))] = {'from_id': str(m.get('from_id') or ''),
                                          'text': render.brief(render.message_view(m, me, content), content) or '引用的消息'}
    _people(c, [q['from_id'] for i, q in _quotes.items() if i in wanted], st)


def _local_messages(st: Storage, chat_id: str, after: int, limit: int = 50):
    """本地库里 position 在 after 之后的消息(daemon 收到的 WS 推送), 格式同 pull_history。

    返回 (消息, 连续到的 position): 中间缺了的(没推送到、中转消息不入库)和卡片(可能是流式输出的中间状态)
    之后的留给服务端拉, 页面的 end 只前进到连续的那一段。
    """
    out, end = [], after
    for r in st.messages_after(chat_id, after, limit):
        if r.position != end + 1 or r.msg_type == 14:
            break
        try:
            data = json.loads(r.content_data) if r.content_data else None
        except ValueError:
            break  # 太长的 content_data 入库时被截断过, 这条也留给服务端拉
        end = r.position
        out.append({'msg_id': r.msg_id, 'msg_type': r.msg_type, 'msg_type_name': r.msg_type_name, 'from_id': r.sender_id,
                    'chat_id': r.chat_id, 'position': r.position, 'create_time': r.create_time, 'content': r.content,
                    'content_data': data, 'root_id': r.root_id, 'parent_id': r.parent_id, 'recalled': False})
    return out, end


def chat(request: Request):
    """一个会话的消息。默认最近 count 条(有未读时往前带到第一条未读); before=<position> 往前翻,
    after=<position> 取新消息; 再加 local=1 只查本地库, 不访问飞书。"""
    c = client()
    chat_id = _digits(request.path_params['chat_id'], 'chat_id')
    q = request.query_params
    count = min(int(q.get('count') or 30), 100)
    st = Storage()
    if q.get('after') and q.get('local'):
        after = int(_digits(q['after'], 'after'))
        msgs, end = _local_messages(st, chat_id, after)
        return _chat_out(c, chat_id, None, msgs, after + 1, end, None, st)
    info = _call(c.get_chat_info, chat_id) or {}
    last = int(info.get('lastMessagePosition') or 0)
    first = max(int(info.get('firstChatMessagePosition') or 1), 1)
    read = int(info.get('readPosition') or 0)
    if q.get('after'):
        start = int(_digits(q['after'], 'after')) + 1
        end = min(last, start + 49)
    elif q.get('before'):
        end = int(_digits(q['before'], 'before')) - 1
        start = max(first, end - count + 1)
    else:
        end, start = last, max(first, last - count + 1)
        if 0 < read < last and read + 1 < start + 3:
            # 未读多于一屏: 往前带到第一条未读之前两条, 但总数不超过 MAX_INITIAL
            start = max(first, min(start, read - 2), last - MAX_INITIAL + 1)
    msgs = _call(c.pull_history, chat_id, range(start, end + 1), command_version=MODERN_COMMAND_VERSION) if end >= start else []
    return _chat_out(c, chat_id, info, msgs, start, end, start > first, st, read)


def _chat_out(c: LarkClient, chat_id: str, info, msgs: list, start: int, end: int, has_more, st: Storage, read: int = 0):
    # 本人私聊里匿名发送留下的中转消息(已撤回)不显示
    msgs = [m for m in msgs if not state.is_relay(m)]
    _people(c, [m.get('from_id') for m in msgs] + [c.auth.user_id], st)
    _load_quotes(c, msgs, st)
    out = {'messages': [_message_out(c, m, chat_id) for m in msgs], 'start': start, 'end': end}
    if info is None:
        return {**out, 'chat': None, 'local': True}
    known = {chat_id: st.get_chat_name(chat_id)}
    last = int(info.get('lastMessagePosition') or 0)
    return {**out, 'has_more': has_more,
            'chat': {'chat_id': chat_id, 'name': _chat_name(c, chat_id, info, known), 'kind': _kind(info),
                     'avatar': _chat_avatar(c, chat_id, info), 'unread': _badge(info), 'last_position': last,
                     'read_position': read, 'member_count': int(info.get('memberCount') or 0)}}


def changes(request: Request):
    """本地库最新一条消息的序号: 页面每隔一两秒问一次, 变了就马上刷新会话列表, 不用等定时刷新。"""
    return {'latest': Storage().latest_message_id()}


def me(request: Request):
    c = client()
    _people(c, [c.auth.user_id], Storage())
    p = _profile(c, c.auth.user_id, '')
    return {'user_id': str(c.auth.user_id), 'name': p['name'], 'avatar': _avatar(c, p['avatar'])}


def send(request: Request):
    c = client()
    chat_id = _digits(request.path_params['chat_id'], 'chat_id')
    b = request.state.body
    text = str(b.get('text') or '').rstrip()
    if not text.strip():
        raise ValueError('消息内容为空')
    if len(text) > MAX_TEXT:
        raise ValueError(f'消息太长(超过 {MAX_TEXT} 字)')
    root_id = _digits(b['root_id'], 'root_id') if b.get('root_id') else None
    read, last = _call(c.read_state, chat_id)
    # 发送前记下别人发的、对我还是未读的消息, 发出后逐条核对; 显式回复的那条变成已读是"回复"本身的意思, 不算
    watch = []
    if read < last:
        try:
            watch = [i for i in _others_unread(c, chat_id, read, last) if i != root_id]
        except Exception as e:
            logger.warning(f'发送前查询未读消息失败: {e}')
            watch = None
    # 网页版一律匿名发送: 有未读时经本人私聊转发, 已读位置不动; 转发不成会退回普通发送, 下面的核对会如实报告
    msg_id = _call(c.send_msg, text, chat_id, root_id=root_id, keep_unread=True)
    Storage().log_action(actor='human', action='send', chat_id=chat_id, msg_id=str(msg_id or ''), content=text,
                         ok=1 if msg_id else 0, error='' if msg_id else '发送失败', turn_kind='web')
    if not msg_id:
        raise UpstreamError('发送失败: 飞书没有确认收到这条消息')
    kept, newly = _check_unread(c, chat_id, read, watch) if read < last else (True, 0)
    if kept is False:
        logger.warning(f'匿名发送后会话 {chat_id} ' + (f'有 {newly} 条别人的消息变成了已读' if newly else '的已读位置前进了, 本人的未读被清掉'))
    return {'ok': True, 'msg_id': str(msg_id), 'kept_unread': kept, 'newly_read': newly}


def _others_unread(c: LarkClient, chat_id: str, read: int, last: int) -> list:
    """未读区间(只看最近 100 条)里别人发的、目前对我仍是未读的消息 id。"""
    me = str(c.auth.user_id)
    ids = [str(m['msg_id']) for m in c.pull_history(chat_id, range(max(read + 1, last - 99), last + 1))
           if m.get('msg_id') and str(m.get('from_id')) != me and m.get('msg_type') != 6]
    status = c.my_read_status(ids) if ids else {}
    return [i for i in ids if status.get(i) is False]


def _check_unread(c: LarkClient, chat_id: str, read: int, watch):
    """发出后核对: 返回 (是否仍保持未读, 变成已读的条数); 核对不了时第一项是 None。"""
    time.sleep(READ_CHECK_DELAY)
    try:
        moved = c.read_state(chat_id)[0] > read
        after = c.my_read_status(watch) if watch else {}
    except Exception as e:
        logger.warning(f'发送后核对已读状态失败: {e}')
        return None, 0
    newly = sum(1 for i in watch or [] if after.get(i))
    if watch is None and not moved:
        return None, 0
    return not (moved or newly), newly


def search(request: Request):
    q = (request.query_params.get('q') or '').strip()
    if not q:
        return []
    results = _call(client().search, q) or []
    return [{'kind': r['type'], 'id': str(r['id']), 'name': re.sub(r'<[^>]+>', '', r.get('title') or '')} for r in results[:20]]


def open_p2p(request: Request):
    """找到(没有就建立)和某人的私聊。"""
    user_id = _digits(request.state.body.get('user_id'), 'user_id')
    chat_id = _call(client().create_chat, user_id)
    if not chat_id:
        raise UpstreamError('打开私聊失败')
    return {'chat_id': str(chat_id)}


def media(request: Request):
    """图片和文件代理: 浏览器拿不到飞书的登录态, 由这里带上 cookie 去取。

    默认按图片显示: 只有认得出的普通图片按图片返回, 其他类型(包括可以带脚本的 svg)一律当附件下载, 不在本站点渲染。
    dl=1 是下载文件: 边取边转发(文件可能很大), 一律当附件, name 是保存时的文件名。
    """
    q = request.query_params
    chat_id, msg_id = _digits(q.get('chat'), 'chat'), _digits(q.get('msg'), 'msg')
    key = q.get('key') or ''
    if not re.fullmatch(r'[\w\-.]{1,200}', key):
        raise ValueError('key 不合法')
    auth = client().auth
    url = message_resource_url(msg_id, key, chat_id, host=auth.region.file_host)
    headers = {'Cache-Control': 'private, max-age=86400', 'X-Content-Type-Options': 'nosniff'}
    if q.get('dl'):
        chunks, length = _call(open_stream, auth, url)
        headers['Content-Disposition'] = _attachment(q.get('name') or '')
        if length:
            headers['Content-Length'] = str(length)
        return StreamingResponse(chunks, media_type='application/octet-stream', headers=headers)
    data, ctype = _call(download, auth, url, max_bytes=20 * 1024 * 1024)
    if not ctype.startswith('image/') or 'svg' in ctype:
        sniffed = next((t for magic, t in IMAGE_MAGIC if data.startswith(magic)), '')
        if not sniffed and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            sniffed = 'image/webp'
        ctype = sniffed or 'application/octet-stream'
    if ctype == 'application/octet-stream':
        headers['Content-Disposition'] = 'attachment'
    return Response(data, media_type=ctype, headers=headers)


def _attachment(name: str) -> str:
    """下载时的文件名: 去掉路径和控制字符; 中文等按 RFC 5987 编码。"""
    name = re.sub(r'[\x00-\x1f\x7f/\\"]', '_', name).strip(' .')[:200] or 'download'
    ascii_name = re.sub(r'[^\w\-. ]', '_', name.encode('ascii', 'replace').decode()) or 'download'
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name, safe="")}'


ROUTES = [
    ('/api/im/me', me, ['GET']),
    ('/api/im/feed', feed, ['GET']),
    ('/api/im/changes', changes, ['GET']),
    ('/api/im/chat/{chat_id}', chat, ['GET']),
    ('/api/im/chat/{chat_id}/send', send, ['POST']),
    ('/api/im/search', search, ['GET']),
    ('/api/im/open', open_p2p, ['POST']),
    ('/api/im/media', media, ['GET']),
]
