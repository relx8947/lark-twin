import copy

from larkx.config import DEFAULTS
from larkx.daemon.triggers import build_prompt, parse_command, should_handle, strip_prefix

ME = '100'


def cfg(**triggers):
    c = copy.deepcopy(DEFAULTS)
    c['triggers'].update(triggers)
    return c


def msg(content, from_id='200', chat_type=1, **kw):
    return {'msg_id': 'm1', 'chat_id': 'c1', 'from_id': from_id, 'chat_type': chat_type, 'content': content, **kw}


def test_prefix_required():
    c = cfg(prefix='/ai')
    assert should_handle(msg('/ai 你好'), c, ME)
    assert not should_handle(msg('你好'), c, ME)


def test_own_message_needs_allow_self():
    c = cfg(prefix='/ai')
    assert not should_handle(msg('/ai 你好', from_id=ME), c, ME)
    c['allow_self'] = True
    assert should_handle(msg('/ai 你好', from_id=ME), c, ME)


def test_group_at_only_and_chat_filters():
    c = cfg(prefix='/ai', group_at_only=True, exclude_chats=['c2'])
    assert not should_handle(msg('/ai x', chat_type=2), c, ME)
    assert should_handle(msg('/ai x', chat_type=2, at_me=True), c, ME)
    assert should_handle(msg('/ai x', chat_type=1), c, ME)
    assert not should_handle(dict(msg('/ai x'), chat_id='c2'), c, ME)


def test_parse_command():
    assert parse_command(strip_prefix('/ai /clear', '/ai')) == ('clear', '')
    assert parse_command(strip_prefix('/ai /stop 然后做别的', '/ai')) == ('stop', '然后做别的')
    assert parse_command('/clearance') == (None, '/clearance')
    assert parse_command('普通消息') == (None, '普通消息')


def test_build_prompt_escapes_and_reply_hint():
    p = build_prompt(msg('x', anchor='a9'), '<b>&', chat_name='群"1', sender_name='张三', is_owner=True)
    assert '<content>&lt;b&gt;&amp;</content>' in p
    assert 'owner="true"' in p
    assert "larkx send c1 --root a9 - <<'EOF'" in p
    p2 = build_prompt(msg('x'), 'hi', note='重试')
    assert '--root' not in p2 and '(重试)' in p2 and 'owner="false"' in p2
