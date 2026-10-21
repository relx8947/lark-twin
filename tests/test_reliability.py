import argparse
import asyncio
import json
import sys
import time

from larkx import cli, notify, state
from larkx.storage import Storage


def turn(kind='message', chat='c1', owner=False):
    return {'kind': kind, 'chat_id': chat, 'anchor': '', 'is_owner': owner, 'ref': 'm1'}


def test_should_mirror():
    assert notify.should_mirror('g1', turn(chat='g1'), 'self')                 # 别人在群里叫它
    assert not notify.should_mirror('g1', turn(chat='g1', owner=True), 'self')  # 本人亲自叫的, 本人看得到
    assert notify.should_mirror('g2', turn(chat='self', owner=True), 'self')    # 本人让它发到别处
    assert not notify.should_mirror('self', turn(kind='say', chat=''), 'self')


def test_alert_command_and_cooldown(tmp_path, fake_client):
    out = tmp_path / 'alert.txt'
    tpl = f'{sys.executable} -c "import sys; open({str(out)!r}, \'a\').write(sys.argv[1])" {{text}}'
    assert notify.build_alert_command(tpl, 'x y')[-1] == 'x y'
    n = notify.Notifier({'alert_cmd': tpl}, client=None)
    assert n.fallback_alert('登录失效', key='auth', cooldown=3600)
    assert not n.fallback_alert('登录失效', key='auth', cooldown=3600)  # 冷却中不重复
    assert out.read_text(encoding='utf-8') == '⚠️ lark-twin：登录失效'
    n2 = notify.Notifier({}, client=fake_client)
    assert n2.alert('出错了', key='k')
    assert fake_client.sent == [('self', '⚠️ lark-twin：出错了')]


def _send_args(chat, text):
    return argparse.Namespace(text=text, json=True, chat_id=chat, root=None)


def _agent_cli(monkeypatch, tmp_path, fake_client, current_turn):
    st = Storage(f'sqlite:///{tmp_path}/t.db')
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'LarkClient', lambda *a, **k: fake_client)
    monkeypatch.setattr(cli, 'LarkAuth', lambda *a, **k: None)
    monkeypatch.setattr(state, 'read_turn', lambda: current_turn)
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setenv('LARKX_REPLY_SIGNATURE', '🤖 sig')
    return st


def test_agent_send_mirrors_to_owner(tmp_path, monkeypatch, fake_client):
    st = _agent_cli(monkeypatch, tmp_path, fake_client, turn(chat='g1'))
    st.update_chat_name('g1', '项目群')
    assert cli.cmd_send(_send_args('g1', ['收到'])) == 0
    assert fake_client.sent[0] == ('g1', '收到\n\n—— 🤖 sig')
    assert fake_client.sent[1][0] == 'self' and '项目群' in fake_client.sent[1][1]
    fake_client.sent.clear()
    monkeypatch.setattr(state, 'read_turn', lambda: turn(chat='g1', owner=True))
    assert cli.cmd_send(_send_args('g1', ['好的'])) == 0
    assert [c for c, _ in fake_client.sent] == ['g1']


def test_agent_send_rate_limit(tmp_path, monkeypatch, fake_client):
    st = _agent_cli(monkeypatch, tmp_path, fake_client, turn(chat='g1'))
    monkeypatch.setenv('LARKX_SEND_RATE_LIMIT', '2')
    monkeypatch.setenv('LARKX_MIRROR', 'false')
    assert cli.cmd_send(_send_args('g1', ['1'])) == 0
    assert cli.cmd_send(_send_args('g1', ['2'])) == 0
    assert cli.cmd_send(_send_args('g1', ['3'])) == 1
    assert [c for c, _ in fake_client.sent] == ['g1', 'g1', 'self']  # 第三条被拦, 给本人发告警
    assert '上限' in st.list_actions()[-1].error


