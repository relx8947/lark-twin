import pytest


@pytest.fixture(autouse=True)
def _no_real_storage(monkeypatch, tmp_path):
    """测试默认用临时库, 防止写进本机真实的 ~/.larkx/messages.db。"""
    monkeypatch.setenv('LARKX_STORAGE_URL', f'sqlite:///{tmp_path}/default.db')
