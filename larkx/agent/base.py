"""各 agent 后端共用的类型。

后端要实现: start / ask / interrupt / reset / close, 属性 busy、session_id;
可选 context_pressure() 返回距自动压缩的比例(0~1), 不支持时返回 None。
"""
import os
import shutil
import sys
from dataclasses import dataclass, field


class SessionCrashed(Exception):
    """agent 进程在一轮处理中途退出或失联; 会话已按原 id 重连, 这一轮的结果未知。"""


@dataclass
class TurnResult:
    subtype: str
    is_error: bool
    text: str
    duration_s: float
    tools: list = field(default_factory=list)


def resolve_agent_cli(cmd: str) -> str:
    path = shutil.which(os.path.expanduser(cmd))
    if not path:
        raise FileNotFoundError(f'找不到 agent 命令 {cmd}, 确认它在 PATH 里(LARKX_CLAUDE_CLI / LARKX_CODEX_CLI 也可以写完整路径)')
    return path


def agent_path() -> str:
    # agent 的 shell 里要能直接调用 larkx
    return os.path.dirname(sys.executable) + os.pathsep + os.environ.get('PATH', '')
