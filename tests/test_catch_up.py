"""补收不丢消息: 核对位置只由补收推进、核对全部近期会话、分批拉全、feed 发现新会话; 补收到的旧消息不触发任何动作;
漏推告警按会话冷却。"""
import asyncio
import time
from datetime import datetime, timedelta

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
