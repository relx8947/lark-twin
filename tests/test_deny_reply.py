"""没权限的人想唤起数字人: daemon 马上引用回复一句"没有权限", 不经过 agent; 冷却、开关、{owner}、审计。"""
import argparse
import asyncio
import copy
import time

import pytest

from larkx import cli, state
from larkx import whitelist as wl
from larkx.config import DEFAULTS, load_config
from larkx.daemon.triggers import denied_attempt, should_handle
from larkx.protocol.client import LarkClient
from larkx.protocol.proto import decoders
from larkx.storage import Storage
from larkx.storage.models import Action
from test_reliability import _daemon
from test_triggers import rich

ME, LISTED, OTHER = '100', '300', '200'
DEFAULT_TEXT = '你没有权限唤起本人的 lark-twin'


def cfg(**triggers):
    c = copy.deepcopy(DEFAULTS)
    c['triggers'].update({'prefix': '/ai', **triggers})
    return c


def msg(content='/ai 在吗', from_id=OTHER, chat_type=1, **kw):
    return {'msg_id': 'm1', 'chat_id': 'c1', 'from_id': from_id, 'chat_type': chat_type, 'content': content, **kw}


def daemon(tmp_path, fake_client, monkeypatch, **triggers):
    obj = _daemon(tmp_path, fake_client, monkeypatch)
    obj.cfg['triggers'].update(triggers)
    return obj


def deny_actions(st):
    s = st.Session()
    try:
        return s.query(Action).filter(Action.action == 'deny_reply').order_by(Action.id).all()
    finally:
        s.close()


# ---- 什么时候回"没有权限" ----

@pytest.mark.parametrize('takeover', [False, True])
@pytest.mark.parametrize('where', ['p2p', 'group_at', 'group'])
@pytest.mark.parametrize('owner_only', [False, True])
@pytest.mark.parametrize('non_empty', [False, True])
@pytest.mark.parametrize('sender', ['owner', 'listed', 'other'])
def test_decision_matrix(tmp_path, monkeypatch, fake_client, sender, non_empty, owner_only, where, takeover):
    """本人 / 白名单里的人 / 别人 × 白名单空/非空 × owner_only 开/关 × 私聊、群里 @ 本人、群里不 @ × 接管中。"""
    obj = daemon(tmp_path, fake_client, monkeypatch, owner_only=owner_only)  # group_at_only 默认开
    me = fake_client.auth.user_id
    if non_empty:
        obj.storage.whitelist_add(LISTED, '白名单里的人')
    chat = 'p1' if where == 'p2p' else 'g1'
    if takeover:
        obj.storage.takeover_start(chat, int(time.time()) + 3600, '', 'owner')
    sender_id = {'owner': me, 'listed': LISTED, 'other': OTHER}[sender]
    m = {'msg_id': 'x1', 'chat_id': chat, 'chat_type': 1 if where == 'p2p' else 2, 'from_id': sender_id,
         'content': '/ai 在吗', 'create_time': int(time.time()), 'at_me': where == 'group_at'}
    asyncio.run(obj.on_message(m, '会话', '某人'))

    if sender == 'owner':
        queued, denied = True, False                     # 本人一直能唤起, 群里不用 @ 自己; 接管不影响本人
    elif takeover:
        queued, denied = True, False                     # 接管中对方的每句话都交给数字人
    else:
        allowed = sender == 'listed' if non_empty else not owner_only
        queued = allowed and where != 'group'            # 群里别人要 @ 本人
        denied = not allowed and where != 'group'        # 没 @ 本人的群消息不算想唤起, 不回
    assert (obj.inbox.qsize() == 1) is queued
    assert [r['text'] for r in fake_client.replies] == ([DEFAULT_TEXT] if denied else [])
    if denied:
        assert obj.session.prompts == [] and len(deny_actions(obj.storage)) == 1


