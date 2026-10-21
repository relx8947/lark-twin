import pytest
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

    def api(self, cmd, data=None):
        assert cmd in READ_ONLY_APIS, f'网页版飞书调用了不该调用的接口 {cmd}'
        if cmd == 'chatters.PullChattersByIdsRequest':
            people = {'100': {'name': '我本人', 'avatar': {'key': 'a100'}},
                      '200': {'name': '张三', 'alias': '张三(备注)', 'avatar': {'key': 'a200'}},
                      '300': {'name': 'Li', 'displayName': {'defaultVal': 'Li', 'i18nVals': {'zh_cn': '李四'}}}}
            return {'chatters': {u: people[u] for u in data['chatterIds'] if u in people}}
        assert cmd == 'feed.PullFeedCardsRequest'
        return {'cards': [{'id': 1, 'type': 1, 'rankTime': 50}, {'id': 2, 'type': 1, 'rankTime': 40},
                          {'id': 9, 'type': 13, 'rankTime': 30}, {'id': 3, 'type': 1, 'isHide': True}],
                'chats': {'1': {'name': '项目群', 'type': 2, 'lastMessagePositionBadgeCount': 7, 'readPositionBadgeCount': 4,
                                'isRemind': False},
                          '2': {'name': '', 'type': 1, 'lastMessageId': 'm2', 'avatar': {'key': 'c2'}}},
                'messages': {'m2': {'fromId': '200', 'type': 4, 'content': b''}},
                'nextCursor': 123}

    def get_user_name(self, uid, chat_id):
        return None

    def get_chat_info(self, chat_id):
        assert not self.offline, '不该访问服务端'
        return {'name': '项目群', 'type': 2, 'lastMessagePosition': 100, 'firstChatMessagePosition': 60,
                'lastMessagePositionBadgeCount': 3, 'readPositionBadgeCount': 3, 'memberCount': 5, 'readPosition': self.chat_read}

    def pull_history(self, chat_id, positions, command_version=None):
        assert not self.offline, '不该访问服务端'
        positions = list(positions)
        self.pulled.append(('history', positions[0] if positions else None, positions[-1] if positions else None, command_version))
        return [self.history.get(p) or {
            'msg_id': str(p), 'position': p, 'from_id': '100' if p % 2 else '300', 'create_time': p,
            'msg_type': 5 if p == 100 else 4, 'msg_type_name': 'IMAGE' if p == 100 else 'TEXT',
            'content': '[图片] imageKey=k1 (1x1)' if p == 100 else f'第{p}条',
            'content_data': {'image': {'origin': {'key': 'img_k1', 'width': 4, 'height': 3}}} if p == 100 else None}
            for p in positions]

    def pull_messages(self, ids, command_version=None):
        assert not self.offline, '不该访问服务端'
        self.pulled.append(('ids', sorted(ids), command_version))
        return [self.by_id[i] for i in ids if i in self.by_id]

    def send_msg(self, text, chat_id, root_id=None, keep_unread=False):
        self.sent.append((chat_id, text, root_id, keep_unread))
        # 和服务端一样: 话题回复时被回复的那条变成已读; 经本人私聊转发不动已读位置
        self.done.update(self.leak + ([root_id] if root_id else []))
        if self.advance:
            self.read_pos = 100
        return '777'

    def read_state(self, chat_id):
        return (self.read_pos, 100)

    def my_read_status(self, ids):
        if self.status_error:
            raise RuntimeError('boom')
        return {i: i in self.done for i in ids}

    def mark_read(self, *a, **k):
        raise AssertionError('网页版飞书不能标已读')


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    fake = FakeIMClient()
    monkeypatch.setattr(web, 'TOKEN_PATH', tmp_path / 'web_token')
    monkeypatch.setattr(im, 'client', lambda: fake)
    monkeypatch.setattr(im, '_owner_chat', lambda: 'self')
    monkeypatch.setattr(im, '_profiles', {})
    monkeypatch.setattr(im, '_p2p_names', {})
    monkeypatch.setattr(im, '_quotes', {})
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


def test_send_reports_fallback_and_unknown(ctx):
    c, fake = ctx
    # 经本人私聊转发: 已读位置不动, 别人的未读也都还在
    assert c.post('/api/im/chat/5/send', json={'text': 'hi'}).json() == {'ok': True, 'msg_id': '777', 'kept_unread': True, 'newly_read': 0}
    assert fake.sent[-1] == ('5', 'hi', None, True)
    fake.status_error = True
    assert c.post('/api/im/chat/5/send', json={'text': 'hi'}).json()['kept_unread'] is None
    # 转发不成退回普通发送: 本人的未读被清掉, 如实告诉页面
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
