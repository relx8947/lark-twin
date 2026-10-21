"""把面向本人的 larkx skill 装进本机的 Claude Code / Codex。"""
import os
import sys
from pathlib import Path

from .agent.home import TEMPLATES

OWNER_SKILL = TEMPLATES / 'skills' / 'larkx-owner' / 'SKILL.md'


def skill_dir(target: str) -> Path:
    if target == 'claude':
        base = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    elif target == 'codex':
        base = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    else:
        raise ValueError(f'未知的目标 {target}')
    return base / 'skills' / 'larkx'


def install_user_skill(target: str) -> Path:
    # 用户自己的 agent 不一定能在 PATH 里找到 larkx, skill 里写死本机的绝对路径
    exe = str(Path(sys.executable).parent / 'larkx')
    text = OWNER_SKILL.read_text(encoding='utf-8').replace('{{LARKX}}', exe)
    d = skill_dir(target)
    d.mkdir(parents=True, exist_ok=True)
    (d / 'SKILL.md').write_text(text, encoding='utf-8')
    return d / 'SKILL.md'
