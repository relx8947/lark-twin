import pytest
from starlette.testclient import TestClient

from larkx.web import app as web


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'TOKEN_PATH', tmp_path / 'web_token')
    monkeypatch.setattr(web, '_login_state', lambda: {'ok': True, 'message': '', 'user_id': '100'})
    return TestClient(web.create_app())


def test_login_url_keeps_token_out_of_query(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'TOKEN_PATH', tmp_path / 'web_token')
    url = web.login_url({'web_url': 'http://h:18340/', 'web_port': 18341})
    assert url == f'http://h:18340/#login={web.get_token()}'
    assert (tmp_path / 'web_token').stat().st_mode & 0o777 == 0o600


def test_api_needs_login(client):
    assert client.get('/').status_code == 200
    r = client.get('/static/feishu.svg')
    assert r.status_code == 200 and r.headers['content-type'].startswith('image/svg') and r.headers['cache-control'] == 'no-cache'
    assert client.get('/api/tasks').status_code == 401
    assert client.post('/login', json={'token': 'wrong'}).status_code == 401
    assert client.get('/login').status_code == 405
    r = client.post('/login', json={'token': web.get_token()})
    assert r.status_code == 200 and 'httponly' in r.headers['set-cookie'].lower()
    assert client.get('/api/tasks').json() == []


def test_writes_need_header(client):
    client.post('/login', json={'token': web.get_token()})
    body = {'chat_id': 'c1', 'prompt': '提醒我喝水', 'in': '10m'}
    assert client.post('/api/tasks', json=body).status_code == 403
    r = client.post('/api/tasks', json=body, headers={'X-Larkx': '1'})
    assert r.status_code == 200, r.text
    assert [t['prompt'] for t in client.get('/api/tasks').json()] == ['提醒我喝水']


def test_bearer_token(client):
    assert client.get('/api/tasks', headers={'Authorization': f'Bearer {web.get_token()}'}).status_code == 200
