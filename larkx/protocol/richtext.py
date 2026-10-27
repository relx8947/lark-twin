"""把 markdown 文本构造成飞书富文本(post, type=2)的 richText 元素树。

结构和约束都是按内部网关上实测出来的(2026-10, 见 tests 与 git 记录):
- 根是一个 DOCS(22), 下面挂块级元素: 段落 P(3)、无序/有序列表 UL(26)/OL(27)/LI(28)、
  引用 QUOTE(29)、代码块 CODE_BLOCK(31)。标题 H1~H6 服务端会降级成段落, 发送侧直接按段落发。
- 行内只有文字 TEXT(1)、加粗 B(7)、斜体 I(8)、下划线 U(9); 链接 A(6) 只能是裸 URL,
  飞书 IM 没有"自定义文字的超链接"([文字](url) 里的 url 会被服务端丢掉)。
- 行内代码元素 CODE(30) 服务端会整条剔除(文字保留), 所以 `` `code` `` 只保留文字。
- 容器元素也要显式带 property(序列化成空 bytes); richText.innerText 必须显式设置,
  缺了这两处服务端直接 400("%PB payload in packet unmarshal failed")。
- 代码块用老版 CodeBlockProperty(tag 31, V1 语言枚举); V2(tag 37) 会被降级成普通段落。

支持的 markdown 语法是个够用的小子集: 围栏代码块(```lang)、标题、引用、有序/无序列表、
段落里的 **加粗**、*斜体*、`行内代码`(降级成普通文字)、[文字](http(s) 链接) 和裸链接。
"""
import re

# RichTextElement.Tag
TEXT, P, A, B, I, U, QUOTE, CODE_BLOCK = 1, 3, 6, 7, 8, 9, 29, 31
UL, OL, LI, DOCS = 26, 27, 28, 22
H_TAGS = (23, 24, 25, 44, 45, 46)

# CodeBlockProperty.Language(V1 枚举, 只有这些); 不认识的语言按 SHELL 发
CODE_LANGS = {
    'js': 0, 'javascript': 0, 'apache': 1, 'bash': 2, 'sh': 2, 'shell': 20, 'zsh': 2,
    'cs': 3, 'csharp': 3, 'cpp': 4, 'c++': 4, 'coffee': 5, 'coffeescript': 5,
    'html': 6, 'xml': 7, 'http': 8, 'json': 9, 'java': 10, 'makefile': 11, 'mk': 11,
    'markdown': 12, 'md': 12, 'nginx': 13, 'objc': 14, 'objectivec': 14, 'php': 15,
    'perl': 16, 'py': 17, 'python': 17, 'ruby': 18, 'rb': 18, 'sql': 19, 'go': 21,
    'golang': 21, 'scss': 22, 'sass': 22, 'lua': 23, 'django': 24, 'kotlin': 25, 'kt': 25,
}
DEFAULT_LANG = 20  # SHELL: 不写语言的代码块大多是命令

FENCE_RE = re.compile(r'^```([\w+\-.#]*)\s*$')
HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')
QUOTE_RE = re.compile(r'^>\s?(.*)$')
UL_ITEM_RE = re.compile(r'^\s*[-*+]\s+(.*)$')
OL_ITEM_RE = re.compile(r'^\s*(\d{1,9})[.)]\s+(.*)$')
# 行内: **bold** __bold__ *italic* _italic_ `code` [text](url)
INLINE = re.compile(
    r'(\*\*.+?\*\*|__.+?__|(?<!\*)\*(?!\*)[^*]+?\*(?!\*)|(?<!_)_(?!_)[^_]+?_(?!_)'
    r'|`.+?`|\[[^]\n]{1,400}\]\(https?://[^\s)]{1,4000}\)|https?://[^\s\x00-\x1f\x7f<>"]{1,4000})'
)
URL = re.compile(r'^https?://', re.I)


