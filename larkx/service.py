"""用 systemd 用户服务托管 larkx daemon 和网页控制台。"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

UNITS = {'daemon': 'lark-twin.service', 'web': 'lark-twin-web.service'}
UNIT = UNITS['daemon']
DESCRIPTIONS = {'daemon': 'lark-twin 飞书数字人 (larkx daemon)', 'web': 'lark-twin 网页控制台 (larkx web)'}
PROJECT_DIR = Path(__file__).resolve().parents[1]


def unit_path(kind: str = 'daemon') -> Path:
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return base / 'systemd' / 'user' / UNITS[kind]


def available() -> bool:
    return shutil.which('systemctl') is not None


def render_unit(kind: str = 'daemon') -> str:
    exe = Path(sys.executable).parent / 'larkx'
    # 记下安装时的 PATH: 服务里要能找到 LARKX_CLAUDE_CLI / LARKX_CODEX_CLI 指定的命令(如 ccd、codex)以及它依赖的工具
    path = os.pathsep.join(dict.fromkeys([str(exe.parent)] + os.environ.get('PATH', '').split(os.pathsep)))
    return f"""[Unit]
Description={DESCRIPTIONS[kind]}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={PROJECT_DIR}
ExecStart={exe} {kind}
Environment=PATH={path}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


def systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True)


def kinds(which: str = 'all') -> list:
    return list(UNITS) if which == 'all' else [which]


def install(which: str = 'all') -> list:
    paths = []
    for kind in kinds(which):
        p = unit_path(kind)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render_unit(kind), encoding='utf-8')
        paths.append(p)
    systemctl('daemon-reload')
    for kind in kinds(which):
        systemctl('enable', UNITS[kind])
    return paths


def uninstall(which: str = 'all'):
    for kind in kinds(which):
        systemctl('disable', '--now', UNITS[kind])
        unit_path(kind).unlink(missing_ok=True)
    systemctl('daemon-reload')


def status(kind: str = 'daemon') -> dict:
    unit = UNITS[kind]
    installed = unit_path(kind).exists()
    info = {'unit': unit, 'installed': installed, 'active': False, 'enabled': False, 'main_pid': 0, 'since': ''}
    if installed:
        info['active'] = systemctl('is-active', unit).stdout.strip() == 'active'
        info['enabled'] = systemctl('is-enabled', unit).stdout.strip() == 'enabled'
        pid = systemctl('show', unit, '-p', 'MainPID', '--value').stdout.strip()
        info['main_pid'] = int(pid) if pid.isdigit() else 0
        info['since'] = systemctl('show', unit, '-p', 'ActiveEnterTimestamp', '--value').stdout.strip()
    return info


def stray_daemons() -> list:
    """不归 systemd 管的 larkx daemon 进程; 和服务同时运行会出现两个数字人。"""
    out = subprocess.run(['pgrep', '-f', r'bin/larkx daemon$'], capture_output=True, text=True).stdout.split()
    managed = status('daemon')['main_pid']
    return [int(p) for p in out if int(p) not in (os.getpid(), managed)]
