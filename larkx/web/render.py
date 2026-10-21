"""把一条消息整理成网页显示用的结构。只输出纯数据, 前端一律用 textContent / createElement 渲染, 不拼 HTML。

文字片段(segments)只有这几种, 字段都是字符串、数字或布尔:
  {'type': 'text', 'text', 可选 'bold' 'italic' 'underline' 'strike' 'code'}
  {'type': 'link', 'text', 'href'}        href 只放行 http/https, 别的协议(javascript: 等)退成普通文字
  {'type': 'at', 'name', 'me'}             me: @ 的是本人或所有人, 页面上更醒目
  {'type': 'image', 'key', 'w', 'h'}       富文本里的图片, 经 /api/im/media 代理显示
文字里的表情(如 [微笑])保持文字, 不引入表情图片。
"""
import json
import re

from ..protocol.media import extract_resource_key
from ..protocol.proto import decoders

MAX_TEXT = 20000
SAFE_KEY = re.compile(r'[\w\-.]{1,200}')
SAFE_HREF = re.compile(r'https?://[^\s\x00-\x1f\x7f]{1,4000}', re.I)
# 消息纯文本里的表情代码, 用来给富文本里的表情元素找回它的文字写法
EMOJI_CODE = re.compile(r'\[[^\[\]\s]{1,12}\]')
# 服务端对不认识的卡片只给这句提示(没有卡片内容)
CARD_UPGRADE_HINT = 'Upgrade to the latest app version'

# 富文本元素类型(RichTextElement.Tag)
TEXT, IMG, P, FIGURE, AT, A, B, I, U, EMOTION = 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
DIV, LINK, MEDIA, DOCS, UL, OL, LI, QUOTE, CODE, CODE_BLOCK, HR = 14, 17, 18, 22, 26, 27, 28, 29, 30, 31, 32
REACTION, MENTION, CODE_BLOCK_V2, TABLE_ROW, TABLE_CELL, MARKDOWN = 35, 36, 37, 40, 41, 49
HEADINGS = (23, 24, 25, 44, 45, 46)
# 块级元素: 前后各自成行
BLOCK_TAGS = {P, FIGURE, DIV, DOCS, UL, OL, LI, QUOTE, CODE_BLOCK, CODE_BLOCK_V2, HR, TABLE_ROW, 39, 42, 43, MARKDOWN,
              *HEADINGS}
STYLE_FLAGS = ('bold', 'italic', 'underline', 'strike', 'code')


class _Out:
    """依次收集片段; 块级元素前后自动换行。"""

    def __init__(self):
        self.segs = []
        self.pending_break = False

    def brk(self):
        if self.segs:
            self.pending_break = True

    def add(self, seg: dict):
        if seg.get('type') == 'text' and not seg.get('text'):
            return
        if self.pending_break:
            self.pending_break = False
            last = self.segs[-1] if self.segs else None
            if last is not None and not (last.get('type') == 'text' and last['text'].endswith('\n')):
                self.segs.append({'type': 'text', 'text': '\n'})
        self.segs.append(seg)

    def text(self, text, flags=None):
        if isinstance(text, str) and text:
            self.add({'type': 'text', 'text': text, **{k: True for k in STYLE_FLAGS if (flags or {}).get(k)}})


# ---- 文本 / 富文本消息 ----

def _prop(el: dict, tag: int) -> dict:
    prop = el.get('prop')
    if prop is None and isinstance(el.get('property'), (bytes, bytearray)) and el['property']:
        try:
            prop = decoders.element_prop(tag, bytes(el['property']))
        except Exception:
            prop = None
    return prop if isinstance(prop, dict) else {}


def _flags(tag: int, style: dict, inherited: dict) -> dict:
    flags = dict(inherited)
    deco = str(style.get('textDecoration') or '')
    if tag == B or tag in HEADINGS or style.get('fontWeight') == 'bold':
        flags['bold'] = True
    if tag == I or style.get('fontStyle') == 'italic':
        flags['italic'] = True
    if tag == U or 'underline' in deco:
        flags['underline'] = True
    if 'line-through' in deco or 'lineThrough' in deco:
        flags['strike'] = True
    if tag == CODE:
        flags['code'] = True
    return flags


