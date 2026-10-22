import threading
import time

import pytest
import requests
from starlette.testclient import TestClient

from larkx.storage import Storage
from larkx.web import app as web
from larkx.web import im


# 网页版飞书只许调用这些只读接口; 标已读、进入会话之类的接口一律不许碰
READ_ONLY_APIS = {'chatters.PullChattersByIdsRequest', 'feed.PullFeedCardsRequest'}


class FakeIMClient:
    """网页版飞书用到的协议接口; 调到 mark_read 或其他会改已读的接口就算失败。"""

    def __init__(self):
        from larkx.protocol import get_region
        self.auth = type('A', (), {'user_id': '100', 'region': get_region('feishu')})()
        self.sent = []
        self.messages = []    # 发出的消息描述(builders 的格式)
        self.relay_fails = False  # 经本人私聊转发不成(网页版这时报发送失败)
        self.read_pos = 95
        self.done = set()     # 对我已是"已读"的消息
        self.leak = []        # 发出时被连带标成已读的消息(经本人私聊转发不该有, 用来验证核对逻辑)
        self.advance = False  # 转发不成、退回了普通发送: 本人的已读位置前进到最新
        self.status_error = False
        self.chat_read = 0    # 会话信息里本人的已读位置
        self.history = {}     # position -> 消息, 覆盖默认生成的
        self.by_id = {}       # pull_messages 能查到的消息
        self.pulled = []      # 记下 pull_history / pull_messages 的调用
        self.offline = False  # 置上后任何服务端请求都失败, 用来验证只读本地库的路径
        self.last = 100       # 会话最新的 position
        self.barrier = None   # 设上后查会话信息和拉消息要同时在进行才能继续(只验证一次), 用来验证两者是并发的
        self.feed_calls = 0
        self.feed_delay = 0
        self.names_asked = []  # 单个查名字(get_user_name)的调用
        self.reply_args = []   # 每次发送的 (root_id, parent_id, in_thread)
        # 话题群: 会话 id -> 会话信息; 话题位置 -> 话题(格式同 LarkClient._thread_dict); 看不见的话题位置;
        # 话题 id -> {话题内位置: 回复}; 服务端一次最多往前查多少个位置
        self.topics = {}
        self.threads = {}
        self.hidden = set()
        self.replies = {}
        self.scan_cap = 1000
        self.thread_pulls = []
        self.feed = {'cards': [{'id': 1, 'type': 1, 'rankTime': 50}, {'id': 2, 'type': 1, 'rankTime': 40},
                               {'id': 9, 'type': 13, 'rankTime': 30}, {'id': 3, 'type': 1, 'isHide': True}],
                     'chats': {'1': {'name': '项目群', 'type': 2, 'lastMessagePositionBadgeCount': 7, 'readPositionBadgeCount': 4,
                                     'isRemind': False},
                               '2': {'name': '', 'type': 1, 'lastMessageId': 'm2', 'avatar': {'key': 'c2'}}},
                     'messages': {'m2': {'fromId': '200', 'type': 4, 'content': b''}},
                     'nextCursor': 123}

    def api(self, cmd, data=None):
        assert cmd in READ_ONLY_APIS, f'网页版飞书调用了不该调用的接口 {cmd}'
        if cmd == 'chatters.PullChattersByIdsRequest':
            people = {'100': {'name': '我本人', 'avatar': {'key': 'a100'}},
                      '200': {'name': '张三', 'alias': '张三(备注)', 'avatar': {'key': 'a200'}},
                      '300': {'name': 'Li', 'displayName': {'defaultVal': 'Li', 'i18nVals': {'zh_cn': '李四'}}}}
            return {'chatters': {u: people[u] for u in data['chatterIds'] if u in people}}
        assert cmd == 'feed.PullFeedCardsRequest'
        self.feed_calls += 1
        time.sleep(self.feed_delay)
        return self.feed

    def _meet(self):
        b = self.barrier
        if b:
            b.wait(timeout=2)
            self.barrier = None

    def get_user_name(self, uid, chat_id):
        self.names_asked.append(uid)
        return None

    def get_chat_info(self, chat_id):
        assert not self.offline, '不该访问服务端'
        self._meet()
        if str(chat_id) in self.topics:
            return dict(self.topics[str(chat_id)])
        return {'name': '项目群', 'type': 2, 'lastMessagePosition': self.last, 'firstChatMessagePosition': 60,
                'lastMessagePositionBadgeCount': 3, 'readPositionBadgeCount': 3, 'memberCount': 5, 'readPosition': self.chat_read}

    def pull_history(self, chat_id, positions, command_version=None):
        assert not self.offline, '不该访问服务端'
        self._meet()
        positions = list(positions)
        self.pulled.append(('history', positions[0] if positions else None, positions[-1] if positions else None, command_version))
        return [self.history.get(p) or {
            'msg_id': str(p), 'position': p, 'from_id': '100' if p % 2 else '300', 'create_time': p,
            'msg_type': 5 if p == 100 else 4, 'msg_type_name': 'IMAGE' if p == 100 else 'TEXT',
            'content': '[图片] imageKey=k1 (1x1)' if p == 100 else f'第{p}条',
            'content_data': {'image': {'origin': {'key': 'img_k1', 'width': 4, 'height': 3}}} if p == 100 else None}
            for p in positions]

    def pull_threads(self, chat_id, positions, command_version=None):
        """和服务端一样: 从最大的位置往前找, 跳过看不见的话题, 凑够 len(positions) 个看得见的(最多查 scan_cap 个位置)。"""
        assert not self.offline, '不该访问服务端'
        self._meet()
        positions = sorted(positions)
        self.thread_pulls.append((positions[0], positions[-1], command_version))
        found, pos = [], positions[-1]
        while pos >= 0 and len(found) < len(positions) and positions[-1] - pos < self.scan_cap:
            if pos in self.threads and pos not in self.hidden:
                found.append(self.threads[pos])
            pos -= 1
        return {'threads': sorted(found, key=lambda t: t['position']), 'start': max(pos + 1, 0)}

    def pull_threads_by_ids(self, ids, command_version=None):
        assert not self.offline, '不该访问服务端'
        self._meet()
        self.thread_pulls.append(('ids', list(ids), command_version))
        return {t['thread_id']: t for t in self.threads.values() if t['thread_id'] in ids}

    def pull_thread_messages(self, chat_id, thread_id, positions, command_version=None):
        assert not self.offline, '不该访问服务端'
        self._meet()
        positions = list(positions)
        self.thread_pulls.append(('replies', thread_id, positions[0], positions[-1], command_version))
        got = self.replies.get(thread_id) or {}
        return [got[p] for p in positions if p in got]

    def pull_messages(self, ids, command_version=None):
        assert not self.offline, '不该访问服务端'
        self.pulled.append(('ids', sorted(ids), command_version))
        return [self.by_id[i] for i in ids if i in self.by_id]

    def send_message(self, msg, chat_id, root_id=None, keep_unread=False, fallback=True, parent_id=None, in_thread=None):
        from larkx.protocol.proto import builders
        assert fallback is False, '网页版转发不成不能退回普通发送'
        self.reply_args.append((root_id, parent_id, in_thread))
        if self.relay_fails and self.relays(chat_id, *self.read_state(chat_id), root_id):
            return None
        self.sent.append((chat_id, builders.plain_text(msg), root_id, keep_unread))
        self.messages.append(msg)
        # 和服务端一样: 话题回复时被回复的那条变成已读; 经本人私聊转发不动已读位置
        self.done.update(self.leak + [i for i in (root_id, parent_id) if i])
        if self.advance:
            self.read_pos = 100
        return '777'

    def relays(self, chat_id, read, last, root_id=None):
        return not root_id and read < last and str(chat_id) != 'self'

    def read_state(self, chat_id):
        return (self.read_pos, 100)

    def my_read_status(self, ids):
        if self.status_error:
            raise RuntimeError('boom')
        return {i: i in self.done for i in ids}

    def mark_read(self, *a, **k):
        raise AssertionError('网页版飞书不能标已读')

    def note_chat(self, chat_id, info):
        pass


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    fake = FakeIMClient()
    monkeypatch.setattr(web, 'TOKEN_PATH', tmp_path / 'web_token')
    monkeypatch.setattr(im, 'client', lambda: fake)
    monkeypatch.setattr(im, 'reader', lambda: fake)
    monkeypatch.setattr(im, '_owner_chat', lambda: 'self')
    for cache in ('_profiles', '_name_tried', '_p2p_names', '_p2p_peers', '_quotes', '_chat_infos'):
        monkeypatch.setattr(im, cache, {})
    monkeypatch.setattr(im, '_feed', {'data': None, 'at': 0.0, 'flight': None})
    monkeypatch.setattr(im, 'READ_CHECK_DELAY', 0)
    c = TestClient(web.create_app())
    c.headers.update({'Authorization': f'Bearer {web.get_token()}', 'X-Larkx': '1'})
    return c, fake


