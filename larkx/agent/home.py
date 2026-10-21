import shutil
import subprocess
from pathlib import Path

TEMPLATES = Path(__file__).parent / 'templates'


def ensure_agent_home(cfg: dict) -> Path:
    home = Path(cfg['agent_home'])
    home.mkdir(parents=True, exist_ok=True)
    Path(cfg['agent_claude_dir']).mkdir(parents=True, exist_ok=True)
    # Claude Code 会从工作目录一路向上找 .claude/skills, 直到 git 仓库根; 目录在家目录下时
    # 会把 ~/.claude/skills 里本机的所有 skill 都加载进来。让数字人目录自成一个仓库, 查找停在这里
    if not (home / '.git').exists():
        subprocess.run(['git', 'init', '-q', str(home)], check=True)
    # 人设和行为准则归用户所有, 只在首次创建, 之后不覆盖
    claude_md = home / 'CLAUDE.md'
    if not claude_md.exists():
        shutil.copyfile(TEMPLATES / 'CLAUDE.md', claude_md)
    # skill 是 larkx CLI 的使用手册, 跟着代码版本走, 每次启动都同步
    skill_dir = home / '.claude' / 'skills' / 'larkx'
    skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATES / 'skills' / 'larkx' / 'SKILL.md', skill_dir / 'SKILL.md')
    return home
