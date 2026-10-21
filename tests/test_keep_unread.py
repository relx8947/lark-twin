from larkx.protocol import client as client_mod
from larkx.protocol.client import LarkClient
from larkx.protocol.proto import builders
from larkx.protocol.proto import lark_all_pb2 as LA


def _client(monkeypatch, read=5, last=8, thread_chat=False, mine=(8,), done=(), status_error=False, extra=None):
    """会话 read+1..last 未读; mine 是本人发的 position, done 是对我已是"已读"的 position。"""
    c = object.__new__(LarkClient)
    c.auth = type('A', (), {'user_id': '100'})()
    c._thread_chats = {}
    sent, pulled = [], []
    c.read_state = lambda chat_id: (read, last)
    c.is_thread_chat = lambda chat_id: thread_chat

    def pull_history(chat_id, positions):
        pulled.append((positions.start, positions.stop - 1))
        out = [{'msg_id': f'm{p}', 'position': p, 'from_id': '100' if p in mine else '200', 'msg_type': 4,
                'root_id': '', 'recalled': False, 'sync_root_id': ''} for p in positions]
        for m in out:
            m.update((extra or {}).get(m['position'], {}))
        return out

    def my_read_status(ids):
        if status_error:
            raise RuntimeError('boom')
        return {i: int(i[1:]) in done for i in ids}

    c.pull_history = pull_history
    c.my_read_status = my_read_status
    c._gateway_post = lambda pkt: sent.append(pkt) or b''
    monkeypatch.setattr(client_mod.builders, 'decode_put_message_response', lambda content: 'new')
    return c, sent, pulled


def _req(pkt):
    r = LA.messages.PutMessageRequest()
    r.ParseFromString(pkt.payload)
    return r


def test_unread_chat_replies_under_own_message(monkeypatch):
    c, sent, pulled = _client(monkeypatch, mine=(3, 6))
    assert c.send_msg('hi', 'c1', keep_unread=True) == 'new'
    r = _req(sent[-1])
    # 挂在本人自己最近发的 m6 下面, 不碰对方的未读 m7、m8
    assert (r.rootId, r.isReplyInThread, r.syncToChat) == ('m6', True, True)
    assert pulled == [(1, 8)]


def test_own_anchor_skips_system_recalled_and_replies(monkeypatch):
    c, _, _ = _client(monkeypatch, mine=(4, 6, 7, 8),
                      extra={6: {'msg_type': 6}, 7: {'recalled': True}, 8: {'root_id': 'm2'}})
    assert c._unread_anchor('c1') == ('m4', False)


def test_own_synced_copy_anchors_on_its_topic_root(monkeypatch):
    # 上次匿名发送在主会话的那份(m8)不能当话题根, 挂到它所在话题的根 m2 上
    c, sent, _ = _client(monkeypatch, extra={8: {'sync_root_id': 'm2'}})
    c.send_msg('hi', 'c1', keep_unread=True)
    assert _req(sent[-1]).rootId == 'm2'


def test_without_own_message_picks_already_read_one(monkeypatch):
    # m7 虽在已读位置之上, 但已经是"我已读"(比如之前被回复过), 再回复一次不泄露; m5 在已读位置以下却仍是未读
    c, _, _ = _client(monkeypatch, mine=(), done=(2, 7))
    assert c._unread_anchor('c1') == ('m7', False)


def test_skips_others_synced_copies(monkeypatch):
    # m7 是别人"同时发送到会话"的那份, 它的话题根不一定读过
    c, _, _ = _client(monkeypatch, mine=(), done=(2, 7), extra={7: {'sync_root_id': 'm6'}})
    assert c._unread_anchor('c1') == ('m2', False)


def test_reads_around_read_position_when_unread_is_long(monkeypatch):
    c, _, pulled = _client(monkeypatch, read=100, last=300, mine=(), done=(95,))
    assert c._unread_anchor('c1') == ('m95', False)
    assert pulled == [(251, 300), (81, 100)]


def test_falls_back_to_position_when_status_query_fails(monkeypatch):
    c, _, _ = _client(monkeypatch, mine=(), status_error=True)
    assert c._unread_anchor('c1') == ('m5', False)


def test_last_resort_leaks_one_and_says_so(monkeypatch):
    c, sent, _ = _client(monkeypatch, mine=())
    warnings = []
    monkeypatch.setattr(client_mod.logger, 'warning', warnings.append)
    assert c._unread_anchor('c1') == ('m8', True)
    c.send_msg('hi', 'c1', keep_unread=True)
    assert _req(sent[-1]).rootId == 'm8' and 'm8' in warnings[-1]


def test_explicit_root_is_kept(monkeypatch):
    c, sent, pulled = _client(monkeypatch)
    c.send_msg('hi', 'c1', root_id='m7', keep_unread=True)
    r = _req(sent[-1])
    assert (r.rootId, r.isReplyInThread, r.syncToChat) == ('m7', True, True) and pulled == []


def test_rejected_send_is_a_failure(monkeypatch):
    c, _, _ = _client(monkeypatch)
    monkeypatch.setattr(client_mod.builders, 'decode_put_message_response', lambda content: '-1')
    assert c.send_msg('hi', 'c1', keep_unread=True) is None


def test_no_unread_sends_normally(monkeypatch):
    c, sent, _ = _client(monkeypatch, read=8, last=8)
    c.send_msg('hi', 'c1', keep_unread=True)
    r = _req(sent[-1])
    assert (r.rootId, r.isReplyInThread, r.syncToChat) == ('', False, False)


def test_without_keep_unread_and_in_topic_groups(monkeypatch):
    c, sent, _ = _client(monkeypatch)
    c.send_msg('hi', 'c1')
    assert not _req(sent[-1]).syncToChat
    c, sent, _ = _client(monkeypatch, thread_chat=True)
    c.send_msg('hi', 'c1', root_id='t1', keep_unread=True)
    r = _req(sent[-1])
    assert (r.rootId, r.isReplyInThread, r.syncToChat) == ('t1', False, False)


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


def test_builder_sync_flag():
    pkt = builders.build_send_message_packet('x', 'c1', 'req', root_id='9', sync_to_chat=True)
    r = _req(pkt)
    assert (r.rootId, r.parentId, r.isReplyInThread, r.syncToChat, r.content.richText.innerText) == ('9', '9', True, True, 'x')
