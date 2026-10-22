import pytest


@pytest.fixture(autouse=True)
def _isolate_local_state(monkeypatch, tmp_path):
    """测试一律用临时库和临时状态目录, 不写进本机真实的 ~/.larkx。"""
    from larkx import daemon, notify, state
    monkeypatch.setenv('LARKX_STORAGE_URL', f'sqlite:///{tmp_path}/default.db')
    monkeypatch.setattr(state, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(state, 'TURN_PATH', tmp_path / 'state' / 'turn.json')
    monkeypatch.setattr(state, 'RELAY_DIR', tmp_path / 'state' / 'relay')
    monkeypatch.setattr(notify, 'NOTIFY_STATE', tmp_path / 'state' / 'notify.json')
    monkeypatch.setattr(daemon, 'DAEMON_STATE', tmp_path / 'state' / 'daemon.json')


class FakeClient:
    """记录发出的消息, 不访问网络。"""

    def __init__(self, user_id='100', owner_chat='self'):
        self.sent, self.api_calls = [], []
        self.keep_unread = []  # 每次发送带的 keep_unread
        self.auth = type('A', (), {'user_id': user_id})()
        self._owner_chat = owner_chat

    def send_msg(self, text, chat_id, root_id=None, keep_unread=False):
        self.sent.append((chat_id, text))
        self.keep_unread.append(keep_unread)
        return f'm{len(self.sent)}'

    def create_chat(self, user_id):
        return self._owner_chat

    def get_user_name(self, user_id, chat_id):
        return None

    def get_group_name(self, chat_id):
        return None

    def api(self, type_or_cmd, data=None):
        self.api_calls.append((type_or_cmd, data))
        return {'message': 'ok'}


@pytest.fixture
def fake_client():
    return FakeClient()
