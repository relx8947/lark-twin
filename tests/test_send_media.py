"""发图片、文件、@ 的单元测试: 消息结构、匿名发送流程、上传协议、网页接口。都不访问网络。"""
import zlib

import pytest
from starlette.testclient import TestClient

from larkx.protocol import client as client_mod
from larkx.protocol import media
from larkx.protocol.client import LarkClient
from larkx.protocol.proto import builders
from larkx.protocol.proto import lark_all_pb2 as LA
from larkx.storage import Storage
from larkx.web import app as web
from larkx.web import im

from test_im import FakeIMClient

E = LA.entities.RichTextElement
AT_MSG = {'kind': 'text', 'segments': [{'type': 'text', 'text': '请 '}, {'type': 'at', 'user_id': '200', 'name': '张三'},
                                       {'type': 'text', 'text': ' 看一下'}]}


def _req(pkt):
    r = LA.messages.PutMessageRequest()
    r.ParseFromString(pkt.payload)
    return r


def _elements(r):
    d = r.content.richText.elements.dictionary
    out = []
    for k in r.content.richText.elementIds:
        el = d[k]
        prop = E.AtProperty.FromString(el.property) if el.tag == 5 else E.TextProperty.FromString(el.property)
        out.append((k, el.tag, getattr(prop, 'userId', None), prop.content))
    return out


# ---- 消息结构 ----

def test_at_message_has_at_element_and_at_user_list():
    r = _req(builders.build_message_packet(AT_MSG, 'c1', 'req'))
    els = _elements(r)
    assert [(t, u, c) for _, t, u, c in els] == [(1, None, '请 '), (5, '200', '@张三'), (1, None, ' 看一下')]
    assert list(r.content.richText.atIds) == [els[1][0]]
    assert list(r.atUserIdList) == [200] and r.type == 4 and r.chatId == 'c1'
    assert r.content.richText.innerText == '请 @张三 看一下'


def test_plain_text_message_is_unchanged():
    r = _req(builders.build_send_message_packet('你好', 'c1', 'req'))
    assert [(t, c) for _, t, _, c in _elements(r)] == [(1, '你好')]
    assert (r.type, list(r.atUserIdList), list(r.content.richText.atIds), r.isNotified) == (4, [], [], True)


def test_image_and_file_messages():
    r = _req(builders.build_message_packet({'kind': 'image', 'key': 'img_v3_k'}, 'c1', 'req'))
    assert (r.type, r.content.imageKey, r.content.cryptoToken, len(r.content.richText.elementIds)) == (5, 'img_v3_k', 'img_v3_k', 0)
    f = {'kind': 'file', 'key': 'file_v3_k', 'name': '报告.pdf', 'size': 1234, 'mime': 'application/pdf'}
    r = _req(builders.build_message_packet(f, 'c1', 'req', root_id='9'))
    assert (r.type, r.content.fileKey, r.content.fileName, r.content.fileSize, r.content.fileMime) == (3, 'file_v3_k', '报告.pdf', 1234, 'application/pdf')
    assert (r.rootId, r.parentId, r.isReplyInThread) == ('9', '9', False)  # 回复默认是主会话里的引用回复
    with pytest.raises(ValueError):
        builders.message_request({'kind': 'sticker'}, 'c1')


def test_segments_are_normalized():
    msg = {'kind': 'text', 'segments': [{'type': 'text', 'text': 'a'}, {'type': 'at', 'user_id': 'x1', 'name': '假的'},
                                        {'type': 'text', 'text': ''}, {'type': 'at', 'user_id': '7', 'name': '@李四'},
                                        {'type': 'at', 'user_id': '7', 'name': '李四'}]}
    # user_id 不是数字的 @ 退成文字并和相邻文字合并; 名字前多余的 @ 去掉; 同一个人只列一次
    assert builders.text_segments(msg) == [{'type': 'text', 'text': 'a@假的'}, {'type': 'at', 'user_id': '7', 'name': '李四'},
                                           {'type': 'at', 'user_id': '7', 'name': '李四'}]
    assert list(_req(builders.build_message_packet(msg, 'c1', 'req')).atUserIdList) == [7]
    assert builders.plain_text(msg) == 'a@假的@李四@李四'
    assert builders.has_mentions(msg) and not builders.has_mentions({'kind': 'text', 'text': '@张三'})
    assert builders.without_mentions(AT_MSG) == {'kind': 'text', 'text': '请 @张三 看一下'}
    assert builders.without_mentions({'kind': 'image', 'key': 'k'}) == {'kind': 'image', 'key': 'k'}
    assert builders.plain_text({'kind': 'file', 'name': 'a.txt'}) == '[文件] a.txt'


