"""网页版飞书的消息显示: 富文本片段、卡片、图片/表情包/文件、撤回。只测纯数据整理, 不访问网络。"""
import json

from larkx.protocol.proto import decoders, lark_all_pb2 as L, proto_pb2 as P
from larkx.storage import Storage
from larkx.web import render

E = L.entities.RichTextElement
ME = '100'


def _rich(content, elements, roots, msg_type=4):
    """按真实结构拼一条文本/富文本消息, 再走一遍解码, 和从飞书收到的一样。"""
    c = P.TextContent() if msg_type == 4 else P.PostContent()
    c.text = content
    if msg_type == 2:
        c.title = '周报'
    c.richText.elementIds.extend(roots)
    for key, (tag, prop, children, style) in elements.items():
        el = c.richText.elements.dictionary[key]
        el.tag = tag
        if prop is not None:
            el.property = prop.SerializeToString()
        el.childIds.extend(children)
        for k, v in style.items():
            el.style[k] = v
    summary, data = decoders.decode_message_content(msg_type, c.SerializeToString())
    return {'msg_id': '1', 'msg_type': msg_type, 'content': summary, 'content_data': data}


def _text(s, **style):
    return (1, E.TextProperty(content=s), [], style)


def test_rich_text_bold_italic_links_and_mentions():
    m = _rich('@我本人 @张三 看 [微笑] 文档 http://x', {
        'a': (5, E.AtProperty(userId=ME, content='@我本人'), [], {}),
        'b': (5, E.AtProperty(userId='200', content='张三'), [], {}),
        'c': _text(' 看 ', fontWeight='bold'),
        'd': (10, E.EmotionProperty(key='SMILE'), [], {}),
        'e': _text(' 文档', fontStyle='italic'),
        'f': (6, E.AnchorProperty(href='https://example.com/a?b=1', content='https://example.com/a?b=1', textContent='文档链接'), [], {}),
        'g': (6, E.AnchorProperty(href='javascript:alert(1)', content='点我'), [], {}),
    }, ['a', 'b', 'c', 'd', 'e', 'f', 'g'])
    v = render.message_view(m, ME, m['content'])
    assert v['kind'] == 'text'
    assert v['segments'] == [
        {'type': 'at', 'name': '@我本人', 'me': True},
        {'type': 'at', 'name': '@张三', 'me': False},
        {'type': 'text', 'text': ' 看 ', 'bold': True},
        {'type': 'text', 'text': '[微笑]'},  # 表情保持文字写法, 从消息的纯文本里找回
        {'type': 'text', 'text': ' 文档', 'italic': True},
        {'type': 'link', 'text': '文档链接', 'href': 'https://example.com/a?b=1'},
        {'type': 'text', 'text': '点我'},  # 不是 http/https 的链接退成普通文字
    ]
    # 解出来的字段在 content_data 里, 入库(字节会被丢掉)后照样能显示格式
    stored = json.loads(json.dumps(m['content_data'], default=lambda o: f'<{len(o)} bytes>'))
    again = render.message_view({**m, 'content_data': stored}, ME, m['content'])
    assert again['segments'] == v['segments']


def test_post_paragraphs_lists_code_and_images():
    m = _rich('第一段 甲 乙', {
        'root': (22, None, ['p1', 'ul', 'fig'], {}),
        'p1': (3, None, ['t1'], {}),
        't1': _text('第一段'),
        'ul': (26, None, ['li1', 'li2'], {}),
        'li1': (28, None, ['t2'], {}),
        't2': _text('甲'),
        'li2': (28, None, ['code'], {}),
        'code': (30, None, ['t3'], {}),
        't3': _text('make test'),
        'fig': (4, None, ['img'], {}),
        'img': (2, E.ImageProperty(token='img_v3_abc', originWidth=640, originHeight=320), [], {}),
    }, ['root'], msg_type=2)
    v = render.message_view(m, ME, m['content'])
    assert v['title'] == '周报'
    assert v['segments'] == [
        {'type': 'text', 'text': '第一段\n• 甲\n• '},
        {'type': 'text', 'text': 'make test', 'code': True},
        {'type': 'text', 'text': '\n'},
        {'type': 'image', 'key': 'img_v3_abc', 'w': 640, 'h': 320},
    ]
    assert render.brief(v) == '周报 第一段 • 甲 • make test [图片]'


def test_old_stored_rich_text_falls_back_to_plain_text():
    # 改版前入库的消息没有解出来的字段, 只能显示纯文本
    m = {'msg_type': 4, 'content': '你好', 'content_data': {
        'text': '你好', 'richText': {'elementIds': ['1'], 'elements': {'dictionary': {'1': {'tag': 1, 'property': '<8 bytes>'}}}}}}
    assert render.message_view(m, ME, '你好')['segments'] == [{'type': 'text', 'text': '你好'}]
    assert render.message_view({'msg_type': 4, 'content_data': None}, ME, '摘要')['segments'] == [{'type': 'text', 'text': '摘要'}]


def _card(**extra):
    return {'msg_type': 14, 'content': '[卡片]', 'content_data': extra}


