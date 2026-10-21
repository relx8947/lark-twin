"""第 0 步验证: Agent SDK 常驻会话能力实测(不碰正式代码)。

用法: python dev/spike_sdk/spike.py [工作根目录, 默认 /tmp/larkx-spike]
需在去掉父会话 CLAUDE_CODE_* 变量的环境下运行, 见同目录 run.sh。
"""
import asyncio
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,
                              ResultMessage, SystemMessage, TextBlock, ToolUseBlock)

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/larkx-spike')
HOME = ROOT / 'agent-home'
CONF = ROOT / 'claude-config'
SID = str(uuid.uuid4())
T0 = time.monotonic()
RESULTS = {}
EVENTS = []


def log(*a):
    print(f'[{time.monotonic() - T0:6.1f}s]', *a, flush=True)


def setup():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    skill = HOME / '.claude/skills/larkx-demo'
    skill.mkdir(parents=True)
    CONF.mkdir(parents=True)
    (HOME / 'CLAUDE.md').write_text('# 数字人测试\n你的名字叫小 X, 工号 SPIKE-7731。\n', encoding='utf-8')
    (skill / 'SKILL.md').write_text(
        '---\nname: larkx-demo\ndescription: larkx 命令行工具的使用说明。用户问到 larkx 的版本或用法时使用。\n---\n'
        '# larkx\n当前版本号是 9.3.1-spike。\n', encoding='utf-8')


async def pre_tool(inp, tool_use_id, ctx):
    cmd = (inp.get('tool_input') or {}).get('command', '')
    EVENTS.append(('PreToolUse', inp.get('tool_name'), cmd))
    if 'lark api' in cmd:
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                       'permissionDecisionReason': '数字人禁止调用原始接口 lark api'}}
    return {}


async def post_tool(inp, tool_use_id, ctx):
    EVENTS.append(('PostToolUse', inp.get('tool_name'), (inp.get('tool_input') or {}).get('command', '')))
    return {}


async def pre_compact(inp, tool_use_id, ctx):
    EVENTS.append(('PreCompact', inp.get('trigger'), ''))
    return {}


def options(**kw):
    return ClaudeAgentOptions(
        cwd=str(HOME),
        setting_sources=['project'],
        system_prompt={'type': 'preset', 'preset': 'claude_code'},
        permission_mode='bypassPermissions',
        fallback_model='claude-sonnet-5-5',
        env={'CLAUDE_CONFIG_DIR': str(CONF), 'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'},
        hooks={'PreToolUse': [HookMatcher(matcher='Bash', hooks=[pre_tool])],
               'PostToolUse': [HookMatcher(matcher='Bash', hooks=[post_tool])],
               'PreCompact': [HookMatcher(hooks=[pre_compact])]},
        **kw)


class Probe:
    """单一消费协程: 持续读 receive_messages, 按 ResultMessage 切分每一轮。"""

    def __init__(self, client):
        self.client = client
        self.q = asyncio.Queue()
        self.init = None
        self.system = []
        self.models = set()
        self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            async for m in self.client.receive_messages():
                await self.q.put(m)
        except Exception as e:
            await self.q.put(e)

    async def turn(self, timeout=240):
        texts, tools = [], []
        while True:
            m = await asyncio.wait_for(self.q.get(), timeout)
            if isinstance(m, Exception):
                raise m
            if isinstance(m, SystemMessage):
                self.system.append(m.subtype)
                if m.subtype != 'init':
                    log(f'  system[{m.subtype}]', str(m.data)[:160])
                if m.subtype == 'init':
                    self.init = m.data
                elif m.subtype == 'compact_boundary':
                    log('收到 compact_boundary:', json.dumps(m.data.get('compact_metadata'), ensure_ascii=False))
            elif isinstance(m, AssistantMessage):
                self.models.add(getattr(m, 'model', None))
                for b in m.content:
                    if isinstance(b, TextBlock):
                        texts.append(b.text)
                    elif isinstance(b, ToolUseBlock):
                        tools.append((b.name, json.dumps(b.input, ensure_ascii=False)[:120]))
            elif isinstance(m, ResultMessage):
                return ''.join(texts).strip(), tools, m