def test_feed_lists_chats_with_unread(ctx):
    c, _ = ctx
    d = c.get('/api/im/feed').json()
    assert [(i['chat_id'], i['name'], i['kind'], i['unread'], i['muted']) for i in d['items']] == [
        ('1', '项目群', 'group', 3, True), ('2', '张三(备注)', 'p2p', 0, False)]
    assert d['items'][1]['avatar'].startswith('https://s1-imfile.feishucdn.com/static-resource/v1/c2~')
    assert c.get('/api/im/me').json()['name'] == '我本人'
    assert d['next_cursor'] == '123'


def test_chat_pages_and_marks_own_messages(ctx):
    c, _ = ctx
    d = c.get('/api/im/chat/5?count=10').json()
    assert (d['start'], d['end'], d['has_more']) == (91, 100, True)
    last = d['messages'][-1]
    assert last['kind'] == 'image' and last['image'] == {'key': 'img_k1', 'w': 4, 'h': 3}
    assert last['content'] == '[图片]' and last['me'] is False
    assert d['messages'][0]['segments'] == [{'type': 'text', 'text': '第91条'}]
    assert d['messages'][0]['me'] is True and d['messages'][1]['sender'] == '李四' and d['messages'][1]['avatar'] == ''
    older = c.get('/api/im/chat/5?before=61&count=30').json()
    assert (older['start'], older['end'], older['has_more']) == (60, 60, False)
    assert c.get('/api/im/chat/5?after=100').json()['messages'] == []
    assert c.get('/api/im/chat/abc').status_code == 400


def test_send_is_audited_as_owner(ctx):
    c, fake = ctx
    assert c.post('/api/im/chat/5/send', json={'text': '  '}).status_code == 400
    assert c.post('/api/im/chat/5/send', json={'text': 'hi', 'root_id': 'x'}).status_code == 400
    r = c.post('/api/im/chat/5/send', json={'text': '收到\n', 'root_id': '42'})
    assert r.json() == {'ok': True, 'msg_id': '777', 'kept_unread': True, 'newly_read': 0} and fake.sent == [('5', '收到', '42', True)]
    # 网页上点"回复"是主会话里的引用回复: 引用点的那条(parent_id), 不回进话题; 没给 parent_id 时引用 root_id 那条
    assert fake.reply_args == [('42', '42', False)]
    c.post('/api/im/chat/5/send', json={'text': '好', 'root_id': '40', 'parent_id': '42'})
    assert fake.reply_args[-1] == ('40', '42', False)
    assert c.post('/api/im/chat/5/send', json={'text': '好', 'root_id': '40', 'parent_id': 'x'}).status_code == 400
    c.post('/api/im/chat/5/send', json={'text': '好', 'parent_id': '42'})  # 不是回复时 parent_id 不起作用
    assert fake.reply_args[-1] == (None, None, None)
    a = Storage().list_actions()[-1]
    assert (a.actor, a.action, a.chat_id, a.msg_id, a.turn_kind) == ('human', 'send', '5', '777', 'web')
    # 改动类请求一样要带 X-Larkx
    assert c.post('/api/im/chat/5/send', json={'text': 'hi'}, headers={'X-Larkx': ''}).status_code == 403


