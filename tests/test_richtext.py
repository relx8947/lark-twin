"""markdown 富文本(post)构造的单元测试, 不访问网络。

服务端实测的约束(2026-10, 见 larkx/protocol/richtext.py 开头注释):
- type=2, 根是 DOCS(22); 容器元素也要显式带 property; innerText 必须显式设置, 否则 400。
- 行内代码 CODE(30)、标题会被服务端丢掉/降级, 所以发送侧就只保留文字。
"""
from larkx.protocol import richtext
from larkx.protocol.proto import builders
from larkx.protocol.proto import lark_all_pb2 as LA

E = LA.entities.RichTextElement
TEXT, P, A, B, I, U, QUOTE, CODE_BLOCK = 1, 3, 6, 7, 8, 9, 29, 31
UL, OL, LI, DOCS = 26, 27, 28, 22


def _request(markdown):
    pkt = builders.build_message_packet({'kind': 'markdown', 'text': markdown}, 'c1', 'req')
    r = LA.messages.PutMessageRequest()
    r.ParseFromString(pkt.payload)
    return r


def _tree(r):
    """返回 {id: (tag, property_bytes, child_ids)} 和根 id 列表。"""
    d = r.content.richText.elements.dictionary
    return {k: (d[k].tag, d[k].property, list(d[k].childIds)) for k in d}


def _ids_by_tag(tree, tag):
    return [k for k, (t, _, _) in tree.items() if t == tag]


def test_markdown_is_post_with_docs_root():
    r = _request('你好')
    assert r.type == 2 and r.chatId == 'c1'
    tree = _tree(r)
    roots = list(r.content.richText.elementIds)
    assert len(roots) == 1 and tree[roots[0]][0] == DOCS
    # innerText 必须显式设置; 容器元素也要显式带 property
    assert r.content.richText.innerText == '你好'
    assert all(prop is not None for _, prop, _ in tree.values())


def test_simple_text_is_one_paragraph():
    r = _request('一行文字')
    tree = _tree(r)
    root = r.content.richText.elementIds[0]
    _, _, children = tree[root]
    assert len(children) == 1 and tree[children[0]][0] == P
    text_id = tree[children[0]][2][0]
    assert tree[text_id][0] == TEXT
    assert E.TextProperty.FromString(tree[text_id][1]).content == '一行文字'


def test_fenced_code_block_keeps_language_and_content():
    md = '先执行:\n\n```bash\nlarkx doctor\necho /ai\n```\n\n跑完贴结果'
    r = _request(md)
    tree = _tree(r)
    blocks = [n for n in tree.values() if n[0] == CODE_BLOCK]
    assert len(blocks) == 1
    cb = E.CodeBlockProperty.FromString(blocks[0][1])
    assert cb.language == 2  # BASH
    assert cb.content == 'larkx doctor\necho /ai\n'
    # 斜杠命令原样保留在纯文本里
    assert '/ai' in r.content.richText.innerText


def test_unknown_code_language_defaults_to_shell():
    r = _request('```brainfuck\nx\n```')
    tree = _tree(r)
    cb = E.CodeBlockProperty.FromString(next(p for t, p, _ in tree.values() if t == CODE_BLOCK))
    assert cb.language == 20  # SHELL


def test_ordered_and_unordered_lists():
    md = '- 甲\n- 乙\n\n1. 第一步\n2. 第二步'
    r = _request(md)
    tree = _tree(r)
    ul_ids, ol_ids = _ids_by_tag(tree, UL), _ids_by_tag(tree, OL)
    assert len(ul_ids) == 1 and len(ol_ids) == 1
    assert len(_ids_by_tag(tree, LI)) == 4
    assert E.ULProperty.FromString(tree[ul_ids[0]][1]).type == 0
    op = E.OLProperty.FromString(tree[ol_ids[0]][1])
    assert (op.type, op.start) == (0, 1)
    # 列表项挂在各自列表下, 两个列表是 DOCS 的直接子块
    assert set(tree[ul_ids[0]][2] + tree[ol_ids[0]][2]) == set(_ids_by_tag(tree, LI))
    root = r.content.richText.elementIds[0]
    assert set(ul_ids + ol_ids) <= set(tree[root][2])


