"""WS 推送: gzip 压缩的帧能解开; PUSH_SID 只推来 sid 时按 sid 补拉; 推送按 cmd 统计, 不逐帧打日志, 解不开的帧限频告警。"""
import asyncio
import gzip

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