def test_send_reports_others_messages_turned_read(ctx):
    c, fake = ctx
    send = lambda **b: c.post('/api/im/chat/5/send', json={'text': 'hi', **b}).json()
    # 未读区间 96~100 里别人发的是 96、98、100; 发出后 98 变成了已读
    fake.leak = ['98']
    r = send()
    assert (r['kept_unread'], r['newly_read']) == (False, 1)
    # 98 发送前就已经是已读, 不再算; 96 是显式回复的
    assert send(root_id='96')['kept_unread'] is True
    # 显式回复的那条变成已读是"回复"本身的意思, 不算泄露
    fake.leak, fake.done = [], set()
    assert send(root_id='98') == {'ok': True, 'msg_id': '777', 'kept_unread': True, 'newly_read': 0}
    fake.done = set()
    fake.leak = ['96', '100']
    assert send(root_id='98')['newly_read'] == 2
    # 引用回复时被引用的那条(parent)变成已读也是"回复"本身的意思
    fake.leak, fake.done = [], set()
    assert send(root_id='90', parent_id='98')['newly_read'] == 0


def test_send_reports_fallback_and_unknown(ctx):
    c, fake = ctx
    # 经本人私聊转发: 已读位置不动, 别人的未读也都还在
    assert c.post('/api/im/chat/5/send', json={'text': 'hi'}).json() == {'ok': True, 'msg_id': '777', 'kept_unread': True, 'newly_read': 0}
    assert fake.sent[-1] == ('5', 'hi', None, True)
    fake.status_error = True
    assert c.post('/api/im/chat/5/send', json={'text': 'hi'}).json()['kept_unread'] is None
    # 万一发出后本人的已读位置还是前进了(比如服务端行为变了), 如实告诉页面
    fake.status_error, fake.advance = False, True
    assert c.post('/api/im/chat/5/send', json={'text': 'hi'}).json() == {'ok': True, 'msg_id': '777', 'kept_unread': False, 'newly_read': 0}


def test_media_only_renders_plain_images(ctx, monkeypatch):
    c, _ = ctx
    monkeypatch.setattr(im, 'download', lambda auth, url, max_bytes: (b'<svg onload=alert(1)>', 'image/svg+xml'))
    r = c.get('/api/im/media?chat=1&msg=2&key=img_k1')
    assert r.headers['content-type'] == 'application/octet-stream' and r.headers['content-disposition'] == 'attachment'
    monkeypatch.setattr(im, 'download', lambda auth, url, max_bytes: (b'\x89PNG', 'image/png'))
    assert c.get('/api/im/media?chat=1&msg=2&key=img_k1').headers['content-type'] == 'image/png'
    assert c.get('/api/im/media?chat=1&msg=2&key=../x').status_code == 400


def test_chat_opens_at_first_unread_without_marking_read(ctx):
    from larkx.protocol.client import MODERN_COMMAND_VERSION
    c, fake = ctx
    # 未读多于一屏: 往前带到第一条未读之前两条
    fake.chat_read = 72
    d = c.get('/api/im/chat/5').json()
    assert (d['start'], d['end'], d['chat']['read_position']) == (70, 100, 72)
    # 用新版格式拉(卡片才完整)
    assert fake.pulled[-1] == ('history', 70, 100, MODERN_COMMAND_VERSION)
    # 第一条未读已在最近一屏里就不多拉; 再早的未读最多带到会话开头
    fake.chat_read = 80
    assert c.get('/api/im/chat/5').json()['start'] == 71
    fake.chat_read = 10
    assert c.get('/api/im/chat/5').json()['start'] == 60


def test_recalled_and_relay_messages(ctx):
    from larkx import state
    c, fake = ctx
    state.mark_relay('cid-relay', '5')
    fake.history[99] = {'msg_id': '99', 'position': 99, 'from_id': '300', 'create_time': 99, 'msg_type': 4, 'recalled': True,
                        'content': '', 'content_data': {}}
    fake.history[97] = {**fake.history[99], 'msg_id': '97', 'position': 97, 'from_id': '100'}
    fake.history[98] = {'msg_id': '98', 'position': 98, 'from_id': '100', 'cid': 'cid-relay', 'chat_id': '5', 'msg_type': 4,
                        'recalled': True, 'content': 'hi', 'content_data': {}}
    msgs = {m['position']: m for m in c.get('/api/im/chat/5?count=5').json()['messages']}
    assert 98 not in msgs  # 匿名发送经本人私聊中转的那条不显示
    assert (msgs[99]['kind'], msgs[99]['content']) == ('recalled', '李四 撤回了一条消息')
    assert msgs[97]['content'] == '你 撤回了一条消息'


