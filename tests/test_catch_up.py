"""补收不丢消息: 核对位置只由补收推进、核对全部近期会话、分批拉全、feed 发现新会话; 补收到的旧消息不触发任何动作;
漏推告警按会话冷却; 长连接重新连上后马上补收, 断线期间的消息不算漏推。"""
import asyncio
import time
from datetime import datetime, timedelta

import pytest
from loguru import logger

from larkx import daemon as d
from larkx.daemon.listener import Listener
from larkx.protocol.client import LarkClient
from larkx.storage.models import CatchupMark
from test_reliability import _daemon

ME, OTHER = '100', '200'


class Server:
    """假的飞书服务端: 每个会话的消息按 position 存着, 记下补收发出的请求。"""

    def __init__(self, fake_client, feed=()):
        self.latest, self.pulls, self.chat_calls, self.feed_calls = {}, [], [], []
        self.feed, self.fail = list(feed), set()
        self.create_time = int(time.time()) - 3 * 3600
        fake_client.api = self.api
        fake_client.pull_history = self.pull_history
        fake_client.feed_chats = self.feed_chats

    def api(self, cmd, data=None):
        assert cmd == 'chats.PullChatsByIdsRequest'
        self.chat_calls.append(list(data['chatIds']))
        return {'chats': {c: {'lastMessagePosition': self.latest[c]} for c in data['chatIds'] if c in self.latest}}

    def pull_history(self, chat_id, positions):
        positions = list(positions)
        if (chat_id, positions[0]) in self.fail:
            raise RuntimeError('网络不通')
        self.pulls.append((chat_id, positions[0], positions[-1]))
        return [self.message(chat_id, p) for p in positions if p <= self.latest[chat_id]]

    def message(self, chat_id, p, **kw):
        return {'msg_id': f'{chat_id}-{p}', 'chat_id': chat_id, 'chat_type': 2, 'from_id': OTHER, 'position': p,
                'create_time': self.create_time, 'content': f'第 {p} 条', **kw}

    def feed_chats(self, since):
        self.feed_calls.append(since)
        return self.feed


def setup(tmp_path, monkeypatch, fake_client, feed=()):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj._feed_since = None
    obj.listener = Listener(fake_client, obj.storage, obj.cfg, ME, on_stored=obj.on_message)
    return obj, Server(fake_client, feed)


def mark(st, chat_id):
    s = st.Session()
    try:
        row = s.get(CatchupMark, chat_id)
        return row.position if row else None
    finally:
        s.close()


def stored(st, chat_id):
    return sorted(m.position for m in st.get_messages(chat_id, limit=10000))


# ---- 核对位置 ----

def test_ws_message_does_not_skip_missed_ones(tmp_path, monkeypatch, fake_client):
    """WS 推来一条较新的消息, 把 last_position 抬到最新; 中间没推的消息下一轮照样补得到。"""
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    srv.latest['g1'] = 10
    asyncio.run(obj.listener.on_message(srv.message('g1', 10)))
    assert mark(obj.storage, 'g1') == 10                      # 新会话第一次入库: 从这条核对起, 以前的不回填
    srv.latest['g1'] = 16
    asyncio.run(obj.listener.on_message(srv.message('g1', 15)))  # WS 只推了 15
    assert mark(obj.storage, 'g1') == 10
    got, left = asyncio.run(obj.catch_up())
    assert got == {'g1': 5} and left == 0                     # 11-14、16; 15 已经有了
    assert stored(obj.storage, 'g1') == list(range(10, 17)) and mark(obj.storage, 'g1') == 16
    assert asyncio.run(obj.catch_up()) == ({}, 0) and len(srv.pulls) == 1


