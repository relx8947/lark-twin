import argparse
import json
import tomllib
from datetime import datetime

import pytest

from larkx import cli, state
from larkx.agent.codex import CodexTurnParser, dump_toml


def test_parse_duration():
    assert state.parse_duration('30m') == 1800
    assert state.parse_duration('1h30m') == 5400
    assert state.parse_duration('1d') == 86400
    with pytest.raises(ValueError):
        state.parse_duration('三十分钟')
    with pytest.raises(ValueError):
        state.parse_duration('30x')


def test_parse_at():
    now = datetime(2026, 10, 21, 12, 0)
    assert datetime.fromtimestamp(state.parse_at('2026-10-22 09:00', now)) == datetime(2026, 10, 22, 9, 0)
    assert datetime.fromtimestamp(state.parse_at('10-25 08:30', now)) == datetime(2026, 10, 25, 8, 30)
    assert datetime.fromtimestamp(state.parse_at('13:00', now)) == datetime(2026, 10, 21, 13, 0)
    assert datetime.fromtimestamp(state.parse_at('09:00', now)) == datetime(2026, 10, 22, 9, 0)
    with pytest.raises(ValueError):
        state.parse_at('明早')


def test_next_due_skips_missed_periods():
    assert state.next_due(1000, 100, now=900) == 1000
    assert state.next_due(1000, 100, now=1000) == 1100
    assert state.next_due(1000, 100, now=1350) == 1400


def turn(kind='message', chat='c1', owner=False):
    return {'kind': kind, 'chat_id': chat, 'anchor': '', 'is_owner': owner, 'ref': 'm1'}


def test_send_policy():
    assert state.send_denied('c1', None)
    assert state.send_denied('c1', turn()) == ''
    assert '本人' in state.send_denied('c2', turn())
    assert state.send_denied('c2', turn(owner=True)) == ''
    assert state.send_denied('c2', turn(kind='say', chat='')) == ''
    assert state.send_denied('c1', turn(kind='system', chat='c1'))


def test_turn_file_ignored_when_daemon_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(state, 'TURN_PATH', tmp_path / 'turn.json')
    monkeypatch.setattr(state, 'STATE_DIR', tmp_path)
    state.write_turn('message', 'c1', is_owner=True, ref='m1')
    assert state.read_turn()['chat_id'] == 'c1'
    data = json.loads((tmp_path / 'turn.json').read_text())
    data['pid'] = 2 ** 22 + 12345  # 不存在的进程
    (tmp_path / 'turn.json').write_text(json.dumps(data))
    assert state.read_turn() is None