def test_quotes_show_replied_message(ctx):
    c, fake = ctx
    reply = lambda p, parent: {'msg_id': str(p), 'position': p, 'from_id': '300', 'create_time': p, 'msg_type': 4,
                               'content': f'回复{p}', 'content_data': None, 'parent_id': parent, 'root_id': parent}
    fake.history.update({96: reply(96, '95'), 94: reply(94, 'p1'), 92: reply(92, 'gone')})
    fake.by_id['p1'] = {'msg_id': 'p1', 'from_id': '200', 'msg_type': 4, 'content': '原来那条\n消息', 'content_data': None}
    msgs = {m['position']: m for m in c.get('/api/im/chat/5?count=10').json()['messages']}
    assert msgs[96]['quote'] == {'sender': '我', 'text': '第95条'}  # 同一批里就有
    assert msgs[94]['quote'] == {'sender': '张三(备注)', 'text': '原来那条 消息'}
    assert msgs[92]['quote'] == {'sender': '', 'text': '引用的消息'}  # 查不到
    assert msgs[93]['quote'] is None
    assert [p for p in fake.pulled if p[0] == 'ids'] == [('ids', ['gone', 'p1'], '7.72.0')]
    # 查到过的记住了, 不再重复查
    c.get('/api/im/chat/5?count=10')
    assert [p[1] for p in fake.pulled if p[0] == 'ids'] == [['gone', 'p1'], ['gone']]


def test_local_poll_reads_storage_only(ctx):
    from larkx.protocol.proto import decoders, lark_all_pb2 as L, proto_pb2 as P
    c, fake = ctx
    st = Storage()
    tc = P.TextContent(text='加粗')
    el = tc.richText.elements.dictionary['1']
    el.tag, el.property = 1, L.entities.RichTextElement.TextProperty(content='加粗').SerializeToString()
    el.style['fontWeight'] = 'bold'
    tc.richText.elementIds.append('1')
    summary, data = decoders.decode_message_content(4, tc.SerializeToString())
    base = {'chat_id': '5', 'from_id': '300', 'msg_type': 4, 'msg_type_name': 'TEXT', 'create_time': 1}
    st.save_message({**base, 'msg_id': 'a', 'position': 101, 'content': summary, 'content_data': data})
    st.save_message({**base, 'msg_id': 'b', 'position': 102, 'content': '第二条'})
    st.save_message({**base, 'msg_id': 'd', 'position': 104, 'content': '隔了一条'})
    fake.offline = True
    d = c.get('/api/im/chat/5?after=100&local=1').json()
    assert d['chat'] is None and (d['start'], d['end']) == (101, 102)
    assert [m['msg_id'] for m in d['messages']] == ['a', 'b']
    assert d['messages'][0]['segments'] == [{'type': 'text', 'text': '加粗', 'bold': True}]
    # 中间缺了 103: 停在缺口前, 留给服务端拉
    assert c.get('/api/im/chat/5?after=102&local=1').json()['messages'] == []
    # 卡片可能还在流式输出, 本地库里的不算数
    st.save_message({**base, 'msg_id': 'c', 'position': 103, 'msg_type': 14, 'content': '[卡片]', 'content_data': {}})
    assert c.get('/api/im/chat/5?after=102&local=1').json()['end'] == 102
    latest = c.get('/api/im/changes').json()['latest']
    st.save_message({**base, 'msg_id': 'e', 'position': 105})
    assert c.get('/api/im/changes').json()['latest'] == latest + 1


def test_media_sniffs_images_and_streams_downloads(ctx, monkeypatch):
    c, _ = ctx
    # 表情包这类图片服务端按 octet-stream 返回: 认得出文件头的按图片显示
    monkeypatch.setattr(im, 'download', lambda auth, url, max_bytes: (b'\xff\xd8\xff\xe0JFIF', 'application/octet-stream'))
    r = c.get('/api/im/media?chat=1&msg=2&key=stk')
    assert r.headers['content-type'] == 'image/jpeg' and 'content-disposition' not in r.headers
    monkeypatch.setattr(im, 'download', lambda auth, url, max_bytes: (b'PK\x03\x04', 'application/octet-stream'))
    assert c.get('/api/im/media?chat=1&msg=2&key=f').headers['content-disposition'] == 'attachment'
    opened = []
    monkeypatch.setattr(im, 'open_stream', lambda auth, url: opened.append(url) or (iter([b'ab', b'c']), 3))
    r = c.get('/api/im/media?chat=1&msg=2&key=file_k&dl=1&name=' + '报告 v2.pdf')
    assert r.content == b'abc' and r.headers['content-type'] == 'application/octet-stream'
    assert r.headers['content-disposition'] == 'attachment; filename="__ v2.pdf"; filename*=UTF-8\'\'%E6%8A%A5%E5%91%8A%20v2.pdf'
    assert opened[0].endswith('/download/messages/2/keys/file_k?chat_id=1')
    bad = c.get('/api/im/media?chat=1&msg=2&key=file_k&dl=1&name=../../etc/passwd').headers['content-disposition']
    assert '/' not in bad.split(';')[1]


def test_client_declares_modern_version_only_when_asked(monkeypatch):
    from larkx.protocol import client as client_mod, get_region
    from larkx.protocol.client import LarkClient
    from larkx.protocol.proto import proto_pb2 as P
    c = object.__new__(LarkClient)
    c.auth = type('A', (), {'user_id': '100', 'region': get_region('feishu'), 'gateway_app_id': '1', 'cookies': {}})()
    c._thread_chats = {}
    seen = []

    def post(url, headers, cookies, data, timeout):
        seen.append(headers['x-command-version'])
        return type('R', (), {'content': P.Packet().SerializeToString(), 'raise_for_status': lambda self: None})()
    monkeypatch.setattr(client_mod.requests, 'post', post)
    assert c.pull_messages(['1', '2'], command_version='7.72.0') == []
    assert seen == ['7.72.0']
    # daemon 用的默认版本不变(查会话模式的那次请求也是)
    assert c.pull_history('5', [1, 2]) == []
    assert seen[1:] == ['5.7.0', '5.7.0']


