"""网页版飞书: 在浏览器里看会话、收发消息, 看过的消息仍是未读。

会话列表用 feed.PullFeedCardsRequest, 消息用 PullMessagesByPositionsRequest, 都是只读接口;
这里从不调用 PutReadMessagesRequest / EnterChatRequest, 所以对方看不到已读, 你自己的未读提醒也还在。
发消息也不改已读: 普通发送不会给别人的消息产生已读回执, 但会清掉本人在这个会话的未读, 所以有未读时经本人私聊
转发过去(见 LarkClient.send_message 的 keep_unread), 对方看到的是一条普通消息, 本人的已读位置不动; 转发不成就报发送
失败, 不退回普通发送。回复某条(主会话里的引用回复, 话题群里回进话题)会让被回复的那条对对方显示已读。发出后再核对已读位置和别人的未读消息,
有变化的如实告诉页面。

能发文字、带 @ 的文字、图片和文件, 各种类型都走同一套匿名发送和发后核对(_deliver)。图片和文件由浏览器把原始内容
POST 过来, 这里存进临时文件再上传到飞书(media.upload_image / upload_file), 拿到 key 后发消息。@ 经本人私聊转发后
会变成普通文字(对方收不到 @ 提醒), 所以有未读时带 @ 的消息要页面确认过才以文字发出。

消息怎么显示(富文本片段、卡片、图片、文件)由 render.message_view 整理成纯数据, 前端只用 textContent 渲染。
打开会话时带上本人的已读位置(readPosition), 页面据此画"以下为新消息", 只是显示, 不会改已读。
新消息优先从本地库取(daemon 收到的 WS 推送都会入库), 取不到再去服务端拉。

看消息的路径(会话列表、打开会话、翻消息)要快: 用只读客户端 ReadClient(复用连接, 只放行查询类命令), 互不依赖的查询
并发执行; 会话列表顺带记下每个会话的信息, 打开会话时据此先猜好该拉哪一段消息, 和查最新会话信息同时进行, 范围以查到的
为准, 猜漏的再补拉。发消息、上传仍用原来的 client(), 流程不变。

话题群(会话列表里 kind 为 topic)的消息按话题组织, 会话信息里没有 lastMessagePosition, 按普通会话拉会一条也拉不到。
打开话题群时改为显示话题列表: 话题按话题位置(lastThreadPosition)用 thread.PullThreadsByPositionsRequest 取(只取看得见的,
入群提示这类占位置的系统消息由服务端跳过), 每个话题带根消息和回复数; 点开一个话题时用 thread.PullThreadsByIDsRequest 取
话题信息、thread.PullThreadMessagesByPositionsRequest 取回复(见 _topic_list、thread)。这几个也都是只读查询。
"""
import asyncio
import json
import re
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import quote

import requests
from loguru import logger
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from .. import state
from ..config import CREDENTIALS_PATH
from ..protocol import LarkAuth, LarkClient
from ..protocol.client import MODERN_COMMAND_VERSION
from ..protocol.media import UploadError, download, message_resource_url, open_stream, upload_file, upload_image
from ..protocol.proto import builders, decoders, gateway
from ..storage import Storage
from . import render

OWNER_CHAT_NAME = '我自己（私聊）'
MAX_TEXT = 10000
# 已读状态要过一会儿才在服务端更新(实测 0.3 秒内), 发出后等这么久再核对
READ_CHECK_DELAY = 1.5
_lock = threading.Lock()
_client = {'obj': None, 'mtime': 0.0}
_reader = {'obj': None, 'base': None}
# 名字和头像只缓存在内存里: 写进 chats 表会刷新 updated_at, 打乱数字人看到的"最近会话"
_profiles = {}   # user_id -> {'name', 'avatar'}
# 批量接口查不到名字的人(比如部分机器人)要单个查; 查过的这么久之内不再查, 免得每次刷新都多一串请求
NAME_RETRY = 300
_name_tried = {}  # user_id -> 上次单个查名字的时间
_p2p_names = {}
_p2p_peers = {}  # 私聊 chat_id -> 对方 user_id; 空串表示找过了, 确实没有对方发的消息
_quotes = {}     # 被引用的消息 id -> {'from_id', 'text'}, 消息内容不会变, 查过就记住
MAX_QUOTES = 5000
# 会话信息(会话列表和打开会话时查到的): 只用来猜打开会话时该拉哪一段消息, 显示的一律以当次查到的为准
CHAT_INFO_TTL = 600
MAX_CHAT_INFOS = 500
_chat_infos = {}  # chat_id -> (查到的时间, 会话信息)
# 按猜的范围拉消息时往后多拉这么多条, 猜过之后来的新消息一般也在里面, 不用补拉
SPECULATE_SLACK = 20
# 会话列表第一页: 记下最近一次的结果(页面刚打开时先显示它), 同时来的几个请求共用一次查询
FEED_COUNT = 40
_feed = {'data': None, 'at': 0.0, 'flight': None}
_feed_lock = threading.Lock()
# 并发执行互不依赖的查询; 只由处理请求的线程提交, 池里的任务不再往池里提交(见 _parallel)
POOL_PREFIX = 'im-read'
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix=POOL_PREFIX)
# 只读客户端放行的网关命令: 都是查询, 不改变任何消息、已读或会话状态。5023 是 GetUserInfoRequest(查单个人的名字)
READ_ONLY_COMMANDS = frozenset([gateway.resolve_cmd(n)[0] for n in (
    'feed.PullFeedCardsRequest', 'chatters.PullChattersByIdsRequest', 'chats.PullChatsByIdsRequest',
    'messages.PullMessagesByPositionsRequest', 'messages.PullMessagesByIdsRequest',
    # 话题群: 按位置取话题、按 id 取话题、取话题里的回复。标话题已读的 thread.PutReadThreadsRequest 不在这里
    'thread.PullThreadsByPositionsRequest', 'thread.PullThreadsByIDsRequest',
    'thread.PullThreadMessagesByPositionsRequest')] + [5023])