def test_existing_chat_starts_from_last_position(tmp_path, monkeypatch, fake_client):
    """升级前就有的会话(没有核对位置): 从当时的 last_position 核对起, 不管是先来新消息还是先轮到补收。"""
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    for cid in ('a', 'b'):
        obj.storage.save_message(srv.message(cid, 50))
    s = obj.storage.Session()
    s.query(CatchupMark).delete()
    s.commit()
    s.close()
    srv.latest.update(a=60, b=60)
    asyncio.run(obj.listener.on_message(srv.message('a', 58)))
    assert mark(obj.storage, 'a') == 50 and mark(obj.storage, 'b') is None
    got, _ = asyncio.run(obj.catch_up())
    assert got == {'a': 9, 'b': 10} and mark(obj.storage, 'b') == 60


def test_all_recent_chats_checked(tmp_path, monkeypatch, fake_client):
    """7 天内有动静的会话全部核对(以前只看前 30 个), 每批 20 个; 7 天前的不看。"""
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    for i in range(45):
        cid = f'c{i:02d}'
        obj.storage.save_message(srv.message(cid, 5))
        srv.latest[cid] = 6
    s = obj.storage.Session()
    from larkx.storage.models import Chat
    s.query(Chat).filter(Chat.chat_id == 'c44').update({'updated_at': datetime.now() - timedelta(days=8)})
    s.commit()
    s.close()
    got, left = asyncio.run(obj.catch_up())
    assert len(got) == 44 and 'c44' not in got and left == 0
    assert [len(c) for c in srv.chat_calls] == [20, 20, 4]


def test_pulls_in_batches_with_round_limit(tmp_path, monkeypatch, fake_client):
    """每次拉 50 条, 不再只拉最后 50 条; 每轮有总量上限, 缺得少的会话先补, 没拉完的下一轮接着拉。"""
    monkeypatch.setattr(d, 'CATCHUP_ROUND_MAX', 70)
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    obj.storage.save_message(srv.message('big', 10))
    obj.storage.save_message(srv.message('small', 10))
    srv.latest.update(big=130, small=13)
    got, left = asyncio.run(obj.catch_up())
    assert srv.pulls == [('small', 11, 13), ('big', 11, 60), ('big', 61, 77)]
    assert got == {'small': 3, 'big': 67} and left == 53 and mark(obj.storage, 'big') == 77
    got, left = asyncio.run(obj.catch_up())
    assert srv.pulls[3:] == [('big', 78, 127), ('big', 128, 130)]
    assert got == {'big': 53} and left == 0
    assert stored(obj.storage, 'big') == list(range(10, 131))


def test_failed_pull_keeps_position(tmp_path, monkeypatch, fake_client):
    """拉某一段出错: 核对位置停在出错前, 下一轮从那里接着拉, 不跳过; 别的会话照常补。"""
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    obj.storage.save_message(srv.message('g1', 10))
    obj.storage.save_message(srv.message('g2', 10))
    srv.latest.update(g1=120, g2=12)
    srv.fail.add(('g1', 61))
    got, left = asyncio.run(obj.catch_up())
    assert got == {'g2': 2, 'g1': 50} and left == 60 and mark(obj.storage, 'g1') == 60
    srv.fail.clear()
    got, left = asyncio.run(obj.catch_up())
    assert got == {'g1': 60} and left == 0 and stored(obj.storage, 'g1') == list(range(10, 121))


def test_feed_finds_chats_never_pushed(tmp_path, monkeypatch, fake_client):
    """长连接一条都没推过的会话本地没有记录, 从 feed 发现: 启动后第一轮从当前位置核对起(不回填),
    之后的轮次里新出现的会话往前多拉 50 条; feed 只拉上一轮以来有更新的。"""
    obj, srv = setup(tmp_path, monkeypatch, fake_client, feed=['n1'])
    srv.latest['n1'] = 300
    before = time.time()
    assert asyncio.run(obj.catch_up()) == ({}, 0)
    assert mark(obj.storage, 'n1') == 300 and srv.pulls == []
    assert srv.feed_calls[0] <= before - 7 * 86400 + 1       # 第一轮看 7 天
    srv.latest['n1'] = 302
    srv.feed = ['n1', 'n2']
    srv.latest['n2'] = 80
    got, _ = asyncio.run(obj.catch_up())
    assert got == {'n1': 2, 'n2': 50} and mark(obj.storage, 'n2') == 80
    assert srv.feed_calls[1] >= before - 300                  # 之后只看上一轮以来的


