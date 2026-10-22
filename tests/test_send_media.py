"""发图片、文件、@ 的单元测试: 消息结构、匿名发送(打开 keep_unread 时)流程、上传协议。都不访问网络。"""
import zlib

import pytest

from larkx.protocol import client as client_mod
from larkx.protocol import media
from larkx.protocol.client import LarkClient
from larkx.protocol.proto import builders
from larkx.protocol.proto import lark_all_pb2 as LA

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


# ---- 匿名发送流程(打开 keep_unread 时): 每种消息都经本人私聊转发 ----

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