# ---- 匿名发送流程: 每种消息都经本人私聊转发 ----

def _client(monkeypatch, read=5, last=8, ids=None):
    c = object.__new__(LarkClient)
    c.auth = type('A', (), {'user_id': '100'})()
    c._thread_chats, c._self_chat = {}, ''
    sent, calls = [], []
    c.read_state = lambda chat_id: (read, last)
    c.is_thread_chat = lambda chat_id: False
    c.create_chat = lambda user_id: 'self'
    c._gateway_post = lambda pkt: sent.append(pkt) or b''
    seq = iter(ids or ['r1', 'new'])
    monkeypatch.setattr(client_mod.builders, 'decode_put_message_response', lambda content: next(seq))

    def api(cmd, data):
        calls.append((cmd, data))
        return {'messages': {'c1': {'id': 'fwd'}}} if cmd == 'messages.PutTransmitMessageRequest' else {}
    c.api = api
    return c, sent, calls


@pytest.mark.parametrize('msg,mtype', [({'kind': 'image', 'key': 'img_k'}, 5),
                                       ({'kind': 'file', 'key': 'file_k', 'name': 'a.txt', 'size': 3}, 3)])
def test_media_goes_through_relay_and_is_recalled(monkeypatch, msg, mtype):
    from larkx import state
    c, sent, calls = _client(monkeypatch)
    assert c.send_message(msg, 'c1', keep_unread=True) == 'fwd'
    r = _req(sent[0])
    assert (len(sent), r.chatId, r.type, r.rootId) == (1, 'self', mtype, '')
    assert state.is_relay({'cid': r.cid, 'chat_id': 'self'})
    assert [cmd for cmd, _ in calls] == ['messages.PutTransmitMessageRequest', 'messages.DeleteMessageRequest']
    assert calls[0][1]['messageId'] == 'r1' and calls[1][1] == {'id': 'r1'}
    assert not any('Read' in cmd for cmd, _ in calls)


def test_relay_failure_without_fallback_sends_nothing(monkeypatch):
    c, sent, calls = _client(monkeypatch, ids=['-1'])
    assert c.send_message({'kind': 'image', 'key': 'img_k'}, 'c1', keep_unread=True, fallback=False) is None
    assert [_req(p).chatId for p in sent] == ['self'] and calls == []


def test_relay_writes_mentions_as_text(monkeypatch):
    # 转发会把 @ 变成文字, 中转那条也不在本人私聊里 @ 别人
    c, sent, _ = _client(monkeypatch)
    c.send_message(AT_MSG, 'c1', keep_unread=True)
    r = _req(sent[0])
    assert r.chatId == 'self' and list(r.atUserIdList) == [] and list(r.content.richText.atIds) == []
    assert r.content.richText.innerText == '请 @张三 看一下'


def test_no_unread_sends_real_mention(monkeypatch):
    c, sent, calls = _client(monkeypatch, read=8, last=8, ids=['new'])
    assert c.send_message(AT_MSG, 'c1', keep_unread=True) == 'new'
    r = _req(sent[0])
    assert r.chatId == 'c1' and list(r.atUserIdList) == [200] and calls == []
    assert c.relays('c1', 5, 8) and not c.relays('c1', 8, 8) and not c.relays('c1', 5, 8, root_id='m') and not c.relays('self', 5, 8)


# ---- 上传协议 ----

class _Resp:
    def __init__(self, content=b'', status=200):
        self.content, self.status_code, self.text = content, status, ''


def _auth():
    from larkx.protocol import get_region
    return type('A', (), {'region': get_region('feishu'), 'cookies': {'session': 'x'}})()