def test_denied_attempt_rules():
    on = cfg(owner_only=True)
    assert denied_attempt(msg(), on, ME)
    assert denied_attempt(msg(chat_type=2, at_me=True), on, ME)
    assert denied_attempt(msg(chat_type=3, at_me=True), on, ME)                   # 话题群
    assert not denied_attempt(msg(from_id=ME), on, ME)                            # 本人
    assert not denied_attempt(msg(from_type=2), on, ME)                           # 机器人(应用)发的
    assert denied_attempt(msg(from_type=1), on, ME)
    assert not denied_attempt(msg(), cfg(), ME)                                   # 白名单空、owner_only 关: 谁都能唤起
    assert not denied_attempt(msg(from_id=LISTED), cfg(), ME, whitelist={LISTED})
    assert denied_attempt(msg(), cfg(), ME, whitelist={LISTED})                   # 白名单非空, 不在名单里
    assert not denied_attempt(msg('在吗'), on, ME)                                # 普通聊天
    assert not denied_attempt(msg('说一下 /ai 的事'), on, ME)
    assert not denied_attempt(msg(), cfg(prefix='', owner_only=True), ME)        # 没有前缀就无所谓"想唤起"
    assert not denied_attempt(msg(chat_type=0), on, ME)                           # 不认识的会话类型
    # 群里没 @ 本人的 /ai 多半是在叫别的机器人: group_at_only 关着也不回
    assert not denied_attempt(msg(chat_type=2), on, ME)
    assert not denied_attempt(msg(chat_type=2), cfg(owner_only=True, group_at_only=False), ME)
    # @ 的判断和触发时一样: 补收的消息从富文本里认出 @ 本人; 开头 @ 了别人是在叫别人的数字人
    content, data = rich(('text', '/ai 问问 '), ('at', ME, '@耿宇贺'))
    assert denied_attempt(msg(content, chat_type=2, content_data=data), on, ME)
    content, data = rich(('at', ME, '@耿宇贺'), ('text', ' /ai 在吗'))
    assert denied_attempt(msg(content, chat_type=2, content_data=data), on, ME)
    content, data = rich(('at', '999', '@江勇涛'), ('text', ' /ai 你好'))
    assert not denied_attempt(msg(content, chat_type=2, content_data=data, at_me=True), on, ME)
    # 换成有权限的人也不会触发的(会话过滤、关键词)不回
    assert not denied_attempt(msg(), cfg(owner_only=True, exclude_chats=['c1']), ME)
    assert not denied_attempt(msg(), cfg(owner_only=True, chat_types=['group']), ME)
    assert not denied_attempt(msg(), cfg(owner_only=True, keywords=['帮忙']), ME)
    # 和 should_handle 互斥: 会触发的一定不算没权限
    for m in (msg(), msg(chat_type=2, at_me=True), msg(from_id=LISTED)):
        for c, w in ((on, ()), (cfg(), ()), (cfg(), {LISTED})):
            assert not (should_handle(m, c, ME, whitelist=w) and denied_attempt(m, c, ME, whitelist=w))


# ---- 回复本身 ----

def test_quote_reply_without_signature_or_agent(tmp_path, monkeypatch, fake_client):
    obj = daemon(tmp_path, fake_client, monkeypatch, owner_only=True)
    obj.cfg['reply_signature'] = '🤖 lark-twin'
    now = int(time.time())
    p2p = {'msg_id': 'p-1', 'chat_id': 'p1', 'chat_type': 1, 'from_id': OTHER, 'content': '/ai 在吗', 'create_time': now}
    topic = {'msg_id': 't-2', 'chat_id': 't1', 'chat_type': 3, 'from_id': '201', 'content': '@耿宇贺 /ai 在吗',
             'anchor': 't-root', 'at_me': True, 'create_time': now}

    async def run():
        await obj.on_message(p2p, '张三', '张三')
        await obj.on_message(topic, '话题群', '李四')
    asyncio.run(run())
    # 私聊、普通群: 引用回复他那条(在话题里时由 send_message 回进话题); 话题群: rootId 是话题的根, 引用他那条
    assert fake_client.replies == [
        {'chat_id': 'p1', 'text': DEFAULT_TEXT, 'root_id': 'p-1', 'parent_id': 'p-1'},
        {'chat_id': 't1', 'text': DEFAULT_TEXT, 'root_id': 't-root', 'parent_id': 't-2'}]
    assert obj.inbox.qsize() == 0 and obj.session.prompts == []
    assert not any(c[0] == 'messages.PutReadMessagesRequest' for c in fake_client.api_calls)