# 话题群: 定时刷新时重取最近这么多个话题(新话题和回复数的变化都在里面); 打开话题时按话题列表给的最后位置先猜着拉,
# 往后多拉这么多条
TOPIC_REFRESH = 10
THREAD_SLACK = 20
TOPIC_ROUNDS = 3
# 打开会话时除了最近 count 条, 还往前多带到第一条未读(最多这么多条), 好在页面上定位到"以下为新消息"
MAX_INITIAL = 100
# 图片文件头: 有的图片(比如表情包)服务端按 application/octet-stream 返回, 认出来的才按图片显示
IMAGE_MAGIC = ((b'\x89PNG\r\n\x1a\n', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'), (b'GIF87a', 'image/gif'),
               (b'GIF89a', 'image/gif'))
# 发送的大小上限: 图片超过这个可以作为文件发
MAX_IMAGE = 20 * 1024 * 1024
MAX_FILE = 100 * 1024 * 1024
# @ 候选的会话成员缓存在内存里, 每个会话 10 分钟; 大群接口一次最多返回 2000 人
MEMBERS_TTL = 600
MEMBERS_CAP = 2000
_members = {}    # chat_id -> (查询时间, 成员列表, 是否完整)
MAX_MENTIONS = 50
AT_RELAY_NOTE = ('这个会话里你还有未读。为了不清掉未读，消息要经你和自己的私聊转发过去，'
                 '转发会把 @ 变成普通文字，对方收不到 @ 提醒。')


class UpstreamError(Exception):
    """飞书接口请求失败。"""


def client() -> LarkClient:
    """共用一个客户端; daemon 刷新了登录凭证就换新的。"""
    mtime = CREDENTIALS_PATH.stat().st_mtime if CREDENTIALS_PATH.exists() else 0.0
    with _lock:
        if _client['obj'] is None or mtime != _client['mtime']:
            _client.update(obj=LarkClient(LarkAuth()), mtime=mtime)
        return _client['obj']


def _pooled_session() -> requests.Session:
    s = requests.Session()
    # 和 requests.post 一样不记住响应里设置的 cookie, 每次只带登录凭证
    s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    s.mount('https://', requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16))
    return s


class ReadClient(LarkClient):
    """看消息的路径专用的客户端: 复用 HTTPS 连接(每个请求省掉一次握手, 实测约 70ms), 只放行查询类命令
    (READ_ONLY_COMMANDS), 别的命令一律拒绝, 保证这条路径不会改变任何消息或会话的状态。"""

    def __init__(self, auth: LarkAuth):
        # 不调 LarkClient.__init__: 凭证已由 client() 校验过, 不再多一次网络请求; 也用不到 WS 的事件循环
        self.auth = auth
        self._thread_chats = {}
        self._self_chat = ''
        self.http = _pooled_session()

    def _gateway_post(self, packet, command_version: str = None) -> bytes:
        if packet.cmd not in READ_ONLY_COMMANDS:
            raise RuntimeError(f'只读客户端不能调用命令 {packet.cmd}')
        try:
            return super()._gateway_post(packet, command_version)
        except requests.ConnectionError as e:
            if isinstance(e, requests.Timeout):
                raise
            # 复用的连接可能刚好被服务端关掉了; 查询重发一次没有副作用
            logger.debug(f'只读请求连接中断, 重试一次: {e}')
            return super()._gateway_post(packet, command_version)

    def note_chat(self, chat_id: str, info: dict):
        """记下会话模式(是不是话题群), 之后 pull_history 不用再为此查一次会话信息。"""
        if info:
            self._thread_chats[str(chat_id)] = decoders.is_thread_chat(info)


def reader() -> ReadClient:
    """看消息用的只读客户端, 和 client() 共用登录凭证, 凭证换了也跟着换。"""
    base = client()
    with _lock:
        if _reader['base'] is not base:
            _reader.update(obj=ReadClient(base.auth), base=base)
        return _reader['obj']


def _parallel(*calls) -> list:
    """并发执行互不依赖的调用 (fn, *args), 按顺序返回结果; 有一个出错就抛出它的异常。
    已经在池里的任务再调用时按顺序执行, 不往池里提交, 免得池满了互相等待。"""
    if len(calls) <= 1 or threading.current_thread().name.startswith(POOL_PREFIX):
        return [fn(*args) for fn, *args in calls]
    futures = [_pool.submit(fn, *args) for fn, *args in calls]
    return [f.result() for f in futures]


def _remember_chat(c, chat_id: str, info: dict):
    if not info:
        return
    if len(_chat_infos) > MAX_CHAT_INFOS:
        _chat_infos.clear()
    _chat_infos[str(chat_id)] = (time.time(), info)
    c.note_chat(chat_id, info)


def _cached_info(chat_id: str):
    hit = _chat_infos.get(str(chat_id))
    return hit[1] if hit and time.time() - hit[0] < CHAT_INFO_TTL else None


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


def _needs_lookup(uid: str) -> bool:
    p = _profiles.get(uid)
    return (p is None or not p['name']) and time.time() - _name_tried.get(uid, 0) > NAME_RETRY


