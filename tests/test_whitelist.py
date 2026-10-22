"""唤起白名单: 触发规则、存储、命令行、网页接口, 以及数字人自己改不了白名单。"""
import argparse
import copy
import os
import subprocess
import sys

import pytest
from starlette.testclient import TestClient

from larkx import cli, guest, state
from larkx import whitelist as wl
from larkx.agent.hooks import check_tool_use
from larkx.config import DEFAULTS, inside_twin
from larkx.daemon.triggers import should_handle
from larkx.protocol.region import get_region
from larkx.storage import Storage
from larkx.web import app as web

ME, LISTED, OTHER = '100', '300', '200'
ZW1, ZW2, LS = '7100000000000000001', '7100000000000000002', '7100000000000000003'
OWNER = '7100000000000000099'
PEOPLE = [
    {'type': 'user', 'id': ZW1, 'title': '<h>张伟</h>', 'summary': '抖音研发-音乐', 'avatar_key': 'v3_a1'},
    {'type': 'group', 'id': '7600000000000000001', 'title': '张伟的群', 'summary': '包含: <h>张伟</h>', 'avatar_key': 'v3_g'},
    {'type': 'user', 'id': ZW2, 'title': '<h>张伟</h>', 'summary': 'TikTok LIVE - Data &amp; Governance', 'avatar_key': ''},
    {'type': 'user', 'id': LS, 'title': '<h>李四</h>', 'summary': '飞书-IM', 'avatar_key': 'v3_a3'},
    {'type': 'user', 'id': '7100000000000000004', 'title': '<h>李四</h>光', 'summary': '飞书-文档', 'avatar_key': ''},
]


def cfg(**triggers):
    c = copy.deepcopy(DEFAULTS)
    c['triggers'].update(triggers)
    return c


def msg(content, from_id=OTHER, chat_type=1, **kw):
    return {'msg_id': 'm1', 'chat_id': 'c1', 'from_id': from_id, 'chat_type': chat_type, 'content': content, **kw}


# ---- 触发规则 ----

@pytest.mark.parametrize('where', ['p2p', 'group_at', 'group'])
@pytest.mark.parametrize('owner_only', [False, True])
@pytest.mark.parametrize('non_empty', [False, True])
@pytest.mark.parametrize('sender', ['owner', 'listed', 'other'])
def test_decision_matrix(sender, non_empty, owner_only, where):
    """本人 / 白名单里的人 / 别人 × 白名单空/非空 × owner_only 开/关 × 私聊、群里 @ 本人、群里不 @。"""
    c = cfg(prefix='/ai', owner_only=owner_only)  # group_at_only 默认开
    whitelist = {LISTED} if non_empty else set()
    sender_id = {'owner': ME, 'listed': LISTED, 'other': OTHER}[sender]
    m = msg('/ai 在吗', from_id=sender_id, chat_type=1 if where == 'p2p' else 2, at_me=where == 'group_at')
    if sender == 'owner':
        expected = True  # 本人一直能唤起, 群里不用 @ 自己
    else:
        allowed = sender == 'listed' if non_empty else not owner_only  # 白名单非空只看名单; 为空看 owner_only
        expected = allowed and where != 'group'  # 群里别人(包括白名单里的人)要 @ 本人
    assert should_handle(m, c, ME, whitelist=whitelist) is expected