def _plain_inline(text: str) -> str:
    """行内 markdown 去掉标记后的纯文字([文字](url) 保留文字和 url)。"""
    out = []
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith('**') and part.endswith('**') or part.startswith('__') and part.endswith('__'):
            out.append(part[2:-2])
        elif part.startswith('*') and part.endswith('*') or part.startswith('_') and part.endswith('_'):
            out.append(part[1:-1])
        elif part.startswith('`') and part.endswith('`'):
            out.append(part[1:-1])
        elif part.startswith('['):
            m = re.match(r'^\[([^]]*)\]\((.*)\)$', part)
            out.append(f'{m.group(1)} {m.group(2)}' if m else part)
        else:
            out.append(part)
    return ''.join(out)


def to_plain_text(markdown: str) -> str:
    """markdown 消息的纯文本(审计、摘要用), 块之间换行。"""
    lines, out = [], []
    in_fence = False
    for raw in (markdown or '').split('\n'):
        fm = FENCE_RE.match(raw.strip())
        if fm:
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(raw)
            continue
        hm = HEADING_RE.match(raw)
        if hm:
            out.append(hm.group(2).strip())
            continue
        qm = QUOTE_RE.match(raw)
        if qm:
            out.append(_plain_inline(qm.group(1)))
            continue
        um = UL_ITEM_RE.match(raw)
        if um:
            out.append('- ' + _plain_inline(um.group(1)))
            continue
        om = OL_ITEM_RE.match(raw)
        if om:
            out.append(f'{om.group(1)}. ' + _plain_inline(om.group(2)))
            continue
        out.append(_plain_inline(raw) if raw.strip() else '')
    return '\n'.join(out).strip()


class _Tree:
    """按文档顺序分配元素 id 的 RichText 构造器(和飞书客户端发出的树编号方式一致)。"""

    def __init__(self, rich):
        self.rich = rich
        self._n = 0
        self.inner = []

    def node(self, tag, prop=b'', children=()):
        self._n += 1
        eid = str(self._n)
        el = self.rich.elements.dictionary[eid]
        el.tag = tag
        el.property = prop   # 容器也要显式带 property
        if children:
            el.childIds.extend(children)
        return eid

    def block(self, tag, prop=b'', children=()):
        eid = self.node(tag, prop, children)
        self._break()
        return eid

    def _break(self):
        if self.inner and not self.inner[-1].endswith('\n'):
            self.inner.append('\n')


