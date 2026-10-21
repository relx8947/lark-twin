import pytest
from starlette.testclient import TestClient

from larkx.storage import Storage
from larkx.web import app as web
from larkx.web import im


class FakeIMClient:
    """网页版飞书用到的协议接口; 调到 mark_read 就算失败。"""

    def __init__(self):
        from larkx.protocol import get_region
        self.auth = type('A', (), {'user_id': '100', 'region': get_region('feishu')})()
        self.sent = []

    def api(self, cmd, data=None):
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
        return {'name': '项目群', 'type': 2, 'lastMessagePosition': 100, 'firstChatMessagePosition': 60,
                'lastMessagePositionBadgeCount': 3, 'readPositionBadgeCount': 3, 'memberCount': 5}

    def pull_history(self, chat_id, positions):
        return [{'msg_id': f'id{p}', 'position': p, 'from_id': '100' if p % 2 else '300', 'create_time': p,
                 'msg_type': 5 if p == 100 else 4, 'msg_type_name': 'IMAGE' if p == 100 else 'TEXT',
                 'content': '[图片] imageKey=k1 (1x1)' if p == 100 else f'第{p}条',
                 'content_data': {'image': {'origin': {'key': 'img_k1'}}} if p == 100 else None} for p in positions]

    def send_msg(self, text, chat_id, root_id=None):
        self.sent.append((chat_id, text, root_id))
        return '777'

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
    assert last['image_key'] == 'img_k1' and last['content'] == '[图片]' and last['me'] is False
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
    assert r.json() == {'ok': True, 'msg_id': '777'} and fake.sent == [('5', '收到', '42')]
    a = Storage().list_actions()[-1]
    assert (a.actor, a.action, a.chat_id, a.msg_id, a.turn_kind) == ('human', 'send', '5', '777', 'web')
    # 改动类请求一样要带 X-Larkx
    assert c.post('/api/im/chat/5/send', json={'text': 'hi'}, headers={'X-Larkx': ''}).status_code == 403


def test_media_only_renders_plain_images(ctx, monkeypatch):
    c, _ = ctx
    monkeypatch.setattr(im, 'download', lambda auth, url, max_bytes: (b'<svg onload=alert(1)>', 'image/svg+xml'))
    r = c.get('/api/im/media?chat=1&msg=2&key=img_k1')
    assert r.headers['content-type'] == 'application/octet-stream' and r.headers['content-disposition'] == 'attachment'
    monkeypatch.setattr(im, 'download', lambda auth, url, max_bytes: (b'\x89PNG', 'image/png'))
    assert c.get('/api/im/media?chat=1&msg=2&key=img_k1').headers['content-type'] == 'image/png'
    assert c.get('/api/im/media?chat=1&msg=2&key=../x').status_code == 400