def test_whitelist_rules_in_detail():
    from test_triggers import rich
    on, off = cfg(prefix='/ai', owner_only=True), cfg(prefix='/ai')
    # 白名单非空时 owner_only 关着也只认名单里的人
    assert not should_handle(msg('/ai x'), off, ME, whitelist={LISTED})
    assert should_handle(msg('/ai x', from_id=LISTED), off, ME, whitelist={LISTED})
    # 按 user_id 匹配(数字和字符串都行), 不看名字
    assert should_handle(msg('/ai x', from_id=int(LISTED)), on, ME, whitelist=[int(LISTED)])
    # 群里照旧: 补收的历史消息从富文本里认出 @ 本人; 开头 @ 了别人不算; 关掉 group_at_only 后不用 @
    content, data = rich(('text', '/ai 问问 '), ('at', ME, '@耿宇贺'))
    assert should_handle(msg(content, from_id=LISTED, chat_type=3, content_data=data), on, ME, whitelist={LISTED})
    content, data = rich(('at', '999', '@江勇涛'), ('text', ' /ai 你好'))
    assert not should_handle(msg(content, from_id=LISTED, chat_type=2, content_data=data, at_me=True), on, ME,
                             whitelist={LISTED})
    assert should_handle(msg('/ai x', from_id=LISTED, chat_type=2), cfg(prefix='/ai', owner_only=True, group_at_only=False),
                         ME, whitelist={LISTED})
    # 前缀照旧要有
    assert not should_handle(msg('在吗', from_id=LISTED), on, ME, whitelist={LISTED})
    # 本人不受白名单影响
    assert should_handle(msg('/ai x', from_id=ME, chat_type=2), on, ME, whitelist={LISTED})


def test_rule_sentence():
    t = DEFAULTS['triggers']
    assert wl.rule_sentence(3, t) == '只有你和下面 3 个人能唤起数字人（群里要 @ 你）'
    assert wl.rule_sentence(1, dict(t, owner_only=True, group_at_only=False), below=False) == '只有你和白名单里的 1 个人能唤起数字人'
    assert wl.rule_sentence(0, dict(t, owner_only=True)) == '只有你能唤起数字人'
    assert wl.rule_sentence(0, dict(t, owner_only=False)) == '所有人都能唤起数字人（群里要 @ 你）'
    assert wl.rule_sentence(0, dict(t, owner_only=False, group_at_only=False)) == '所有人都能唤起数字人'


def test_whitelisted_people_stay_guests():
    """白名单只管能不能唤起: 白名单里的人发起的一轮照旧是访客, 访客的限制一样不少。"""
    turn = {'kind': 'message', 'chat_id': 'c1', 'is_owner': False, 'ref': 'm1'}
    assert state.is_guest(turn)
    assert guest.cli_denied('whitelist', argparse.Namespace(), turn)  # 访客这一轮里连看白名单都不行
    assert guest.GUEST_CLI_ALLOWED == {'send', 'recall', 'task', 'messages', 'history', 'download'}


# ---- 存储 ----

def test_storage_crud(tmp_path):
    url = f'sqlite:///{tmp_path}/w.db'
    st = Storage(url)
    assert st.whitelist_list() == [] and st.whitelist_ids() == set() and st.whitelist_get(ZW1) is None
    assert st.whitelist_add(ZW1, '张伟', '抖音研发-音乐', 'v3_a1', added_by='cli') is True
    assert st.whitelist_add(ZW2, '张伟', 'HRBP-抖音', added_by='web') is True
    # 再加一次不会重复, 只刷新快照; 没给的字段保留原值
    assert st.whitelist_add(ZW1, '张伟', '抖音研发-音乐-新部门') is False
    row = st.whitelist_get(ZW1)
    assert (row.name, row.department, row.avatar_key, row.added_by) == ('张伟', '抖音研发-音乐-新部门', 'v3_a1', 'cli')
    assert [r.user_id for r in st.whitelist_list()] == [ZW1, ZW2]
    # 另一个连接(比如 daemon 进程)马上能看到
    assert Storage(url).whitelist_ids() == {ZW1, ZW2}
    assert st.whitelist_remove(ZW1) is True and st.whitelist_remove(ZW1) is False
    assert Storage(url).whitelist_ids() == {ZW2}
    with pytest.raises(ValueError):
        st.whitelist_add('张伟')


def test_storage_refuses_writes_inside_twin(tmp_path, monkeypatch):
    st = Storage(f'sqlite:///{tmp_path}/w.db')
    st.whitelist_add(LS, '李四')
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    with pytest.raises(PermissionError):
        st.whitelist_add(ZW1, '张伟')
    with pytest.raises(PermissionError):
        st.whitelist_remove(LS)
    assert st.whitelist_ids() == {LS} and [r.name for r in st.whitelist_list()] == ['李四']  # 看还是可以的


