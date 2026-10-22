import os
import time

import pytest

from larkx import state
from larkx.protocol import client as client_mod
from larkx.protocol.client import LarkClient
from larkx.protocol.proto import builders
from larkx.protocol.proto import lark_all_pb2 as LA


def _client(monkeypatch, read=5, last=8, thread_chat=False, forward=None, recall_error=False, ids=None, targets=None):
    """会话 read+1..last 是本人的未读。forward 是转发接口的返回(异常就抛出), ids 是依次发出的消息拿到的 id,
    targets 是按 id 能查到的消息(查被回复的那条用; 是异常就抛出)。"""
    c = object.__new__(LarkClient)
    c.auth = type('A', (), {'user_id': '100'})()
    c._thread_chats, c._self_chat = {}, ''
    sent, calls, chats = [], [], []
    c.read_state = lambda chat_id: (read, last)
    c.is_thread_chat = lambda chat_id: thread_chat
    c.create_chat = lambda user_id: chats.append(user_id) or 'self'
    c._gateway_post = lambda pkt: sent.append(pkt) or b''
    seq = iter(ids or ['r1', 'new'])
    monkeypatch.setattr(client_mod.builders, 'decode_put_message_response', lambda content: next(seq))

    def api(cmd, data):
        calls.append((cmd, data))
        if cmd == 'messages.DeleteMessageRequest' and recall_error:
            raise RuntimeError('recall boom')
        if cmd == 'messages.PullMessagesByIdsRequest':
            if isinstance(targets, Exception):
                raise targets
            return {'messages': {i: (targets or {})[i] for i in data['messageIds'] if i in (targets or {})}}
        if cmd == 'messages.PutTransmitMessageRequest':
            if isinstance(forward, Exception):
                raise forward
            return {'messages': {'c1': {'id': 'fwd'}}} if forward is None else forward
        return {}

    c.api = api
    return c, sent, calls, chats


def _req(pkt):
    r = LA.messages.PutMessageRequest()
    r.ParseFromString(pkt.payload)
    return r


def test_unread_chat_relays_through_self_chat_and_recalls(monkeypatch):
    c, sent, calls, _ = _client(monkeypatch)
    assert c.send_msg('hi', 'c1', keep_unread=True) == 'fwd'
    # 只发了一条, 发在本人私聊, 不是回复; 它的 cid 登记过, 只认本人私聊里的那份
    assert len(sent) == 1
    r = _req(sent[0])
    assert (r.chatId, r.rootId, r.isReplyInThread, r.content.richText.innerText) == ('self', '', False, 'hi')
    assert state.is_relay({'cid': r.cid, 'chat_id': 'self'}) and not state.is_relay({'cid': r.cid, 'chat_id': 'c1'})
    (fwd_cmd, fwd), (del_cmd, rec) = calls
    assert fwd_cmd == 'messages.PutTransmitMessageRequest' and (fwd['chatIds'], fwd['messageId']) == (['c1'], 'r1')
    assert list(fwd['cids']) == ['c1'] and fwd['cids']['c1'] != r.cid
    assert (del_cmd, rec) == ('messages.DeleteMessageRequest', {'id': 'r1'})


def test_self_chat_is_cached(monkeypatch):
    c, _, _, chats = _client(monkeypatch, ids=['r1', 'r2'])
    monkeypatch.setattr(c, 'api', lambda cmd, data: {'messages': {'c1': {'id': 'fwd'}}})
    c.send_msg('a', 'c1', keep_unread=True)
    c.send_msg('b', 'c1', keep_unread=True)
    assert chats == ['100']


@pytest.mark.parametrize('forward', [RuntimeError('boom'), {'messages': {'c1': {'id': '-1'}}}, {'messages': {}}, {}])
def test_forward_failure_recalls_relay_and_falls_back(monkeypatch, forward):
    c, sent, calls, _ = _client(monkeypatch, forward=forward)
    warnings = []
    monkeypatch.setattr(client_mod.logger, 'warning', warnings.append)
    assert c.send_msg('hi', 'c1', keep_unread=True) == 'new'
    assert [_req(p).chatId for p in sent] == ['self', 'c1'] and _req(sent[1]).rootId == ''
    assert calls[-1] == ('messages.DeleteMessageRequest', {'id': 'r1'})
    assert '退回普通发送' in warnings[-1]


def test_relay_send_failure_falls_back_without_forwarding(monkeypatch):
    c, sent, calls, _ = _client(monkeypatch, ids=['-1', 'new'])
    assert c.send_msg('hi', 'c1', keep_unread=True) == 'new'
    assert [_req(p).chatId for p in sent] == ['self', 'c1'] and calls == []


def test_recall_failure_only_logs(monkeypatch):
    c, _, _, _ = _client(monkeypatch, recall_error=True)
    warnings = []
    monkeypatch.setattr(client_mod.logger, 'warning', warnings.append)
    assert c.send_msg('hi', 'c1', keep_unread=True) == 'fwd'
    assert 'r1' in warnings[-1]


def test_no_unread_sends_normally(monkeypatch):
    c, sent, calls, chats = _client(monkeypatch, read=8, last=8, ids=['new'])
    assert c.send_msg('hi', 'c1', keep_unread=True) == 'new'
    r = _req(sent[-1])
    assert (len(sent), r.chatId, r.rootId, r.isReplyInThread, r.syncToChat) == (1, 'c1', '', False, False)
    assert calls == [] and chats == []