def test_bold_italic_underline_inline():
    r = _request('**粗** 和 *斜* 还有 __粗2__ _斜2_')
    tree = _tree(r)
    tags = {t for t, _, _ in tree.values()}
    assert B in tags and I in tags
    bold_texts = [E.TextProperty.FromString(p).content for t, p, _ in tree.values() if t == B]
    italic_texts = [E.TextProperty.FromString(p).content for t, p, _ in tree.values() if t == I]
    assert sorted(bold_texts) == ['粗', '粗2'] and sorted(italic_texts) == ['斜', '斜2']


def test_inline_code_degrades_to_plain_text():
    r = _request('运行 `larkx send` 就行')
    tree = _tree(r)
    assert 30 not in {t for t, _, _ in tree.values()}   # 不发 CODE 元素
    assert 'larkx send' in r.content.richText.innerText


def test_bare_and_markdown_links():
    r = _request('看 https://a.com 和 [飞书](https://b.com)')
    tree = _tree(r)
    links = [E.LinkProperty.FromString(p) for t, p, _ in tree.values() if t == A]
    assert sorted(x.url for x in links) == ['https://a.com', 'https://b.com']
    assert '飞书' in r.content.richText.innerText and 'https://b.com' in r.content.richText.innerText


def test_quote_block():
    r = _request('> 引用一句\n> 第二行')
    tree = _tree(r)
    quotes = [n for n in tree.values() if n[0] == QUOTE]
    assert len(quotes) == 1
    # 引用里的内容是段落
    assert all(tree[c][0] == P for c in quotes[0][2])


def test_heading_degrades_to_paragraph_text():
    r = _request('# 标题\n\n正文')
    tree = _tree(r)
    assert not any(t in (23, 24, 25, 44, 45, 46) for t, _, _ in tree.values())
    assert '标题' in r.content.richText.innerText


def test_element_ids_are_sequential_in_document_order():
    r = _request('```bash\nx\n```\n\n- 甲\n')
    ids = sorted(r.content.richText.elements.dictionary.keys(), key=int)
    assert ids == [str(i + 1) for i in range(len(ids))]


def test_plain_text_for_audit_and_prefix_guard():
    md = '# 标题\n\n**重点** `/ai`\n\n- 一\n- 二\n\n```bash\nls\n```'
    plain = richtext.to_plain_text(md)
    assert plain.startswith('标题')
    assert '/ai' in plain and '重点' in plain
    assert '- 一' in plain and 'ls' in plain
    assert '**' not in plain and '`' not in plain and '#' not in plain
    assert builders.plain_text({'kind': 'markdown', 'text': md}) == plain


def test_empty_markdown_does_not_crash():
    r = _request('')
    assert r.type == 2
    assert r.content.richText.innerText == ''


# ---- 接收侧: 富文本树 -> 纯文本摘要 ----

def _summary(markdown):
    from protobuf_to_dict import protobuf_to_dict
    from larkx.protocol.proto import decoders
    r = _request(markdown)
    d = protobuf_to_dict(r.content)
    return decoders.extract_rich_text(d.get('richText') or {})


def test_summary_has_code_block_lists_and_inline_text():
    s = _summary('开头:\n\n```bash\nlarkx doctor\n```\n\n- 甲\n- 乙\n\n1. 一\n2. 二')
    assert 'larkx doctor' in s
    assert '- 甲' in s and '- 乙' in s
    assert '1. 一' in s and '2. 二' in s
    assert '开头' in s


def test_summary_keeps_bold_italic_and_link_url():
    s = _summary('**粗** *斜* 看 https://a.com')
    assert '粗' in s and '斜' in s and 'https://a.com' in s


def test_summary_flat_layout_still_works():
    # 老的扁平式 type=4 消息: 叶子直接挂在 elementIds 下
    pkt = builders.build_send_message_packet('第一行\n第二行', 'c1', 'req')
    r = LA.messages.PutMessageRequest()
    r.ParseFromString(pkt.payload)
    from protobuf_to_dict import protobuf_to_dict
    from larkx.protocol.proto import decoders
    d = protobuf_to_dict(r.content)
    assert decoders.extract_rich_text(d.get('richText') or {}) == '第一行\n第二行'