# ---- 认出数字人的会话 ----

def _fake_proc(tmp_path, chain, cgroup='0::/user.slice/user-1001.slice/user@1001.service/app.slice/x.scope'):
    """chain: [(pid, ppid, 环境或 None)], 第一个是当前进程(self)。环境为 None 表示读不到。"""
    root = tmp_path / 'proc'
    for pid, ppid, env in chain:
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / 'stat').write_text(f'{pid} (ba sh) S {ppid} 1 1 0 -1\n')
        if env is not None:
            (d / 'environ').write_bytes(b''.join(f'{k}={v}\0'.encode() for k, v in env.items()))
    (root / str(chain[0][0]) / 'cgroup').write_text(cgroup + '\n')
    (root / 'self').symlink_to(root / str(chain[0][0]))
    return str(root)


def test_inside_twin_sees_through_stripped_env(tmp_path, monkeypatch):
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    plain = {'PATH': '/bin'}
    assert inside_twin(_fake_proc(tmp_path / 'a', [(50, 40, plain), (40, 30, plain), (30, 1, plain)])) is False
    # 数字人清掉 LARKX_ACTOR 再起 larkx: 祖先进程(agent)启动时的环境里还有
    agent = {'PATH': '/bin', 'LARKX_ACTOR': 'agent'}
    assert inside_twin(_fake_proc(tmp_path / 'b', [(50, 40, plain), (40, 30, None), (30, 20, agent), (20, 1, plain)])) is True
    # systemd 托管时在 daemon 的服务单元里(网页控制台的单元不算)
    unit = '0::/user.slice/user-1001.slice/user@1001.service/app.slice/lark-twin.service'
    assert inside_twin(_fake_proc(tmp_path / 'c', [(50, 1, plain)], cgroup=unit)) is True
    assert inside_twin(_fake_proc(tmp_path / 'd', [(50, 1, plain)], cgroup=unit.replace('lark-twin', 'lark-twin-web'))) is False
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    assert inside_twin(str(tmp_path / 'nothing')) is True


@pytest.mark.skipif(not os.path.exists('/proc/self/stat'), reason='需要 /proc')
def test_inside_twin_real_processes():
    code = 'from larkx.config import inside_twin; print(inside_twin())'
    env = {k: v for k, v in os.environ.items() if k != 'LARKX_ACTOR'}
    run = lambda cmd, e: subprocess.run(cmd, env=e, capture_output=True, text=True, check=True).stdout.strip()
    assert run([sys.executable, '-c', code], env) == 'False'
    # 外层 shell 带着 LARKX_ACTOR=agent(就像 agent 起的 shell), 里面 env -u 清掉后再运行
    inner = f'env -u LARKX_ACTOR {sys.executable} -c "{code}"'
    assert run(['sh', '-c', inner + '; true'], dict(env, LARKX_ACTOR='agent')) == 'True'


def test_hook_blocks_whitelist_writes():
    assert check_tool_use('Bash', {'command': 'larkx whitelist add 张伟'})
    assert check_tool_use('Bash', {'command': f'cd /tmp && lark whitelist remove {LS} --json'})
    assert check_tool_use('Bash', {'command': 'larkx whitelist list --json'}) is None


# ---- 搜人 ----

def test_search_people_and_avatar(fake_client):
    fake_client.people = PEOPLE
    found = wl.search_people(fake_client, '张伟')
    assert [p['user_id'] for p in found] == [ZW1, ZW2]  # 群不算
    assert found[1] == {'user_id': ZW2, 'name': '张伟', 'department': 'TikTok LIVE - Data & Governance', 'avatar_key': ''}
    fake_client.user_names = {ZW1: '张伟', '7100000000000000008': '王五'}
    assert wl.lookup_user(fake_client, ZW1)['department'] == '抖音研发-音乐'
    assert wl.lookup_user(fake_client, '7100000000000000008') == {'user_id': '7100000000000000008', 'name': '王五',
                                                                 'department': '', 'avatar_key': ''}
    assert wl.lookup_user(fake_client, '7100000000000000009') is None
    region = get_region('feishu')
    assert region.avatar_url('v3_a1').startswith('https://s1-imfile.feishucdn.com/static-resource/v1/v3_a1~?image_size=72x72')
    assert region.avatar_url('') == ''