def test_cooldown_per_chat_and_sender(tmp_path, monkeypatch, fake_client):
    obj = daemon(tmp_path, fake_client, monkeypatch, owner_only=True)
    now = int(time.time())
    seq = iter(range(100))

    def m(chat='p1', sender=OTHER, **kw):
        return {'msg_id': f'm{next(seq)}', 'chat_id': chat, 'chat_type': 1, 'from_id': sender, 'content': '/ai 在吗',
                'create_time': now, **kw}

    async def run(*msgs):
        for x in msgs:
            await obj.on_message(x, '', '')
    asyncio.run(run(m(), m(), m()))
    assert len(fake_client.replies) == 1                               # 同一会话同一人 10 分钟内只回一次
    asyncio.run(run(m(sender='201'), m(chat='g1', chat_type=2, at_me=True)))
    assert len(fake_client.replies) == 3                               # 换个人、换个会话各回一次
    # 记在状态目录里, daemon 重启(新的 Daemon 对象)后照样算数
    obj2 = daemon(tmp_path, fake_client, monkeypatch, owner_only=True)
    asyncio.run(obj2.on_message(m(), '', ''))
    assert len(fake_client.replies) == 3
    # 过了冷却时间再回
    real = time.time
    monkeypatch.setattr(state.time, 'time', lambda: real() + 601)
    asyncio.run(obj2.on_message(m(), '', ''))
    assert len(fake_client.replies) == 4
    # 冷却设成 0: 每次都回
    obj.cfg['deny_reply_cooldown'] = 0
    asyncio.run(run(m(sender='202'), m(sender='202')))
    assert len(fake_client.replies) == 6
    assert len(deny_actions(obj.storage)) == 6


def test_deny_cooling_state_file(tmp_path):
    assert not state.deny_cooling('c1', 'u1', 600, now=1000)
    assert state.deny_cooling('c1', 'u1', 600, now=1500)
    assert not state.deny_cooling('c1', 'u2', 600, now=1500)
    assert not state.deny_cooling('c1', 'u1', 600, now=1600)            # 过了冷却
    import json
    saved = json.loads((state.STATE_DIR / 'deny_reply.json').read_text())
    assert saved == {'c1:u2': 1500, 'c1:u1': 1600}                     # 过期的记录写入时顺带清掉
    assert not state.deny_cooling('c1', 'u3', 600, now=2200)
    assert json.loads((state.STATE_DIR / 'deny_reply.json').read_text()) == {'c1:u3': 2200}
    (state.STATE_DIR / 'deny_reply.json').write_text('坏了')
    assert not state.deny_cooling('c1', 'u3', 600, now=2300)           # 文件坏了当成没回过


def test_empty_text_disables(tmp_path, monkeypatch, fake_client):
    obj = daemon(tmp_path, fake_client, monkeypatch, owner_only=True)
    for text in ('', '   '):
        obj.cfg['deny_reply'] = text
        asyncio.run(obj.on_message({'msg_id': f'e{len(text)}', 'chat_id': 'p1', 'chat_type': 1, 'from_id': OTHER,
                                    'content': '/ai 在吗', 'create_time': int(time.time())}, '', ''))
    assert fake_client.replies == [] and deny_actions(obj.storage) == [] and obj.inbox.qsize() == 0


def test_config_keys(monkeypatch):
    c = load_config()
    assert c['deny_reply'] == '你没有权限唤起{owner}的 lark-twin' and c['deny_reply_cooldown'] == 600
    monkeypatch.setenv('LARKX_DENY_REPLY', '你没权限唤起gengyuhe的lark twin')
    monkeypatch.setenv('LARKX_DENY_REPLY_COOLDOWN', '300')
    c = load_config()
    assert c['deny_reply'] == '你没权限唤起gengyuhe的lark twin' and c['deny_reply_cooldown'] == 300
    monkeypatch.setenv('LARKX_DENY_REPLY', '')                         # 写成空的是关掉, 不是退回默认值
    assert load_config()['deny_reply'] == '' and wl.deny_text(load_config()) == ''


def test_owner_placeholder(tmp_path, monkeypatch, fake_client):
    assert wl.deny_text({'deny_reply': '你没有权限唤起{owner}的 lark-twin'}, '耿宇贺') == '你没有权限唤起耿宇贺的 lark-twin'
    assert wl.deny_text({'deny_reply': '你没有权限唤起{owner}的 lark-twin'}) == DEFAULT_TEXT
    assert wl.deny_text({'deny_reply': '固定的 {话}'}, '耿宇贺') == '固定的 {话}'
    obj = daemon(tmp_path, fake_client, monkeypatch, owner_only=True)
    fake_client.user_names = {fake_client.auth.user_id: '耿宇贺'}
    asyncio.run(obj.on_message({'msg_id': 'o1', 'chat_id': 'p1', 'chat_type': 1, 'from_id': OTHER,
                                'content': '/ai 在吗', 'create_time': int(time.time())}, '', ''))
    assert fake_client.replies[-1]['text'] == '你没有权限唤起耿宇贺的 lark-twin'
    # 查不到名字时用本地消息记录里本人的名字
    obj2 = daemon(tmp_path, fake_client, monkeypatch, owner_only=True)
    fake_client.user_names = {}
    obj2.storage.save_message({'msg_id': 's1', 'chat_id': 'p9', 'from_id': fake_client.auth.user_id, 'content': 'hi'},
                              sender_name='耿宇贺二号', direction='out')
    asyncio.run(obj2.on_message({'msg_id': 'o2', 'chat_id': 'p2', 'chat_type': 1, 'from_id': OTHER,
                                 'content': '/ai 在吗', 'create_time': int(time.time())}, '', ''))
    assert fake_client.replies[-1]['text'] == '你没有权限唤起耿宇贺二号的 lark-twin'


