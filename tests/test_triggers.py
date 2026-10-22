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


def test_own_message_needs_allow_self_and_prefix():
    c = cfg(prefix='/ai')
    assert should_handle(msg('/ai 你好', from_id=ME), c, ME)          # 默认允许本人下指令
    c['allow_self'] = False
    assert not should_handle(msg('/ai 你好', from_id=ME), c, ME)
    c = cfg(prefix='')
    assert not should_handle(msg('你好', from_id=ME), c, ME)           # 没有前缀时本人的消息一律不触发
    assert should_handle(msg('你好', from_id='200'), c, ME)


def test_group_at_only_and_chat_filters():
    c = cfg(prefix='/ai', group_at_only=True, exclude_chats=['c2'])
    assert not should_handle(msg('/ai x', chat_type=2), c, ME)
    assert should_handle(msg('/ai x', chat_type=2, at_me=True), c, ME)
    assert should_handle(msg('/ai x', chat_type=1), c, ME)
    assert not should_handle(dict(msg('/ai x'), chat_id='c2'), c, ME)


def test_group_at_only_defaults_on():
    assert DEFAULTS['triggers']['group_at_only'] is True
    c = cfg(prefix='/ai')
    # 别人在群/话题群里不 @ 本人: 不触发(多半是在叫别的机器人)
    assert not should_handle(msg('/ai 整理一下群发言', chat_type=2), c, ME)
    assert not should_handle(msg('/ai 整理一下群发言', chat_type=3), c, ME)
    assert not should_handle(msg('/ai x', chat_type=2, at_me=False), c, ME)
    # 别人在群里 @ 本人: 触发
    assert should_handle(msg('/ai x', chat_type=2, at_me=True), c, ME)
    assert should_handle(msg('@耿宇贺 /ai x', chat_type=3, at_me=True), c, ME)
    # @ 了本人但没有触发前缀: 仍不触发
    assert not should_handle(msg('@耿宇贺 你好', chat_type=2, at_me=True), c, ME)
    # 本人自己在群里发 /ai: 不用 @ 自己也触发
    assert should_handle(msg('/ai x', from_id=ME, chat_type=2), c, ME)
    assert should_handle(msg('/ai x', from_id=ME, chat_type=3), c, ME)
    # 私聊照旧, 不需要 @
    assert should_handle(msg('/ai x', chat_type=1), c, ME)
    assert not should_handle(msg('你好', chat_type=1), c, ME)
    # 关掉后回到群里任何人发 /ai 都触发
    assert should_handle(msg('/ai x', chat_type=2), cfg(prefix='/ai', group_at_only=False), ME)


def test_group_at_only_reads_rich_text_mentions():
    """补收的历史消息 at_me 恒为 False, 要从富文本里认出 @ 本人。"""
    c = cfg(prefix='/ai')
    content, data = rich(('text', '/ai 问问 '), ('at', ME, '@耿宇贺'))
    assert should_handle(msg(content, chat_type=2, content_data=data, at_me=False), c, ME)
    content, data = rich(('text', '/ai 问问 '), ('at', '999', '@张三'))
    assert not should_handle(msg(content, chat_type=2, content_data=data, at_me=False), c, ME)


def test_group_at_only_env(monkeypatch):
    from larkx.config import load_config
    monkeypatch.delenv('LARKX_TRIGGER_GROUP_AT_ONLY', raising=False)
    assert load_config()['triggers']['group_at_only'] is True
    monkeypatch.setenv('LARKX_TRIGGER_GROUP_AT_ONLY', 'false')
    assert load_config()['triggers']['group_at_only'] is False
    monkeypatch.setenv('LARKX_TRIGGER_GROUP_AT_ONLY', 'true')
    assert load_config()['triggers']['group_at_only'] is True


def test_owner_only_env(monkeypatch):
    from larkx.config import load_config
    assert DEFAULTS['triggers']['owner_only'] is False
    monkeypatch.delenv('LARKX_TRIGGER_OWNER_ONLY', raising=False)
    assert load_config()['triggers']['owner_only'] is False
    monkeypatch.setenv('LARKX_TRIGGER_OWNER_ONLY', 'true')
    assert load_config()['triggers']['owner_only'] is True
    monkeypatch.setenv('LARKX_TRIGGER_OWNER_ONLY', 'false')
    assert load_config()['triggers']['owner_only'] is False