def rich_segments(rich_text: dict, plain: str = '') -> list:
    """富文本(TextContent/PostContent 的 richText)按元素树依次展开成片段; plain 是消息自带的纯文本, 用来还原表情的文字。"""
    try:
        dictionary = rich_text['elements']['dictionary']
    except (TypeError, KeyError):
        return []
    if not isinstance(dictionary, dict):
        return []
    roots = [k for k in (rich_text.get('elementIds') or []) if k in dictionary]
    if not roots:
        children = {c for el in dictionary.values() if isinstance(el, dict) for c in (el.get('childIds') or [])}
        roots = [k for k in dictionary if k not in children]
        try:
            roots.sort(key=int)
        except ValueError:
            pass
    out, seen = _Out(), set()
    lists = []  # 正在展开的列表: [类型, 已编号到几]

    def walk(key, flags, href=''):
        el = dictionary.get(key)
        if key in seen or not isinstance(el, dict):
            return
        seen.add(key)
        tag = decoders.enum_to_int(el.get('tag', TEXT))
        style = el.get('style') if isinstance(el.get('style'), dict) else {}
        flags = _flags(tag, style, flags)
        prop = _prop(el, tag)
        block = tag in BLOCK_TAGS
        if block:
            out.brk()
        if tag == LI:
            if lists and lists[-1][0] == OL:
                lists[-1][1] += 1
                out.text(f'{lists[-1][1]}. ')
            else:
                out.text('• ')
        if tag in decoders.TEXT_LIKE_TAGS or tag in (MENTION, MARKDOWN, CODE_BLOCK, CODE_BLOCK_V2):
            text = prop.get('content') or ''
            if href and text:
                out.add({'type': 'link', 'text': text, 'href': href})
            else:
                out.text(text, dict(flags, code=True) if tag in (CODE_BLOCK, CODE_BLOCK_V2) else flags)
        elif tag == A:
            link = prop.get('href') or ''
            out.add({'type': 'link', 'text': prop.get('textContent') or prop.get('content') or link, 'href': link})
        elif tag == AT:
            out.add({'type': 'at', 'name': prop.get('content') or '', 'user_id': prop.get('userId') or ''})
        elif tag in (EMOTION, REACTION):
            out.add({'type': 'emotion', 'key': prop.get('key') or ''})
        elif tag == IMG:
            out.add({'type': 'image', 'key': prop.get('key') or '', 'w': prop.get('width') or 0, 'h': prop.get('height') or 0})
        elif tag == MEDIA:
            out.text('[视频]')
        elif tag == HR:
            out.text('————————')
        elif tag == TABLE_CELL:
            out.text(' ')
        elif tag == LINK:
            href = prop.get('url') or href
        if tag in (UL, OL):
            lists.append([tag, int(prop.get('start') or 1) - 1])
        for child in el.get('childIds') or []:
            walk(child, flags, href)
        if tag in (UL, OL):
            lists.pop()
        if block:
            out.brk()

    for root in roots:
        walk(root, {})
    return _emotion_text(out.segs, plain or '')


def _emotion_text(segs: list, plain: str) -> list:
    """富文本里的表情元素只有内部 key, 从纯文本里按顺序找回它的文字写法(如 [微笑]); 找不到就用 key。"""
    cursor, out = 0, []
    for seg in segs:
        if seg['type'] == 'emotion':
            m = EMOJI_CODE.search(plain, cursor)
            if m:
                cursor = m.end()
                text = m.group()
            else:
                name = re.sub(r'^Lark_Emoji_|_\d+$', '', seg.get('key') or '')
                text = f'[{name}]' if name else '[表情]'
            out.append({'type': 'text', 'text': text})
            continue
        shown = seg.get('text') or seg.get('name') or ''
        if shown:
            at = plain.find(shown, cursor)
            if at >= 0:
                cursor = at + len(shown)
        out.append(seg)
    return out


# ---- 卡片 ----

# 卡片 JSON 里这些字段是同一内容的另一份(降级版、旧版写法)或纯装饰, 不展开
CARD_SKIP_KEYS = {'fallback', 'markdownElements', 'icon', 'alt', 'textStyle', 'expandedTitle', 'behaviors',
                  'confirm', 'placeholder', 'i18nElements', 'i18nContent'}