def test_agent_recall_only_own_messages(tmp_path, monkeypatch, fake_client):
    st = _agent_cli(monkeypatch, tmp_path, fake_client, turn(chat='g1'))
    st.log_action(actor='agent', action='send', chat_id='g1', msg_id='m-own', content='x')
    st.log_action(actor='agent', action='send', chat_id='g2', msg_id='m-other-chat', content='x')
    args = lambda mid: argparse.Namespace(msg_id=mid, json=True)
    assert cli.cmd_recall(args('m-human')) == 1
    assert cli.cmd_recall(args('m-other-chat')) == 1
    assert cli.cmd_recall(args('m-own')) == 0
    assert fake_client.api_calls == [('messages.DeleteMessageRequest', {'id': 'm-own'})]
    monkeypatch.setattr(state, 'read_turn', lambda: turn(chat='self', owner=True))
    assert cli.cmd_recall(args('m-other-chat')) == 0


class FakeSession:
    def __init__(self):
        self.prompts, self.events = [], []

    async def ask(self, prompt):
        from larkx.agent import TurnResult
        self.prompts.append(prompt)
        return TurnResult('success', False, '', 0.1)

    async def context_pressure(self):
        return None


def _daemon(tmp_path, fake_client, monkeypatch):
    from larkx import daemon as d
    from larkx.config import DEFAULTS
    obj = d.Daemon.__new__(d.Daemon)
    obj.cfg = json.loads(json.dumps(DEFAULTS))
    obj.cfg['triggers']['prefix'] = '/ai'
    obj.storage = Storage(f'sqlite:///{tmp_path}/d.db')
    obj.session, obj.client = FakeSession(), fake_client
    obj.notifier = notify.Notifier(obj.cfg, fake_client)
    obj.auth = fake_client.auth
    obj.prefix, obj.home, obj._disconnects = '/ai', tmp_path, []
    obj.inbox = asyncio.Queue()
    monkeypatch.setattr(d, 'commit_agent_home', lambda *a: False)
    return obj