def test_skips_unsafe_or_stale(tmp_path, monkeypatch, fake_client):
    obj = daemon(tmp_path, fake_client, monkeypatch, owner_only=True)
    now = int(time.time())
    base = {'chat_id': 'p1', 'chat_type': 1, 'from_id': OTHER, 'content': '/ai 在吗', 'create_time': now}
    obj.cfg['deny_reply'] = '/ai 没权限'                                # 以前缀开头会触发本人自己的数字人, 不发
    asyncio.run(obj.on_message(dict(base, msg_id='u1'), '', ''))
    obj.cfg['deny_reply'] = DEFAULTS['deny_reply']
    asyncio.run(obj.on_message(dict(base, msg_id='u2', create_time=now - 3 * 3600), '', ''))  # 太久以前的不回
    asyncio.run(obj.on_message(dict(base, msg_id='u3', from_type=2), '', ''))                 # 消息标着是机器人发的
    fake_client.bots = {'201'}
    asyncio.run(obj.on_message(dict(base, msg_id='u5', from_id='201'), '', ''))               # 查出来是机器人
    assert fake_client.replies == [] and deny_actions(obj.storage) == []
    asyncio.run(obj.on_message(dict(base, msg_id='u4'), '', ''))
    assert len(fake_client.replies) == 1


def test_audit_record_and_cli(tmp_path, monkeypatch, fake_client, capsys):
    obj = daemon(tmp_path, fake_client, monkeypatch, owner_only=True)
    now = int(time.time())
    asyncio.run(obj.on_message({'msg_id': 'a1', 'chat_id': 'p1', 'chat_type': 1, 'from_id': OTHER,
                                'content': '/ai 在吗', 'create_time': now}, '张三', '张三'))
    monkeypatch.setattr(fake_client, 'send_msg', lambda *a, **k: None)  # 发送失败也记一笔
    asyncio.run(obj.on_message({'msg_id': 'a2', 'chat_id': 'p2', 'chat_type': 1, 'from_id': '201',
                                'content': '/ai 在吗', 'create_time': now}, '李四', '李四'))
    rows = deny_actions(obj.storage)
    assert [(r.actor, r.chat_id, r.msg_id, r.content, r.ok, r.error, r.turn_kind, r.turn_ref) for r in rows] == [
        ('daemon', 'p1', 'm1', DEFAULT_TEXT, 1, '', 'message', 'a1'),
        ('daemon', 'p2', '', DEFAULT_TEXT, 0, '发送失败', 'message', 'a2')]
    # 数字人发消息的频率限制只数它自己发的
    from datetime import datetime, timedelta
    assert obj.storage.count_actions('agent', 'send', datetime.now() - timedelta(minutes=10)) == 0
    monkeypatch.setattr(cli, 'Storage', lambda: obj.storage)
    assert cli.cmd_audit(argparse.Namespace(limit=10, json=False)) == 0
    out = capsys.readouterr().out
    assert 'daemon (message a1) deny_reply chat=p1: ' + DEFAULT_TEXT in out and '发送失败' in out


def test_deny_errors_never_break_on_message(tmp_path, monkeypatch, fake_client):
    obj = daemon(tmp_path, fake_client, monkeypatch, owner_only=True)

    def boom(*a, **k):
        raise RuntimeError('网络不通')
    monkeypatch.setattr(fake_client, 'send_message', boom)
    asyncio.run(obj.on_message({'msg_id': 'b1', 'chat_id': 'p1', 'chat_type': 1, 'from_id': OTHER,
                                'content': '/ai 在吗', 'create_time': int(time.time())}, '', ''))
    assert obj.inbox.qsize() == 0


# ---- 机器人识别 ----

def test_from_type_decoded():
    assert [decoders.from_type(v) for v in (2, 'BOT', 'USER', '2', None, 'x')] == [2, 2, 1, 2, 0, 0]
    m = LarkClient._message_dict({'id': '1', 'type': 4, 'fromId': '9', 'fromType': 2, 'chatType': 2}, 'g1', False, 1)
    assert m['from_type'] == 2


