"""哪些消息交给数字人、控制命令解析、每条消息喂给 agent 的提示词。"""
import re
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

from ..protocol.proto import proto_pb2 as P

CHAT_TYPE_NAMES = {1: 'p2p', 2: 'group', 3: 'topic_group'}
COMMANDS = ('clear', 'stop')
# 群里常见"@某人 /ai ..."的写法: 判断前缀前先去掉开头的 @提及(可以有多个)
LEADING_MENTIONS = re.compile(r'^(?:@[^\s/@]+\s*)+')


# 富文本里的元素类型: 5 是 @; 这些是纯文本类
AT_TAG = 5
TEXT_TAGS = (1, 3, 7, 8, 9, 17)


def strip_mentions(content: str) -> str:
    return LEADING_MENTIONS.sub('', (content or '').strip())


def leading_mention_ids(msg: dict):
    """消息开头 @ 的人的 user_id(@所有人 是 'all'); 拿不到富文本时返回 None。"""
    data = msg.get('content_data')
    rich = data.get('richText') if isinstance(data, dict) else None
    try:
        dictionary = rich['elements']['dictionary']
    except (TypeError, KeyError):
        return None
    ids = []
    for key in rich.get('elementIds') or sorted(dictionary, key=lambda k: int(k) if str(k).isdigit() else 0):
        el = dictionary.get(key) or {}
        prop = el.get('property')
        if not isinstance(prop, (bytes, bytearray)):
            return None
        tag = el.get('tag', 1)
        if tag == AT_TAG:
            ap = P.AtProperty()
            ap.ParseFromString(prop)
            ids.append(ap.userId)
        elif tag in TEXT_TAGS:
            tp = P.TextProperty()
            tp.ParseFromString(prop)
            if tp.content.strip():
                break
        else:
            break
    return ids


def addressed_to_me(msg: dict, my_user_id: str) -> bool:
    """开头带 @ 的消息, 只有 @ 到本人才算叫本人的数字人; 群里"@某人 /ai"多半是在叫那个人的数字人。"""
    if not (msg.get('content') or '').strip().startswith('@'):
        return True
    ids = leading_mention_ids(msg)
    if ids is None:
        return bool(msg.get('at_me'))
    return str(my_user_id) in ids


def should_handle(msg: dict, cfg: dict, my_user_id: str) -> bool:
    # 默认丢弃自己账号的消息防自回环; allow_self 放开时靠触发前缀阻断(agent 的回复被 CLI 禁止以前缀开头)
    if (not cfg.get('allow_self')) and str(msg.get('from_id')) == str(my_user_id):
        return False
    t = cfg.get('triggers') or {}
    content = strip_mentions(msg.get('content'))
    chat_id = str(msg.get('chat_id'))
    sender_id = str(msg.get('from_id'))
    type_name = CHAT_TYPE_NAMES.get(msg.get('chat_type'), '')
    if t.get('prefix') and (not content.startswith(t['prefix'])):
        return False
    if not addressed_to_me(msg, my_user_id):
        return False
    chat_types = t.get('chat_types') or []
    if chat_types and type_name not in chat_types:
        return False
    include = [str(x) for x in t.get('include_chats') or []]
    if include and chat_id not in include:
        return False
    exclude = [str(x) for x in t.get('exclude_chats') or []]
    if exclude and chat_id in exclude:
        return False
    senders = [str(x) for x in t.get('include_senders') or []]
    if senders and sender_id not in senders:
        return False
    keywords = t.get('keywords') or []
    if keywords and (not any((kw in content for kw in keywords))):
        return False
    if t.get('group_at_only') and type_name in ('group', 'topic_group') and (not msg.get('at_me')):
        return False
    return True


def strip_prefix(content: str, prefix: str) -> str:
    c = strip_mentions(content)
    if prefix and c.startswith(prefix):
        c = c[len(prefix):].lstrip()
    return c


def parse_command(body: str):
    """body 已去掉触发前缀。返回 (命令, 余下内容); 不是命令时返回 (None, body)。"""
    for cmd in COMMANDS:
        word = '/' + cmd
        if body == word:
            return cmd, ''
        if body.startswith(word) and body[len(word)] in (' ', '\t', '\n'):
            return cmd, body[len(word):].strip()
    return None, body


def _ts(msg: dict) -> str:
    t = msg.get('create_time')
    if not t:
        return ''
    try:
        return datetime.fromtimestamp(int(t)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(t)


def build_prompt(msg: dict, body: str, chat_name: str = '', sender_name: str = '', is_owner: bool = False,
                 note: str = '') -> str:
    chat_id = str(msg.get('chat_id'))
    anchor = msg.get('anchor') or ''
    type_name = CHAT_TYPE_NAMES.get(msg.get('chat_type'), str(msg.get('chat_type') or ''))
    lines = ['<message>',
             f'  <chat id={quoteattr(chat_id)} name={quoteattr(chat_name)} type={quoteattr(type_name)} anchor={quoteattr(anchor)}/>',
             f'  <sender id={quoteattr(str(msg.get("from_id")))} name={quoteattr(sender_name)} owner={quoteattr(str(is_owner).lower())}/>',
             f'  <msg id={quoteattr(str(msg.get("msg_id") or ""))}/>']
    if msg.get('at_me'):
        lines.append('  <at_me>true</at_me>')
    lines.append(f'  <time>{escape(_ts(msg))}</time>')
    lines.append(f'  <content>{escape(body)}</content>')
    lines.append('</message>')
    reply = f'larkx send {chat_id}' + (f' --root {anchor}' if anchor else '') + " - <<'EOF'"
    tail = f'\n回复这条消息用: {reply} ... EOF。不需要回复就直接结束。'
    if note:
        tail = f'\n({note})' + tail
    return '\n'.join(lines) + '\n' + tail


def _fmt_ts(ts) -> str:
    return _ts({'create_time': ts}) if ts else ''


def build_task_prompt(task, now: float) -> str:
    attrs = (f'id={quoteattr(str(task.id))} due={quoteattr(_fmt_ts(task.due_at))} now={quoteattr(_fmt_ts(now))} '
             f'chat={quoteattr(task.chat_id or "")} anchor={quoteattr(task.anchor or "")} '
             f'by_owner={quoteattr(str(bool(task.by_owner)).lower())}')
    if task.every_sec:
        attrs += f' every_sec={quoteattr(str(task.every_sec))}'
    tail = '这是之前安排的任务, 现在到时间了, 按内容去做。'
    if task.chat_id:
        reply = f'larkx send {task.chat_id}' + (f' --root {task.anchor}' if task.anchor else '') + " - <<'EOF'"
        tail += f'需要在原会话里通知时用: {reply} ... EOF。'
    return f'<task {attrs}>{escape(task.prompt)}</task>\n\n{tail}'


def build_say_prompt(text: str) -> str:
    return f'<say>{escape(text)}</say>\n\n本人在终端里跟你说话。你最后的文字回答会显示给本人。'


def build_system_prompt(text: str) -> str:
    return f'<system>{escape(text)}</system>'