def _profile(c: LarkClient, user_id, chat_id) -> dict:
    uid = str(user_id or '')
    if not uid:
        return {'name': '', 'avatar': ''}
    p = _profiles.get(uid)
    if _needs_lookup(uid):
        # 批量接口没查到(比如部分机器人), 单个再查一次名字; 查不到的过一阵再试
        _name_tried[uid] = time.time()
        p = _profiles[uid] = {'name': c.get_user_name(uid, chat_id) or '', 'avatar': (p or {}).get('avatar', '')}
    return p or {'name': '', 'avatar': ''}


def _lookup_names(c: LarkClient, pairs):
    """批量查过之后还没有名字的人 [(user_id, chat_id)], 并发地单个查(结果记在 _profiles 里)。"""
    todo = {}
    for uid, chat_id in pairs:
        uid = str(uid or '')
        if uid and uid not in todo and _needs_lookup(uid):
            todo[uid] = chat_id
    _parallel(*[(_profile, c, uid, chat_id) for uid, chat_id in todo.items()])


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


def _chat_name(c: LarkClient, chat_id: str, chat: dict, known: dict, owner: str) -> str:
    """群直接有名字; 私聊用对方的名字(对方是谁先由 _find_peers 找好)。"""
    if chat.get('name'):
        return chat['name']
    if chat_id == owner:
        return OWNER_CHAT_NAME
    if chat_id in _p2p_names:
        return _p2p_names[chat_id]
    peer = _p2p_peers.get(chat_id, '')
    name = known.get(chat_id, '') or (_user_name(c, peer, chat_id) if peer else '')
    # 有名字了, 或者确实找不到对方, 就记住; 这次没找成(查询失败)的下次再找
    if name or (chat_id in _p2p_peers and not peer):
        _p2p_names[chat_id] = name = name or '私聊'
    return name or '私聊'


def _find_peers(c: LarkClient, st: Storage, chats: dict, owner: str) -> list:
    """私聊要找出对方是谁, chats 是 {chat_id: (会话信息, 已知的一条消息的发送人)}。先用已知消息的发送人
    (会话列表带的最后一条、刚拉到的消息), 再查本地库; 都没有的返回出来, 由 _server_peer 去服务端找。"""
    me, pending = str(c.auth.user_id), []
    for chat_id, (chat, hint) in chats.items():
        if chat.get('name') or chat_id == owner or chat_id in _p2p_names or _p2p_peers.get(chat_id):
            continue
        peer = hint if hint and hint != me else st.chat_peer(chat_id, me)
        if peer:
            _p2p_peers[chat_id] = peer
        else:
            pending.append((chat_id, chat))
    return pending


def _server_peer(c: LarkClient, chat_id: str, chat: dict):
    """拉私聊最近 10 条消息找出对方; 查询失败时不记, 下次再找。"""
    me = str(c.auth.user_id)
    last = int(chat.get('lastMessagePosition') or 0)
    msgs = []
    if last:
        try:
            msgs = c.pull_history(chat_id, range(max(1, last - 9), last + 1))
        except Exception as e:
            logger.warning(f'查私聊 {chat_id} 的对方失败: {e}')
            return
    _p2p_peers[chat_id] = next((str(m['from_id']) for m in reversed(msgs) if m.get('from_id') and str(m['from_id']) != me), '')


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
    """会话列表, 顺序和未读数与飞书客户端一致。cached=1 时有上次的第一页就直接返回它(带 cached: true),
    页面刚打开时先显示, 紧接着再正常刷新一次。"""
    q = request.query_params
    if q.get('cursor'):
        return _feed_page(reader(), int(_digits(q['cursor'], 'cursor')))
    if q.get('cached') and _feed['data'] is not None:
        return {**_feed['data'], 'cached': True}
    return _first_feed_page()


def _first_feed_page() -> dict:
    """第一页。同时来的几个请求(定时刷新、新消息触发的刷新、几个标签页)共用正在进行的那次查询。"""
    with _feed_lock:
        flight, owner = _feed['flight'], _feed['flight'] is None
        if owner:
            flight = _feed['flight'] = Future()
    if not owner:
        return flight.result()
    try:
        data = _feed_page(reader(), None)
        _feed.update(data=data, at=time.time())
        flight.set_result(data)
        return data
    except BaseException as e:
        flight.set_exception(e)
        raise
    finally:
        with _feed_lock:
            _feed['flight'] = None