def build_rich_text(rich, markdown: str):
    """把 markdown 填进一个空的 lark.entities.RichText, 返回纯文本。"""
    from .proto.gateway import _lark_all
    E = _lark_all().entities.RichTextElement

    tree = _Tree(rich)

    def text_prop(s):
        return E.TextProperty(content=s).SerializeToString()

    def add_inline(parent_children, text):
        """把一行文本的行内 markdown 展开成 TEXT/B/I/A 叶子, 追加到 parent_children。"""
        for part in INLINE.split(text):
            if not part:
                continue
            if (part.startswith('**') and part.endswith('**')) or (part.startswith('__') and part.endswith('__')):
                parent_children.append(tree.node(B, text_prop(part[2:-2])))
                tree.inner.append(part[2:-2])
            elif (part.startswith('*') and part.endswith('*')) or (part.startswith('_') and part.endswith('_')):
                parent_children.append(tree.node(I, text_prop(part[1:-1])))
                tree.inner.append(part[1:-1])
            elif part.startswith('`') and part.endswith('`'):
                # 行内代码 IM 不支持, 只保留文字
                parent_children.append(tree.node(TEXT, text_prop(part[1:-1])))
                tree.inner.append(part[1:-1])
            elif part.startswith('['):
                m = re.match(r'^\[([^]]*)\]\((.*)\)$', part)
                if m:
                    label, url = m.group(1), m.group(2)
                    if label:
                        parent_children.append(tree.node(TEXT, text_prop(label + ' ')))
                        tree.inner.append(label + ' ')
                    parent_children.append(tree.node(A, E.LinkProperty(url=url).SerializeToString()))
                    tree.inner.append(url)
                else:
                    parent_children.append(tree.node(TEXT, text_prop(part)))
                    tree.inner.append(part)
            elif URL.match(part):
                parent_children.append(tree.node(A, E.LinkProperty(url=part).SerializeToString()))
                tree.inner.append(part)
            else:
                parent_children.append(tree.node(TEXT, text_prop(part)))
                tree.inner.append(part)

    def paragraph(line):
        children = []
        add_inline(children, line)
        return tree.block(P, children=children)

    blocks = []
    lines = (markdown or '').split('\n')
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        fm = FENCE_RE.match(stripped)
        if fm:
            lang, body = fm.group(1).lower(), []
            i += 1
            while i < len(lines) and not FENCE_RE.match(lines[i].strip()):
                body.append(lines[i])
                i += 1
            code = '\n'.join(body)
            if code:
                tree.inner.append(code)
                tree._break()
            prop = E.CodeBlockProperty(language=CODE_LANGS.get(lang, DEFAULT_LANG),
                                       content=code + '\n').SerializeToString()
            blocks.append(tree.block(CODE_BLOCK, prop))
            i += 1  # 跳过结束围栏(没有结束围栏时 i==len, 无妨)
            continue
        if not stripped:
            i += 1
            continue
        hm = HEADING_RE.match(stripped)
        if hm:
            # IM 不渲染标题(服务端也会把 H1~H6 降级成段落), 直接按段落发, 去掉 '#' 前缀
            children = []
            add_inline(children, hm.group(2).strip())
            blocks.append(tree.block(P, children=children))
            i += 1
            continue
        if QUOTE_RE.match(stripped):
            quote_children = []
            while i < len(lines) and QUOTE_RE.match(lines[i].strip()):
                qm = QUOTE_RE.match(lines[i].strip())
                if qm.group(1):
                    children = []
                    add_inline(children, qm.group(1))
                    quote_children.append(tree.node(P, children=children))
                i += 1
            blocks.append(tree.block(QUOTE, children=quote_children))
            continue
        um = UL_ITEM_RE.match(raw)
        om = OL_ITEM_RE.match(raw)
        if um or om:
            ordered = bool(om)
            item_lines = []
            while i < len(lines):
                um2, om2 = UL_ITEM_RE.match(lines[i]), OL_ITEM_RE.match(lines[i])
                if ordered:
                    if not om2:
                        break
                    item_lines.append((int(om2.group(1)), om2.group(2)))
                else:
                    if not um2:
                        break
                    item_lines.append((None, um2.group(1)))
                i += 1
            li_ids = []
            for _, content in item_lines:
                children = []
                add_inline(children, content)
                li_ids.append(tree.node(LI, children=children))
            if ordered:
                prop = E.OLProperty(type=0, start=item_lines[0][0] or 1).SerializeToString()
                blocks.append(tree.block(OL, prop, children=li_ids))
            else:
                prop = E.ULProperty(type=0).SerializeToString()
                blocks.append(tree.block(UL, prop, children=li_ids))
            continue
        # 普通行: 连续的非空、非块级行合并成一个段落, 行间用换行
        para_lines = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i]
            s2 = nxt.strip()
            if (not s2 or FENCE_RE.match(s2) or HEADING_RE.match(s2) or QUOTE_RE.match(s2)
                    or UL_ITEM_RE.match(nxt) or OL_ITEM_RE.match(nxt)):
                break
            para_lines.append(s2)
            i += 1
        children = []
        for j, line in enumerate(para_lines):
            if j:
                children.append(tree.node(TEXT, text_prop('\n')))
                tree.inner.append('\n')
            add_inline(children, line)
        blocks.append(tree.block(P, children=children))

    root = tree.node(DOCS, children=blocks)
    rich.elementIds.append(root)
    rich.innerText = ''.join(tree.inner).strip()
    rich.version = 1
    return rich.innerText
