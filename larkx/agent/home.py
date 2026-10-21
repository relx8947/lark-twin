import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from loguru import logger

TEMPLATES = Path(__file__).parent / 'templates'
# 历次发布的人设模板。数字人目录里的 CLAUDE.md 和其中某一版完全相同(用户没改过)时, 自动升级到当前模板
OLD_TEMPLATE_SHA256 = {'6927812ddbec998de7f1785c415889ed74adb266f83768a6a5647216dad11bca',
                       'de2a5b963785a90cbcd3e8e3ae5a0f627804a044352d36d5d8eed506a97337a6'}
GITIGNORE = 'downloads/\n'
MEMORY_INDEX = '# 记忆索引\n\n每行一条: - [标题](文件名.md) — 一句话说明。具体内容写在对应文件里。\n'


def _git(home: Path, *args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(home), *args], capture_output=True, text=True, check=check)


def ensure_agent_home(cfg: dict) -> Path:
    home = Path(cfg['agent_home'])
    home.mkdir(parents=True, exist_ok=True)
    Path(cfg['agent_claude_dir']).mkdir(parents=True, exist_ok=True)
    # Claude Code 会从工作目录一路向上找 .claude/skills, 直到 git 仓库根; 目录在家目录下时
    # 会把 ~/.claude/skills 里本机的所有 skill 都加载进来。让数字人目录自成一个仓库, 查找停在这里。
    # 这个仓库同时记录人设和记忆的每次变化, 只在本机, 不推送
    if not (home / '.git').exists():
        subprocess.run(['git', 'init', '-q', str(home)], check=True)
    _git(home, 'config', 'user.name', 'lark-twin')
    _git(home, 'config', 'user.email', 'lark-twin@localhost')
    if not (home / '.gitignore').exists():
        (home / '.gitignore').write_text(GITIGNORE, encoding='utf-8')
    (home / 'downloads').mkdir(exist_ok=True)
    # 人设和行为准则归用户所有, 只在首次创建; 用户没改过的旧版模板自动升级
    claude_md = home / 'CLAUDE.md'
    if not claude_md.exists():
        shutil.copyfile(TEMPLATES / 'CLAUDE.md', claude_md)
    elif hashlib.sha256(claude_md.read_bytes()).hexdigest() in OLD_TEMPLATE_SHA256:
        shutil.copyfile(TEMPLATES / 'CLAUDE.md', claude_md)
        logger.info('CLAUDE.md 是未修改过的旧版模板, 已升级到当前模板')
    # Codex 读 AGENTS.md: 指向同一份人设
    agents_md = home / 'AGENTS.md'
    if not agents_md.exists() and not agents_md.is_symlink():
        agents_md.symlink_to('CLAUDE.md')
    memory = home / 'memory'
    memory.mkdir(exist_ok=True)
    if not (memory / 'MEMORY.md').exists():
        (memory / 'MEMORY.md').write_text(MEMORY_INDEX, encoding='utf-8')
    # 会话记录默认 30 天清理, 常驻会话要长期保留。写在项目配置里而不是 --settings 参数,
    # 因为 ccd 这类启动器自己也会传 --settings, 两个参数会互相覆盖
    settings = home / '.claude' / 'settings.json'
    settings.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(settings.read_text(encoding='utf-8')) if settings.exists() else {}
    if data.get('cleanupPeriodDays') != 3650:
        data['cleanupPeriodDays'] = 3650
        settings.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # skill 是 larkx CLI 的使用手册, 跟着代码版本走, 每次启动都同步
    skill_dir = home / '.claude' / 'skills' / 'larkx'
    skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATES / 'skills' / 'larkx' / 'SKILL.md', skill_dir / 'SKILL.md')
    commit_agent_home(home, '启动时同步数字人目录')
    return home


def commit_agent_home(home: Path, message: str) -> bool:
    """把人设、记忆等变化提交进数字人目录自己的 git 仓库; 没有变化时返回 False。"""
    try:
        _git(home, 'add', '-A')
        if _git(home, 'diff', '--cached', '--quiet', check=False).returncode == 0:
            return False
        _git(home, 'commit', '-q', '-m', message)
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f'提交数字人目录失败: {e.stderr.strip()[:200]}')
        return False