def test_reply_is_a_quote_reply_unless_already_in_a_thread(monkeypatch):
    """普通群和私聊里回复是主会话里的引用回复(以前一律带 isReplyInThread, 回复都变成了新开的话题);
    回的那条已经在话题里时回进那个话题; 话题群里回进话题。数字人和 larkx send --root 都走这里。"""
    def reply(root_id, targets=None, thread_chat=False, **kw):
        c, sent, calls, _ = _client(monkeypatch, ids=['new'], targets=targets, thread_chat=thread_chat)
        if kw:
            msg_id = c.send_message({'kind': 'text', 'text': 'hi'}, 'c1', root_id=root_id, keep_unread=True, **kw)
        else:
            msg_id = c.send_msg('hi', 'c1', root_id=root_id, keep_unread=True)
        assert msg_id == 'new'
        r = _req(sent[-1])
        assert (len(sent), r.chatId, r.syncToChat) == (1, 'c1', False)
        return (r.rootId, r.parentId, r.isReplyInThread), [cmd for cmd, _ in calls]
    # 普通消息: 引用回复, 不经本人私聊转发
    assert reply('m7', {'m7': {'id': 'm7'}}) == (('m7', 'm7', False), ['messages.PullMessagesByIdsRequest'])
    # 回的那条本身是引用回复: rootId 用它的 rootId, 引用的还是它
    assert reply('m7', {'m7': {'id': 'm7', 'rootId': 'a1', 'parentId': 'a1'}})[0] == ('a1', 'm7', False)
    # 那条已经有话题(数字人在普通群的话题里答复时, --root 是话题的根消息): 回进这个话题
    assert reply('m7', {'m7': {'id': 'm7', 'threadId': 'm7', 'threadMessageType': 1}})[0] == ('m7', 'm7', True)
    # 那条是话题里的回复: 回进它所在的话题, 引用它
    assert reply('m8', {'m8': {'id': 'm8', 'rootId': 'm7', 'threadId': 'm7', 'threadMessageType': 2}})[0] == ('m7', 'm8', True)
    # 查不到或查询失败: 按引用回复发
    assert reply('m7', {})[0] == ('m7', 'm7', False)
    assert reply('m7', RuntimeError('boom'))[0] == ('m7', 'm7', False)
    # 话题群拒收 isReplyInThread, 回复本来就在话题里; 不用查
    assert reply('t1', thread_chat=True) == (('t1', 't1', False), [])
    # 网页版给了引用的那条和 in_thread: 照给的发, 不用查
    assert reply('m7', parent_id='m9', in_thread=False) == (('m7', 'm9', False), [])


def test_self_chat_target_and_keep_unread_off_send_normally(monkeypatch):
    c, sent, calls, _ = _client(monkeypatch, ids=['new', 'new2'])
    assert c.send_msg('hi', 'self', keep_unread=True) == 'new'
    assert c.send_msg('hi', 'c1') == 'new2'
    assert [_req(p).chatId for p in sent] == ['self', 'c1'] and calls == []


def test_rejected_send_is_a_failure(monkeypatch):
    c, _, _, _ = _client(monkeypatch, ids=['-1'])
    assert c.send_msg('hi', 'c1') is None


def test_relay_marks_expire(monkeypatch):
    state.mark_relay('old', 'self')
    old = time.time() - state.RELAY_TTL - 10
    os.utime(state.RELAY_DIR / 'old', (old, old))
    state.mark_relay('new', 'self')
    assert not state.is_relay({'cid': 'old', 'chat_id': 'self'}) and state.is_relay({'cid': 'new', 'chat_id': 'self'})
    assert not state.is_relay({'cid': '', 'chat_id': 'self'}) and not state.is_relay({'cid': '../x', 'chat_id': 'self'})


def test_my_read_status_batches(monkeypatch):
    c = object.__new__(LarkClient)
    calls = []

    def api(cmd, data):
        calls.append((cmd, len(data['messageIds'])))
        return {'readStatus': {i: i == 'm3' for i in data['messageIds']}}

    c.api = api
    st = c.my_read_status([f'm{i}' for i in range(120)])
    assert calls == [('messages.PullMessageMeReadStateRequest', n) for n in (50, 50, 20)]
    assert st['m3'] is True and st['m4'] is False and len(st) == 120


def test_builder_uses_given_cid():
    r = _req(builders.build_send_message_packet('x', 'c1', 'req', root_id='9', cid='abc'))
    assert (r.cid, r.rootId, r.parentId, r.isReplyInThread, r.syncToChat, r.content.richText.innerText) == ('abc', '9', '9', False, False, 'x')
    # 引用回复引用的是 parent_id; 回进已有话题才带 isReplyInThread, 话题群里不带
    r = _req(builders.build_send_message_packet('x', 'c1', 'req', root_id='9', parent_id='10'))
    assert (r.rootId, r.parentId, r.isReplyInThread) == ('9', '10', False)
    assert _req(builders.build_send_message_packet('x', 'c1', 'req', root_id='9', in_thread=True)).isReplyInThread is True
    assert _req(builders.build_send_message_packet('x', 'c1', 'req', root_id='9', in_thread=True, thread_chat=True)).isReplyInThread is False
    assert _req(builders.build_send_message_packet('x', 'c1', 'req')).cid
