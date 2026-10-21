"""哪些消息交给数字人、控制命令解析、每条消息喂给 agent 的提示词。"""
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

CHAT_TYPE_NAMES = {1: 'p2p', 2: 'group', 3: 'topic_group'}
COMMANDS = ('clear', 'stop')


def should_handle(msg: dict, cfg: dict, my_user_id: str) -> bool:
    # 默认丢弃自己账号的消息防自回环; allow_self 放开时靠触发前缀阻断(agent 的回复被 CLI 禁止以前缀开头)
    if (not cfg.get('allow_self')) and str(msg.get('from_id')) == str(my_user_id):
        return False
    t = cfg.get('triggers') or {}
    content = (msg.get('content') or '').strip()
    chat_id = str(msg.get('chat_id'))
    sender_id = str(msg.get('from_id'))
    type_name = CHAT_TYPE_NAMES.get(msg.get('chat_type'), '')
    if t.get('prefix') and (not content.startswith(t['prefix'])):
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
    c = (content or '').strip()
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