def test_card_uses_new_body_title_and_safe_links():
    body = {'tag': 'body', 'property': {'elements': [
        {'tag': 'markdown', 'property': {
            'elements': [{'tag': 'plain_text', 'property': {'content': '状态', 'textStyle': {'attributes': ['bold']}}},
                         {'tag': 'plain_text', 'property': {'content': '：通过'}}, {'tag': 'br', 'property': {}},
                         {'tag': 'link', 'property': {'content': '详情', 'url': {'url': 'https://example.com/x'}}},
                         {'tag': 'at', 'property': {'userID': 'ou_xxx'}}],
            'markdownElements': [{'tag': 'plain_text', 'property': {'content': '重复的一份'}}]}},
        {'tag': 'img', 'property': {'imageID': '1'}},
        {'tag': 'action', 'property': {'actions': [{'tag': 'button', 'property': {
            'text': {'tag': 'plain_text', 'property': {'content': '去处理'}},
            'actions': [{'type': 'open_url', 'action': {'url': 'javascript:alert(1)'}}]}},
            # 下拉框只显示提示文字, 选项不展开
            {'tag': 'select_static', 'property': {
                'placeholder': {'tag': 'plain_text', 'property': {'content': '屏蔽'}},
                'options': [{'text': {'tag': 'plain_text', 'property': {'content': '1小时'}}, 'value': '1'}]}}]}},
    ]}}
    card = {'header': {'tag': 'card_header', 'property': {'template': 'green',
                                                           'title': {'tag': 'plain_text', 'property': {'content': '审批结果'}}}},
            # 有 newBody 时 body 只是给旧客户端的降级提示
            'body': {'elements': [{'tag': 'plain_text', 'property': {'content': '请升级至最新版本客户端，以查看内容'}}]},
            'newBody': body}
    v = render.message_view(_card(jsonCard=json.dumps(card, ensure_ascii=False), cardHeader={'title': '旧标题'}), ME, '[卡片]')
    assert (v['kind'], v['title'], v['theme']) == ('card', '审批结果', 'green')
    assert v['segments'] == [
        {'type': 'text', 'text': '状态', 'bold': True}, {'type': 'text', 'text': '：通过\n'},
        {'type': 'link', 'text': '详情', 'href': 'https://example.com/x'}, {'type': 'at', 'name': '@某人', 'me': False},
        {'type': 'text', 'text': '\n[去处理][屏蔽]'},
    ]
    assert render.brief(v).startswith('[卡片] 审批结果 状态')


def test_card_without_json_uses_header_and_hides_upgrade_hint():
    rich = {'elementIds': ['1'], 'elements': {'dictionary': {
        '1': {'tag': 1, 'prop': {'content': 'Upgrade to the latest app version to view the content'}}}}}
    v = render.message_view(_card(cardHeader={'title': '日报', 'theme': 'blue'}, richtext=rich), ME, '[卡片]')
    assert (v['title'], v['theme']) == ('日报', 'blue')
    assert v['segments'] == [{'type': 'text', 'text': '这张卡片暂时不能在网页上显示，请在飞书客户端查看'}]
    plain = render.message_view(_card(cardHeader={'title': 'x', 'theme': '<b>'}, cardDesc='说明'), ME, '[卡片]')
    assert plain['theme'] == '' and plain['segments'] == [{'type': 'text', 'text': '说明'}]


def test_media_kinds_and_recalled():
    img = {'msg_type': 5, 'content_data': {'imageV2': {'imageKey': 'img_k', 'variants': [{'type': 1, 'size': {'width': 30, 'height': 20}}]}}}
    assert render.message_view(img, ME)['image'] == {'key': 'img_k', 'w': 30, 'h': 20}
    sticker = {'msg_type': 10, 'content_data': {'key': 'stk_1', 'width': 206, 'height': 80}}
    assert render.message_view(sticker, ME) == {'kind': 'sticker', 'image': {'key': 'stk_1', 'w': 206, 'h': 80}}
    f = render.message_view({'msg_type': 3, 'content_data': {'key': 'file_k', 'name': 'a.md', 'size': 680}}, ME)
    assert f == {'kind': 'file', 'file': {'key': 'file_k', 'name': 'a.md', 'size': 680, 'video': False, 'duration': 0}}
    assert render.brief(f) == '[文件] a.md'
    # key 不合规的不当成资源, 退回文字摘要
    assert render.message_view({'msg_type': 10, 'content_data': {'key': '../x'}}, ME, '[表情包]')['kind'] == 'text'
    assert render.message_view({'msg_type': 4, 'recalled': True, 'content_data': {}}, ME) == {'kind': 'recalled'}


def test_finish_only_lets_known_fields_through():
    segs = render.finish([{'type': 'text', 'text': '\n\na', 'bold': True, 'onclick': 'x'}, {'type': 'text', 'text': 'b', 'bold': 1},
                          {'type': 'image', 'key': 'bad key'}, {'type': 'link', 'text': 'x', 'href': ' HTTPS://ok.example '},
                          {'type': 'weird', 'text': 'y'}], ME)
    assert segs == [{'type': 'text', 'text': 'ab', 'bold': True}, {'type': 'text', 'text': '[图片]'},
                    {'type': 'link', 'text': 'x', 'href': 'HTTPS://ok.example'}, {'type': 'text', 'text': 'y'}]


def test_storage_messages_after_and_latest():
    st = Storage()
    assert st.latest_message_id() == 0
    for p in (3, 1, 2):
        st.save_message({'msg_id': f'm{p}', 'chat_id': 'c1', 'position': p, 'content': str(p)})
    st.save_message({'msg_id': 'x', 'chat_id': 'c2', 'position': 9})
    assert [r.position for r in st.messages_after('c1', 1)] == [2, 3]
    assert st.latest_message_id() == 4