def test_codex_parser():
    p = CodexTurnParser()
    lines = [
        {'type': 'thread.started', 'thread_id': 't-1'},
        {'type': 'turn.started'},
        {'type': 'error', 'message': 'Reconnecting... 1/5'},
        {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': "zsh -lc 'larkx chats'"}},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '已回复'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 10}},
    ]
    cmds = [p.feed(json.dumps(e)) for e in lines] + [p.feed('not json')]
    assert p.thread_id == 't-1' and p.completed and not p.failed
    assert p.texts == ['已回复'] and p.tools == ['shell']
    assert cmds[3] == "zsh -lc 'larkx chats'" and cmds.count(None) == len(cmds) - 1
    f = CodexTurnParser()
    f.feed(json.dumps({'type': 'turn.failed', 'error': {'message': 'model not supported'}}))
    assert f.failed and f.error == 'model not supported'


def test_dump_toml_roundtrip():
    conf = {'approval_policy': 'never', 'features': {'hooks': False},
            'projects': {'/home/u/.larkx/agent': {'trust_level': 'trusted'}},
            'model_providers': {'relay': {'base_url': 'https://x', 'env_key': 'K', 'retries': 3}}}
    assert tomllib.loads('\n'.join(dump_toml(conf))) == conf


def test_agent_home_upgrades_untouched_template_and_commits(tmp_path):
    from larkx.agent.home import OLD_TEMPLATE_SHA256, TEMPLATES, ensure_agent_home
    import subprocess
    cfg = {'agent_home': str(tmp_path / 'agent'), 'agent_claude_dir': str(tmp_path / 'claude')}
    home = ensure_agent_home(cfg)
    assert (home / 'AGENTS.md').resolve() == (home / 'CLAUDE.md').resolve()
    assert (home / 'memory' / 'MEMORY.md').exists()
    log = subprocess.run(['git', '-C', str(home), 'log', '--format=%an'], capture_output=True, text=True).stdout
    assert log.strip() == 'lark-twin'
    # 用户改过的人设不被覆盖
    (home / 'CLAUDE.md').write_text('我的人设', encoding='utf-8')
    ensure_agent_home(cfg)
    assert (home / 'CLAUDE.md').read_text(encoding='utf-8') == '我的人设'
    assert OLD_TEMPLATE_SHA256 and (TEMPLATES / 'CLAUDE.md').exists()


def _task_args(text, **kw):
    base = dict(action='add', text=text, at=None, in_='10m', every=None, chat=None, root=None, all=False, json=True)
    base.update(kw)
    return argparse.Namespace(**base)


def test_agent_task_scoped_to_current_chat(tmp_path, monkeypatch, capsys):
    from larkx.storage import Storage
    st = Storage(f'sqlite:///{tmp_path}/t.db')
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setattr(state, 'read_turn', lambda: turn(chat='c1'))
    assert cli.cmd_task(_task_args(['提醒', '喝水'])) == 0
    t = st.list_tasks()[0]
    assert (t.chat_id, t.by_owner, t.creator) == ('c1', 0, 'agent')
    assert cli.cmd_task(_task_args(['发到别处'], chat='c2')) == 1
    assert '本人' in capsys.readouterr().out
    monkeypatch.setattr(state, 'read_turn', lambda: turn(kind='system', chat=''))
    assert cli.cmd_task(_task_args(['维护里建任务'])) == 1


def test_daemon_schedules_due_task(tmp_path, monkeypatch):
    import asyncio
    from larkx import daemon as d
    from larkx.storage import Storage

    st = Storage(f'sqlite:///{tmp_path}/t.db')
    tid = st.add_task(kind='task', due_at=1, every_sec=3600, prompt='提醒喝水', chat_id='c1', by_owner=0)
    obj = d.Daemon.__new__(d.Daemon)
    obj.storage, obj.inbox = st, asyncio.Queue()
    obj.cfg = {'memory_review_hour': -1}
    monkeypatch.setattr(d, 'SCHEDULER_INTERVAL', 0)

    async def one_round():
        task = asyncio.create_task(obj._schedule())
        ev = await asyncio.wait_for(obj.inbox.get(), 2)
        task.cancel()
        return ev

    ev = asyncio.run(one_round())
    assert ev.kind == 'task' and ev.chat_id == 'c1' and not ev.is_owner and '提醒喝水' in ev.prompt
    assert st.get_task(tid).status == 'running'
    from larkx.agent import TurnResult
    obj._finish_task(ev, TurnResult('success', False, '已提醒', 1.0))
    t = st.get_task(tid)
    assert t.status == 'pending' and t.due_at > 1 and t.run_count == 1 and t.result == '已提醒'


def test_memory_flush_before_compaction(tmp_path, monkeypatch):
    import asyncio
    from larkx import daemon as d
    from larkx.agent import TurnResult

    class FakeSession:
        def __init__(self, pressure):
            self.pressure, self.prompts = pressure, []

        async def context_pressure(self):
            return self.pressure

        async def ask(self, prompt):
            self.prompts.append(prompt)
            return TurnResult('success', False, '', 0.1)

    monkeypatch.setattr(d.state, 'write_turn', lambda *a, **k: None)
    monkeypatch.setattr(d.state, 'clear_turn', lambda: None)
    monkeypatch.setattr(d, 'commit_agent_home', lambda *a: False)
    obj = d.Daemon.__new__(d.Daemon)
    obj.cfg, obj.home = {'memory_flush_ratio': 0.85}, tmp_path
    obj.session = FakeSession(0.5)
    asyncio.run(obj._maybe_flush_memory())
    assert obj.session.prompts == []
    obj.session = FakeSession(0.9)
    asyncio.run(obj._maybe_flush_memory())
    assert len(obj.session.prompts) == 2 and 'memory/' in obj.session.prompts[0] and obj.session.prompts[1] == '/compact'
    obj.session = FakeSession(None)  # codex 后端不支持
    asyncio.run(obj._maybe_flush_memory())
    assert obj.session.prompts == []
