"""WS 推送: gzip 压缩的帧能解开; PUSH_SID 只推来 sid 时按 sid 补拉; 逐条转发(cmd 134)推送里没带的消息按 id 拉回来;
推送按 cmd 统计, 不逐帧打日志, 解不开的帧限频告警; payloadEncoding=json 的帧按 JSON 解开计数, 不算解不开;
连上长连接后调用 on_open。"""
import asyncio
import gzip
import json

import websockets
from loguru import logger

from larkx.protocol.client import LarkClient, PushStats
from larkx.protocol.proto import decoders, gateway, proto_pb2 as P

OTHER = '200'


def push_frame(cmd=6, payload=b'', encoding=''):
    pkt = P.Packet()
    pkt.cmd, pkt.sid, pkt.payload = cmd, 's1', payload
    frame = P.Frame()
    frame.payload = pkt.SerializeToString()
    if encoding == 'gzip':
        frame.payload, frame.payloadEncoding = gzip.compress(frame.payload), 'gzip'
    elif encoding == 'sniff':
        frame.payload = gzip.compress(frame.payload)
    return frame.SerializeToString()


def push_request(msg_id='7001', text='大卡片'):
    req = P.PushMessagesRequest()
    m = req.messages[msg_id]
    m.id, m.type, m.fromId, m.chatId, m.position, m.createTime = msg_id, 4, OTHER, 'g1', 9, 1700000000
    tc = P.TextContent()
    tc.text = text
    m.content = tc.SerializeToString()
    return req.SerializeToString()


def test_gzip_frames_decoded():
    """服务端 gzip 压缩的推送(payloadEncoding=gzip)要先解压, 否则整条消息丢掉。"""
    for enc in ('', 'gzip', 'sniff'):
        raw = push_frame(6, push_request(), enc)
        _, packet = decoders.parse_ws_frame(raw)
        assert packet['cmd'] == 6 and packet['sid'] == 's1'
        [msg] = decoders.decode_push_messages(raw)
        assert (msg['msg_id'], msg['chat_id'], msg['position'], msg['content']) == ('7001', 'g1', 9, '大卡片')


def test_push_stats_log_sparingly():
    lines, now = [], [0.0]
    sink = logger.add(lambda m: lines.append(m.record['message']), level='INFO')
    try:
        stats = PushStats(interval=600, clock=lambda: now[0])
        for _ in range(5):
            stats.packet(6, {}, {})
            stats.packet(18, {}, {'payload': b''})
            stats.packet(1002, {'payloadEncoding': 'gzip'}, {'payload': b''})
            stats.maybe_report()
        firsts = [x for x in lines if '第一次收到' in x]
        assert len(firsts) == 2 and 'chats.PushChatsRequest' in firsts[0] and 'cmd 1002' in firsts[1]
        for _ in range(3):
            stats.error(ValueError('坏帧'), b'xx')
        assert sum('解不开的帧' in x for x in lines) == 1        # 限频: 一个周期只打一条
        now[0] = 601
        stats.maybe_report()
        report = lines[-1]
        assert '新消息(cmd 6) 5 帧' in report and 'cmd 18(chats.PushChatsRequest) 5' in report
        assert '压缩帧 5' in report and '解不开的帧 3' in report
        stats.error(ValueError('坏帧'), b'xx')
        assert '还有 2 次也出错' in lines[-1]
    finally:
        logger.remove(sink)


def test_push_sid_pulled_and_dispatched():
    """PUSH_SID(cmd 6000)只推来 sid: 按 sid 补拉, 拉到的新消息推送照常交给回调; 补拉出错只计数, 不影响别的。"""
    L = gateway._lark_all()
    pkt = L.improto.Packet()
    pkt.cmd, pkt.sid = 6000, 's9'
    pkt.compactedSids.extend([11, 12])
    frame = P.Frame()
    frame.payload = pkt.SerializeToString()
    assert decoders.push_sids(frame.SerializeToString()) == ['s9', '11', '12']

    calls, got = [], []

    def api(cmd, data=None):
        calls.append((cmd, data))
        if data['sids'] == ['bad']:
            raise RuntimeError('网关出错')
        return {'packets': {'s9': {'cmd': 6, 'payload': push_request('8001', '卡片')}, '11': {'cmd': 27, 'payload': b''}}}

    async def on_message(msg):
        got.append((msg['msg_id'], msg['content']))
    c = LarkClient.__new__(LarkClient)
    c.api, c.push_stats, c._thread_chats = api, PushStats(), {'g1': False}
    asyncio.run(c._pull_sids(['s9', '11', '12'], on_message))
    asyncio.run(c._pull_sids(['bad'], on_message))
    asyncio.run(c._pull_sids([], on_message))
    assert calls == [('improto.PullPacketsBySidsRequest', {'sids': ['s9', '11', '12']}),
                     ('improto.PullPacketsBySidsRequest', {'sids': ['bad']})]
    assert got == [('8001', '卡片')]
    st = c.push_stats
    assert (st.pulls, st.pulled, st.pulled_msgs, st.pull_errors) == (1, {6: 1, 27: 1}, 1, 1)


