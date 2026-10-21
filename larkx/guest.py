"""访客模式: 不是本人发起的一轮(别人的 /ai、被接管会话里对方的消息、别人安排的任务), 数字人能做的事要收窄。

本人自己的对话、本人在群里发起的 /ai 不受这里限制。两处使用:
- Claude 后端的工具调用检查(agent/hooks.py): 只许单条 larkx 命令, 只能读写数字人自己的目录, 不能联网、不能开子 agent;
- larkx CLI(cli.py): 不能列会话、搜人、查"谁找过我", 不能看别的会话, 不能动接管。
按字符串匹配可以被刻意绕过, 这是减少误用和注入的防线, 不是安全隔离。
"""
import os
import re
import shlex

from . import state

# 访客发起的这一轮里允许用的 larkx 子命令; 其余一律拒绝
GUEST_CLI_ALLOWED = {'send', 'recall', 'task', 'messages', 'history', 'download'}
# 访客这一轮里完全禁止的工具: 联网、开子 agent
GUEST_BLOCKED_TOOLS = {'WebFetch', 'WebSearch', 'Task', 'Agent'}
READ_TOOLS = {'Read', 'Glob', 'Grep', 'LS'}
WRITE_TOOLS = {'Write', 'Edit', 'MultiEdit', 'NotebookEdit'}
SHELL_META = re.compile(r'[;&|<>`]|\$\(')
HEREDOC = re.compile(r"\s+<<-?\s*(['\"]?)(\w+)\1\s*$")


def is_guest(turn) -> bool:
    return state.is_guest(turn)


def _inside(path: str, root: str) -> bool:
    p = os.path.realpath(os.path.expanduser(path))
    r = os.path.realpath(os.path.expanduser(root))
    return p == r or p.startswith(r + os.sep)


def bash_denied(command: str, agent_home: str) -> str:
    """访客这一轮里的 shell 命令: 只许一条 larkx 命令(可以带 heredoc 正文), 不许串接、重定向。"""
    command = (command or '').strip()
    first, _, rest = command.partition('\n')
    line = first.strip()
    m = re.match(r'^cd\s+(\S+)\s*(&&|;)\s*', line)
    if m and _inside(m.group(1), agent_home):
        line = line[m.end():]
    hd = HEREDOC.search(line)
    if hd:
        body = rest.split('\n')
        if hd.group(2) not in [b.strip() for b in body]:
            return 'heredoc 没有结束标记'
        after = body[[b.strip() for b in body].index(hd.group(2)) + 1:]
        if any(b.strip() for b in after):
            return '访客发起的请求里, heredoc 之后不能再跟别的命令'
        line = line[:hd.start()]
    elif rest.strip():
        return '访客发起的请求里只能运行一条命令'
    if SHELL_META.search(line):
        return '访客发起的请求里不能串接命令、重定向或嵌套执行'
    try:
        tokens = shlex.split(line)
    except ValueError:
        return '命令无法解析'
    if not tokens:
        return '空命令'
    exe = os.path.basename(tokens[0])
    if exe in ('larkx', 'lark'):
        return ''
    if exe == 'date' and len(tokens) <= 2:
        return ''
    return f'访客发起的请求里只能运行 larkx 命令(这次是 {exe})'


def tool_denied(tool: str, tool_input: dict, agent_home: str) -> str:
    """访客这一轮里的工具调用是否放行; 拒绝时返回原因。"""
    ti = tool_input or {}
    if tool in GUEST_BLOCKED_TOOLS:
        return f'访客发起的请求里不能使用 {tool}'
    if tool == 'Bash':
        return bash_denied(ti.get('command', ''), agent_home)
    if tool in READ_TOOLS:
        for key in ('file_path', 'path'):
            if ti.get(key) and not _inside(ti[key], agent_home):
                return '访客发起的请求里只能读数字人自己目录里的记忆和人设'
        pattern = ti.get('pattern') or ''
        if tool == 'Glob' and (pattern.startswith(('/', '~')) or '..' in pattern):
            return '访客发起的请求里只能在数字人自己的目录里查找'
        return ''
    if tool in WRITE_TOOLS:
        path = ti.get('file_path') or ti.get('notebook_path') or ''
        if not path or not _inside(path, os.path.join(agent_home, 'memory')):
            return '访客发起的请求里只能写 memory/ 下的记忆文件'
        return ''
    return ''


def cli_denied(cmd: str, args, turn, storage=None) -> str:
    """访客这一轮里调用 larkx 子命令是否放行; 拒绝时返回原因。"""
    if cmd not in GUEST_CLI_ALLOWED:
        return f'访客发起的请求不能使用 larkx {cmd}'
    current = turn.get('chat_id') or ''
    if cmd in ('messages', 'history') and str(getattr(args, 'chat_id', '')) != current:
        return '访客发起的请求只能查看当前会话的记录'
    if cmd == 'download':
        chat = getattr(args, 'chat', None)
        if not chat and storage is not None:
            from .storage.models import Message
            s = storage.Session()
            try:
                row = s.query(Message).filter_by(msg_id=str(args.msg_id)).first()
            finally:
                s.close()
            chat = row.chat_id if row else ''
        if str(chat or '') != current:
            return '访客发起的请求只能下载当前会话里的文件'
    return ''
