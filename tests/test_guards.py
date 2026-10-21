import argparse
import io

from larkx import cli
from larkx.agent.hooks import check_tool_use


def test_hook_blocks_raw_api_and_credentials():
    assert check_tool_use('Bash', {'command': 'larkx api chats.PullChatsByIdsRequest {}'})
    assert check_tool_use('Bash', {'command': 'cd /tmp && lark auth import --cookie x'})
    assert check_tool_use('Read', {'file_path': '/home/u/.larkx/credentials.json'})
    assert check_tool_use('Bash', {'command': "larkx send c1 --json - <<'EOF'\nhi\nEOF"}) is None
    assert check_tool_use('Bash', {'command': 'larkx messages c1 --json'}) is None


def _send_args(text):
    return argparse.Namespace(text=text, json=True, chat_id='c1', root=None)


def test_send_text_from_stdin(monkeypatch):
    monkeypatch.setattr('sys.stdin', io.StringIO("第一行\n$HOME `x` '引号'\n"))
    assert cli._read_send_text(_send_args(['-'])) == "第一行\n$HOME `x` '引号'"
    assert cli._read_send_text(_send_args(['你好', '世界'])) == '你好 世界'


def test_agent_reply_cannot_start_with_prefix(tmp_path, monkeypatch, capsys):
    from larkx.storage import Storage
    st = Storage(f'sqlite:///{tmp_path}/t.db')
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setenv('LARKX_TRIGGER_PREFIX', '/ai')
    monkeypatch.setattr(cli, 'LarkClient', lambda *a, **k: (_ for _ in ()).throw(AssertionError('不应发送')))
    assert cli.cmd_send(_send_args(['/ai', '回声'])) == 1
    assert '触发前缀' in capsys.readouterr().out
    assert cli.cmd_send(_send_args(['@张三', '/ai', '回声'])) == 1  # 开头带 @ 也会被 daemon 当成触发
    assert [(a.actor, a.ok) for a in st.list_actions()] == [('agent', 0), ('agent', 0)]
