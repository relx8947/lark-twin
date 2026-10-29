"""WS 推送: gzip 压缩的帧能解开; 推送按 cmd 统计, 不逐帧打日志, 解不开的帧限频告警。"""
import gzip

from loguru import logger

from larkx.protocol.client import PushStats
from larkx.protocol.proto import decoders, proto_pb2 as P

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


def test_gzip_frames_decoded():
    """服务端 gzip 压缩的推送(payloadEncoding=gzip)要先解压, 否则整条消息丢掉。"""
    req = P.PushMessagesRequest()
    m = req.messages['7001']
    m.id, m.type, m.fromId, m.chatId, m.position, m.createTime = '7001', 4, OTHER, 'g1', 9, 1700000000
    tc = P.TextContent()
    tc.text = '大卡片'
    m.content = tc.SerializeToString()
    for enc in ('', 'gzip', 'sniff'):
        raw = push_frame(6, req.SerializeToString(), enc)
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
        assert '还有 2 帧也解不开' in lines[-1]
    finally:
        logger.remove(sink)