def test_clean_keeps_only_readable_parts():
    assert im._clean('[系统消息] type=124 ') == '[系统消息]'
    assert im._clean('[系统消息] type=1 张三 加入了群聊') == '张三 加入了群聊'
    assert im._clean('[语音] 时长3.2s, key=abc') == '[语音] 3.2 秒'
    assert im._clean('[文件] a.pdf (application/pdf, 10字节, key=k)') == '[文件] a.pdf'


def test_read_client_only_sends_queries_over_a_reused_connection(monkeypatch):
    """只读客户端: 查询走复用的连接(连接刚好断了重试一次); 发消息、标已读这类命令一个请求都发不出去。"""
    from larkx.protocol import client as client_mod, get_region
    from larkx.protocol.proto import proto_pb2 as P
    auth = type('A', (), {'user_id': '100', 'region': get_region('feishu'), 'gateway_app_id': '1', 'cookies': {}})()
    rc = im.ReadClient(auth)
    posts, fail = [], []

    class Session:
        def post(self, url, headers, cookies, data, timeout):
            posts.append(headers['x-command'])
            if fail:
                raise fail.pop(0)
            return type('R', (), {'content': P.Packet().SerializeToString(), 'raise_for_status': lambda self: None})()
    rc.http = Session()
    monkeypatch.setattr(client_mod.requests, 'post', lambda *a, **k: pytest.fail('只读客户端不该每次新建连接'))
    rc.api('feed.PullFeedCardsRequest', {'type': 1})
    assert posts == ['1000']
    fail.append(requests.ConnectionError('连接被关闭'))
    rc.get_chat_info('5')
    assert posts[1:] == ['64', '64']
    fail.append(requests.ConnectTimeout('超时'))  # 超时不重试
    with pytest.raises(requests.ConnectTimeout):
        rc.get_chat_info('5')
    posts.clear()
    assert rc.mark_read('5', 100) is False
    assert rc.send_msg('hi', '5') is None
    with pytest.raises(RuntimeError, match='只读客户端'):
        rc.api('messages.PutReadMessagesRequest', {'chatId': '5', 'maxPosition': 1})
    assert posts == []
    # 记下会话模式后, 拉消息不再为此查会话信息
    rc.note_chat('7', {'chatMode': 2})
    assert rc.is_thread_chat('7') is True and posts == []


def test_chat_open_queries_info_and_messages_together(ctx):
    c, fake = ctx
    # 第一次打开: 没有记下的会话信息, 先查信息再拉消息
    d = c.get('/api/im/chat/5').json()
    assert (d['start'], d['end']) == (71, 100) and fake.pulled == [('history', 71, 100, '7.72.0')]
    # 再打开: 按记下的会话信息猜范围, 和查会话信息同时进行(两边要同时在进行才能通过 barrier); 多拉的新消息位置去掉
    fake.barrier, fake.pulled = threading.Barrier(2), []
    d = c.get('/api/im/chat/5').json()
    assert (d['start'], d['end'], len(d['messages'])) == (71, 100, 30)
    assert fake.pulled == [('history', 71, 120, '7.72.0')]
    # 这期间来了新消息, 还在多拉的范围里: 不用补拉
    fake.last, fake.pulled, fake.barrier = 110, [], threading.Barrier(2)
    d = c.get('/api/im/chat/5').json()
    assert (d['start'], d['end'], d['messages'][-1]['position']) == (81, 110, 110) and len(fake.pulled) == 1
    # 新消息超出了多拉的范围: 补拉缺的那段
    fake.last, fake.pulled, fake.barrier = 140, [], threading.Barrier(2)
    d = c.get('/api/im/chat/5').json()
    assert (d['start'], d['end']) == (111, 140) and [m['position'] for m in d['messages']] == list(range(111, 141))
    assert fake.pulled[-1][1:3] == (131, 140)
    # 往前翻也是同时查
    fake.pulled, fake.barrier = [], threading.Barrier(2)
    older = c.get('/api/im/chat/5?before=111&count=30').json()
    assert (older['start'], older['end'], older['has_more']) == (81, 110, True) and fake.pulled == [('history', 81, 110, '7.72.0')]
    # 定时取新消息: 记下的会话信息里没有新消息时只查会话信息, 有了再拉
    fake.pulled = []
    assert c.get('/api/im/chat/5?after=140').json()['messages'] == [] and fake.pulled == []
    fake.last = 142
    assert [m['position'] for m in c.get('/api/im/chat/5?after=140').json()['messages']] == [141, 142]


