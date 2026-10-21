"""网页版飞书: 在浏览器里看会话、收发消息, 看过的消息仍是未读。

会话列表用 feed.PullFeedCardsRequest, 消息用 PullMessagesByPositionsRequest, 都是只读接口;
这里从不调用 PutReadMessagesRequest / EnterChatRequest, 所以对方看不到已读, 你自己的未读提醒也还在。
发消息也不改已读: 普通发送会被服务端把会话之前的消息都算作已读, 所以有未读时改用"回复话题并同时发送到会话"
(见 LarkClient.send_msg 的 keep_unread), 对方在主会话照常看到, 已读位置不动。被回复的那条会变成已读,
所以默认挂在自己发的消息下面; 发出后再逐条核对别人的未读消息, 有变成已读的如实告诉页面。
"""
import re
import threading
import time

from loguru import logger
from starlette.requests import Request
from starlette.responses import Response

from ..config import CREDENTIALS_PATH
from ..protocol import LarkAuth, LarkClient
from ..protocol.media import download, extract_resource_key, message_resource_url
from ..protocol.proto import decoders
from ..storage import Storage

OWNER_CHAT_NAME = '我自己（私聊）'
MAX_TEXT = 10000
# 被回复的那条要过一会儿才在服务端变成已读(实测 0.3 秒内), 发出后等这么久再核对
READ_CHECK_DELAY = 1.5
_lock = threading.Lock()
_client = {'obj': None, 'mtime': 0.0}
# 名字和头像只缓存在内存里: 写进 chats 表会刷新 updated_at, 打乱数字人看到的"最近会话"
_profiles = {}   # user_id -> {'name', 'avatar'}
_p2p_names = {}
_p2p_peers = {}  # 私聊 chat_id -> 对方 user_id


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
    key = extract_resource_key(m.get('msg_type', 0), m.get('content_data') or {}) if m.get('msg_type') == 5 else ''
    who = _profile(c, m.get('from_id'), chat_id)
    return {'msg_id': str(m.get('msg_id') or ''), 'position': int(m.get('position') or 0),
            'from_id': str(m.get('from_id') or ''), 'sender': who['name'], 'avatar': _avatar(c, who['avatar']),
            'me': str(m.get('from_id')) == me, 'time': int(m.get('create_time') or 0),
            'type': m.get('msg_type_name', ''), 'content': _clean(m.get('content', '')),
            'root_id': str(m.get('root_id') or ''), 'image_key': key}


def chat(request: Request):
    """一个会话的消息。默认最近 count 条; before=<position> 往前翻, after=<position> 取新消息。"""
    c = client()
    chat_id = _digits(request.path_params['chat_id'], 'chat_id')
    q = request.query_params
    count = min(int(q.get('count') or 30), 100)
    info = _call(c.get_chat_info, chat_id) or {}
    last = int(info.get('lastMessagePosition') or 0)
    first = max(int(info.get('firstChatMessagePosition') or 1), 1)
    if q.get('after'):
        start = int(_digits(q['after'], 'after')) + 1
        end = min(last, start + 49)
    elif q.get('before'):
        end = int(_digits(q['before'], 'before')) - 1
        start = max(first, end - count + 1)
    else:
        end, start = last, max(first, last - count + 1)
    msgs = _call(c.pull_history, chat_id, range(start, end + 1)) if end >= start else []
    st = Storage()
    _people(c, [m.get('from_id') for m in msgs] + [c.auth.user_id], st)
    known = {chat_id: st.get_chat_name(chat_id)}
    return {'chat': {'chat_id': chat_id, 'name': _chat_name(c, chat_id, info, known), 'kind': _kind(info),
                     'avatar': _chat_avatar(c, chat_id, info),
                     'unread': _badge(info), 'last_position': last, 'member_count': int(info.get('memberCount') or 0)},
            'messages': [_message_out(c, m, chat_id) for m in msgs],
            'start': start, 'end': end, 'has_more': start > first}


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
    # 网页版一律匿名发送: 有未读时挂在自己发的消息下面, 同时发送到会话, 已读位置不动
    msg_id = _call(c.send_msg, text, chat_id, root_id=root_id, keep_unread=True)
    Storage().log_action(actor='human', action='send', chat_id=chat_id, msg_id=str(msg_id or ''), content=text,
                         ok=1 if msg_id else 0, error='' if msg_id else '发送失败', turn_kind='web')
    if not msg_id:
        raise UpstreamError('发送失败: 飞书没有确认收到这条消息')
    kept, newly = _check_unread(c, chat_id, read, watch) if read < last else (True, 0)
    if kept is False:
        logger.warning(f'匿名发送后会话 {chat_id} ' + (f'有 {newly} 条别人的消息变成了已读' if newly else '的已读位置前进了'))
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
    """图片代理: 浏览器拿不到飞书的登录态, 由这里带上 cookie 去取。"""
    q = request.query_params
    chat_id, msg_id = _digits(q.get('chat'), 'chat'), _digits(q.get('msg'), 'msg')
    key = q.get('key') or ''
    if not re.fullmatch(r'[\w\-.]{1,200}', key):
        raise ValueError('key 不合法')
    auth = client().auth
    data, ctype = _call(download, auth, message_resource_url(msg_id, key, chat_id, host=auth.region.file_host),
                        max_bytes=20 * 1024 * 1024)
    headers = {'Cache-Control': 'private, max-age=86400', 'X-Content-Type-Options': 'nosniff'}
    # 只有普通图片按图片返回; 其他类型(包括可以带脚本的 svg)一律当附件下载, 不在本站点渲染
    if not ctype.startswith('image/') or 'svg' in ctype:
        ctype = 'application/octet-stream'
        headers['Content-Disposition'] = 'attachment'
    return Response(data, media_type=ctype, headers=headers)


ROUTES = [
    ('/api/im/me', me, ['GET']),
    ('/api/im/feed', feed, ['GET']),
    ('/api/im/chat/{chat_id}', chat, ['GET']),
    ('/api/im/chat/{chat_id}/send', send, ['POST']),
    ('/api/im/search', search, ['GET']),
    ('/api/im/open', open_p2p, ['POST']),
    ('/api/im/media', media, ['GET']),
]