def test_inbox_survives_restart(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    now = int(time.time())
    msg = {'msg_id': 'x1', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '200', 'content': '/ai 在吗', 'create_time': now}

    async def receive_then_crash():
        await obj.on_message(msg, '项目群', '张三')
        await obj.on_message(msg, '项目群', '张三')  # 重复推送不会再排一次
        return obj.inbox.qsize()

    assert asyncio.run(receive_then_crash()) == 1
    old = dict(msg, msg_id='x0', create_time=now - 3 * 3600)
    obj.storage.inbox_add('x0', 'g1', {'msg': old, 'body': '很久以前', 'is_owner': False})
    # "重启": 新的内存队列, 从库里恢复
    obj.inbox = asyncio.Queue()
    obj._restore_inbox()
    assert obj.inbox.qsize() == 1
    ev = obj.inbox.get_nowait()
    assert ev.ref == 'x1' and ev.note
    asyncio.run(obj._handle(ev))
    assert '重启前' in obj.session.prompts[0] and '在吗' in obj.session.prompts[0]
    assert obj.storage.inbox_unfinished() == []


def test_catch_up_feeds_missed_messages(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.storage.save_message({'msg_id': 'a', 'chat_id': 'g1', 'position': 10, 'create_time': 1, 'content': '旧'})
    fed = []

    class FakeListener:
        async def on_message(self, m):
            fed.append(m['msg_id'])
            return True

    obj.listener = FakeListener()
    fake_client.api = lambda t, d=None: {'chats': {'g1': {'lastMessagePosition': 12}}}
    fake_client.pull_history = lambda cid, positions: [{'msg_id': f'p{p}', 'from_id': '200', 'chat_id': cid} for p in positions]
    assert asyncio.run(obj.catch_up()) == 2 and fed == ['p11', 'p12']


def test_reconnect_storm_alerts_once(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    for _ in range(5):
        obj._on_disconnect(RuntimeError('boom'))
    alerts = [t for c, t in fake_client.sent if '长连接' in t]
    assert len(alerts) == 1


def test_parse_takeover():
    from larkx.daemon.triggers import parse_takeover
    assert parse_takeover('2h 帮我约下周二开会', 3600) == (7200, '帮我约下周二开会')
    assert parse_takeover('帮我约时间', 3600) == (3600, '帮我约时间')
    assert parse_takeover('', 3600) == (3600, '')


def test_takeover_flow(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    me = fake_client.auth.user_id
    now = int(time.time())
    base = {'chat_id': 'p1', 'chat_type': 1, 'create_time': now}

    async def run():
        await obj.on_message(dict(base, msg_id='c1', from_id=me, content='/ai /takeover 2h 帮我约下周二开会'), '张三', '我')
        await obj.on_message(dict(base, msg_id='c2', from_id='200', content='周二下午可以吗'), '张三', '张三')
        await obj.on_message(dict(base, msg_id='c3', from_id=me, content='我自己插一句'), '张三', '我')
        n_during = obj.inbox.qsize()
        await obj.on_message(dict(base, msg_id='c4', from_id=me, content='/ai /release'), '张三', '我')
        return n_during

    assert asyncio.run(run()) == 1                       # 只有对方那条进了队列, 本人插话不触发
    ev = obj.inbox.get_nowait()
    assert ev.ref == 'c2' and '接管' in ev.note and '下周二' in ev.note and not ev.is_owner
    summary = obj.inbox.get_nowait()                     # 结束后让数字人在本人私聊发总结
    assert summary.kind == 'task' and summary.chat_id == 'self' and summary.is_owner and 'p1' in summary.prompt
    assert '本人结束' in summary.prompt
    assert any('已接管' in t for c, t in fake_client.sent if c == 'self')  # 确认只发到本人私聊
    assert all(c == 'self' for c, _ in fake_client.sent)
    assert obj.storage.takeover_get('p1', time.time()) is None


def test_takeover_in_owner_chat_gives_hint(tmp_path, monkeypatch, fake_client):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    msg = {'chat_id': 'self', 'chat_type': 1, 'create_time': int(time.time()), 'msg_id': 'h1',
           'from_id': fake_client.auth.user_id, 'content': '/ai /takeover 1h'}
    obj.cfg['allow_self'] = True
    asyncio.run(obj.on_message(msg, '', '我'))
    assert obj.storage.takeover_list() == [] and '要接管哪个会话' in fake_client.sent[-1][1]


def test_mentions_query(tmp_path):
    st = Storage(f'sqlite:///{tmp_path}/m.db')
    t = int(time.time())
    st.save_message({'msg_id': 'a', 'chat_id': 'p1', 'chat_type': 1, 'from_id': '200', 'content': '在吗', 'create_time': t - 100}, sender_name='张三')
    st.save_message({'msg_id': 'b', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '300', 'content': '@耿宇贺 看下工单', 'create_time': t - 90}, sender_name='李四')
    st.save_message({'msg_id': 'c', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '300', 'content': '无关的话', 'create_time': t - 80}, sender_name='李四')
    st.save_message({'msg_id': 'd', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '100', 'content': '好的', 'create_time': t - 50}, sender_name='耿宇贺', direction='out')
    rows = st.mentions('100', st.owner_name('100'), t - 3600)
    assert [(r['msg_id'], r['kind'], r['replied']) for r in rows] == [('a', 'p2p', False), ('b', 'at', True)]


def test_listener_leaves_messages_unread_by_default(tmp_path, fake_client):
    from larkx.config import DEFAULTS
    from larkx.daemon.listener import Listener
    marked = []
    fake_client.mark_read = lambda *a, **k: marked.append(a)
    fake_client.get_user_name = lambda uid, cid: '张三'
    fake_client.get_group_name = lambda cid: '项目群'
    assert DEFAULTS['mark_read'] is False
    cfg = {'mark_read': DEFAULTS['mark_read']}
    lst = Listener(fake_client, Storage(f'sqlite:///{tmp_path}/t.db'), cfg, '100')
    msg = {'msg_id': 'x1', 'chat_id': 'g1', 'chat_type': 2, 'from_id': '200', 'content': '@你 /ai 在吗', 'position': 9}
    assert asyncio.run(lst.on_message(msg)) and marked == []
    cfg['mark_read'] = True
    assert asyncio.run(lst.on_message({**msg, 'msg_id': 'x2', 'position': 10})) and len(marked) == 1
