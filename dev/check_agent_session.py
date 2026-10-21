"""不接飞书, 单独检查常驻 agent 会话: 隔离、skill、larkx 可用性、拦截、重启恢复。

用法: [LARKX_AGENT_BACKEND=codex] .venv/bin/python dev/check_agent_session.py [临时根目录, 默认 /tmp/larkx-session-check]
会话和库都写在临时目录, 不影响真正的数字人主会话。
"""
import asyncio
import shutil
import sys
from pathlib import Path

from loguru import logger

from larkx.agent import create_session, ensure_agent_home
from larkx.config import load_config
from larkx.storage import Storage

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/larkx-session-check')
INIT_LINES = []


def _capture(message):
    if 'agent init' in message:
        INIT_LINES.append(message.strip())


async def ask(s, prompt, label):
    r = await s.ask(prompt)
    print(f'--- {label}: {r.subtype} {r.duration_s:.1f}s 工具={r.tools}\n{r.text[:400]}\n', flush=True)
    return r


async def main():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    cfg = load_config()
    cfg.update(agent_home=str(ROOT / 'agent'), agent_claude_dir=str(ROOT / 'claude'), agent_codex_dir=str(ROOT / 'codex'))
    print('后端:', cfg['agent_backend'])
    cfg['agent_fallback_model'] = cfg['agent_fallback_model'] or 'claude-sonnet-5-5'
    storage = Storage(f'sqlite:///{ROOT}/check.db')
    ensure_agent_home(cfg)
    logger.add(_capture, level='DEBUG')

    s = create_session(cfg, storage)
    await s.start()
    await ask(s, '用一句话说明你是谁、代表谁。', '人设')
    print('init:', INIT_LINES[:1], '\n')
    await ask(s, '用 Bash 依次执行下面三条命令, 然后逐条告诉我结果(输出或被拒绝的原因):\n'
                 '1) larkx chats --json --limit 1\n2) echo actor=$LARKX_ACTOR\n3) larkx api chats.PullChatsByIdsRequest',
              '工具与拦截')
    await ask(s, '记住暗号: 青松-58。只回复"好"。', '记忆写入')
    sid = s.session_id
    await s.close()

    s2 = create_session(cfg, storage)
    await s2.start()
    r = await ask(s2, '刚才让你记住的暗号是什么?只回复暗号。', '重启后恢复')
    print('会话 id 不变:', s2.session_id == sid, '| 暗号保留:', '青松-58' in r.text)
    await s2.close()


if __name__ == '__main__':
    asyncio.run(main())