CARD_INPUT_TAGS = {'overflow', 'date_picker', 'datetime_picker', 'input', 'checker', 'person_list'}


def _card_flags(prop: dict) -> dict:
    attrs = (prop.get('textStyle') or {}).get('attributes') if isinstance(prop.get('textStyle'), dict) else None
    attrs = [str(a) for a in attrs or []]
    return {'bold': 'bold' in attrs, 'italic': 'italic' in attrs, 'strike': 'strikethrough' in attrs,
            'underline': 'underline' in attrs}


def _button_url(prop: dict) -> str:
    for action in prop.get('actions') or []:
        a = (action or {}).get('action') if isinstance(action, dict) else None
        if isinstance(a, dict):
            for k in ('url', 'pcURL', 'pcUrl'):
                if isinstance(a.get(k), str) and a[k]:
                    return a[k]
    return ''


def _card_walk(node, out: _Out):
    if isinstance(node, list):
        for x in node:
            _card_walk(x, out)
        return
    if not isinstance(node, dict):
        return
    tag = node.get('tag')
    prop = node.get('property') if isinstance(node.get('property'), dict) else {}
    if tag is None:
        for k, v in node.items():
            if k not in CARD_SKIP_KEYS:
                _card_walk(v, out)
        return
    content = prop.get('content') if isinstance(prop.get('content'), str) else node.get('content')
    if tag in ('plain_text', 'text', 'lark_md'):
        out.text(content, _card_flags(prop))
    elif tag == 'code_span':
        out.text(content, {'code': True})
    elif tag == 'link':
        url = prop.get('url')
        if isinstance(url, dict):
            url = url.get('url') or url.get('pcURL') or ''
        url = url if isinstance(url, str) else ''
        out.add({'type': 'link', 'text': content or url, 'href': url})
    elif tag == 'at':
        name = content or prop.get('userName') or ''
        uid = str(prop.get('userID') or prop.get('userId') or '')
        out.add({'type': 'at', 'name': name or ('@所有人' if uid == 'all' else '@某人'), 'user_id': uid})
    elif tag == 'br':
        out.add({'type': 'text', 'text': '\n'})
    elif tag == 'hr':
        out.brk()
        out.text('————————')
        out.brk()
    elif tag in ('img', 'image', 'standard_icon', 'custom_icon', 'img_combination', 'chart', 'audio', 'video'):
        return
    elif tag in CARD_INPUT_TAGS or tag.startswith(('select_', 'multi_select_', 'picker_')):
        # 下拉框、日期选择、输入框之类的交互组件: 只显示提示文字, 不展开选项
        label = _plain(prop.get('placeholder'))
        if label:
            out.text(f'[{label}]')
    elif tag == 'button':
        sub = _Out()
        _card_walk(prop.get('text'), sub)
        label = ''.join(s.get('text', '') for s in sub.segs).strip()
        url = _button_url(prop)
        if label and url:
            out.add({'type': 'link', 'text': f'[{label}]', 'href': url})
        elif label:
            out.text(f'[{label}]')
    elif tag == 'code_block':
        lines = []
        for line in prop.get('contents') or []:
            lines.append(''.join(str(c.get('content') or '') for c in (line or {}).get('contents') or [] if isinstance(c, dict)))
        out.brk()
        out.text('\n'.join(lines), {'code': True})
        out.brk()
    elif tag == 'list':
        for n, item in enumerate(prop.get('items') or [], 1):
            if not isinstance(item, dict):
                continue
            out.brk()
            out.text(f'{n}. ' if item.get('type') == 'ol' else '• ')
            _card_walk(item.get('elements'), out)
        out.brk()
    else:
        # 其余都当容器: div、markdown、column_set、column、note、collapsible_panel、action、form ...
        out.brk()
        for k, v in prop.items():
            if k not in CARD_SKIP_KEYS:
                _card_walk(v, out)
        for k in ('elements', 'text'):
            if k in node and k not in prop:
                _card_walk(node[k], out)
        out.brk()


def _plain(node) -> str:
    out = _Out()
    _card_walk(node, out)
    return ''.join(s.get('text') or s.get('name') or '' for s in out.segs).strip()