def test_feed_failure_falls_back_to_local_chats(tmp_path, monkeypatch, fake_client):
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    obj.storage.save_message(srv.message('g1', 10))
    srv.latest['g1'] = 11

    def broken(since):
        raise RuntimeError('feed 挂了')
    fake_client.feed_chats = broken
    assert asyncio.run(obj.catch_up()) == ({'g1': 1}, 0)
    assert obj._feed_since is None                            # 下一轮还按第一轮看 7 天


# ---- 补收到的旧消息不触发任何动作 ----

class Session:
    def __init__(self):
        self.prompts, self.events, self.interrupts = [], [], 0

    async def interrupt(self):
        self.interrupts += 1
        return False


def test_old_messages_trigger_nothing(tmp_path, monkeypatch, fake_client):
    """超过 LARKX_INBOX_MAX_AGE 的消息只入库: 不交给数字人, 不回"没有权限", 接管中也不处理, 本人的指令也不执行。"""
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    obj.session = Session()
    obj.cfg['triggers']['owner_only'] = True
    obj.storage.takeover_start('tk', int(time.time()) + 3600, '', 'owner')
    old = int(time.time()) - 3 * 3600
    msgs = [{'msg_id': 'm1', 'chat_id': 'p1', 'chat_type': 1, 'from_id': OTHER, 'content': '/ai 在吗'},
            {'msg_id': 'm2', 'chat_id': 'g1', 'chat_type': 2, 'from_id': OTHER, 'content': '/ai 在吗', 'at_me': True},
            {'msg_id': 'm3', 'chat_id': 'tk', 'chat_type': 1, 'from_id': OTHER, 'content': '你好'},
            {'msg_id': 'm4', 'chat_id': 'self', 'chat_type': 1, 'from_id': ME, 'content': '/ai /stop'},
            {'msg_id': 'm5', 'chat_id': 'self', 'chat_type': 1, 'from_id': ME, 'content': '/ai 帮我看看'}]
    for m in msgs:
        asyncio.run(obj.on_message(dict(m, create_time=old), '会话', '某人'))
    assert fake_client.sent == [] and obj.inbox.qsize() == 0 and obj.session.interrupts == 0
    # 同样的消息是新的: 照常回"没有权限"、交给数字人
    for m in msgs[:3]:
        asyncio.run(obj.on_message(dict(m, msg_id=m['msg_id'] + 'n', create_time=int(time.time())), '会话', '某人'))
    assert len(fake_client.replies) == 2 and obj.inbox.qsize() == 1


def test_catch_up_of_old_ai_messages_sends_nothing(tmp_path, monkeypatch, fake_client):
    """补收拉到几小时前别人发的 /ai: 入库, 但不回"没有权限"、不触发数字人。"""
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    obj.cfg['triggers']['owner_only'] = True
    obj.storage.save_message(srv.message('p1', 10, chat_type=1))
    srv.latest['p1'] = 12
    srv.message = lambda cid, p, **kw: {'msg_id': f'{cid}-{p}', 'chat_id': cid, 'chat_type': 1, 'from_id': OTHER,
                                        'position': p, 'create_time': int(time.time()) - 3 * 3600, 'content': '/ai 在吗'}
    assert asyncio.run(obj.catch_up()) == ({'p1': 2}, 0)
    assert fake_client.sent == [] and obj.inbox.qsize() == 0


# ---- 漏推告警 ----