async def ask(probe, prompt, label):
    t = time.monotonic()
    await probe.client.query(prompt)
    text, tools, res = await probe.turn()
    log(f'{label}: {time.monotonic() - t:.1f}s subtype={res.subtype} session={res.session_id[:8]} 工具={tools} 回复={text[:160]!r}')
    return text, res


async def phase_main():
    t = time.monotonic()
    client = ClaudeSDKClient(options(session_id=SID))
    await client.connect()
    probe = Probe(client)
    log(f'connect 耗时 {time.monotonic() - t:.1f}s')

    # T1 多轮共享上下文
    await ask(probe, '记住这个数字: 4187。只回复"记住了"。', 'T1a')
    text, res = await ask(probe, '我刚才让你记的数字是多少?只回复数字。', 'T1b')
    RESULTS['T1 多轮记忆'] = '4187' in text and res.session_id == SID
    init = probe.init or {}
    log('init 键:', sorted(init.keys()))
    log('init plugins:', init.get('plugins'), '| skills:', init.get('skills'), '| cwd:', init.get('cwd'))

    # T3 隔离: 只应看到项目 CLAUDE.md, 不应看到用户全局 CLAUDE.md(含 Nginx 规则)与插件
    text, _ = await ask(probe, '你叫什么名字, 工号多少?另外, 我有没有跟你约定过服务器上 Nginx 怎么管理?'
                               '按格式回答 "名字=<值>; 工号=<值>; Nginx 约定=有/无"。', 'T3')
    RESULTS['T3 加载项目 CLAUDE.md'] = 'SPIKE-7731' in text
    RESULTS['T3 未加载全局 CLAUDE.md(模型自述)'] = 'Nginx约定=无' in text.replace(' ', '')
    RESULTS['T3 未加载用户插件'] = not any('superpowers' in json.dumps(p) for p in (init.get('plugins') or []))

    # T4 skill 发现与使用
    text, _ = await ask(probe, 'larkx 现在是什么版本?只回复版本号。', 'T4')
    RESULTS['T4 skill 出现在 init'] = 'larkx-demo' in json.dumps(init, ensure_ascii=False)
    RESULTS['T4 skill 被使用'] = '9.3.1-spike' in text

    # T5 PreToolUse 在 bypassPermissions 下能否按命令内容拒绝; PostToolUse 能否用于审计
    EVENTS.clear()
    await ask(probe, "用 Bash 执行命令 echo 'lark api ping', 然后如实告诉我执行结果(成功/被拒绝及原因)。", 'T5a')
    denied = [e for e in EVENTS if e[0] == 'PreToolUse' and 'lark api' in e[2]]
    ran = [e for e in EVENTS if e[0] == 'PostToolUse' and 'lark api' in e[2]]
    await ask(probe, '用 Bash 执行 echo audit-ok, 只回复输出。', 'T5b')
    RESULTS['T5 hook 拦截生效'] = bool(denied) and not ran
    RESULTS['T5 PostToolUse 审计触发'] = any(e[0] == 'PostToolUse' and 'audit-ok' in e[2] for e in EVENTS)
    log('T5 hook 事件:', EVENTS)

    # T2 上一轮未结束时再 query: 排队、并入当前轮, 还是报错
    await client.query('用 Bash 执行 `sleep 8 && echo slept`, 然后只回复 A-DONE。')
    await asyncio.sleep(2)
    t = time.monotonic()
    try:
        await client.query('只回复 B-DONE。')
        log(f'T2 第二条 query 立即返回(耗时 {time.monotonic() - t:.2f}s), 未抛异常')
        q2_ok = True
    except Exception as e:
        log('T2 第二条 query 异常:', repr(e))
        q2_ok = False
    turns = []
    for _ in range(2):
        try:
            turns.append(await probe.turn(timeout=60))
        except asyncio.TimeoutError:
            break
    for i, (text, tools, res) in enumerate(turns):
        log(f'T2 第 {i + 1} 个 ResultMessage: subtype={res.subtype} 回复={text[:80]!r}')
    RESULTS['T2 忙时 query 不报错'] = q2_ok
    RESULTS['T2 行为'] = ('排队, 分两轮完成' if len(turns) == 2 and 'A-DONE' in turns[0][0] and 'B-DONE' in turns[1][0]
                        else f'{len(turns)} 轮: ' + ' | '.join(x[0][:40] for x in turns))

    # T2b interrupt: 打断长任务后会话仍可用
    await client.query('用 Bash 执行 `sleep 30 && echo late`, 然后回复 LATE-DONE。')
    await asyncio.sleep(4)
    await client.interrupt()
    text, tools, res = await probe.turn(timeout=60)
    log(f'T2b 打断后 ResultMessage: subtype={res.subtype} terminal_reason={getattr(res, "terminal_reason", None)} 回复={text[:60]!r}')
    text, _ = await ask(probe, '只回复 ALIVE。', 'T2b-after')
    RESULTS['T2b interrupt 后会话可用'] = 'ALIVE' in text and 'LATE-DONE' not in text

    # T8 上下文用量
    try:
        usage = await client.get_context_usage()
        log('T8 context usage:', str(usage)[:300])
        RESULTS['T8 get_context_usage 可用'] = True
    except Exception as e:
        log('T8 get_context_usage 异常:', repr(e))
        RESULTS['T8 get_context_usage 可用'] = False

    RESULTS['主阶段使用的模型'] = sorted(str(x) for x in probe.models)

    # T6 模拟崩溃: kill -9 CLI 子进程
    pids = subprocess.run(['pgrep', '-f', SID], capture_output=True, text=True).stdout.split()
    log('T6 找到 CLI 进程:', pids)
    for p in pids:
        subprocess.run(['kill', '-9', p])
    try:
        await asyncio.wait_for(client.disconnect(), 10)
    except Exception as e:
        log('T6 disconnect(崩溃后) 异常(预期内):', repr(e)[:120])
    probe.task.cancel()


