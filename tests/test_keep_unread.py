from larkx.protocol import client as client_mod
from larkx.protocol.client import LarkClient
from larkx.protocol.proto import builders
from larkx.protocol.proto import lark_all_pb2 as LA


def _client(monkeypatch, read=5, last=8, thread_chat=False):
    c = object.__new__(LarkClient)
    c.auth = type('A', (), {'user_id': '100'})()
    c._thread_chats = {}
    sent = []
    c.read_state = lambda chat_id: (read, last)
    c.is_thread_chat = lambda chat_id: thread_chat
    c.pull_history = lambda chat_id, positions: [
        {'msg_id': f'm{p}', 'from_id': '100' if p == 8 else '200', 'msg_type': 4} for p in positions]
    c._gateway_post = lambda pkt: sent.append(pkt) or b''
    monkeypatch.setattr(client_mod.builders, 'decode_put_message_response', lambda content: 'new')
    return c, sent


def _req(pkt):
    r = LA.messages.PutMessageRequest()
    r.ParseFromString(pkt.payload)
    return r


def test_unread_chat_replies_in_thread_and_syncs(monkeypatch):
    c, sent = _client(monkeypatch)
    assert c.send_msg('hi', 'c1', keep_unread=True) == 'new'
    r = _req(sent[-1])
    # 挂在对方最近的一条(m7)下面, 不是自己发的 m8
    assert (r.rootId, r.isReplyInThread, r.syncToChat) == ('m7', True, True)
    c.send_msg('hi', 'c1', keep_unread=True, anchor_id='m3')
    assert _req(sent[-1]).rootId == 'm3'


def test_no_unread_sends_normally(monkeypatch):
    c, sent = _client(monkeypatch, read=8, last=8)
    c.send_msg('hi', 'c1', keep_unread=True)
    r = _req(sent[-1])
    assert (r.rootId, r.isReplyInThread, r.syncToChat) == ('', False, False)


def test_without_keep_unread_and_in_topic_groups(monkeypatch):
    c, sent = _client(monkeypatch)
    c.send_msg('hi', 'c1')
    assert not _req(sent[-1]).syncToChat
    c, sent = _client(monkeypatch, thread_chat=True)
    c.send_msg('hi', 'c1', root_id='t1', keep_unread=True)
    r = _req(sent[-1])
    assert (r.rootId, r.isReplyInThread, r.syncToChat) == ('t1', False, False)


def test_builder_sync_flag():
    pkt = builders.build_send_message_packet('x', 'c1', 'req', root_id='9', sync_to_chat=True)
    r = _req(pkt)
    assert (r.rootId, r.parentId, r.isReplyInThread, r.syncToChat, r.content.richText.innerText) == ('9', '9', True, True, 'x')