def test_ws_miss_alert_names_chats_and_cools_down_per_chat(tmp_path, monkeypatch, fake_client):
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    obj.storage.update_chat_name('g1', '报警群', 2)
    obj.storage.update_chat_name('g2', '项目群', 2)
    obj._alert_ws_miss({'g1': 40, 'g2': 3}, 0)
    assert len(fake_client.sent) == 1
    text = fake_client.sent[0][1]
    assert '「报警群」40 条、「项目群」3 条' in text and '43 条' in text and '已补收并处理' not in text
    obj._alert_ws_miss({'g1': 35}, 0)                         # 同一个会话一天内不再报
    assert len(fake_client.sent) == 1
    obj._alert_ws_miss({'g1': 30, 'g3': 2}, 120)              # 只报新的会话, 一并说明报过的和没拉完的
    text = fake_client.sent[1][1]
    assert '漏推了 2 条' in text and '「g3」2 条' in text and '另有 1 个会话一天内报过, 这轮也补收了 30 条' in text
    assert '还有 120 条没拉完' in text


def system_tip(cid, p, srv):
    """大群"已关闭通知"的系统提示(type=1500): 服务端本来就不经长连接推, 只有补收拉得到。"""
    return {'msg_id': f'{cid}-{p}', 'chat_id': cid, 'chat_type': 2, 'from_id': '1', 'position': p,
            'create_time': srv.create_time, 'msg_type': 6, 'msg_type_name': 'SYSTEM', 'content': '[系统消息] type=1500 ',
            'content_data': {'messageTemplate': 'This is a large group. Notifications are turned off to avoid distractions. ',
                             'type': 1500, 'sysContentValues': {'visible_chatters': {}}}}


def test_system_messages_stored_but_not_counted(tmp_path, monkeypatch, fake_client):
    """补收到的系统提示照样入库、推进核对位置, 但不算进漏推条数, 只打一条 INFO。"""
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    obj.storage.save_message(srv.message('g1', 10))
    srv.latest['g1'] = 14
    plain = srv.message
    srv.message = lambda cid, p, **kw: plain(cid, p, **kw) if p == 14 else system_tip(cid, p, srv)
    lines = []
    sink = logger.add(lambda m: lines.append((m.record['level'].name, m.record['message'])), level='INFO')
    try:
        got, left = asyncio.run(obj.catch_up())
    finally:
        logger.remove(sink)
    assert got == {'g1': 1} and left == 0
    assert stored(obj.storage, 'g1') == [10, 11, 12, 13, 14] and mark(obj.storage, 'g1') == 14
    [info] = [msg for lvl, msg in lines if '系统提示' in msg]
    assert '补收到 3 条系统提示(type=1500 3 条)' in info and '「g1」3 条' in info
    assert not [msg for lvl, msg in lines if lvl == 'WARNING']


def test_system_messages_do_not_alert_or_use_cooldown(tmp_path, monkeypatch, fake_client):
    """补收循环里只补到系统提示: 不发漏推告警、不打 WARNING, 也不占这个会话一天一次的告警额度;
    之后真漏推了人发的消息照样告警, 条数里不算系统提示。"""
    monkeypatch.setattr(d, 'CATCHUP_DELAY', 0)
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    obj.cfg['catchup_interval'] = 0.01
    obj.storage.update_chat_name('g1', '大群', 2)
    obj.storage.save_message(srv.message('g1', 10))
    srv.latest['g1'] = 10
    plain = srv.message
    srv.message = lambda cid, p, **kw: plain(cid, p, **kw) if p in (14, 15) else system_tip(cid, p, srv)
    rounds = [lambda: None,                                   # 第一轮: 启动时没有积压
              lambda: srv.latest.update(g1=13),               # 只有系统提示
              lambda: srv.latest.update(g1=15),               # 人发的 14、15
              ]
    real, warnings, before = obj.catch_up, [], []

    async def next_round():
        before.append((len(fake_client.sent), len(warnings)))   # 每轮开始前: 已发的通知、已打的 WARNING
        if not rounds:
            raise asyncio.CancelledError
        rounds.pop(0)()
        return await real()
    obj.catch_up = next_round
    sink = logger.add(lambda m: warnings.append(m.record['message']), level='WARNING')
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(obj._catch_up_loop())
    finally:
        logger.remove(sink)
    assert stored(obj.storage, 'g1') == [10, 11, 12, 13, 14, 15] and mark(obj.storage, 'g1') == 15
    assert before[2] == (0, 0)                                # 只补到系统提示的那一轮过后: 没通知, 没 WARNING
    [(_, text)] = fake_client.sent
    # 这个会话的告警额度没被系统提示占掉: 人发的消息照样报, 条数里不算系统提示
    assert '漏推了 2 条' in text and '「大群」2 条' in text and '一天内报过' not in text
    assert warnings == ['补收到 2 条 WS 没推送的消息: 「大群」2 条']