async def phase_resume():
    t = time.monotonic()
    client = ClaudeSDKClient(options(resume=SID))
    await client.connect()
    probe = Probe(client)
    log(f'T6 resume connect 耗时 {time.monotonic() - t:.1f}s')
    text, res = await ask(probe, '最早我让你记的那个数字是多少?只回复数字。', 'T6')
    RESULTS['T6 崩溃后 resume 保留上下文'] = '4187' in text
    RESULTS['T6 resume 后 session_id 不变'] = res.session_id == SID
    log('T6 resume 后 session_id:', res.session_id)

    # T7 手动压缩: PreCompact hook / compact_boundary / 压缩后记忆
    EVENTS.clear()
    before = probe.system.count('compact_boundary')
    await ask(probe, '/compact', 'T7')
    RESULTS['T7 PreCompact hook 触发'] = any(e[0] == 'PreCompact' for e in EVENTS)
    RESULTS['T7 收到 compact_boundary'] = probe.system.count('compact_boundary') > before
    text, _ = await ask(probe, '压缩之后, 你还记得最早让你记的数字吗?只回复数字或"不记得"。', 'T7-after')
    RESULTS['T7 压缩后仍记得'] = '4187' in text
    RESULTS['期间使用的模型'] = sorted(str(x) for x in probe.models)
    await client.disconnect()


async def main():
    setup()
    log('工作根目录:', ROOT, '| session:', SID)
    try:
        await phase_main()
        await phase_resume()
    finally:
        tx = list(CONF.glob(f'projects/*/{SID}.jsonl'))
        log('会话记录位置:', [str(p) for p in tx])
        RESULTS['会话记录写在隔离目录'] = bool(tx)
        RESULTS['未写入全局 ~/.claude/projects'] = not list(Path.home().glob(f'.claude/projects/*/{SID}.jsonl'))
        print('\n===== 结果 =====')
        for k, v in RESULTS.items():
            print(f'{"✅" if v is True else ("❌" if v is False else "ℹ️ ")} {k}: {v}')


if __name__ == '__main__':
    asyncio.run(main())