def json_frame(data, service=1):
    frame = P.Frame()
    frame.service, frame.method, frame.payloadEncoding = service, 1, 'json'
    frame.payload = json.dumps(data).encode()
    return frame.SerializeToString()


def test_json_frames_decoded():
    """payloadEncoding=json 的帧 payload 是 JSON, 不是 Packet: 按 JSON 解开, 取出顶层的 cmd(数字或数字字符串), 不带 sid。"""
    _, packet = decoders.parse_ws_frame(json_frame({'cmd': 3603, 'sid': 'x', 'data': {'a': 1}}))
    assert packet == {'json': {'cmd': 3603, 'sid': 'x', 'data': {'a': 1}}, 'cmd': 3603}
    assert decoders.parse_ws_frame(json_frame({'command': '42'}))[1]['cmd'] == 42
    assert decoders.parse_ws_frame(json_frame({'type': 'notice'}))[1]['cmd'] is None
    assert decoders.parse_ws_frame(json_frame([1, 2]))[1] == {'json': [1, 2], 'cmd': None}


def test_json_frames_counted_not_errors():
    """JSON 帧按 cmd 计数, 每种 cmd 只在第一次打一条带内容的日志, 10 分钟汇总里单列, 不再算"解不开的帧";
    里面要是新消息推送就打 WARNING。"""
    lines, now = [], [0.0]
    sink = logger.add(lambda m: lines.append((m.record['level'].name, m.record['message'])), level='INFO')
    try:
        stats = PushStats(interval=600, clock=lambda: now[0])
        for raw in [json_frame({'cmd': 3603, 'data': '提示'})] * 3 + [json_frame({'x': 1})] * 2:
            frame, packet = decoders.parse_ws_frame(raw)
            stats.json_frame(frame, packet)
        firsts = [msg for _, msg in lines if '第一次收到 JSON 帧' in msg]
        assert len(firsts) == 2
        assert 'cmd 3603(未知)' in firsts[0] and 'service 1' in firsts[0] and '"data": "提示"' in firsts[0]
        assert '(没有 cmd)' in firsts[1]
        assert not [lvl for lvl, _ in lines if lvl == 'WARNING']
        now[0] = 601
        stats.maybe_report()
        report = lines[-1][1]
        assert 'JSON 帧: cmd 3603(未知) 3, (没有 cmd) 2' in report and '解不开的帧 0' in report and '压缩帧 0' in report
        now[0] = 1202
        stats.maybe_report()
        assert 'JSON 帧: 无' in lines[-1][1]
        frame, packet = decoders.parse_ws_frame(json_frame({'cmd': 6, 'payload': ''}))
        stats.json_frame(frame, packet)
        assert lines[-1][0] == 'WARNING' and '新消息推送' in lines[-1][1]
    finally:
        logger.remove(sink)


class FakeWS:
    def __init__(self, frames):
        self.frames, self.sent = frames, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _frames(self):
        for f in self.frames:
            yield f

    def __aiter__(self):
        return self._frames()

    async def send(self, data):
        self.sent.append(data)


def test_ws_loop_skips_ack_for_json_frames(monkeypatch):
    """长连接里混着 JSON 帧: JSON 帧只计数, 不 ack(哪怕 JSON 里有 sid); 普通推送照常 ack、计数。"""
    ws = FakeWS([json_frame({'cmd': 3603, 'sid': 'j1'}), push_frame(27), b'\xff\xff'])
    monkeypatch.setattr(websockets, 'connect', lambda url: ws)
    c = LarkClient.__new__(LarkClient)
    c.push_stats, c._loop_started, c.loop = PushStats(), True, None
    c.build_ws_url = lambda: 'wss://example'
    asyncio.run(c.connect_websocket(lambda msg: None))
    acks = []
    for raw in ws.sent:
        frame = P.Frame()
        frame.ParseFromString(raw)
        pkt = P.Packet()
        pkt.ParseFromString(frame.payload)
        acks.append((pkt.cmd, pkt.sid))
    assert acks == [(1, 's1')]
    st = c.push_stats
    assert st.json_counts == {3603: 1} and st.counts == {27: 1} and st.errors == 1   # 最后那个是真坏帧


