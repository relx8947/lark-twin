"""用 systemd 用户服务托管 larkx daemon。"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

UNIT = 'lark-twin.service'
PROJECT_DIR = Path(__file__).resolve().parents[1]


def unit_path() -> Path:
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / 'systemd' / 'user' / UNIT


def available() -> bool:
    return shutil.which('systemctl') is not None


def render_unit() -> str:
    exe = Path(sys.executable).parent / 'larkx'
    # 记下安装时的 PATH: 服务里要能找到 LARKX_CLAUDE_CLI / LARKX_CODEX_CLI 指定的命令(如 ccd、codex)以及它依赖的工具
    path = os.pathsep.join(dict.fromkeys([str(exe.parent)] + os.environ.get('PATH', '').split(os.pathsep)))
    return f"""[Unit]
Description=lark-twin 飞书数字人 (larkx daemon)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={PROJECT_DIR}
ExecStart={exe} daemon
Environment=PATH={path}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


def systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True)


def install() -> Path:
    p = unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_unit(), encoding='utf-8')
    systemctl('daemon-reload')
    systemctl('enable', UNIT)
    return p


def uninstall():
    systemctl('disable', '--now', UNIT)
    unit_path().unlink(missing_ok=True)
    systemctl('daemon-reload')


def status() -> dict:
    installed = unit_path().exists()
    info = {'unit': UNIT, 'installed': installed, 'active': False, 'enabled': False, 'main_pid': 0}
    if installed:
        info['active'] = systemctl('is-active', UNIT).stdout.strip() == 'active'
        info['enabled'] = systemctl('is-enabled', UNIT).stdout.strip() == 'enabled'
        pid = systemctl('show', UNIT, '-p', 'MainPID', '--value').stdout.strip()
        info['main_pid'] = int(pid) if pid.isdigit() else 0
    return info


def stray_daemons() -> list:
    """不归 systemd 管的 larkx daemon 进程; 和服务同时运行会出现两个数字人。"""
    out = subprocess.run(['pgrep', '-f', r'bin/larkx daemon$'], capture_output=True, text=True).stdout.split()
    managed = status()['main_pid']
    return [int(p) for p in out if int(p) not in (os.getpid(), managed)]
