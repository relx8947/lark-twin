"""agent 每次调用工具前后的检查。

这里是 daemon 一侧的拦截, CLI 自己还有一层检查(见 larkx/cli.py 的 _agent_forbidden)。
按字符串匹配命令能被刻意绕过, 只用来挡住误操作, 不是安全边界。
"""
import json
import re

from claude_agent_sdk import HookMatcher
from loguru import logger

from .. import guest, state

# 原始网关接口、登录凭证、再起一个监听进程、管理自己的服务、以本人身份下指令、改 skill: 都不该由数字人自己做
FORBIDDEN_CMD = re.compile(r'\b(larkx|lark)\s+(api|auth|listen|daemon|service|doctor|say|skill)\b')


def _deny(reason: str) -> dict:
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                   'permissionDecisionReason': reason}}


def check_tool_use(tool_name: str, tool_input: dict):
    """返回拒绝原因; 放行时返回 None。"""
    blob = json.dumps(tool_input or {}, ensure_ascii=False)
    if 'credentials.json' in blob:
        return '数字人不能读取或修改飞书登录凭证'
    if tool_name == 'Bash' and FORBIDDEN_CMD.search((tool_input or {}).get('command', '')):
        return '数字人不能调用 larkx api / auth / listen / daemon / service / doctor / say / skill; 需要的能力请用 larkx 的其他子命令'
    return None


def make_pre_tool_use(agent_home: str, on_alert=None):
    async def _pre_tool_use(inp, tool_use_id, ctx):
        tool, ti = inp.get('tool_name'), inp.get('tool_input')
        reason = check_tool_use(tool, ti)
        turn = state.read_turn()
        guest_mode = guest.is_guest(turn)
        if not reason and guest_mode:
            reason = guest.tool_denied(tool, ti, agent_home)
        if reason:
            detail = json.dumps(ti, ensure_ascii=False)[:200]
            logger.warning(f'[agent] 已拒绝 {tool}: {reason} | {detail}')
            if guest_mode and on_alert:
                on_alert(f'访客发起的请求({turn.get("ref")})想让数字人调用 {tool}, 已拦截: {reason}。{detail[:120]}')
            return _deny(reason)
        return {}
    return _pre_tool_use


async def _post_tool_use(inp, tool_use_id, ctx):
    logger.info(f'[agent] $ {((inp.get("tool_input") or {}).get("command") or "")[:300]}')
    return {}


async def _pre_compact(inp, tool_use_id, ctx):
    logger.info(f'[agent] 开始压缩上下文 trigger={inp.get("trigger")}')
    return {}


def build_hooks(agent_home: str, on_alert=None) -> dict:
    return {
        'PreToolUse': [HookMatcher(hooks=[make_pre_tool_use(agent_home, on_alert)])],
        'PostToolUse': [HookMatcher(matcher='Bash', hooks=[_post_tool_use])],
        'PreCompact': [HookMatcher(hooks=[_pre_compact])],
    }