def card_view(d: dict) -> dict:
    """卡片: 标题 + 正文片段。优先用 jsonCard(新版正文在 newBody, body 那时只是给旧客户端的降级提示), 其次是卡片富文本。"""
    d = d if isinstance(d, dict) else {}
    header = d.get('cardHeader') if isinstance(d.get('cardHeader'), dict) else {}
    title = header.get('title') or header.get('mainTitle') or ''
    subtitle = header.get('subtitle') or ''
    theme = header.get('theme') or ''
    segs = []
    try:
        card = json.loads(d['jsonCard']) if isinstance(d.get('jsonCard'), str) and d['jsonCard'] else None
    except ValueError:
        card = None
    if isinstance(card, dict):
        head = (card.get('header') or {}).get('property') if isinstance(card.get('header'), dict) else None
        if isinstance(head, dict):
            title = _plain(head.get('title')) or title
            subtitle = _plain(head.get('subtitle')) or subtitle
            theme = head.get('template') or theme
        out = _Out()
        _card_walk(card.get('newBody') or card.get('body'), out)
        segs = out.segs
        if not _has_text(segs):
            summary = (card.get('config') or {}).get('summary') if isinstance(card.get('config'), dict) else None
            if isinstance(summary, dict) and isinstance(summary.get('content'), str):
                segs = [{'type': 'text', 'text': summary['content']}]
    if not _has_text(segs):
        segs = rich_segments(d.get('richtext'))
        if CARD_UPGRADE_HINT in ''.join(s.get('text', '') for s in segs):
            segs = [{'type': 'text', 'text': '这张卡片暂时不能在网页上显示，请在飞书客户端查看'}]
    if not _has_text(segs) and d.get('cardDesc'):
        segs = [{'type': 'text', 'text': str(d['cardDesc'])}]
    segs = [s for s in segs if s.get('type') != 'image']
    theme = theme if isinstance(theme, str) and re.fullmatch(r'[a-z]{1,16}', theme) else ''
    return {'title': str(title or ''), 'subtitle': str(subtitle or ''), 'theme': theme, 'segments': segs}


def _has_text(segs) -> bool:
    return any((s.get('text') or s.get('name') or '').strip() for s in segs or [])


# ---- 整条消息 ----

def finish(segs: list, me_id: str) -> list:
    """最后一道: 只放行约定的几种片段和字段, 链接只留 http/https, 合并相邻同样式的文字, 去掉首尾空行, 限制总长度。"""
    out, total = [], 0
    for seg in segs or []:
        kind = seg.get('type')
        if kind == 'link':
            href = str(seg.get('href') or '').strip()
            text = str(seg.get('text') or href)
            seg = {'type': 'link', 'text': text, 'href': href} if SAFE_HREF.fullmatch(href) else {'type': 'text', 'text': text}
        elif kind == 'at':
            name = str(seg.get('name') or '').strip() or '@'
            uid = str(seg.get('user_id') or '')
            seg = {'type': 'at', 'name': name if name.startswith('@') else '@' + name,
                   'me': bool(uid) and (uid == str(me_id) or uid == 'all')}
        elif kind == 'image':
            key = str(seg.get('key') or '')
            if not SAFE_KEY.fullmatch(key):
                seg = {'type': 'text', 'text': '[图片]'}
            else:
                seg = {'type': 'image', 'key': key, 'w': _int(seg.get('w')), 'h': _int(seg.get('h'))}
        else:
            seg = {'type': 'text', 'text': str(seg.get('text') or ''),
                   **{k: True for k in STYLE_FLAGS if seg.get(k)}}
        text = seg.get('text') or seg.get('name') or ''
        if total >= MAX_TEXT:
            break
        if len(text) > MAX_TEXT - total:
            seg[('text' if 'text' in seg else 'name')] = text[:MAX_TEXT - total] + '…'
        total += len(text)
        prev = out[-1] if out else None
        if (prev and prev['type'] == 'text' and seg['type'] == 'text'
                and all(prev.get(k) == seg.get(k) for k in STYLE_FLAGS)):
            prev['text'] += seg['text']
        else:
            out.append(seg)
    # 首尾的空白换行没有意义
    while out and out[0]['type'] == 'text' and not out[0]['text'].strip():
        out.pop(0)
    while out and out[-1]['type'] == 'text' and not out[-1]['text'].strip():
        out.pop()
    if out and out[0]['type'] == 'text':
        out[0]['text'] = out[0]['text'].lstrip('\n')
    if out and out[-1]['type'] == 'text':
        out[-1]['text'] = out[-1]['text'].rstrip()
    return out