def test_decode_search_response_keeps_department_and_avatar():
    from larkx.protocol.proto import builders, proto_pb2 as P
    resp = P.UniversalSearchResponse()
    r = resp.results.add()
    r.id, r.type, r.titleHighlighted, r.summaryHighlighted, r.avatarKey = ZW1, 1, '<h>张伟</h>', '抖音研发-音乐', 'v3_a1'
    g = resp.results.add()
    g.id, g.type, g.titleHighlighted = '76', 3, '群'
    resp.results.add().type = 5  # 消息之类的结果不要
    pkt = P.Packet(payload=resp.SerializeToString())
    assert builders.decode_search_response(pkt.SerializeToString()) == [
        {'type': 'user', 'id': ZW1, 'title': '<h>张伟</h>', 'summary': '抖音研发-音乐', 'avatar_key': 'v3_a1'},
        {'type': 'group', 'id': '76', 'title': '群', 'summary': '', 'avatar_key': ''}]


# ---- 命令行 ----

def _args(action, *target, json=False):
    return argparse.Namespace(action=action, target=list(target), json=json)


@pytest.fixture
def cli_env(monkeypatch, fake_client):
    st = Storage()
    fake_client.people = PEOPLE + [{'type': 'user', 'id': ME, 'title': '<h>我自己</h>', 'summary': '', 'avatar_key': ''}]
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'LarkClient', lambda *a, **k: fake_client)
    monkeypatch.setattr(cli, 'LarkAuth', lambda *a, **k: None)
    monkeypatch.setattr(cli, '_interactive', lambda: False)
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    monkeypatch.setenv('LARKX_TRIGGER_OWNER_ONLY', 'true')
    monkeypatch.setenv('LARKX_TRIGGER_GROUP_AT_ONLY', 'true')
    return st, fake_client