# ---- 存储与 feed ----

def test_catchup_marks_only_advance(tmp_path, monkeypatch, fake_client):
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    obj.storage.catchup_advance('g1', 30)
    obj.storage.catchup_advance('g1', 20)
    assert mark(obj.storage, 'g1') == 30
    # 只在 extra 里、本地 7 天内没有动静的会话: 有核对位置才核对
    assert obj.storage.catchup_marks(datetime.now() - timedelta(days=7), extra=['g1', 'none']) == {'g1': 30}


def test_feed_chats_pages_until_since():
    pages = {0: {'cards': [{'id': 'c1', 'type': 1, 'updateTime': 1000}, {'id': 't1', 'type': 4, 'updateTime': 990},
                           {'id': '42', 'type': 5, 'updateTime': 980}], 'nextCursor': 980},
             980: {'cards': [{'id': 'c2', 'type': 1, 'updateTime': 900}, {'id': 'c3', 'type': 1, 'updateTime': 700}],
                   'nextCursor': 700}}
    calls = []

    def api(cmd, data=None):
        assert cmd == 'feed.PullFeedCardsRequest'
        calls.append(data)
        if data.get('parentCardId') == 42:
            return {'cards': [{'id': 'b1', 'type': 1, 'updateTime': 975}, {'id': 'b0', 'type': 1, 'updateTime': 500}]}
        return pages[data.get('cursor', 0)]
    c = LarkClient.__new__(LarkClient)
    c.api = api
    assert c.feed_chats(800) == ['c1', 'b1', 'c2']
    assert [x.get('pullType') for x in calls] == ['REFRESH', 'REFRESH', 'LOAD_MORE']  # 翻到比 since 早就停


# ---- 断线重连后马上补收, 断线期间的消息不算漏推 ----

def test_gap_messages_not_counted_as_ws_miss(tmp_path, monkeypatch, fake_client):
    """补收到的消息发送时间落在断线窗口里(前后各放宽 60 秒): 照样入库、推进核对位置, 但不算漏推, 只打一条 INFO;
    窗口外的照旧算漏推。"""
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    now = int(time.time())
    obj._ws_gaps = [(now - 300, now - 290)]
    obj.storage.save_message(srv.message('g1', 10))
    obj.storage.save_message(srv.message('g2', 10))
    srv.latest.update(g1=13, g2=11)
    sent_at = {('g1', 11): now - 295, ('g1', 12): now - 355, ('g1', 13): now - 365, ('g2', 11): now - 3 * 3600}
    plain = srv.message
    srv.message = lambda cid, p, **kw: plain(cid, p, **kw) | {'create_time': sent_at[(cid, p)]}
    lines = []
    sink = logger.add(lambda m: lines.append((m.record['level'].name, m.record['message'])), level='INFO')
    try:
        got, left = asyncio.run(obj.catch_up())
    finally:
        logger.remove(sink)
    assert got == {'g1': 1, 'g2': 1} and left == 0            # g1 的 13 比断开早 65 秒, g2 的是 3 小时前的
    assert stored(obj.storage, 'g1') == [10, 11, 12, 13] and mark(obj.storage, 'g1') == 13
    hms = lambda t: datetime.fromtimestamp(t).strftime('%H:%M:%S')
    [info] = [msg for lvl, msg in lines if '断线期间' in msg]
    assert info == f'断线期间补收 2 条(长连接 {hms(now - 300)} 断开 {hms(now - 290)} 连上), 已入库, 不算长连接漏推: 「g1」2 条'
    assert not [msg for lvl, msg in lines if lvl == 'WARNING']