def _int(v) -> int:
    try:
        return max(0, min(int(v or 0), 100000))
    except (TypeError, ValueError):
        return 0


def _image_size(d: dict) -> tuple:
    v2 = d.get('imageV2') if isinstance(d.get('imageV2'), dict) else {}
    for variant in v2.get('variants') or v2.get('images') or []:
        size = (variant or {}).get('size') or (variant or {}).get('attribute') or {}
        if size.get('width') and size.get('height'):
            return _int(size['width']), _int(size['height'])
    origin = ((d.get('image') or {}).get('origin') or {}) if isinstance(d.get('image'), dict) else {}
    return _int(origin.get('width')), _int(origin.get('height'))


def message_view(m: dict, me_id: str, fallback: str = '') -> dict:
    """一条消息怎么显示: kind 是 text / card / image / sticker / file / recalled / system 之一, 附带对应的数据。

    fallback 是给人看的摘要, 没有更好的内容时当正文。
    """
    t = m.get('msg_type')
    d = m.get('content_data') if isinstance(m.get('content_data'), dict) else {}
    if m.get('recalled'):
        return {'kind': 'recalled'}
    if t == 6:
        return {'kind': 'system'}
    if t == 5:
        key = extract_resource_key(5, d)
        if SAFE_KEY.fullmatch(key or ''):
            w, h = _image_size(d)
            return {'kind': 'image', 'image': {'key': key, 'w': w, 'h': h}}
    if t == 10 and SAFE_KEY.fullmatch(str(d.get('key') or '')):
        return {'kind': 'sticker', 'image': {'key': d['key'], 'w': _int(d.get('width')), 'h': _int(d.get('height'))}}
    if t in (3, 15) and SAFE_KEY.fullmatch(str(d.get('key') or '')):
        return {'kind': 'file', 'file': {'key': d['key'], 'name': str(d.get('name') or ('视频' if t == 15 else '文件')),
                                         'size': _int(d.get('size')) if d.get('size') else 0, 'video': t == 15,
                                         'duration': _int(d.get('duration'))}}
    view = {'kind': 'text', 'title': '', 'segments': []}
    if t in (2, 4):
        view['segments'] = rich_segments(d.get('richText'), d.get('text') or '')
        if not _has_text(view['segments']) and not any(s['type'] == 'image' for s in view['segments']):
            view['segments'] = [{'type': 'text', 'text': d.get('text') or ''}] if d.get('text') else []
        if t == 2:
            view['title'] = str(d.get('title') or '')
    elif t == 14:
        view = {'kind': 'card', **card_view(d)}
    view['segments'] = finish(view['segments'], me_id)
    if not view['segments'] and not (view['kind'] == 'card' and view.get('title')):
        text = fallback
        if t == 2:
            text = re.sub(r'^\[富文本\]\s*' + re.escape(view['title']), '', text).strip()
        view['segments'] = finish([{'type': 'text', 'text': text}], me_id)
    return view


def brief(view: dict, fallback: str = '', limit: int = 60) -> str:
    """引用回复里显示的一行摘要。"""
    kind = view.get('kind')
    if kind == 'recalled':
        text = '[消息已撤回]'
    elif kind == 'image':
        text = '[图片]'
    elif kind == 'sticker':
        text = '[表情包]'
    elif kind == 'file':
        text = ('[视频] ' if view['file'].get('video') else '[文件] ') + view['file'].get('name', '')
    else:
        parts = [view.get('title') or '']
        for s in view.get('segments') or []:
            parts.append('[图片]' if s['type'] == 'image' else (s.get('text') or s.get('name') or ''))
        text = ' '.join(p for p in [parts[0], ''.join(parts[1:])] if p)
        if kind == 'card':
            text = '[卡片] ' + text
        text = text or fallback
    text = re.sub(r'\s+', ' ', text or '').strip()
    return text[:limit] + ('…' if len(text) > limit else '')