# ---- 逐条转发(cmd 134 messages.PushBatchCopyMessages) ----

CHAT = '7657843073757580233'
IMG, POST = '7702372020750732292', '7702372020775980025'


def entity(msg_id, position, kind='post', text='这不是搞笑吗', chat_type=2, chat_id=CHAT, create_time=1793348235):
    """一条消息实体(entities.Message), 和推送、按 id 拉回来的一样。"""
    m = gateway._lark_all().entities.Message()
    m.id, m.fromId, m.chatId, m.chatType, m.position, m.createTime = msg_id, OTHER, chat_id, chat_type, position, create_time
    if kind == 'post':
        c = P.PostContent()
        c.text = text
        m.type = 2
    elif kind == 'text':
        c = P.TextContent()
        c.text = text
        m.type = 4
    else:
        c = P.ImageContent()
        c.imageV2.imageKey = 'img_v3_0216v'
        m.type = 5
    m.content = c.SerializeToString()
    return m


def batch_copy_payload(ids=(IMG, POST), last=None):
    """日志里 16:17:17 那次的形状: messageIds、会话的 feedCard、最后一条(POST)的完整内容。"""
    p = gateway._lark_all().messages.PushBatchCopyMessages()
    p.messageIds.extend(int(i) for i in ids)
    p.feedCard.id, p.feedCard.type, p.feedCard.updateTime = CHAT, 1, 1793348235
    p.lastMessage.CopyFrom(last if last is not None else entity(POST, 151681))
    return p.SerializeToString()


def test_batch_copy_decoded():
    """cmd 134 里只有最后一条带着完整内容, 别的只有 id; lastMessage 不在这批里就不用。"""
    ids, msgs = decoders.batch_copy_messages(batch_copy_payload())
    assert ids == [IMG, POST]
    [m] = msgs
    assert (m['msg_id'], m['msg_type_name'], m['chat_id'], m['position'], m['content']) == (POST, 'POST', CHAT, 151681, '[富文本] \n这不是搞笑吗')
    assert decoders.batch_copy_messages(batch_copy_payload(ids=(IMG,))) == ([IMG], [])
    ids, msgs = decoders.batch_copy_messages(batch_copy_payload(ids=()))
    assert ids == [POST] and [m['msg_id'] for m in msgs] == [POST]
    names = gateway.push_types(134)
    assert names == ['messages.PushBatchCopyMessages']


def batch_client(api):
    c = LarkClient.__new__(LarkClient)
    c.api, c.push_stats, c._thread_chats = api, PushStats(), {CHAT: False}
    return c


def test_batch_copy_pulls_rest_by_id_and_dispatches():
    """推送里带着的那条直接用, 其余的按 id 只读地拉回来(PullMessagesByIdsRequest), 按 position 排好交给回调;
    推送里都带着了就不再拉; 拉不成只计数, 靠补收兜底。"""
    calls, got = [], []

    def api(cmd, data=None):
        calls.append((cmd, data))
        if data['messageIds'] == ['404']:
            raise RuntimeError('网关出错')
        return {'messages': {IMG: gateway.response_to_dict(type(entity(IMG, 1)), entity(IMG, 151680, kind='image').SerializeToString())}}

    async def on_message(msg):
        got.append((msg['msg_id'], msg['msg_type_name'], msg['content'][:4]))
    c = batch_client(api)
    asyncio.run(c._batch_copy(batch_copy_payload(), on_message))
    assert calls == [('messages.PullMessagesByIdsRequest', {'messageIds': [IMG]})]
    assert got == [(IMG, 'IMAGE', '[图片]'), (POST, 'POST', '[富文本')]       # 先 IMAGE 后 POST, 和发的顺序一样
    asyncio.run(c._batch_copy(batch_copy_payload(ids=(POST,)), on_message))   # 只有一条, 推送里就带着
    assert len(calls) == 1 and got[-1][0] == POST
    asyncio.run(c._batch_copy(batch_copy_payload(ids=('404', POST)), on_message))
    assert len(got) == 3
    st = c.push_stats
    assert (st.batches, st.batch_msgs, st.batch_pulled, st.batch_missing, st.batch_errors) == (2, 3, 1, 0, 1)