def _feed_page(c: ReadClient, cursor) -> dict:
    body = {'type': 1, 'pullType': 2 if cursor else 1, 'count': FEED_COUNT, 'pullFeedType': 1, 'businessType': 1}
    if cursor:
        body['cursor'] = cursor
    r = _call(c.api, 'feed.PullFeedCardsRequest', body) or {}
    chats, msgs = r.get('chats') or {}, r.get('messages') or {}
    st, owner = Storage(), _owner_chat()
    rows = []
    for card in r.get('cards') or []:
        if decoders.enum_to_int(card.get('type', 0)) != 1 or card.get('isHide'):
            continue
        chat_id = str(card.get('id'))
        chat = chats.get(chat_id) or {}
        # 会话列表返回的会话信息和单独查的一样完整: 记下来, 打开会话时据此先猜该拉哪段消息, 也不用再查会话模式
        _remember_chat(c, chat_id, chat)
        last_id = chat.get('lastMessageId')
        if not last_id and _kind(chat) == 'topic':
            last_id = chat.get('lastVisibleThreadId')  # 话题群没有 lastMessageId, 预览用最后一个看得见的话题(根消息)
        rows.append((card, chat_id, chat, msgs.get(str(last_id or '')) or {}))
    # 名字: 最后一条消息的发送人和私聊的对方一起批量查; 私聊的对方本地找不到的, 和批量查名字同时去服务端找
    pending = _find_peers(c, st, {chat_id: (chat, str(m.get('fromId') or '')) for _, chat_id, chat, m in rows}, owner)
    senders = [(m.get('fromId'), chat_id) for _, chat_id, chat, m in rows if m.get('fromId') and _kind(chat) != 'p2p']
    def peers():
        return [(_p2p_peers[chat_id], chat_id) for _, chat_id, _, _ in rows if _p2p_peers.get(chat_id)]
    _parallel((_people, c, [u for u, _ in senders + peers()], st), *[(_server_peer, c, cid, chat) for cid, chat in pending])
    if pending:
        _people(c, [u for u, _ in peers()], st)
    _lookup_names(c, senders + peers())
    known = {ch.chat_id: ch.name for ch in st.list_chats(limit=2000) if ch.name}
    items = []
    for card, chat_id, chat, last_msg in rows:
        items.append({'chat_id': chat_id, 'name': _chat_name(c, chat_id, chat, known, owner),
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


def _load_quotes(c: LarkClient, msgs: list) -> set:
    """查好这批消息引用的那些消息的摘要和发送人(记在 _quotes 里), 返回被引用的消息 id:
    同一批里有的直接用, 其余用 PullMessagesByIdsRequest 一次批量查。发送人的名字由调用方再批量查。"""
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
    return wanted


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


def _chat_range(info: dict, count: int, after=None, before=None) -> tuple:
    """按会话信息算出要拉的 (start, end)。after: 取它之后的新消息(一次最多 50 条); before: 往前翻 count 条;
    都没有是打开会话: 最近 count 条, 未读多于一屏时往前带到第一条未读之前两条, 但总数不超过 MAX_INITIAL。"""
    last = int(info.get('lastMessagePosition') or 0)
    first = max(int(info.get('firstChatMessagePosition') or 1), 1)
    read = int(info.get('readPosition') or 0)
    if after is not None:
        return after + 1, min(last, after + 50)
    if before is not None:
        return max(first, before - count), before - 1
    end, start = last, max(first, last - count + 1)
    if 0 < read < last and read + 1 < start + 3:
        start = max(first, min(start, read - 2), last - MAX_INITIAL + 1)
    return start, end


def _fresh_info(c: ReadClient, chat_id: str) -> dict:
    info = _call(c.get_chat_info, chat_id) or {}
    _remember_chat(c, chat_id, info)
    return info


def _pull(c: ReadClient, chat_id: str, positions) -> list:
    positions = [p for p in positions if p > 0]
    return _call(c.pull_history, chat_id, positions, command_version=MODERN_COMMAND_VERSION) if positions else []


def chat(request: Request):
    """一个会话的消息。默认最近 count 条(有未读时往前带到第一条未读); before=<position> 往前翻,
    after=<position> 取新消息; 再加 local=1 只查本地库, 不访问飞书。

    要拉的范围取决于会话信息(最新 position、已读位置), 所以先按记下的会话信息猜范围, 和查最新会话信息同时进行;
    范围以查到的为准, 多拉的去掉, 猜漏的(比如这期间新消息太多、未读位置变了)再补拉。没有记下的会话信息时按顺序查。"""
    c = reader()
    chat_id = _digits(request.path_params['chat_id'], 'chat_id')
    q = request.query_params
    count = min(int(q.get('count') or 30), 100)
    st = Storage()
    after = int(_digits(q['after'], 'after')) if q.get('after') else None
    before = int(_digits(q['before'], 'before')) if q.get('before') and after is None else None
    guess, fetched, msgs = _cached_info(chat_id), range(0), []
    topic_guess = bool(guess) and decoders.is_thread_chat(guess)
    if after is not None and q.get('local'):
        # 本地库里是按会话位置存的消息, 和话题位置对不上; 话题群的新话题只从服务端取
        msgs, end = _local_messages(st, chat_id, after) if not topic_guess else ([], after)
        return _chat_out(c, chat_id, None, msgs, after + 1, end, None, st)
    if topic_guess:
        out = _topic_list(c, chat_id, count, after, before, st, guess=guess)
        if out is not None:
            return out
        guess = _cached_info(chat_id)  # 已经不是话题群了, 按刚查到的会话信息当普通会话继续
    if guess:
        g_start, g_end = _chat_range(guess, count, after, before)
        # 打开会话时往后多拉一些, 猜过之后来的新消息一般也就有了; 定时取新消息时不多拉, 没有新消息就只查会话信息
        if after is None and before is None and g_end > 0:
            g_end += SPECULATE_SLACK
        fetched = range(g_start, g_end + 1)
    if len(fetched):
        info, msgs = _parallel((_fresh_info, c, chat_id), (_pull, c, chat_id, fetched))
    else:
        info = _fresh_info(c, chat_id)
    if decoders.is_thread_chat(info):
        return _topic_list(c, chat_id, count, after, before, st, info=info)
    start, end = _chat_range(info, count, after, before)
    msgs = msgs + _pull(c, chat_id, [p for p in range(start, end + 1) if p not in fetched])
    first = max(int(info.get('firstChatMessagePosition') or 1), 1)
    msgs = sorted({int(m.get('position') or 0): m for m in msgs if start <= int(m.get('position') or 0) <= end}.values(),
                  key=lambda m: int(m.get('position') or 0))
    return _chat_out(c, chat_id, info, msgs, start, end, start > first, st, int(info.get('readPosition') or 0))


def _chat_out(c: ReadClient, chat_id: str, info, msgs: list, start: int, end: int, has_more, st: Storage, read: int = 0):
    # 本人私聊里匿名发送留下的中转消息(已撤回)不显示
    msgs = [m for m in msgs if not state.is_relay(m)]
    me, owner = str(c.auth.user_id), _owner_chat()
    pending = []
    if info is not None:
        hint = next((str(m['from_id']) for m in reversed(msgs) if m.get('from_id') and str(m['from_id']) != me), '')
        pending = _find_peers(c, st, {chat_id: (info, hint)}, owner)
    uids = [m.get('from_id') for m in msgs] + [m.get('remover_id') for m in msgs if m.get('recalled')] + [me]
    # 发送人的名字、被引用的消息、(少见)私聊的对方互不依赖, 同时查; 之后再补查引用里的发送人
    _, quoted, *_ = _parallel((_people, c, uids + [_p2p_peers.get(chat_id)], st), (_load_quotes, c, msgs),
                              *[(_server_peer, c, cid, chat) for cid, chat in pending])
    later = [(_quotes.get(i) or {}).get('from_id') for i in quoted] + ([_p2p_peers.get(chat_id)] if pending else [])
    _people(c, later, st)
    _lookup_names(c, [(u, chat_id) for u in uids + later])
    out = {'messages': [_message_out(c, m, chat_id) for m in msgs], 'start': start, 'end': end}
    if info is None:
        return {**out, 'chat': None, 'local': True}
    known = {chat_id: st.get_chat_name(chat_id)}
    last = int(info.get('lastMessagePosition') or 0)
    return {**out, 'has_more': has_more,
            'chat': {'chat_id': chat_id, 'name': _chat_name(c, chat_id, info, known, owner), 'kind': _kind(info),
                     'avatar': _chat_avatar(c, chat_id, info), 'unread': _badge(info), 'last_position': last,
                     'read_position': read, 'member_count': int(info.get('memberCount') or 0)}}


# ---- 话题群 ----

def _last_topic(info: dict) -> int:
    """最新话题的位置; 还没有话题时是 -1。"""
    v = info.get('lastThreadPosition')
    return int(v) if v is not None else -1


def _topic_positions(info: dict, count: int, after=None, before=None) -> range:
    """要查的话题位置(服务端从其中最大的往前凑够约这么多个看得见的话题)。before: 往前翻 count 个, 不依赖会话信息;
    after(定时刷新): 最近 TOPIC_REFRESH 个, 新话题和最近几个话题回复数的变化都在里面; 都没有是打开会话: 最近 count 个。"""
    if before is not None:
        return range(max(0, before - count), max(0, before))
    last = _last_topic(info)
    n = TOPIC_REFRESH if after is not None else count
    return range(max(0, last - n + 1), last + 1)


def _pull_topics(c: ReadClient, chat_id: str, positions) -> dict:
    if not len(positions):
        return {'threads': [], 'start': 0}
    return _call(c.pull_threads, chat_id, positions, command_version=MODERN_COMMAND_VERSION)


def _thread_out(t: dict) -> dict:
    return {'id': t['thread_id'], 'reply_count': t['reply_count'], 'last_position': t['last_position'],
            'update_time': t['update_time']}


def _load_senders(c: ReadClient, chat_id: str, msgs: list, st: Storage):
    """查好这批消息的发送人名字和被引用的消息(都记在内存缓存里), 做法同 _chat_out。"""
    me = str(c.auth.user_id)
    uids = [m.get('from_id') for m in msgs] + [m.get('remover_id') for m in msgs if m.get('recalled')] + [me]
    _, quoted = _parallel((_people, c, uids, st), (_load_quotes, c, msgs))
    later = [(_quotes.get(i) or {}).get('from_id') for i in quoted]
    _people(c, later, st)
    _lookup_names(c, [(u, chat_id) for u in uids + later])


def _topic_list(c: ReadClient, chat_id: str, count: int, after, before, st: Storage, guess=None, info=None):
    """话题群的话题列表, 格式同 chat(): messages 里每项是一个话题的根消息, position 是话题位置, 另带 thread
    (话题 id、回复数、最后一条回复的位置、最后有动静的时间); start/end/has_more 也都按话题位置。
    after 是定时刷新: 不管给的位置, 都重取最近 TOPIC_REFRESH 个话题, 页面用它替换这一段(回复数会变)。

    有记下的会话信息(guess)时按它猜要查的位置, 和查最新会话信息同时进行; 最新话题位置比猜的靠后(来了新话题或入群提示)
    就只补查多出来的那段, 多得太多才整段重查。会话信息里的已读位置换成 readThreadPosition, 页面据此画"以下为新话题"。"""
    got, guessed = None, range(0)
    if info is None:
        guessed = _topic_positions(guess, count, after, before)
        info, got = _parallel((_fresh_info, c, chat_id), (_pull_topics, c, chat_id, guessed))
        if not decoders.is_thread_chat(info):
            return None  # 会话模式变了(极少见): 由调用方按普通会话处理
    want = _topic_positions(info, count, after, before)
    if got is None:
        got = _pull_topics(c, chat_id, want)
    elif len(want) and (not len(guessed) or want[-1] > guessed[-1]):
        g_end = guessed[-1] if len(guessed) else -1
        if want[-1] - g_end > len(want):
            got = _pull_topics(c, chat_id, want)
        else:
            more = _pull_topics(c, chat_id, range(g_end + 1, want[-1] + 1))
            got = {'threads': got['threads'] + more['threads'], 'start': min(got['start'], more['start'])}
    end = want[-1] if len(want) else (before - 1 if before is not None else -1)
    start = min(got['start'], end + 1)
    found = {t['position']: t for t in got['threads'] if start <= t['position'] <= end}
    first = max(int(info.get('threadStartPosition') or 0), 0)
    # 看得见的话题很稀(比如只有管理员能发言的大群, 几乎全是入群提示)时服务端一次找不够, 接着往前找, 最多再查 TOPIC_ROUNDS 次
    rounds = 0
    while after is None and len(found) < count and start > first and rounds < TOPIC_ROUNDS:
        more = _pull_topics(c, chat_id, range(max(first, start - (count - len(found))), start))
        found.update({t['position']: t for t in more['threads'] if t['position'] < start})
        start, rounds = max(first, min(more['start'], start - 1)), rounds + 1
    threads = sorted(found.values(), key=lambda t: t['position'])
    _load_senders(c, chat_id, [t['root'] for t in threads], st)
    items = [{**_message_out(c, t['root'], chat_id), 'position': t['position'], 'thread': _thread_out(t)} for t in threads]
    known = {chat_id: st.get_chat_name(chat_id)}
    return {'messages': items, 'start': start, 'end': end, 'has_more': start > first,
            'chat': {'chat_id': chat_id, 'name': _chat_name(c, chat_id, info, known, _owner_chat()), 'kind': 'topic',
                     'avatar': _chat_avatar(c, chat_id, info), 'unread': _badge(info), 'last_position': _last_topic(info),
                     'read_position': int(info.get('readThreadPosition') or 0), 'member_count': int(info.get('memberCount') or 0)}}


def _position(value, what: str):
    """话题内的位置: 根消息是 -1, 回复从 0 起。"""
    if value in (None, ''):
        return None
    value = str(value)
    if not re.fullmatch(r'-?\d{1,12}', value) or int(value) < -1:
        raise ValueError(f'{what} 不合法')
    return int(value)


def _thread_info(c: ReadClient, thread_id: str):
    return _call(c.pull_threads_by_ids, [thread_id], command_version=MODERN_COMMAND_VERSION).get(thread_id)


def _pull_replies(c: ReadClient, chat_id: str, thread_id: str, positions) -> list:
    positions = [p for p in positions if p >= 0]
    return _call(c.pull_thread_messages, chat_id, thread_id, positions, command_version=MODERN_COMMAND_VERSION) if positions else []


def thread(request: Request):
    """话题群里的一个话题: 根消息(root)和回复(messages, 按话题内的位置从小到大, 也就是时间顺序)。
    默认最近 count 条回复; before=<位置> 往前翻; after=<位置> 取新回复(一次最多 50 条)。
    last=<位置> 是话题列表给的最后一条回复的位置: 有它就先按它猜着拉回复, 和查话题信息同时进行, 猜漏的再补拉。
    回复直接回的是根消息时不再显示引用(都在这个话题里); 回的是话题里的另一条时照常显示引用。"""
    c = reader()
    chat_id = _digits(request.path_params['chat_id'], 'chat_id')
    thread_id = _digits(request.path_params['thread_id'], 'thread_id')
    q = request.query_params
    count = min(int(q.get('count') or 30), 100)
    after = _position(q.get('after'), 'after')
    before = _position(q.get('before'), 'before') if after is None else None
    hint = _position(q.get('last'), 'last')
    if after is not None:
        guessed = range(after + 1, after + 51)
    elif before is not None:
        guessed = range(max(0, before - count), max(0, before))
    elif hint is not None:
        guessed = range(max(0, hint - count + 1), hint + THREAD_SLACK + 1)
    else:
        guessed = range(0)
    if len(guessed):
        t, msgs = _parallel((_thread_info, c, thread_id), (_pull_replies, c, chat_id, thread_id, guessed))
    else:
        t, msgs = _thread_info(c, thread_id), []
    if not t or str(t['root'].get('chat_id') or '') != chat_id:
        raise ValueError('找不到这个话题')
    last = t['last_position']
    if after is not None:
        start, end = after + 1, min(last, after + 50)
    elif before is not None:
        start, end = max(0, before - count), before - 1
    else:
        start, end = max(0, last - count + 1), last
    msgs = msgs + _pull_replies(c, chat_id, thread_id, [p for p in range(start, end + 1) if p not in guessed])
    msgs = sorted({int(m['position']): m for m in msgs if start <= int(m['position']) <= end}.values(),
                  key=lambda m: int(m['position']))
    msgs = [{**m, 'parent_id': ''} if str(m.get('parent_id') or '') == thread_id else m for m in msgs]
    root = t['root']
    _load_senders(c, chat_id, [root] + msgs, Storage())
    return {'root': {**_message_out(c, root, chat_id), 'position': -1},
            'messages': [_message_out(c, m, chat_id) for m in msgs], 'start': start, 'end': end, 'has_more': start > 0,
            'thread': _thread_out(t)}


def changes(request: Request):
    """本地库最新一条消息的序号: 页面每隔一两秒问一次, 变了就马上刷新会话列表, 不用等定时刷新。"""
    return {'latest': Storage().latest_message_id()}


def me(request: Request):
    c = reader()
    _people(c, [c.auth.user_id], Storage())
    p = _profile(c, c.auth.user_id, '')
    return {'user_id': str(c.auth.user_id), 'name': p['name'], 'avatar': _avatar(c, p['avatar'])}


def _text_message(text: str, mentions) -> dict:
    """输入框里的文字按 "@名字" 切成片段。mentions 是页面上从候选里选中的人 [{user_id, name}];
    文字里找不到的(选完又删掉了)不算, 没从候选里选、手打的 "@名字" 仍是普通文字。同名的按出现顺序依次对上。"""
    picks = []
    for m in (mentions if isinstance(mentions, list) else [])[:MAX_MENTIONS]:
        if not isinstance(m, dict):
            continue
        uid, name = str(m.get('user_id') or ''), str(m.get('name') or '').strip().lstrip('@')
        if uid.isdigit() and 0 < len(name) <= 100:
            picks.append((uid, name))
    if not picks:
        return {'kind': 'text', 'text': text}
    queue = {}
    for uid, name in picks:
        queue.setdefault(name, []).append(uid)
    pattern = re.compile('@(' + '|'.join(re.escape(n) for n in sorted(queue, key=len, reverse=True)) + ')')
    segs, pos = [], 0
    for m in pattern.finditer(text):
        uids = queue[m.group(1)]
        uid = uids.pop(0) if len(uids) > 1 else uids[0]
        segs += [{'type': 'text', 'text': text[pos:m.start()]}, {'type': 'at', 'user_id': uid, 'name': m.group(1)}]
        pos = m.end()
    segs.append({'type': 'text', 'text': text[pos:]})
    return {'kind': 'text', 'segments': segs}


def send(request: Request):
    """发文字。body: text, root_id/parent_id(回复时: 被回复那条的根消息和它自己, 可选), mentions(@ 了谁, 见 _text_message),
    plain_at(有未读时同意把 @ 当普通文字发出, 见 _deliver)。"""
    c = client()
    chat_id = _digits(request.path_params['chat_id'], 'chat_id')
    b = request.state.body
    text = str(b.get('text') or '').rstrip()
    if not text.strip():
        raise ValueError('消息内容为空')
    if len(text) > MAX_TEXT:
        raise ValueError(f'消息太长(超过 {MAX_TEXT} 字)')
    root_id = _digits(b['root_id'], 'root_id') if b.get('root_id') else None
    parent_id = _digits(b['parent_id'], 'parent_id') if b.get('parent_id') and root_id else None
    return _deliver(c, chat_id, _text_message(text, b.get('mentions')), root_id, plain_at=bool(b.get('plain_at')),
                    parent_id=parent_id)


def _deliver(c: LarkClient, chat_id: str, msg: dict, root_id=None, plain_at: bool = False, parent_id=None):
    """发出一条消息(文字、图片、文件都走这里): 一律匿名发送, 发出后核对, 记审计。

    有未读时经本人私聊转发(见 LarkClient.send_message), 转发会把 @ 变成普通文字: 带 @ 的消息这时先返回 409 让页面
    确认, plain_at 为真才以文字发出(结果里 at_plain 为真)。没有未读时照常发送, @ 是真的 @。
    """
    read, last = _call(c.read_state, chat_id)
    at_plain = builders.has_mentions(msg) and c.relays(chat_id, read, last, root_id)
    if at_plain and not plain_at:
        return Response(json.dumps({'error': AT_RELAY_NOTE, 'confirm': 'plain_at'}, ensure_ascii=False), status_code=409,
                        media_type='application/json')
    # 发送前记下别人发的、对我还是未读的消息, 发出后逐条核对; 显式回复的那条变成已读是"回复"本身的意思, 不算
    watch = []
    if read < last:
        try:
            watch = [i for i in _others_unread(c, chat_id, read, last) if i not in (root_id, parent_id)]
        except Exception as e:
            logger.warning(f'发送前查询未读消息失败: {e}')
            watch = None
    # 网页版一律匿名发送: 有未读时经本人私聊转发, 已读位置不动; 转发不成就算发送失败, 不退回普通发送(会清掉本人的未读)。
    # 发出后照样核对, 万一有变化如实报告
    # 回复: 网页上点"回复"是飞书常规的引用回复(留在主会话, 上方带被回复的那条), 不回进话题; 话题群里的回复照样进话题
    reply = {'parent_id': parent_id or root_id, 'in_thread': False} if root_id else {}
    msg_id = _call(c.send_message, msg, chat_id, root_id=root_id, keep_unread=True, fallback=False, **reply)
    Storage().log_action(actor='human', action='send', chat_id=chat_id, msg_id=str(msg_id or ''),
                         content=builders.plain_text(msg), ok=1 if msg_id else 0, error='' if msg_id else '发送失败',
                         turn_kind='web')
    if not msg_id:
        raise UpstreamError('发送失败: 飞书没有确认收到这条消息' + ('(经私聊转发没成功; 为了不清掉你的未读, 没有改用普通发送)'
                                                       if c.relays(chat_id, read, last, root_id) else ''))
    kept, newly = _check_unread(c, chat_id, read, watch) if read < last else (True, 0)
    if kept is False:
        logger.warning(f'匿名发送后会话 {chat_id} ' + (f'有 {newly} 条别人的消息变成了已读' if newly else '的已读位置前进了, 本人的未读被清掉'))
    out = {'ok': True, 'msg_id': str(msg_id), 'kept_unread': kept, 'newly_read': newly}
    if at_plain:
        out['at_plain'] = True
    return out


def _sniff_image(data: bytes) -> str:
    """按文件头认出普通图片, 返回 MIME; 认不出(包括可以带脚本的 svg)返回空串。"""
    sniffed = next((t for magic, t in IMAGE_MAGIC if data.startswith(magic)), '')
    if not sniffed and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        sniffed = 'image/webp'
    return sniffed


def _upload_name(name: str, kind: str) -> str:
    """发出去的文件名: 去掉路径和控制字符, 限制长度。"""
    name = re.sub(r'[\x00-\x1f\x7f]', '', str(name or '')).replace('\\', '/').rsplit('/', 1)[-1].strip()[:200]
    return name or ('image.png' if kind == 'image' else 'file')


def _size_text(n: int) -> str:
    return f'{n // (1024 * 1024)}MB'


async def upload(request: Request):
    """发图片或文件。请求体是文件的原始内容(不是表单), query: kind=image|file, name=文件名, type=MIME,
    root_id/parent_id=回复哪条(可选, 同 send)。边收边写进临时文件(超过上限立即停止), 上传到飞书后和文字一样匿名发出。"""
    chat_id = _digits(request.path_params['chat_id'], 'chat_id')
    q = request.query_params
    kind = q.get('kind')
    if kind not in ('image', 'file'):
        raise ValueError('kind 只能是 image 或 file')
    limit = MAX_IMAGE if kind == 'image' else MAX_FILE
    too_big = f'图片不能超过 {_size_text(MAX_IMAGE)}，可以作为文件发送' if kind == 'image' else f'文件不能超过 {_size_text(MAX_FILE)}'
    name = _upload_name(q.get('name'), kind)
    mime = q.get('type') or ''
    if not re.fullmatch(r'[\w.+\-]{1,100}/[\w.+\-]{1,100}', mime):
        mime = ''
    root_id = _digits(q['root_id'], 'root_id') if q.get('root_id') else None
    parent_id = _digits(q['parent_id'], 'parent_id') if q.get('parent_id') and root_id else None
    if int(request.headers.get('content-length') or 0) > limit:
        raise ValueError(too_big)
    with tempfile.TemporaryFile() as tmp:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise ValueError(too_big)
            tmp.write(chunk)
        if not size:
            raise ValueError('文件是空的')
        tmp.seek(0)
        return await asyncio.to_thread(_send_upload, chat_id, kind, tmp, size, name, mime, root_id, parent_id)


def _send_upload(chat_id: str, kind: str, fileobj, size: int, name: str, mime: str, root_id, parent_id=None):
    c = client()
    try:
        if kind == 'image':
            data = fileobj.read()
            mime = _sniff_image(data)
            if not mime:
                raise ValueError('只能发 PNG、JPEG、GIF、WebP 图片，其他格式可以作为文件发送')
            msg = {'kind': 'image', 'key': upload_image(c.auth, data, name, mime), 'name': name}
        else:
            key = upload_file(c.auth, fileobj, size, name, mime or 'application/octet-stream')
            msg = {'kind': 'file', 'key': key, 'name': name, 'size': size, 'mime': mime}
    except UploadError as e:
        logger.warning(f'上传到飞书失败: {e}')
        raise UpstreamError(f'上传到飞书失败: {e}') from e
    return _deliver(c, chat_id, msg, root_id, parent_id=parent_id)


def _real_name(chatter: dict, locale: str) -> str:
    """@ 时显示的名字: 用对方自己的名字(当前语言), 不用本人给他设的备注名。"""
    return _display_name({k: v for k, v in chatter.items() if k != 'alias'}, locale)


def members(request: Request):
    """@ 候选: 会话成员(chats.PullChatChattersRequest, 只读), 不含本人, 按群里的顺序。
    complete 为假时(大群只返回了前 2000 人)页面在这里找不到再去搜联系人。"""
    c = client()
    chat_id = _digits(request.path_params['chat_id'], 'chat_id')
    hit = _members.get(chat_id)
    if not hit or time.time() - hit[0] > MEMBERS_TTL:
        r = _call(c.api, 'chats.PullChatChattersRequest', {'chatId': chat_id, 'needChatters': True}) or {}
        chatters = r.get('chatters') or {}
        weight = {str(k): int(v or 0) for k, v in (r.get('orderedWeight') or {}).items()}
        locale, me = c.auth.region.locale, str(c.auth.user_id)
        out = []
        for uid, ch in sorted(chatters.items(), key=lambda kv: weight.get(str(kv[0]), 0)):
            uid = str(uid)
            name = _real_name(ch, locale) if isinstance(ch, dict) else ''
            if uid == me or not uid.isdigit() or not name:
                continue
            alias = _display_name(ch, locale)
            out.append({'user_id': uid, 'name': name, 'alias': alias if alias != name else '',
                        'pinyin': str(ch.get('namePinyin') or ch.get('namePy') or ''), 'en': str(ch.get('enUsName') or ''),
                        'avatar': _avatar(c, (ch.get('avatar') or {}).get('key') or ''), 'bot': ch.get('type') == 2})
        if len(_members) > 200:
            _members.clear()
        hit = _members[chat_id] = (time.time(), out, len(chatters) < MEMBERS_CAP)
    return {'members': hit[1], 'complete': hit[2]}


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
        ctype = _sniff_image(data) or 'application/octet-stream'
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
    ('/api/im/chat/{chat_id}/upload', upload, ['POST']),
    ('/api/im/chat/{chat_id}/members', members, ['GET']),
    ('/api/im/chat/{chat_id}/thread/{thread_id}', thread, ['GET']),
    ('/api/im/search', search, ['GET']),
    ('/api/im/open', open_p2p, ['POST']),
    ('/api/im/media', media, ['GET']),
]
