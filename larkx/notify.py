"""给本人的通知: 抄送数字人对外发的消息, 以及各种告警。

默认发到本人和自己的私聊(用本人账号)。本人账号用不了时(比如飞书登录失效), 改用 LARKX_ALERT_CMD
指定的备用命令, 例如官方 lark-cli 的机器人身份。同一类告警在冷却时间内只发一次。
"""
import json
import shlex
import subprocess
import time

from loguru import logger

from . import state

NOTIFY_STATE = state.STATE_DIR / 'notify.json'


def _load() -> dict:
    try:
        return json.loads(NOTIFY_STATE.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict):
    NOTIFY_STATE.parent.mkdir(parents=True, exist_ok=True)
    NOTIFY_STATE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def _cooling(key: str, cooldown: int) -> bool:
    """同一个 key 在冷却时间内已经通知过时返回 True; 否则记下这次并返回 False。"""
    if not key or cooldown <= 0:
        return False
    data = _load()
    sent = data.setdefault('sent', {})
    now = time.time()
    if now - sent.get(key, 0) < cooldown:
        return True
    sent[key] = now
    _save(data)
    return False


def build_alert_command(template: str, text: str) -> list:
    """LARKX_ALERT_CMD 是一条命令, 其中的 {text} 会替换成通知内容(不经过 shell)。"""
    args = shlex.split(template)
    if '{text}' not in args:
        return args + [text]
    return [text if a == '{text}' else a for a in args]


def run_alert_command(cfg: dict, text: str) -> bool:
    template = cfg.get('alert_cmd') or ''
    if not template:
        return False
    try:
        r = subprocess.run(build_alert_command(template, text), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        logger.error(f'备用通知命令执行失败: {e!r}')
        return False
    if r.returncode != 0:
        logger.error(f'备用通知命令失败({r.returncode}): {(r.stderr or r.stdout).strip()[:200]}')
        return False
    return True


class Notifier:
    def __init__(self, cfg: dict, client=None):
        self.cfg = cfg
        self.client = client

    def owner_chat(self) -> str:
        """本人和自己的私聊 id: 配置优先, 否则向服务端取一次并缓存。"""
        if self.cfg.get('notify_chat'):
            return str(self.cfg['notify_chat'])
        data = _load()
        if data.get('owner_chat'):
            return data['owner_chat']
        if not self.client or not self.client.auth.user_id:
            return ''
        try:
            chat_id = self.client.create_chat(self.client.auth.user_id)
        except Exception as e:
            logger.warning(f'获取本人私聊失败: {e!r}')
            return ''
        if chat_id:
            data = _load()
            data['owner_chat'] = str(chat_id)
            _save(data)
        return str(chat_id or '')

    def send(self, text: str, key: str = '', cooldown: int = 0, fallback: bool = True) -> bool:
        """发到本人私聊; 失败且允许时改用备用命令。返回是否送达。"""
        if _cooling(key, cooldown):
            return False
        logger.info(f'[通知] {text[:200]}')
        chat = self.owner_chat()
        if chat and self.client:
            try:
                if self.client.send_msg(text, chat):
                    return True
            except Exception as e:
                logger.warning(f'通知发到本人私聊失败: {e!r}')
        return run_alert_command(self.cfg, text) if fallback else False

    def alert(self, text: str, key: str = '', cooldown: int = 600) -> bool:
        return self.send(f'⚠️ lark-twin：{text}', key=key, cooldown=cooldown)

    def fallback_alert(self, text: str, key: str = '', cooldown: int = 3600) -> bool:
        """本人账号用不了时(如登录失效), 直接走备用命令。"""
        if _cooling(key, cooldown):
            return False
        logger.warning(f'[告警] {text}')
        return run_alert_command(self.cfg, f'⚠️ lark-twin：{text}')


def should_mirror(target_chat: str, turn, owner_chat: str) -> bool:
    """数字人发出的消息是否要抄送给本人: 本人在原会话里亲自叫它、或发到本人私聊的, 本人已经看得到, 不必抄送。"""
    if not target_chat or str(target_chat) == str(owner_chat):
        return False
    if turn and turn.get('is_owner') and str(target_chat) == turn.get('chat_id'):
        return False
    return True
