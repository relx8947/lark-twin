from .base import SessionCrashed, TurnResult, resolve_agent_cli
from .home import ensure_agent_home

BACKENDS = ('claude', 'codex')


def create_session(cfg: dict, storage):
    """按 LARKX_AGENT_BACKEND 创建数字人会话。"""
    backend = cfg.get('agent_backend') or 'claude'
    if backend == 'codex':
        from .codex import CodexSession
        return CodexSession(cfg, storage)
    if backend == 'claude':
        from .claude import ClaudeSession
        return ClaudeSession(cfg, storage)
    raise ValueError(f'未知的 agent 后端 {backend}, 可选: {", ".join(BACKENDS)}')
