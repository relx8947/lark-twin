"""daemon 和 CLI、网页之间共享的状态, 以及时间解析。

daemon 每轮开始前写入 turn.json, 结束后删除。数字人调用 larkx 时, CLI 据此判断
这次发消息、建任务是否越权: 默认只能作用于触发这一轮的会话, 本人发起时才能跨会话。

relay/ 下登记经本人私聊中转的消息(见 LarkClient.send_msg 的 keep_unread), daemon 收到它们时跳过。
"""
import json
import os
import re
import time
from datetime import datetime, timedelta

from .config import DATA_DIR

STATE_DIR = DATA_DIR / 'state'
TURN_PATH = STATE_DIR / 'turn.json'
RELAY_DIR = STATE_DIR / 'relay'
# 补收漏推消息只看 7 天内有动静的会话, 登记留得比它久, 中转消息无论什么时候补收到都认得出来
RELAY_TTL = 8 * 86400
KINDS = ('message', 'task', 'say', 'system')


def write_turn(kind: str, chat_id: str = '', anchor: str = '', is_owner: bool = False, ref: str = ''):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {'kind': kind, 'chat_id': str(chat_id or ''), 'anchor': anchor or '', 'is_owner': bool(is_owner),
            'ref': str(ref or ''), 'pid': os.getpid(), 'started_at': time.time()}
    tmp = TURN_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    tmp.replace(TURN_PATH)


def clear_turn():
    TURN_PATH.unlink(missing_ok=True)


def read_turn():
    try:
        data = json.loads(TURN_PATH.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    try:
        os.kill(int(data.get('pid') or 0), 0)
    except (OSError, ValueError):
        return None  # 写入它的 daemon 已经退出
    return data


def mark_relay(cid: str, chat_id: str):
    """发中转消息前登记它的 cid: 每条一个文件(内容是中转会话 id), 多个进程同时写也不会互相覆盖。顺带清掉过期的。"""
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for p in RELAY_DIR.iterdir():
        try:
            if now - p.stat().st_mtime > RELAY_TTL:
                p.unlink()
        except FileNotFoundError:
            pass
    (RELAY_DIR / cid).write_text(str(chat_id), encoding='utf-8')


def is_relay(msg: dict) -> bool:
    """这条消息是不是登记过的中转消息(cid 和所在会话都对得上); 转发到目标会话的那份 cid 不同, 不算。"""
    cid = str(msg.get('cid') or '')
    if not re.fullmatch(r'[\w-]{1,128}', cid):
        return False
    try:
        return (RELAY_DIR / cid).read_text(encoding='utf-8') == str(msg.get('chat_id'))
    except (FileNotFoundError, NotADirectoryError):
        return False


def is_guest(turn) -> bool:
    """访客发起的这一轮: 别人的消息(含被接管会话)、别人安排的任务。本人发起、终端指令、维护任务都不算。"""
    return bool(turn) and turn.get('kind') in ('message', 'task') and not turn.get('is_owner')


def send_denied(target_chat: str, turn) -> str:
    """数字人给 target_chat 发消息是否越权; 越权时返回原因, 允许时返回空串。"""
    if not turn:
        return '当前没有正在处理的消息, 数字人不能主动发消息'
    if turn['kind'] == 'system':
        return '维护任务里不能发飞书消息'
    if turn['kind'] == 'say' or turn['is_owner']:
        return ''
    if str(target_chat) == turn['chat_id']:
        return ''
    return f'只能发到触发这一轮的会话 {turn["chat_id"] or "(无)"}; 给其他会话发消息需要本人发起'


DURATION_UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}


def parse_duration(text: str) -> int:
    """'90s' '30m' '2h' '1d' '1w', 也可以组合如 '1h30m'。"""
    parts = re.findall(r'(\d+)\s*([smhdw])', (text or '').strip().lower())
    if not parts or ''.join(n + u for n, u in parts) != re.sub(r'\s+', '', text.strip().lower()):
        raise ValueError(f'无法识别的时长 {text!r}, 例: 30m、2h、1d、1h30m')
    return sum(int(n) * DURATION_UNITS[u] for n, u in parts)


def parse_at(text: str, now: datetime = None) -> float:
    """'2026-10-22 09:00'、'10-22 09:00'、'09:00'(今天, 已过则明天)。返回 Unix 时间戳。"""
    now = now or datetime.now()
    s = (text or '').strip()
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    try:
        dt = datetime.strptime(f'{now.year}-{s}', '%Y-%m-%d %H:%M')
        return dt.timestamp()
    except ValueError:
        pass
    try:
        t = datetime.strptime(s, '%H:%M')
    except ValueError:
        raise ValueError(f'无法识别的时间 {text!r}, 例: 2026-10-22 09:00、10-22 09:00、09:00') from None
    dt = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if dt <= now:
        dt += timedelta(days=1)
    return dt.timestamp()


def next_due(due_at: float, every_sec: int, now: float) -> float:
    """周期任务的下一次时间: 跳过已经错过的周期。"""
    if due_at > now:
        return due_at
    missed = int((now - due_at) // every_sec) + 1
    return due_at + missed * every_sec
