import argparse
import io

from larkx import cli
from larkx.agent.hooks import check_tool_use


def test_hook_blocks_raw_api_and_credentials():
    assert check_tool_use('Bash', {'command': 'larkx api chats.PullChatsByIdsRequest {}'})
    assert check_tool_use('Bash', {'command': 'cd /tmp && lark auth import --cookie x'})
    assert check_tool_use('Read', {'file_path': '/home/u/.larkx/credentials.json'})
    assert check_tool_use('Bash', {'command': 'larkx web url'})
    assert check_tool_use('Bash', {'command': 'cat ~/.larkx/web_token'})
    assert check_tool_use('Bash', {'command': "larkx send c1 --json - <<'EOF'\nhi\nEOF"}) is None
    assert check_tool_use('Bash', {'command': 'larkx messages c1 --json'}) is None


def _send_args(text):
    return argparse.Namespace(text=text, json=True, chat_id='c1', root=None)


def test_send_text_from_stdin(monkeypatch):
    monkeypatch.setattr('sys.stdin', io.StringIO("第一行\n$HOME `x` '引号'\n"))
    assert cli._read_send_text(_send_args(['-'])) == "第一行\n$HOME `x` '引号'"
    assert cli._read_send_text(_send_args(['你好', '世界'])) == '你好 世界'


def test_agent_reply_cannot_start_with_prefix(tmp_path, monkeypatch, capsys, fake_client):
    from larkx.storage import Storage
    st = Storage(f'sqlite:///{tmp_path}/t.db')
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'LarkClient', lambda *a, **k: fake_client)
    monkeypatch.setattr(cli, 'LarkAuth', lambda *a, **k: None)
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setenv('LARKX_TRIGGER_PREFIX', '/ai')
    assert cli.cmd_send(_send_args(['/ai', '回声'])) == 1
    assert '触发前缀' in capsys.readouterr().out
    assert cli.cmd_send(_send_args(['@张三', '/ai', '回声'])) == 1  # 开头带 @ 也会被 daemon 当成触发
    assert [(a.actor, a.ok) for a in st.list_actions()] == [('agent', 0), ('agent', 0)]
    # 被拦下时只给本人私聊发告警, 不会发到目标会话
    assert {c for c, _ in fake_client.sent} == {'self'} and '拦下' in fake_client.sent[0][1]


def test_agent_reply_signature_is_set_apart(tmp_path, monkeypatch, fake_client):
    from larkx import state
    from larkx.storage import Storage
    st = Storage(f'sqlite:///{tmp_path}/t.db')
    monkeypatch.setattr(cli, 'Storage', lambda: st)
    monkeypatch.setattr(cli, 'LarkClient', lambda *a, **k: fake_client)
    monkeypatch.setattr(cli, 'LarkAuth', lambda *a, **k: None)
    monkeypatch.setattr(state, 'read_turn', lambda: {'kind': 'message', 'chat_id': 'c1', 'is_owner': True, 'ref': 'm1'})
    monkeypatch.setenv('LARKX_ACTOR', 'agent')
    monkeypatch.setenv('LARKX_MIRROR', 'false')
    monkeypatch.setenv('LARKX_REPLY_SIGNATURE', '🤖 lark-twin · 数字分身')
    assert cli.cmd_send(_send_args(['收到\n'])) == 0
    assert fake_client.sent[-1] == ('c1', '收到\n\n—— 🤖 lark-twin · 数字分身')
    # 本人自己用 larkx send 发的消息不加签名
    monkeypatch.delenv('LARKX_ACTOR')
    assert cli.cmd_send(_send_args(['收到'])) == 0
    assert fake_client.sent[-1] == ('c1', '收到')