def test_upload_image_posts_multipart(monkeypatch):
    seen = []

    def post(url, cookies, timeout, files=None, headers=None, **kw):
        seen.append((url, files, cookies))
        return _Resp(LA.pan.PutFileResponse(key='img_v3_abc').SerializeToString())
    monkeypatch.setattr(media.requests, 'post', post)
    assert media.upload_image(_auth(), b'\x89PNG', 'a.png', 'image/png') == 'img_v3_abc'
    url, files, cookies = seen[0]
    assert url == 'https://internal-api-lark-file.feishu.cn/api/image' and files == {'image': ('a.png', b'\x89PNG', 'image/png')}
    assert cookies == {'session': 'x'}
    monkeypatch.setattr(media.requests, 'post', lambda *a, **k: _Resp(b''))
    with pytest.raises(media.UploadError):
        media.upload_image(_auth(), b'x')
    monkeypatch.setattr(media.requests, 'post', lambda *a, **k: _Resp(b'{}', 400))
    with pytest.raises(media.UploadError):
        media.upload_image(_auth(), b'x')


def test_upload_file_in_parts(monkeypatch, tmp_path):
    import io
    data = bytes(range(256)) * 41  # 10496 字节, 按 4096 分成 3 块
    calls = []

    def post(url, cookies, timeout, data=None, params=None, headers=None, **kw):
        path = url.split('feishu.cn', 1)[1]
        calls.append((path, params, data))
        if path == '/upload/create':
            req = LA.pan.CreateFileUploadRequest.FromString(data)
            assert (req.fileType, req.fileName, req.fileSize) == (1, '数据.bin', len(data_all))
            return _Resp(LA.pan.CreateFileUploadResponse(uploadId='file_v3_k').SerializeToString())
        if path == '/upload/part':
            return _Resp(LA.pan.UploadFilePartResponse(partId=params['part_id'], crc32=zlib.crc32(data)).SerializeToString())
        assert path == '/upload/complete'
        return _Resp(LA.pan.CompleteFileUploadResponse(code=0).SerializeToString())
    data_all = data
    monkeypatch.setattr(media.requests, 'post', post)
    progress = []
    key = media.upload_file(_auth(), io.BytesIO(data), len(data), '数据.bin', 'application/octet-stream',
                            progress=lambda d, n: progress.append((d, n)), part_size=4096, workers=2)
    assert key == 'file_v3_k'
    parts = [c for c in calls if c[0] == '/upload/part']
    assert sorted(p[1]['part_id'] for p in parts) == [1, 2, 3] and all(p[1]['upload_id'] == 'file_v3_k' for p in parts)
    assert b''.join(p[2] for p in sorted(parts, key=lambda p: p[1]['part_id'])) == data
    done = LA.pan.CompleteFileUploadRequest.FromString(calls[-1][2])
    assert (done.uploadId, done.fileType, done.fileName) == ('file_v3_k', 1, '数据.bin')
    assert dict(done.partToCrc32) == {1: zlib.crc32(data[:4096]), 2: zlib.crc32(data[4096:8192]), 3: zlib.crc32(data[8192:])}
    assert progress[-1] == (len(data), len(data))


def test_upload_file_failures(monkeypatch):
    import io

    def post(url, cookies, timeout, data=None, params=None, headers=None, **kw):
        if url.endswith('/upload/create'):
            return _Resp(LA.pan.CreateFileUploadResponse(uploadId='file_v3_k').SerializeToString())
        if url.endswith('/upload/part'):
            return _Resp(LA.pan.UploadFilePartResponse(partId=params['part_id'], crc32=1).SerializeToString())
        return _Resp(LA.pan.CompleteFileUploadResponse(code=0).SerializeToString())
    monkeypatch.setattr(media.requests, 'post', post)
    with pytest.raises(media.UploadError, match='校验和'):
        media.upload_file(_auth(), io.BytesIO(b'abc'), 3, 'a.txt')
    with pytest.raises(media.UploadError, match='空文件'):
        media.upload_file(_auth(), io.BytesIO(b''), 0, 'a.txt')
    with pytest.raises(media.UploadError, match='比声明的短'):
        media.upload_file(_auth(), io.BytesIO(b'ab'), 5, 'a.txt')


# ---- 网页接口 ----

class FakeSendClient(FakeIMClient):
    def api(self, cmd, data=None):
        if cmd == 'chats.PullChatChattersRequest':
            self.member_calls = getattr(self, 'member_calls', 0) + 1
            return {'chatters': {'100': {'name': '我本人'}, '300': {'name': 'Li', 'alias': '小李', 'namePinyin': 'li', 'type': 1},
                                 '200': {'name': '张三', 'alias': '张三(备注)', 'enUsName': 'Zhang San',
                                         'avatar': {'key': 'a200'}},
                                 '400': {'name': '机器人', 'type': 2}, 'x': {'name': '坏 id'}},
                    'orderedWeight': {'200': 1, '300': 2, '400': 3, '100': 0}}
        return super().api(cmd, data)


