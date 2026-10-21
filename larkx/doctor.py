"""larkx doctor: 部署前后的一键检查, 每项给出结论和修复办法。"""
import asyncio
import os
import tempfile

from . import service
from .config import load_config

OK, WARN, FAIL = 'ok', 'warn', 'fail'


def _check(name, level, detail, fix=''):
    return {'name': name, 'level': level, 'detail': detail, 'fix': fix}


def check_login():
    from .protocol import LarkAuth
    auth = LarkAuth()
    ok, msg = auth.validate(fetch_profile=True)
    if not ok:
        return _check('飞书登录', FAIL, msg, '运行 larkx auth qr 扫码登录')
    auth.save()
    return _check('飞书登录', OK, f'user_id={auth.user_id} region={auth.region.name}')


def check_config(cfg):
    out = []
    prefix = (cfg.get('triggers') or {}).get('prefix') or ''
    if cfg.get('allow_self') and not prefix:
        out.append(_check('触发规则', FAIL, '允许自己触发但没有触发前缀, 数字人的回复会再次触发自己',
                          '在 .env 设置 LARKX_TRIGGER_PREFIX, 或关闭 LARKX_ALLOW_SELF'))
    elif not prefix:
        out.append(_check('触发规则', WARN, '没有触发前缀, 所有符合规则的消息都会交给数字人', '在 .env 设置 LARKX_TRIGGER_PREFIX=/ai'))
    else:
        out.append(_check('触发规则', OK, f'前缀 {prefix}  允许自己触发={cfg.get("allow_self")}'))
    if cfg.get('agent_fallback_model'):
        out.append(_check('备用模型', OK, cfg['agent_fallback_model']))
    else:
        out.append(_check('备用模型', WARN, '未设置。主模型被安全策略拦截时会切到 Claude Code 默认备用模型, 接入点没有这个模型时会话会一直失败',
                          '在 .env 设置 LARKX_AGENT_FALLBACK_MODEL 为当前接入点可用的模型'))
    return out


def check_agent_cli(cfg):
    from .agent.session import resolve_agent_cli
    if cfg.get('agent_cli'):
        try:
            return _check('agent 命令', OK, resolve_agent_cli(cfg['agent_cli']))
        except FileNotFoundError as e:
            return _check('agent 命令', FAIL, str(e), '把 LARKX_AGENT_CLI 改成完整路径')
    if os.environ.get('ANTHROPIC_AUTH_TOKEN') or os.environ.get('ANTHROPIC_API_KEY'):
        return _check('agent 命令', OK, 'Agent SDK 自带 claude, 认证取 .env / 环境里的 ANTHROPIC_* 变量')
    return _check('agent 命令', WARN, '未设置 LARKX_AGENT_CLI, 环境里也没有 ANTHROPIC_* 认证; 交给 systemd 后很可能调不通模型',
                  '在 .env 设置 LARKX_AGENT_CLI 为平时启动 Claude Code 的命令(如 claude 或 ccd), 或填 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN')


async def _probe_model(cfg):
    from .agent import AgentSession, ensure_agent_home
    from .storage import Storage
    with tempfile.TemporaryDirectory(prefix='larkx-doctor-') as tmp:
        c = dict(cfg, agent_home=f'{tmp}/agent', agent_claude_dir=f'{tmp}/claude')
        ensure_agent_home(c)
        s = AgentSession(c, Storage(f'sqlite:///{tmp}/doctor.db'))
        await s.start()
        try:
            return await s.ask('这是连通性检查, 只回复 OK 两个字母。')
        finally:
            await s.close()


def check_model(cfg):
    try:
        r = asyncio.run(_probe_model(cfg))
    except Exception as e:
        return _check('模型调用', FAIL, f'{e!r}'[:300], '检查 LARKX_AGENT_CLI 或 ANTHROPIC_* 认证, 再运行 larkx doctor')
    if 'OK' in r.text.upper():
        return _check('模型调用', OK, f'{r.duration_s:.1f}s')
    return _check('模型调用', FAIL, f'{r.subtype}: {r.text[:200]}', '检查模型接入点和 key 是否可用')


def check_service():
    if not service.available():
        return [_check('后台服务', WARN, '本机没有 systemd, 需要自己托管 larkx daemon')]
    st = service.status()
    out = []
    if not st['installed']:
        out.append(_check('后台服务', WARN, '未安装', '运行 larkx service install && larkx service start'))
    elif not st['active']:
        out.append(_check('后台服务', WARN, '已安装但未运行', '运行 larkx service start, 失败时看 larkx service logs'))
    else:
        out.append(_check('后台服务', OK, f'运行中 pid={st["main_pid"]} 开机自启={st["enabled"]}'))
    stray = service.stray_daemons()
    if stray:
        out.append(_check('重复进程', WARN, f'还有不归 systemd 管的 larkx daemon: {stray}, 同一账号会出现两个数字人',
                          f'确认后结束这些进程: kill {" ".join(map(str, stray))}'))
    return out


def run(skip_model=False) -> list:
    cfg = load_config()
    checks = [check_login(), *check_config(cfg), check_agent_cli(cfg)]
    if not skip_model and checks[-1]['level'] != FAIL:
        checks.append(check_model(cfg))
    checks.extend(check_service())
    return checks