def test_is_bot_queries_chatter_type_once(monkeypatch):
    assert [decoders.chatter_is_bot(v) for v in (1, 2, 3, 'BOT', 'AI', 'USER', None, 'x')] == [
        False, True, True, True, True, False, False, False]
    calls = []
    replies = {'b1': {'chatters': {'b1': {'id': 'b1', 'type': 2}}}, 'u1': {'chatters': {'u1': {'id': 'u1', 'type': 1}}},
               'gone': {'chatters': {}}}

    def api(cmd, data=None):
        calls.append((cmd, data['chatterIds'][0]))
        if data['chatterIds'][0] == 'err':
            raise RuntimeError('网络不通')
        return replies[data['chatterIds'][0]]
    c = LarkClient.__new__(LarkClient)
    c._bots, c.api = {}, api
    assert c.is_bot('b1') and c.is_bot('b1') and not c.is_bot('u1') and not c.is_bot('gone') and not c.is_bot('err')
    assert calls == [('chatters.PullChattersByIdsRequest', x) for x in ('b1', 'u1', 'gone', 'err')]  # 查过的记住


# ---- 说明文字(控制台、命令行) ----

def test_deny_sentence():
    c = cfg(owner_only=True)
    assert wl.deny_sentence(c, 0) == f'没权限的人发 /ai 会收到：{DEFAULT_TEXT}（10 分钟内同一人只回一次）'
    assert wl.deny_sentence(cfg(), 0) == ''                            # 谁都能唤起, 没人会被拒
    assert wl.deny_sentence(cfg(), 2, '耿宇贺').startswith('没权限的人发 /ai 会收到：你没有权限唤起耿宇贺的')
    assert wl.deny_sentence(dict(c, deny_reply=''), 0) == '没权限的人发 /ai 不会收到任何回复（LARKX_DENY_REPLY 为空）'
    assert wl.deny_sentence(dict(c, deny_reply_cooldown=90), 0).endswith('（90 秒内同一人只回一次）')
    assert wl.deny_sentence(dict(c, deny_reply_cooldown=0), 0).endswith('（每次都回）')


def test_web_shows_deny_line_and_activity(tmp_path, monkeypatch, fake_client):
    from starlette.testclient import TestClient
    from larkx.protocol.region import get_region
    from larkx.web import app as web
    monkeypatch.setattr(web, 'TOKEN_PATH', tmp_path / 'web_token')
    fake_client.auth.region = get_region('feishu')
    monkeypatch.setattr(web, '_auth', lambda: fake_client.auth)
    monkeypatch.setattr(web, '_lark', lambda: fake_client)
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    monkeypatch.setenv('LARKX_TRIGGER_OWNER_ONLY', 'true')
    c = TestClient(web.create_app())
    c.post('/login', json={'token': web.get_token()})
    assert c.get('/api/whitelist').json()['deny'] == f'没权限的人发 /ai 会收到：{DEFAULT_TEXT}（10 分钟内同一人只回一次）'
    monkeypatch.setenv('LARKX_DENY_REPLY', '你没权限唤起gengyuhe的lark twin')
    assert c.get('/api/whitelist').json()['deny'].startswith('没权限的人发 /ai 会收到：你没权限唤起gengyuhe的lark twin（')
    monkeypatch.setenv('LARKX_TRIGGER_OWNER_ONLY', 'false')
    assert c.get('/api/whitelist').json()['deny'] == ''
    # 动态页: 自动回复的那条能看到, 也能撤回
    st = Storage()
    st.log_action(actor='daemon', action='deny_reply', chat_id='p1', msg_id='900', content=DEFAULT_TEXT,
                  turn_kind='message', turn_ref='a1')
    st.log_action(actor='human', action='recall', chat_id='p1', msg_id='900', content='网页撤回 900')
    acts = {a['action']: a for a in c.get('/api/activity').json()}
    assert acts['deny_reply']['actor'] == 'daemon' and acts['deny_reply']['recalled'] is True
    import larkx.protocol as protocol
    monkeypatch.setattr(protocol, 'LarkAuth', lambda *a, **k: None)
    monkeypatch.setattr(protocol, 'LarkClient', lambda *a, **k: fake_client)
    r = c.post('/api/recall', json={'msg_id': '900'}, headers={'X-Larkx': '1'})
    assert r.json()['ok'] and fake_client.api_calls[-1] == ('messages.DeleteMessageRequest', {'id': '900'})