@pytest.fixture
def sctx(tmp_path, monkeypatch):
    fake = FakeSendClient()
    monkeypatch.setattr(web, 'TOKEN_PATH', tmp_path / 'web_token')
    monkeypatch.setattr(im, 'client', lambda: fake)
    monkeypatch.setattr(im, '_owner_chat', lambda: 'self')
    for name in ('_profiles', '_p2p_names', '_quotes', '_members'):
        monkeypatch.setattr(im, name, {})
    monkeypatch.setattr(im, 'READ_CHECK_DELAY', 0)
    c = TestClient(web.create_app())
    c.headers.update({'Authorization': f'Bearer {web.get_token()}', 'X-Larkx': '1'})
    return c, fake


def test_text_message_splits_chosen_mentions():
    picks = [{'user_id': '200', 'name': '张三'}, {'user_id': '300', 'name': '张三丰'}, {'user_id': 'bad', 'name': '王五'}]
    msg = im._text_message('@张三丰 和 @张三 好, @王五 不算, 邮箱 a@张三', picks)
    assert [(s['type'], s.get('user_id') or s.get('text')) for s in msg['segments'] if s.get('text') != ''] == [
        ('at', '300'), ('text', ' 和 '), ('at', '200'), ('text', ' 好, @王五 不算, 邮箱 a'), ('at', '200')]
    assert im._text_message('没有 @', []) == {'kind': 'text', 'text': '没有 @'}
    assert im._text_message('选完删掉了', picks) == {'kind': 'text', 'segments': [{'type': 'text', 'text': '选完删掉了'}]}
    # 同名的两个人按出现顺序对上
    two = im._text_message('@李 @李', [{'user_id': '1', 'name': '李'}, {'user_id': '2', 'name': '李'}])
    assert [s['user_id'] for s in two['segments'] if s['type'] == 'at'] == ['1', '2']


def test_send_with_mentions_without_unread_is_a_real_at(sctx):
    c, fake = sctx
    fake.read_pos = 100
    r = c.post('/api/im/chat/5/send', json={'text': '@张三 你好', 'mentions': [{'user_id': '200', 'name': '张三'}]})
    assert r.json() == {'ok': True, 'msg_id': '777', 'kept_unread': True, 'newly_read': 0}
    assert fake.messages[-1]['segments'][1] == {'type': 'at', 'user_id': '200', 'name': '张三'}
    assert Storage().list_actions()[-1].content == '@张三 你好'


def test_send_with_mentions_and_unread_needs_confirmation(sctx):
    c, fake = sctx
    body = {'text': '@张三 你好', 'mentions': [{'user_id': '200', 'name': '张三'}]}
    r = c.post('/api/im/chat/5/send', json=body)
    assert r.status_code == 409 and r.json()['confirm'] == 'plain_at' and '收不到 @ 提醒' in r.json()['error']
    assert fake.sent == []
    r = c.post('/api/im/chat/5/send', json={**body, 'plain_at': True}).json()
    assert r['at_plain'] is True and r['kept_unread'] is True and fake.sent == [('5', '@张三 你好', None, True)]
    # 回复不经转发, @ 照常是真的 @, 不用确认
    assert c.post('/api/im/chat/5/send', json={**body, 'root_id': '42'}).json().get('at_plain') is None