def test_cli_list(cli_env, capsys):
    import json
    st, _ = cli_env
    assert cli.cmd_whitelist(_args('list')) == 0
    assert capsys.readouterr().out.strip().splitlines() == [
        '只有你能唤起数字人', '没权限的人发 /ai 会收到：你没有权限唤起本人的 lark-twin（10 分钟内同一人只回一次）']
    st.whitelist_add(LS, '李四', '飞书-IM', added_by='cli')
    assert cli.cmd_whitelist(_args('list', json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['rule'] == '只有你和白名单里的 1 个人能唤起数字人（群里要 @ 你）' and out['owner_only'] is True
    assert [(e['user_id'], e['name'], e['department']) for e in out['entries']] == [(LS, '李四', '飞书-IM')]


def test_cli_add_unique_name_and_user_id(cli_env, capsys):
    st, fake = cli_env
    assert cli.cmd_whitelist(_args('add', '李四')) == 1  # 搜到"李四"和"李四光", 要选
    assert st.whitelist_ids() == set()
    fake.people = [p for p in fake.people if p['title'] != '<h>李四</h>光']
    assert cli.cmd_whitelist(_args('add', '李四')) == 0
    out = capsys.readouterr().out
    assert f'已加入白名单: 李四 · 飞书-IM · user_id {LS}' in out and '只有你和白名单里的 1 个人能唤起数字人' in out
    assert cli.cmd_whitelist(_args('add', '李四')) == 0 and '已经在白名单里' in capsys.readouterr().out
    # 直接给 user_id: 查出名字, 再按名字搜一次补上部门
    fake.user_names = {ZW2: '张伟'}
    assert cli.cmd_whitelist(_args('add', ZW2)) == 0
    assert st.whitelist_get(ZW2).department == 'TikTok LIVE - Data & Governance'
    assert cli.cmd_whitelist(_args('add', '7100000000000000009')) == 1
    assert st.whitelist_ids() == {LS, ZW2}
    assert [(a.actor, a.action) for a in st.list_actions()] == [('human', 'whitelist')] * 2


def test_cli_add_same_name_lists_candidates(cli_env, capsys):
    import json
    st, _ = cli_env
    assert cli.cmd_whitelist(_args('add', '张伟')) == 1
    err = capsys.readouterr().err
    assert '搜到 2 个人' in err and 'larkx whitelist add <user_id>' in err
    assert f'张伟 · 抖音研发-音乐 · user_id {ZW1}' in err and f'张伟 · TikTok LIVE - Data & Governance · user_id {ZW2}' in err
    assert cli.cmd_whitelist(_args('add', '张伟', json=True)) == 1
    out = json.loads(capsys.readouterr().out)
    assert out['ok'] is False and [c['user_id'] for c in out['candidates']] == [ZW1, ZW2]
    # 只搜到名字相近的一个人, 也要确认
    assert cli.cmd_whitelist(_args('add', '四光')) == 1 and '只搜到名字相近的「李四光」' in capsys.readouterr().err
    assert st.whitelist_ids() == set()
    # 用 user_id 重跑就加上了
    assert cli.cmd_whitelist(_args('add', ZW2)) == 1  # 这个 user_id 查不到名字(假客户端里没有)
    cli_env[1].user_names = {ZW2: '张伟'}
    assert cli.cmd_whitelist(_args('add', ZW2)) == 0 and st.whitelist_ids() == {ZW2}


def test_cli_add_interactive_pick(cli_env, monkeypatch, capsys):
    st, _ = cli_env
    monkeypatch.setattr(cli, '_interactive', lambda: True)
    asked = []
    monkeypatch.setattr(cli, '_choose', lambda prompt, labels: asked.append(labels) or 1)
    assert cli.cmd_whitelist(_args('add', '张伟')) == 0
    assert asked == [[f'张伟 · 抖音研发-音乐 · user_id {ZW1}', f'张伟 · TikTok LIVE - Data & Governance · user_id {ZW2}']]
    assert st.whitelist_ids() == {ZW2}
    monkeypatch.setattr(cli, '_choose', lambda prompt, labels: None)  # 取消
    assert cli.cmd_whitelist(_args('add', '李四')) == 1 and st.whitelist_ids() == {ZW2}


def test_cli_add_owner_refused(cli_env, capsys):
    st, _ = cli_env
    assert cli.cmd_whitelist(_args('add', '我自己')) == 1
    assert '你本人一直能唤起' in capsys.readouterr().err and st.whitelist_ids() == set()


def test_cli_remove(cli_env, capsys):
    st, _ = cli_env
    st.whitelist_add(ZW1, '张伟', '抖音研发-音乐')
    st.whitelist_add(ZW2, '张伟', 'HRBP-抖音')
    st.whitelist_add(LS, '李四', '飞书-IM')
    assert cli.cmd_whitelist(_args('remove', '王五')) == 1 and '白名单里没有「王五」' in capsys.readouterr().err
    assert cli.cmd_whitelist(_args('remove', '张伟')) == 1  # 两个张伟: 列出来, 请用 user_id
    assert f'user_id {ZW1}' in capsys.readouterr().err and len(st.whitelist_ids()) == 3
    assert cli.cmd_whitelist(_args('remove', ZW1)) == 0
    assert f'已移出白名单: 张伟 · 抖音研发-音乐 · user_id {ZW1}' in capsys.readouterr().out
    assert cli.cmd_whitelist(_args('remove', '李')) == 0  # 名字只有一个对得上时可以只写一部分
    assert st.whitelist_ids() == {ZW2}
    assert cli.cmd_whitelist(_args('remove', ZW2)) == 0
    assert '现在只有你能唤起数字人' in capsys.readouterr().out and st.whitelist_ids() == set()


def test_twin_cannot_change_whitelist(cli_env, monkeypatch, capsys):
    st, fake = cli_env
    st.whitelist_add(LS, '李四', '飞书-IM')
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'message', 'chat_id': 'c1', 'is_owner': True, 'ref': 'm1'})
    # 本人发起的一轮里也不行
    assert cli.cmd_whitelist(_args('add', '张伟')) == 3
    assert cli.cmd_whitelist(_args('remove', '李四', json=True)) == 3
    assert '数字人不能修改唤起白名单' in capsys.readouterr().out
    assert st.whitelist_ids() == {LS} and fake.searches == []
    # 记进审计; 告警只发到本人私聊
    assert [(a.actor, a.action, a.ok, a.turn_ref) for a in st.list_actions()] == [('agent', 'whitelist', 0, 'm1')] * 2
    assert {c for c, _ in fake.sent} == {'self'} and '张伟' in fake.sent[0][1]
    # 看还是可以的
    assert cli.cmd_whitelist(_args('list')) == 0 and '1 个人' in capsys.readouterr().out