def test_owner_only_on():
    c = cfg(prefix='/ai', owner_only=True)
    # 别人发的: 私聊、群聊、话题群, 包括 @ 本人的, 一律不触发
    assert not should_handle(msg('/ai 你好', chat_type=1), c, ME)
    assert not should_handle(msg('/ai 你好', chat_type=2), c, ME)
    assert not should_handle(msg('/ai 你好', chat_type=2, at_me=True), c, ME)
    assert not should_handle(msg('@耿宇贺 /ai 你好', chat_type=3, at_me=True), c, ME)
    content, data = rich(('at', ME, '@耿宇贺'), ('text', ' /ai 你好'))
    assert not should_handle(msg(content, chat_type=2, content_data=data, at_me=True), c, ME)
    # 本人发的 /ai: 私聊、群聊照常触发(群里不用 @ 自己)
    assert should_handle(msg('/ai 你好', from_id=ME, chat_type=1), c, ME)
    assert should_handle(msg('/ai 你好', from_id=ME, chat_type=2), c, ME)
    assert should_handle(msg('/ai /stop', from_id=ME, chat_type=3), c, ME)
    # 本人发的没有前缀、或开头 @ 了别人: 仍不触发
    assert not should_handle(msg('你好', from_id=ME, chat_type=1), c, ME)
    content, data = rich(('at', '999', '@江勇涛'), ('text', ' /ai 你好'))
    assert not should_handle(msg(content, from_id=ME, chat_type=2, content_data=data), c, ME)


def test_owner_only_off_matches_group_rules():
    """开关关闭(默认)时和群聊规则一致: 私聊照旧, 群里别人要 @ 本人, 本人不用 @。"""
    c = cfg(prefix='/ai')
    assert c['triggers']['owner_only'] is False
    assert should_handle(msg('/ai 你好', chat_type=1), c, ME)
    assert not should_handle(msg('/ai 你好', chat_type=2), c, ME)
    assert should_handle(msg('/ai 你好', chat_type=2, at_me=True), c, ME)
    assert should_handle(msg('/ai 你好', from_id=ME, chat_type=2), c, ME)
    content, data = rich(('at', '999', '@江勇涛'), ('text', ' /ai 你好'))
    assert not should_handle(msg(content, chat_type=2, content_data=data, at_me=True), c, ME)


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


def rich(*parts):
    """构造带富文本的消息内容: parts 是 ('at', user_id, 显示名) 或 ('text', 文本)。"""
    from larkx.protocol.proto import proto_pb2 as P
    dictionary, content = {}, ''
    for i, part in enumerate(parts):
        if part[0] == 'at':
            prop = P.AtProperty(userId=part[1], content=part[2]).SerializeToString()
            dictionary[str(i)] = {'tag': 5, 'property': prop}
            content += part[2]
        else:
            dictionary[str(i)] = {'tag': 1, 'property': P.TextProperty(content=part[1]).SerializeToString()}
            content += part[1]
    data = {'richText': {'elementIds': list(dictionary), 'elements': {'dictionary': dictionary}}}
    return content, data


def test_leading_mentions_must_include_me():
    c = cfg(prefix='/ai')
    content, data = rich(('at', ME, '@耿宇贺'), ('text', ' /ai 你喜欢喝什么咖啡？'))
    assert should_handle(msg(content, chat_type=2, content_data=data), c, ME)
    assert strip_prefix(content, '/ai') == '你喜欢喝什么咖啡？'
    content, data = rich(('at', '999', '@江勇涛'), ('text', ' /ai 你喜欢耿宇贺吗'))
    assert not should_handle(msg(content, chat_type=2, content_data=data), c, ME)
    content, data = rich(('at', '999', '@张三'), ('at', ME, '@耿宇贺'), ('text', ' /ai 看一下'))
    assert should_handle(msg(content, chat_type=2, content_data=data), c, ME)
    content, data = rich(('at', 'all', '@所有人'), ('text', '/ai 通知'))
    assert not should_handle(msg(content, chat_type=2, content_data=data), c, ME)
    # 开头 @ 了别人, 即使后面也 @ 了本人(at_me 为真): 仍不触发
    content, data = rich(('at', '999', '@江勇涛'), ('text', ' /ai 问下 '), ('at', ME, '@耿宇贺'))
    assert not should_handle(msg(content, chat_type=2, content_data=data, at_me=True), c, ME)
    # 本人自己开头 @ 别人: 同样不触发(是在叫那个人的数字人)
    content, data = rich(('at', '999', '@江勇涛'), ('text', ' /ai 你好'))
    assert not should_handle(msg(content, from_id=ME, chat_type=2, content_data=data), c, ME)
    # 开头没有 @、@ 在指令后面: 群里还要看有没有 @ 本人(group_at_only 默认开)
    content, data = rich(('text', '/ai 问问 '), ('at', '999', '@张三'))
    assert not should_handle(msg(content, chat_type=2, content_data=data), c, ME)
    assert should_handle(msg(content, chat_type=2, content_data=data), cfg(prefix='/ai', group_at_only=False), ME)
    # 解析不出富文本时退回 at_me
    assert should_handle(msg('@耿宇贺 /ai x', chat_type=2, at_me=True), c, ME)
    assert not should_handle(msg('@江勇涛 /ai x', chat_type=2), c, ME)
    assert not should_handle(msg('@耿宇贺 帮我看下 /ai', chat_type=2, at_me=True), c, ME)
    assert parse_command(strip_prefix('@耿宇贺 /ai /stop', '/ai')) == ('stop', '')