def test_upload_image_and_file_are_sent_anonymously(sctx, monkeypatch):
    c, fake = sctx
    got = []
    monkeypatch.setattr(im, 'upload_image', lambda auth, data, name, mime: got.append(('image', data, name, mime)) or 'img_v3_k')

    def fake_upload_file(auth, fileobj, size, name, mime):
        got.append(('file', fileobj.read(), size, name, mime))
        return 'file_v3_k'
    monkeypatch.setattr(im, 'upload_file', fake_upload_file)
    png = b'\x89PNG\r\n\x1a\n' + b'0' * 20
    up = lambda q, data, **kw: c.post('/api/im/chat/5/upload?' + q, content=data,
                                      headers={'Content-Type': 'application/octet-stream', **kw.get('headers', {})})
    r = up('kind=image&name=shot.png&type=image/png', png)
    assert r.json() == {'ok': True, 'msg_id': '777', 'kept_unread': True, 'newly_read': 0}
    assert got[-1] == ('image', png, 'shot.png', 'image/png') and fake.messages[-1] == {'kind': 'image', 'key': 'img_v3_k', 'name': 'shot.png'}
    assert fake.sent[-1] == ('5', '[图片]', None, True)
    r = up('kind=file&name=' + '..%2F笔记.txt' + '&type=text/plain&root_id=40&parent_id=42', b'hello')
    assert r.json()['ok'] is True and got[-1] == ('file', b'hello', 5, '笔记.txt', 'text/plain')
    assert fake.messages[-1] == {'kind': 'file', 'key': 'file_v3_k', 'name': '笔记.txt', 'size': 5, 'mime': 'text/plain'}
    assert fake.sent[-1] == ('5', '[文件] 笔记.txt', '40', True) and fake.reply_args[-1] == ('40', '42', False)
    a = Storage().list_actions()[-1]
    assert (a.actor, a.action, a.content, a.turn_kind) == ('human', 'send', '[文件] 笔记.txt', 'web')


def test_upload_rejects_bad_input(sctx, monkeypatch):
    c, fake = sctx
    monkeypatch.setattr(im, 'upload_image', lambda *a: pytest.fail('不该上传'))
    up = lambda q, data, headers=None: c.post('/api/im/chat/5/upload?' + q, content=data,
                                              headers={'Content-Type': 'application/octet-stream', **(headers or {})})
    r = up('kind=image&name=x.svg', b'<svg onload=alert(1)>')
    assert r.status_code == 400 and '作为文件发送' in r.json()['error']
    assert up('kind=sticker', b'x').status_code == 400
    assert up('kind=file&name=a', b'').status_code == 400
    monkeypatch.setattr(im, 'MAX_FILE', 4)
    r = up('kind=file&name=a', b'12345')
    assert r.status_code == 400 and '不能超过' in r.json()['error']
    assert up('kind=file&name=a', b'1', headers={'X-Larkx': ''}).status_code == 403
    assert fake.sent == []


def test_relay_failure_is_reported_not_downgraded(sctx, monkeypatch):
    c, fake = sctx
    fake.relay_fails = True
    r = c.post('/api/im/chat/5/send', json={'text': 'hi'})
    assert r.status_code == 502 and '没有改用普通发送' in r.json()['error'] and fake.sent == []
    a = Storage().list_actions()[-1]
    assert (a.ok, a.error) == (0, '发送失败')


def test_upload_failure_is_reported(sctx, monkeypatch):
    c, fake = sctx

    def boom(*a, **k):
        raise media.UploadError('创建上传失败: HTTP 500')
    monkeypatch.setattr(im, 'upload_file', boom)
    r = c.post('/api/im/chat/5/upload?kind=file&name=a.txt', content=b'abc', headers={'Content-Type': 'application/octet-stream'})
    assert r.status_code == 502 and '上传到飞书失败' in r.json()['error'] and fake.sent == []


def test_members_for_mentions(sctx):
    c, fake = sctx
    d = c.get('/api/im/chat/5/members').json()
    # 不含本人和不合法的 id, 按群里的顺序; 名字用对方自己的名字, 备注名另给
    assert [(m['user_id'], m['name'], m['alias'], m['bot']) for m in d['members']] == [
        ('200', '张三', '张三(备注)', False), ('300', 'Li', '小李', False), ('400', '机器人', '', True)]
    assert d['members'][0]['en'] == 'Zhang San' and d['members'][0]['avatar'].endswith('a200~?image_size=72x72&cut_type=&quality=&format=png&sticker_format=.webp')
    assert d['complete'] is True
    c.get('/api/im/chat/5/members')
    assert fake.member_calls == 1  # 缓存住了


def test_binary_body_does_not_break_json_endpoints(sctx):
    c, _ = sctx
    # 不是 JSON 的请求体不会让中间件出错; 发送接口照常校验
    r = c.post('/api/im/chat/5/send', content=b'\xff\xfe\x00', headers={'Content-Type': 'application/json'})
    assert r.status_code == 400 and r.json()['error'] == '消息内容为空'