def test_twin_refused_even_without_actor_env(cli_env, monkeypatch):
    """清掉 LARKX_ACTOR 也没用: 认出是数字人的进程(祖先进程、服务单元)就拒绝。"""
    import larkx.config
    st, _ = cli_env
    monkeypatch.setattr(larkx.config, 'inside_twin', lambda: True)
    assert cli.cmd_whitelist(_args('add', '张伟')) == 3
    assert st.whitelist_ids() == set() and st.list_actions()[0].actor == 'agent'


def test_twin_refused_through_main(cli_env, monkeypatch):
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'say', 'chat_id': '', 'is_owner': True, 'ref': 'say#1'})
    monkeypatch.setattr(sys, 'argv', ['larkx', 'whitelist', 'add', '张伟'])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 3 and cli_env[0].whitelist_ids() == set()


# ---- 网页接口 ----

@pytest.fixture
def wclient(tmp_path, monkeypatch, fake_client):
    monkeypatch.setattr(web, 'TOKEN_PATH', tmp_path / 'web_token')
    fake_client.auth.user_id, fake_client.auth.region = OWNER, get_region('feishu')
    fake_client.people = PEOPLE + [{'type': 'user', 'id': OWNER, 'title': '<h>张伟</h>', 'summary': '本人的部门', 'avatar_key': ''}]
    monkeypatch.setattr(web, '_auth', lambda: fake_client.auth)
    monkeypatch.setattr(web, '_lark', lambda: fake_client)
    monkeypatch.delenv('LARKX_ACTOR', raising=False)
    monkeypatch.setenv('LARKX_TRIGGER_OWNER_ONLY', 'true')
    monkeypatch.setenv('LARKX_TRIGGER_GROUP_AT_ONLY', 'true')
    return TestClient(web.create_app()), fake_client


H = {'X-Larkx': '1'}


def test_web_whitelist_needs_login_and_header(wclient):
    c, _ = wclient
    body = {'user_id': ZW1, 'name': '张伟', 'department': '抖音研发-音乐'}
    assert c.get('/api/whitelist').status_code == 401
    assert c.get('/api/people?q=张伟').status_code == 401
    assert c.post('/api/whitelist', json=body, headers=H).status_code == 401
    assert c.delete(f'/api/whitelist/{ZW1}', headers=H).status_code == 401
    c.post('/login', json={'token': 'wrong'})
    assert c.post('/api/whitelist', json=body, headers=H).status_code == 401
    c.post('/login', json={'token': web.get_token()})
    assert c.post('/api/whitelist', json=body).status_code == 403  # 没有 X-Larkx
    assert c.get('/api/whitelist').json()['entries'] == []
    Storage().whitelist_add(ZW1, '张伟')
    assert c.delete(f'/api/whitelist/{ZW1}').status_code == 403
    assert Storage().whitelist_ids() == {ZW1}