def test_feed_requests_share_one_query_and_cached_returns_last(ctx):
    c, fake = ctx
    assert c.get('/api/im/feed?cached=1').json()['items']  # 还没有上一次的结果: 正常查
    assert fake.feed_calls == 1
    d = c.get('/api/im/feed?cached=1').json()
    assert d['cached'] is True and [i['chat_id'] for i in d['items']] == ['1', '2'] and fake.feed_calls == 1
    # 同时来的几个请求共用一次查询
    fake.feed_delay = 0.3
    out = []
    threads = [threading.Thread(target=lambda: out.append(im._first_feed_page())) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake.feed_calls == 2 and len(out) == 3 and all(o is out[0] for o in out)
    # 之后的请求照常查新的
    fake.feed_delay = 0
    c.get('/api/im/feed')
    assert fake.feed_calls == 3


def test_unnamed_senders_are_not_looked_up_every_time(ctx):
    c, fake = ctx
    fake.history[100] = {'msg_id': '100', 'position': 100, 'from_id': '999', 'create_time': 100, 'msg_type': 4,
                         'content': '机器人发的', 'content_data': None}
    for _ in range(3):
        assert c.get('/api/im/chat/5?count=5').json()['messages'][-1]['sender'] == ''
    assert fake.names_asked == ['999']
    im._name_tried['999'] -= im.NAME_RETRY + 1  # 过了一阵再试
    c.get('/api/im/chat/5?count=5')
    assert fake.names_asked == ['999', '999']


def test_p2p_peer_found_in_local_storage_first(ctx):
    c, fake = ctx
    # 私聊最后一条是本人发的: 先查本地库里对方发过的消息, 不去服务端拉
    fake.feed['messages']['m2']['fromId'] = '100'
    fake.feed['chats']['2']['lastMessagePosition'] = 5
    Storage().save_message({'msg_id': 'x1', 'chat_id': '2', 'from_id': '200', 'msg_type': 4, 'position': 3, 'content': 'hi'})
    items = {i['chat_id']: i for i in c.get('/api/im/feed').json()['items']}
    assert items['2']['name'] == '张三(备注)'
    assert fake.pulled == []
    # 本地库也没有: 去服务端拉最近 10 条找对方
    fake.feed['chats']['4'] = {'name': '', 'type': 1, 'lastMessageId': 'm4', 'lastMessagePosition': 8}
    fake.feed['messages']['m4'] = {'fromId': '100', 'type': 4, 'content': b''}
    fake.feed['cards'].append({'id': 4, 'type': 1, 'rankTime': 20})
    items = {i['chat_id']: i for i in c.get('/api/im/feed').json()['items']}
    assert items['4']['name'] == '李四' and fake.pulled == [('history', 1, 8, None)]
    # 找到过就记住了
    c.get('/api/im/feed')
    assert len(fake.pulled) == 1


# ---- 话题群 ----

def _topic(pos, chat='7', replies=0, sender='300'):
    """一个话题(格式同 LarkClient._thread_dict), 话题 id 和根消息 id 都是 9000 + 位置。"""
    tid = str(9000 + pos)
    return {'thread_id': tid, 'position': pos, 'reply_count': replies, 'last_position': replies - 1, 'update_time': 1000 + pos,
            'owner_id': sender, 'root': {'msg_id': tid, 'position': 5000 + pos, 'from_id': sender, 'chat_id': chat,
                                         'create_time': pos, 'msg_type': 4, 'msg_type_name': 'TEXT', 'content': f'话题{pos}',
                                         'content_data': None, 'root_id': '', 'parent_id': ''}}


def _reply(tid, pos, parent=None, sender='200'):
    return {'msg_id': f'{tid}{pos:02d}', 'position': pos, 'chat_position': 6000 + pos, 'from_id': sender, 'chat_id': '7',
            'create_time': 2000 + pos, 'msg_type': 4, 'msg_type_name': 'TEXT', 'content': f'回复{pos}', 'content_data': None,
            'root_id': tid, 'parent_id': parent or tid}


def _topic_group(fake, last=50, read=45):
    fake.topics['7'] = {'name': '话题群', 'type': 2, 'chatMode': 3, 'lastThreadPosition': last, 'readThreadPosition': read,
                        'memberCount': 9, 'firstChatMessagePosition': -1}
    fake.threads.update({p: _topic(p, replies=p % 3) for p in range(0, last + 1)})


def test_topic_chat_lists_topics_instead_of_messages(ctx):
    """话题群的会话信息里没有 lastMessagePosition: 按话题位置取话题, 不再按消息位置拉(以前因此显示"还没有消息")。"""
    from larkx.protocol.client import MODERN_COMMAND_VERSION
    c, fake = ctx
    _topic_group(fake)
    fake.hidden = {47, 48, 49}  # 入群提示之类看不见的话题, 由服务端跳过
    d = c.get('/api/im/chat/7?count=5').json()
    assert [m['position'] for m in d['messages']] == [43, 44, 45, 46, 50]
    assert (d['start'], d['end'], d['has_more']) == (43, 50, True)
    assert d['chat']['kind'] == 'topic' and d['chat']['read_position'] == 45 and d['chat']['last_position'] == 50
    top = d['messages'][-1]
    assert (top['msg_id'], top['sender'], top['content'], top['segments']) == ('9050', '李四', '话题50', [{'type': 'text', 'text': '话题50'}])
    assert top['thread'] == {'id': '9050', 'reply_count': 2, 'last_position': 1, 'update_time': 1050}
    assert fake.thread_pulls == [(46, 50, MODERN_COMMAND_VERSION)]
    assert not [p for p in fake.pulled if p[0] == 'history']  # 没有按消息位置拉
    # 再打开: 按记下的会话信息猜位置, 和查会话信息同时进行; 这期间来了新话题, 只补查多出来的那段
    fake.threads.update({51: _topic(51), 52: _topic(52)})
    fake.topics['7']['lastThreadPosition'] = 52
    fake.thread_pulls, fake.barrier = [], threading.Barrier(2)
    d = c.get('/api/im/chat/7?count=5').json()
    assert fake.thread_pulls == [(46, 50, MODERN_COMMAND_VERSION), (51, 52, MODERN_COMMAND_VERSION)]
    assert [m['position'] for m in d['messages']] == [43, 44, 45, 46, 50, 51, 52] and (d['start'], d['end']) == (43, 52)
    # 往前翻: 位置不依赖会话信息, 也是同时查
    fake.thread_pulls, fake.barrier = [], threading.Barrier(2)
    older = c.get('/api/im/chat/7?before=43&count=5').json()
    assert [m['position'] for m in older['messages']] == [38, 39, 40, 41, 42] and (older['start'], older['has_more']) == (38, True)
    assert fake.thread_pulls == [(38, 42, MODERN_COMMAND_VERSION)]
    # 定时刷新: 不管给的位置, 都重取最近 TOPIC_REFRESH 个(回复数会变), 页面替换这一段
    fake.threads[52] = _topic(52, replies=4)
    fake.thread_pulls = []
    d = c.get('/api/im/chat/7?after=52').json()
    assert len(d['messages']) == im.TOPIC_REFRESH and d['messages'][-1]['thread']['reply_count'] == 4
    assert fake.thread_pulls == [(43, 52, MODERN_COMMAND_VERSION)]
    assert (d['start'], d['end']) == (40, 52)
    # 翻到头了
    assert c.get('/api/im/chat/7?before=3&count=5').json()['has_more'] is False


def test_sparse_topic_chat_keeps_looking_further_back(ctx):
    """几乎全是入群提示的大群: 服务端一次找不够看得见的话题, 接着往前找, 最多再查 TOPIC_ROUNDS 次。"""
    c, fake = ctx
    _topic_group(fake, last=100, read=0)
    fake.hidden = set(range(0, 101)) - {3, 10, 100}
    fake.scan_cap = 30
    d = c.get('/api/im/chat/7?count=5').json()
    assert [m['position'] for m in d['messages']] == [3, 10, 100]
    assert (d['start'], d['has_more']) == (0, False)
    assert len(fake.thread_pulls) == 1 + im.TOPIC_ROUNDS
    # 还没有话题的话题群
    fake.topics['7'].pop('lastThreadPosition')
    fake.thread_pulls = []
    im._chat_infos.clear()  # 不按记下的会话信息猜
    d = c.get('/api/im/chat/7').json()
    assert (d['messages'], d['start'], d['end'], d['has_more']) == ([], 0, -1, False) and fake.thread_pulls == []


def test_topic_chat_never_mixes_in_local_messages(ctx):
    """本地库按会话位置存消息, 和话题位置对不上: 话题群的 local 轮询不返回本地库里的消息。"""
    c, fake = ctx
    _topic_group(fake)
    c.get('/api/im/chat/7?count=5')
    Storage().save_message({'msg_id': 'x', 'chat_id': '7', 'from_id': '300', 'msg_type': 4, 'position': 60, 'content': 'hi'})
    fake.offline = True
    d = c.get('/api/im/chat/7?after=50&local=1').json()
    assert d['messages'] == [] and d['local'] is True


def test_thread_shows_root_and_replies_in_order(ctx):
    from larkx.protocol.client import MODERN_COMMAND_VERSION
    c, fake = ctx
    _topic_group(fake)
    fake.threads[50] = _topic(50, replies=4)
    fake.replies['9050'] = {0: _reply('9050', 0), 1: _reply('9050', 1, sender='300'), 2: _reply('9050', 2, parent='905001'),
                            3: _reply('9050', 3, sender='100')}
    fake.by_id['905001'] = fake.replies['9050'][1]  # 被引用的那条不在这一批里, 按 id 查
    # 带上话题列表给的最后位置: 先猜着拉回复, 和查话题信息同时进行
    fake.barrier = threading.Barrier(2)
    d = c.get('/api/im/chat/7/thread/9050?count=2&last=3').json()
    assert (d['root']['msg_id'], d['root']['position'], d['root']['content']) == ('9050', -1, '话题50')
    assert [m['position'] for m in d['messages']] == [2, 3] and (d['start'], d['end'], d['has_more']) == (2, 3, True)
    assert d['thread'] == {'id': '9050', 'reply_count': 4, 'last_position': 3, 'update_time': 1050}
    # 回的是话题里另一条的照常显示引用; 直接回根消息的不显示(都在这个话题里)
    assert d['messages'][0]['quote'] == {'sender': '李四', 'text': '回复1'}
    assert d['messages'][1]['quote'] is None and d['messages'][1]['me'] is True
    assert ('replies', '9050', 2, 23, MODERN_COMMAND_VERSION) in fake.thread_pulls
    older = c.get('/api/im/chat/7/thread/9050?before=2&count=2').json()
    assert [m['position'] for m in older['messages']] == [0, 1] and older['has_more'] is False
    assert older['messages'][0]['quote'] is None and older['messages'][0]['sender'] == '张三(备注)'
    # 没给最后位置: 先查话题信息再拉
    fake.thread_pulls = []
    assert [m['position'] for m in c.get('/api/im/chat/7/thread/9050').json()['messages']] == [0, 1, 2, 3]
    assert fake.thread_pulls == [('ids', ['9050'], MODERN_COMMAND_VERSION), ('replies', '9050', 0, 3, MODERN_COMMAND_VERSION)]
    # 取新回复
    assert c.get('/api/im/chat/7/thread/9050?after=3').json()['messages'] == []
    fake.replies['9050'][4] = _reply('9050', 4)
    fake.threads[50] = _topic(50, replies=5)
    d = c.get('/api/im/chat/7/thread/9050?after=3').json()
    assert [m['position'] for m in d['messages']] == [4] and d['thread']['reply_count'] == 5
    # 没有回复的话题
    d = c.get('/api/im/chat/7/thread/9049?last=-1').json()
    assert (d['messages'], d['root']['msg_id'], d['has_more']) == ([], '9049', False)
    # 别的会话的话题、不存在的话题、不合法的位置
    assert c.get('/api/im/chat/5/thread/9050').status_code == 400
    assert c.get('/api/im/chat/7/thread/1').status_code == 400
    assert c.get('/api/im/chat/7/thread/9050?last=-5').status_code == 400
    assert c.get('/api/im/chat/7/thread/9050?before=x').status_code == 400
    assert not [p for p in fake.pulled if p[0] == 'history']


def test_feed_previews_topic_chat_with_last_visible_topic(ctx):
    from larkx.protocol.proto import proto_pb2 as P
    c, fake = ctx
    fake.feed['cards'].append({'id': 7, 'type': 1, 'rankTime': 45})
    fake.feed['chats']['7'] = {'name': '话题群', 'type': 2, 'chatMode': 3, 'lastThreadId': 'join', 'lastVisibleThreadId': 'r7'}
    fake.feed['messages']['r7'] = {'fromId': '300', 'type': 4, 'content': P.TextContent(text='最新话题').SerializeToString()}
    items = {i['chat_id']: i for i in c.get('/api/im/feed').json()['items']}
    assert (items['7']['kind'], items['7']['preview']) == ('topic', '李四: 最新话题')
    assert items['1']['preview'] == '' and items['2']['kind'] == 'p2p'  # 普通群、私聊照旧


def test_read_client_allows_thread_queries_only(monkeypatch):
    from larkx.protocol import get_region
    from larkx.protocol.proto import proto_pb2 as P
    auth = type('A', (), {'user_id': '100', 'region': get_region('feishu'), 'gateway_app_id': '1', 'cookies': {}})()
    rc = im.ReadClient(auth)
    posts = []

    class Session:
        def post(self, url, headers, cookies, data, timeout):
            posts.append(headers['x-command'])
            return type('R', (), {'content': P.Packet().SerializeToString(), 'raise_for_status': lambda self: None})()
    rc.http = Session()
    assert rc.pull_threads('7', [1, 2]) == {'threads': [], 'start': 1}
    assert rc.pull_threads_by_ids(['9']) == {} and rc.pull_thread_messages('7', '9', [0]) == []
    assert posts == ['8108', '8111', '8109']
    # 标话题已读、标消息已读的命令发不出去
    for cmd in ('thread.PutReadThreadsRequest', 'topic_group.PutTopicsReadStateRequest', 'messages.PutReadMessagesRequest'):
        with pytest.raises(RuntimeError, match='只读客户端'):
            rc.api(cmd, {})
    assert len(posts) == 3


def test_client_pulls_visible_threads_and_thread_messages(monkeypatch):
    """协议层: 请求带上只取看得见的话题、往前找; 响应里看不见的话题不要, 位置换成话题位置。"""
    from larkx.protocol import get_region
    from larkx.protocol.client import LarkClient
    from larkx.protocol.proto import gateway, proto_pb2 as P
    c = object.__new__(LarkClient)
    c.auth = type('A', (), {'user_id': '100', 'region': get_region('feishu'), 'gateway_app_id': '1', 'cookies': {}})()
    c._thread_chats = {}
    sent, replies = [], []

    def post(packet, command_version=None):
        sent.append((packet.cmd, packet.payload, command_version))
        return replies.pop(0).SerializeToString()
    monkeypatch.setattr(c, '_gateway_post', post)

    def packet(msg):
        pkt = P.Packet()
        pkt.payload = msg.SerializeToString()
        return pkt

    def root(m, mid, pos):
        m.id, m.type, m.fromId, m.chatId, m.createTime, m.position = mid, 4, '300', '7', 50, pos
        m.content = P.TextContent(text=f'话题{mid}').SerializeToString()

    R = gateway.resolve_type('thread.PullThreadsByPositionsResponse')
    r = R()
    for pos, tid, visible in ((10, '91', True), (9, '90', False)):
        t = r.threads[pos]
        t.id, t.position, t.isVisible, t.rootMessageId, t.ownerId, t.updateTime = tid, pos, visible, tid, '300', 99
        root(r.rootMessages[tid], tid, 500 + pos)
    r.threads[10].replyCount, r.threads[10].lastMessagePosition = 2, 1
    r.invisibleThreads[7].position = 7
    replies.append(packet(r))
    out = c.pull_threads('7', [10, 9], command_version='7.72.0')
    assert out['start'] == 7 and [t['thread_id'] for t in out['threads']] == ['91']
    t = out['threads'][0]
    assert (t['position'], t['reply_count'], t['last_position'], t['update_time'], t['owner_id']) == (10, 2, 1, 99, '300')
    assert (t['root']['msg_id'], t['root']['content'], t['root']['scope'], t['root']['position']) == ('91', '话题91', 'topic', 510)
    cmd, payload, version = sent[-1]
    req = gateway.resolve_type('thread.PullThreadsByPositionsRequest').FromString(payload)
    assert (cmd, version, req.channelId, list(req.positions), req.onlyVisibleThreads, req.direction) == (8108, '7.72.0', '7', [9, 10], True, 1)
    # 话题里的消息: position 换成话题内的位置(根消息 -1), 会话里的位置另放
    M = gateway.resolve_type('thread.PullThreadMessagesByPositionsResponse')
    m = M()
    root(m.messages[-1], '91', 510)
    root(m.messages[0], '92', 511)
    m.messages[0].rootId = m.messages[0].parentId = '91'
    replies.append(packet(m))
    msgs = c.pull_thread_messages('7', '91', [-1, 0])
    assert [(x['msg_id'], x['position'], x['chat_position'], x['root_id']) for x in msgs] == [('91', -1, 510, ''), ('92', 0, 511, '91')]
    req = gateway.resolve_type('thread.PullThreadMessagesByPositionsRequest').FromString(sent[-1][1])
    assert (req.threadId, req.channelId, list(req.positions)) == ('91', '7', [-1, 0])
    # 按 id 取话题; 没有 lastMessagePosition 时按回复数推算
    B = gateway.resolve_type('thread.PullThreadsByIDsResponse')
    b = B()
    t = b.threads['91']
    t.id, t.channelId, t.position, t.isVisible, t.rootMessageId, t.replyCount = '91', '7', 10, True, '91', 3
    root(b.rootMessages['91'], '91', 510)
    replies.append(packet(b))
    got = c.pull_threads_by_ids(['91'])
    assert list(got) == ['91'] and got['91']['last_position'] == 2 and got['91']['root']['chat_id'] == '7'
    # 没有位置就不发请求
    assert c.pull_threads('7', []) == {'threads': [], 'start': 0} and c.pull_thread_messages('7', '91', []) == [] and len(sent) == 3
