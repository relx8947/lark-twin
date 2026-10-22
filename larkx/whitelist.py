"""唤起白名单: 除了本人, 还有谁能叫起数字人。命令行(larkx whitelist)和网页控制台共用同一张表。

规则(daemon 每条消息都现查, 改了立即生效, 不用重启):
- 本人一直能唤起;
- 白名单非空: 只有本人和白名单里的人能唤起, 别人的 /ai 一律不理;
- 白名单为空: 看 LARKX_TRIGGER_OWNER_ONLY, 打开时只有本人, 关着时所有人;
- 群里照旧要 @ 本人(LARKX_TRIGGER_GROUP_AT_ONLY); 接管中的会话、定时任务、larkx say 不受影响。
没权限的人想唤起时(私聊发 /ai, 群里 @ 本人发 /ai), daemon 马上回一句 LARKX_DENY_REPLY, 不经过 agent;
同一会话同一个人 LARKX_DENY_REPLY_COOLDOWN 秒内只回一次。见 triggers.denied_attempt 和 Daemon._deny。
白名单里的人和其他人一样按访客对待(见 guest.py), 不会因此拿到本人的权限。

只按 user_id 匹配, 从不按名字。白名单只有本人能改, 数字人自己不能改(见 Storage.whitelist_add 和 cli.py)。
"""
import html
import re

HIGHLIGHT = re.compile(r'</?h>')
USER_ID = re.compile(r'\d{6,}')


def is_user_id(text: str) -> bool:
    return bool(USER_ID.fullmatch((text or '').strip()))


def may_invoke(sender_id: str, triggers: dict, whitelist) -> bool:
    """别人(不是本人)发的消息能不能唤起数字人; 群里要不要 @ 本人另外判断。"""
    if whitelist:
        return str(sender_id) in {str(x) for x in whitelist}
    return not triggers.get('owner_only')


def rule_sentence(count: int, triggers: dict, below: bool = True) -> str:
    """当前生效的规则, 一句话。below: 网页上名单就在这句话下面。"""
    group = '（群里要 @ 你）' if triggers.get('group_at_only') else ''
    if count:
        return f'只有你和{"下面" if below else "白名单里的"} {count} 个人能唤起数字人{group}'
    if triggers.get('owner_only'):
        return '只有你能唤起数字人'
    return f'所有人都能唤起数字人{group}'


def deny_text(cfg: dict, owner: str = '') -> str:
    """没权限的人想唤起时自动回的那句话; 关掉(LARKX_DENY_REPLY 为空)时是空字符串。{owner} 换成本人的名字。"""
    text = (cfg.get('deny_reply') or '').strip()
    return text.replace('{owner}', owner or '本人') if text else ''


def deny_sentence(cfg: dict, count: int, owner: str = '') -> str:
    """规则下面的一句补充: 没权限的人发 /ai 会怎样。所有人都能唤起时没人会被拒, 返回空字符串。"""
    triggers = cfg.get('triggers') or {}
    if not count and not triggers.get('owner_only'):
        return ''
    text = deny_text(cfg, owner)
    if not text:
        return '没权限的人发 /ai 不会收到任何回复（LARKX_DENY_REPLY 为空）'
    cooldown = int(cfg.get('deny_reply_cooldown') or 0)
    if cooldown <= 0:
        limit = '每次都回'
    elif cooldown % 60:
        limit = f'{cooldown} 秒内同一人只回一次'
    else:
        limit = f'{cooldown // 60} 分钟内同一人只回一次'
    return f'没权限的人发 /ai 会收到：{text}（{limit}）'


def _clean(text) -> str:
    """去掉搜索结果里高亮关键词的 <h> 标记, 还原 &amp; 这类转义(页面上用 textContent 显示, 不会当成 HTML)。"""
    return html.unescape(HIGHLIGHT.sub('', text or '')).strip()


def search_people(client, query: str, limit: int = 20) -> list:
    """按名字搜人(只要用户, 不要群): 带上部门和头像, 同名的人靠这些区分。"""
    people = []
    for r in client.search(query) or []:
        uid = str(r.get('id') or '')
        if r.get('type') == 'user' and uid.isdigit():
            people.append({'user_id': uid, 'name': _clean(r.get('title')), 'department': _clean(r.get('summary')),
                           'avatar_key': r.get('avatar_key') or ''})
    return people[:limit]


def lookup_user(client, user_id: str):
    """按 user_id 找人: 先取名字, 再按名字搜一次补上部门和头像。找不到这个人时返回 None。"""
    name = client.get_user_name(str(user_id), '0')
    if not name:
        return None
    for p in search_people(client, name):
        if p['user_id'] == str(user_id):
            return p
    return {'user_id': str(user_id), 'name': name, 'department': '', 'avatar_key': ''}


def entry_dict(row, region=None) -> dict:
    d = {'user_id': row.user_id, 'name': row.name, 'department': row.department, 'avatar_key': row.avatar_key,
         'added_by': row.added_by, 'added_at': row.added_at}
    if region is not None:
        d['avatar'] = region.avatar_url(row.avatar_key)
    return d


def label(p) -> str:
    """一行说清是谁: 名字 · 部门 · user_id。"""
    get = p.get if isinstance(p, dict) else lambda k: getattr(p, k, '')
    return ' · '.join(x for x in (get('name') or '(无名字)', get('department'), f'user_id {get("user_id")}') if x)