def test_web_whitelist_flow(wclient, monkeypatch):
    c, fake = wclient
    c.post('/login', json={'token': web.get_token()})
    s = c.get('/api/whitelist').json()
    assert s['rule'] == '只有你能唤起数字人' and s['owner_only'] is True and s['entries'] == []
    monkeypatch.setenv('LARKX_TRIGGER_OWNER_ONLY', 'false')
    assert c.get('/api/whitelist').json()['rule'] == '所有人都能唤起数字人（群里要 @ 你）'
    # 搜人: 只要用户; 带部门和头像; 标出本人
    r = c.get('/api/people', params={'q': '张伟'}).json()
    assert [(p['user_id'], p['department'], p['is_owner'], p['whitelisted']) for p in r['people']] == [
        (ZW1, '抖音研发-音乐', False, False), (ZW2, 'TikTok LIVE - Data & Governance', False, False), (OWNER, '本人的部门', True, False)]
    assert r['people'][0]['avatar'].startswith('https://s1-imfile.feishucdn.com/static-resource/v1/v3_a1~') and r['people'][1]['avatar'] == ''
    # 添加
    for p in r['people'][:2]:
        res = c.post('/api/whitelist', json={k: p[k] for k in ('user_id', 'name', 'department', 'avatar_key')}, headers=H).json()
        assert res['ok'] and res['created']
    fake.user_names = {LS: '李四'}
    res = c.post('/api/whitelist', json={'user_id': LS}, headers=H).json()  # 只给 user_id: 服务端查名字和部门
    assert res['created'] and res['entries'][-1]['name'] == '李四' and res['entries'][-1]['department'] == '飞书-IM'
    assert res['rule'] == '只有你和下面 3 个人能唤起数字人（群里要 @ 你）'
    assert c.post('/api/whitelist', json={'user_id': ZW1, 'name': '张伟'}, headers=H).json()['created'] is False
    assert [p['whitelisted'] for p in c.get('/api/people', params={'q': '张伟'}).json()['people']] == [True, True, False]
    assert {e['added_by'] for e in res['entries']} == {'web'} and res['entries'][0]['avatar'].endswith('&sticker_format=.webp')
    # 不合法的
    assert c.post('/api/whitelist', json={'user_id': OWNER, 'name': '张伟'}, headers=H).status_code == 400
    assert c.post('/api/whitelist', json={'user_id': '张伟'}, headers=H).status_code == 400
    assert c.post('/api/whitelist', json={'user_id': '7100000000000000009'}, headers=H).status_code == 400
    assert c.get('/api/people', params={'q': ' '}).status_code == 400
    # 移除
    res = c.delete(f'/api/whitelist/{ZW1}', headers=H).json()
    assert res['ok'] and [e['user_id'] for e in res['entries']] == [ZW2, LS] and '下面 2 个人' in res['rule']
    assert c.delete(f'/api/whitelist/{ZW1}', headers=H).status_code == 404
    assert c.delete('/api/whitelist/abc', headers=H).status_code == 400
    # 改动记进动态
    acts = [a for a in c.get('/api/activity').json() if a['action'] == 'whitelist']
    assert [a['content'].split(' ')[0] for a in acts] == ['网页移出白名单'] + ['网页加入白名单'] * 3  # 新的在前
    assert Storage().whitelist_ids() == {ZW2, LS}


def test_web_whitelist_search_error_and_twin_guard(wclient, monkeypatch):
    c, fake = wclient
    c.post('/login', json={'token': web.get_token()})

    def boom(q):
        raise RuntimeError('网络不通')
    monkeypatch.setattr(fake, 'search', boom)
    r = c.get('/api/people', params={'q': '张伟'})
    assert r.status_code == 502 and '网络不通' in r.json()['error']
    # 存储层的保护: 就算从数字人的进程里调到这个接口也写不进去
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    r = c.post('/api/whitelist', json={'user_id': ZW1, 'name': '张伟'}, headers=H)
    assert r.status_code == 403 and Storage().whitelist_ids() == set()