def test_disconnect_window_starts_at_first_drop(tmp_path, monkeypatch, fake_client):
    """断线窗口从第一次断开算到重新连上(中间重连失败不改起点); 第一次连上(daemon 启动)不算; 一天前的窗口丢掉。"""
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    now = [1000.0]
    monkeypatch.setattr(d, 'time', type('T', (), {'time': staticmethod(lambda: now[0])}))
    obj._on_connect()                                         # 启动时第一次连上
    assert obj._ws_gaps == [] and not obj._catchup_wake.is_set()
    obj._ws_gaps = [(1.0, 2.0)]
    now[0] = 90000.0
    obj._on_disconnect(RuntimeError('3007'))
    now[0] = 90010.0
    obj._on_disconnect(RuntimeError('连不上'))                  # 重连失败
    now[0] = 90020.0
    obj._on_connect()
    assert obj._ws_gaps == [(90000.0, 90020.0)] and obj._ws_down_at is None and obj._catchup_wake.is_set()
    assert obj._ws_gap(90000 - 60) and obj._ws_gap(90080) and not obj._ws_gap(90081) and not obj._ws_gap(None)


def test_reconnect_wakes_catch_up_loop(tmp_path, monkeypatch, fake_client):
    """重新连上后补收循环马上醒来补一轮(不等 15 分钟), 补收只在这个循环里一轮接一轮地跑;
    断线期间的消息不告警、不打 WARNING, 同一轮里窗口外的照旧告警。"""
    monkeypatch.setattr(d, 'CATCHUP_DELAY', 0)
    monkeypatch.setattr(d, 'RECONNECT_CATCHUP_DELAY', 0)
    obj, srv = setup(tmp_path, monkeypatch, fake_client)
    obj.cfg['catchup_interval'] = 3600
    obj.storage.update_chat_name('g1', '严肃 Agent 交流群', 2)
    for cid in ('g1', 'g2'):
        obj.storage.save_message(srv.message(cid, 10))
        srv.latest[cid] = 10
    plain = srv.message
    srv.message = lambda cid, p, **kw: plain(cid, p, **kw) | ({'create_time': int(time.time())} if cid == 'g1' else {})

    def reconnect():
        obj._on_disconnect(RuntimeError('received 3007 (registered) close by server api'))
        obj._on_connect()

    rounds = [reconnect,                                      # 第一轮(启动)跑着的时候断线又连上
              lambda: srv.latest.update(g1=11, g2=11)]        # g1 的是断线期间发的, g2 的是 3 小时前的
    real, warnings, running = obj.catch_up, [], []

    async def next_round():
        if not rounds:
            raise asyncio.CancelledError
        running.append(1)
        assert len(running) == 1                              # 不会两轮同时跑
        try:
            rounds.pop(0)()
            return await real()
        finally:
            running.pop()
            if not rounds:
                obj._catchup_wake.set()                       # 让循环再醒一次, 结束测试
    obj.catch_up = next_round
    sink = logger.add(lambda m: warnings.append(m.record['message']), level='WARNING')
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(asyncio.wait_for(obj._catch_up_loop(), 5))
    finally:
        logger.remove(sink)
    assert stored(obj.storage, 'g1') == [10, 11] and stored(obj.storage, 'g2') == [10, 11]
    assert warnings == ['补收到 1 条 WS 没推送的消息: 「g2」1 条']
    [(_, text)] = fake_client.sent
    assert '漏推了 1 条' in text and '「g2」1 条' in text and '严肃 Agent' not in text