def test_batch_copy_counts_missing_and_reports():
    """按 id 没拉到的条数计数; 10 分钟汇总里写上逐条转发的处理结果。"""
    lines, now = [], [0.0]
    sink = logger.add(lambda m: lines.append(m.record['message']), level='INFO')
    try:
        c = batch_client(lambda cmd, data=None: {'messages': {}})
        c.push_stats = PushStats(interval=600, clock=lambda: now[0])
        got = []

        async def on_message(msg):
            got.append(msg['msg_id'])
        asyncio.run(c._batch_copy(batch_copy_payload(), on_message))
        assert got == [POST]
        assert [x for x in lines if '第一次处理逐条转发' in x] == [
            f"[WS 推送] 第一次处理逐条转发 cmd 134: 消息 id ['{IMG}', '{POST}'], 推送里直接带着 1 条, 按 id 拉回 0 条, 交给回调 1 条"]
        now[0] = 601
        c.push_stats.maybe_report()
        assert '逐条转发(cmd 134) 1 次, 消息 1 条(按 id 拉回 0 条, 没拉到 1 条), 处理失败 0 次' in lines[-1]
    finally:
        logger.remove(sink)


def test_ws_loop_handles_batch_copy_frames(monkeypatch):
    """长连接里推来 cmd 134: 照常 ack, 这批消息交给回调(和 cmd 6 一样在协议层自己的事件循环里处理)。"""
    import threading
    ws = FakeWS([push_frame(134, batch_copy_payload(ids=(POST,)))])
    monkeypatch.setattr(websockets, 'connect', lambda url: ws)
    c = batch_client(lambda cmd, data=None: {'messages': {}})
    c._loop_started, c.loop = False, asyncio.new_event_loop()
    c.build_ws_url = lambda: 'wss://example'
    got, done = [], threading.Event()

    async def on_message(msg):
        got.append(msg['msg_id'])
        done.set()
    try:
        asyncio.run(c.connect_websocket(on_message))
        assert done.wait(5) and got == [POST]
    finally:
        c.loop.call_soon_threadsafe(c.loop.stop)
    frame = P.Frame()
    frame.ParseFromString(ws.sent[0])
    pkt = P.Packet()
    pkt.ParseFromString(frame.payload)
    assert (pkt.cmd, pkt.sid) == (1, 's1') and c.push_stats.counts == {134: 1}


def test_batch_copy_follows_dedup_and_old_message_rules(tmp_path, monkeypatch, fake_client):
    """逐条转发的消息走和 cmd 6 一样的回调: 已经收过的不再触发, 太久远的只入库, 新的照常交给数字人。"""
    import time
    from larkx.daemon.listener import Listener
    from test_reliability import _daemon
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    listener = Listener(fake_client, obj.storage, obj.cfg, '100', on_stored=obj.on_message)
    now = int(time.time())
    old = entity('901', 11, kind='text', text='/ai 旧的', chat_type=1, chat_id='p1', create_time=now - 3 * 3600)
    seen = entity('902', 12, kind='text', text='/ai 在吗', chat_type=1, chat_id='p1', create_time=now)
    new = entity('903', 13, kind='text', text='/ai 帮我看看', chat_type=1, chat_id='p1', create_time=now)

    def api(cmd, data=None):
        assert cmd == 'messages.PullMessagesByIdsRequest' and data == {'messageIds': ['901', '902']}
        return {'messages': {m.id: gateway.response_to_dict(type(m), m.SerializeToString()) for m in (old, seen)}}
    c = batch_client(api)

    async def run():
        # 902 已经由 cmd 6 推过、交给过数字人
        assert await listener.on_message(decoders.message_dict(gateway.response_to_dict(type(seen), seen.SerializeToString())))
        await c._batch_copy(batch_copy_payload(ids=('901', '902', '903'), last=new), listener.on_message)
    asyncio.run(run())
    assert sorted(m.msg_id for m in obj.storage.get_messages('p1')) == ['901', '902', '903']
    events = [obj.inbox.get_nowait() for _ in range(obj.inbox.qsize())]
    assert [(e.ref, e.body) for e in events] == [('902', '在吗'), ('903', '帮我看看')]


def test_ws_open_callback(monkeypatch):
    """连上长连接后调用一次 on_open; 回调出错不影响收推送。"""
    calls = []
    for cb in (lambda: calls.append('open'), lambda: 1 / 0):
        ws = FakeWS([push_frame(27)])
        monkeypatch.setattr(websockets, 'connect', lambda url: ws)
        c = LarkClient.__new__(LarkClient)
        c.push_stats, c._loop_started, c.loop = PushStats(), True, None
        c.build_ws_url = lambda: 'wss://example'
        asyncio.run(c.connect_websocket(lambda msg: None, on_open=cb))
        assert c.push_stats.counts == {27: 1}
    assert calls == ['open']
