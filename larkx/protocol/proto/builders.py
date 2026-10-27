from . import proto_pb2 as P
from .ids import generate_request_cid


def wrap_packet(cmd: int, payload_msg, request_id: str) -> P.Packet:
    pkt = P.Packet()
    pkt.payloadType = 1
    pkt.cmd = cmd
    pkt.cid = request_id
    pkt.payload = payload_msg.SerializeToString()
    return pkt


def build_send_message_packet(text: str, chat_id: str, request_id: str, root_id: str=None, thread_chat: bool=False,
                              cid: str=None, parent_id: str=None, in_thread: bool=False) -> P.Packet:
    """普通发送只把本人在这个会话的已读位置推到最新(清掉本人的未读), 不会给别人的消息产生已读回执;
    带 root_id 是回复(默认是主会话里的引用回复, 见 message_request), 被回复的那条会对对方显示已读。
    cid: 这条消息的客户端 id, 推送和拉取到的消息里都带着它; 不传就随机生成。"""
    return build_message_packet({'kind': 'text', 'text': text}, chat_id, request_id, root_id=root_id,
                                thread_chat=thread_chat, cid=cid, parent_id=parent_id, in_thread=in_thread)


# 要发出的消息统一用一个 dict 描述(LarkClient.send_message 收的就是它), 和发到哪个会话、是不是回复无关,
# 匿名发送(打开 keep_unread 时)同一条先发到本人私聊、再转发过去:
#   {'kind': 'text', 'text'}                               纯文字
#   {'kind': 'text', 'segments': [...]}                    带 @ 的文字, 片段是 {'type': 'text', 'text'} 或
#                                                          {'type': 'at', 'user_id', 'name'}
#   {'kind': 'markdown', 'text'}                           markdown 富文本(代码块、列表、标题、引用、
#                                                          加粗/斜体、链接, 语法见 protocol/richtext.py)
#   {'kind': 'image', 'key'}                               图片, key 是上传后拿到的 img_ 开头的 key
#   {'kind': 'file', 'key', 'name', 'size', 'mime'}        文件, key 是上传后拿到的 file_ 开头的 key
MESSAGE_KINDS = ('text', 'markdown', 'image', 'file')
TYPE_TEXT, TYPE_POST, TYPE_FILE, TYPE_IMAGE = 4, 2, 3, 5
TAG_TEXT, TAG_AT = 1, 5


def text_segments(msg: dict) -> list:
    """文字消息的片段: 相邻的文字合并, 空文字去掉, user_id 不合法的 @ 退成普通文字。"""
    segs = msg.get('segments')
    if segs is None:
        segs = [{'type': 'text', 'text': msg.get('text') or ''}]
    out = []
    for s in segs:
        if s.get('type') == 'at':
            uid = str(s.get('user_id') or '')
            if uid.isdigit():
                name = str(s.get('name') or '').lstrip('@')
                out.append({'type': 'at', 'user_id': uid, 'name': name})
                continue
            s = {'type': 'text', 'text': '@' + str(s.get('name') or '').lstrip('@')}
        text = str(s.get('text') or '')
        if not text:
            continue
        if out and out[-1]['type'] == 'text':
            out[-1]['text'] += text
        else:
            out.append({'type': 'text', 'text': text})
    return out or [{'type': 'text', 'text': ''}]


def has_mentions(msg: dict) -> bool:
    return msg.get('kind') == 'text' and any(s['type'] == 'at' for s in text_segments(msg))


def without_mentions(msg: dict) -> dict:
    """把 @ 写成普通文字 "@名字"(不再 @ 到人); 不是带 @ 的文字消息原样返回。"""
    if not has_mentions(msg):
        return msg
    return {'kind': 'text', 'text': plain_text(msg)}


def plain_text(msg: dict) -> str:
    """消息的纯文本(@ 写成 @名字), 用于摘要和审计记录。"""
    if msg.get('kind') == 'image':
        return '[图片]'
    if msg.get('kind') == 'file':
        return f"[文件] {msg.get('name') or ''}".strip()
    if msg.get('kind') == 'markdown':
        from .. import richtext
        return richtext.to_plain_text(msg.get('text') or '')
    return ''.join('@' + s['name'] if s['type'] == 'at' else s['text'] for s in text_segments(msg))


def message_request(msg: dict, chat_id: str, root_id: str=None, thread_chat: bool=False, cid: str=None,
                    parent_id: str=None, in_thread: bool=False):
    """按消息描述生成 messages.PutMessageRequest(完整版 proto, 带 atUserIdList)。

    带 root_id 是回复, 和飞书客户端一样: parentId 是直接回的那条(上方显示的引用, 默认就是 root_id), rootId 是这串回复
    最早的那条(回的那条本身是回复时是它的 rootId)。默认是主会话里的引用回复; in_thread 为真才回进普通群里已有的话题。
    话题群里的回复本来就在话题里(rootId 是话题的根消息), 不带 isReplyInThread。
    """
    from .gateway import _lark_all
    L = _lark_all()
    kind = msg.get('kind')
    if kind not in MESSAGE_KINDS:
        raise ValueError(f'不支持的消息类型: {kind}')
    req = L.messages.PutMessageRequest()
    req.chatId = str(chat_id)
    req.cid = cid or generate_request_cid()
    req.isNotified = True
    req.version = 1
    if root_id:
        req.rootId = str(root_id)
        req.parentId = str(parent_id or root_id)
        # 带 isReplyInThread 是回进话题(根消息没有话题时会新开一个); 不带就留在主会话, 是引用回复。
        # 话题群反而拒收它(replyInThread not support chat)
        if in_thread and not thread_chat:
            req.isReplyInThread = True
    if kind == 'image':
        # 实测只填 imageKey 会被拒("found invalid image key"), 上传图片拿到的 key 要同时填进 cryptoToken
        req.type = TYPE_IMAGE
        req.content.imageKey = req.content.cryptoToken = str(msg['key'])
        return req
    if kind == 'file':
        req.type = TYPE_FILE
        req.content.fileKey = str(msg['key'])
        req.content.fileName = str(msg.get('name') or 'file')
        req.content.fileSize = int(msg.get('size') or 0)
        if msg.get('mime'):
            req.content.fileMime = str(msg['mime'])
        return req
    if kind == 'markdown':
        # 富文本(post): markdown 转成 richText 元素树(语法和服务端的约束见 richtext 模块开头)
        from .. import richtext
        req.type = TYPE_POST
        richtext.build_rich_text(req.content.richText, str(msg.get('text') or ''))
        return req
    # 文字: 富文本, 每段一个元素; @ 是 AT 元素(属性里是 userId 和显示的 "@名字"), 元素 id 记进 atIds,
    # 被 @ 的人还要列进 atUserIdList, 对方才会收到 @ 提醒(和客户端发出的 @ 消息结构一致, 实测过)
    req.type = TYPE_TEXT
    rich = req.content.richText
    E = L.entities.RichTextElement
    inner = []
    for seg in text_segments(msg):
        el_id = generate_request_cid()
        rich.elementIds.append(el_id)
        el = rich.elements.dictionary[el_id]
        if seg['type'] == 'at':
            el.tag = TAG_AT
            el.property = E.AtProperty(userId=seg['user_id'], content='@' + seg['name']).SerializeToString()
            rich.atIds.append(el_id)
            if int(seg['user_id']) not in req.atUserIdList:
                req.atUserIdList.append(int(seg['user_id']))
            inner.append('@' + seg['name'])
        else:
            el.tag = TAG_TEXT
            el.property = E.TextProperty(content=seg['text']).SerializeToString()
            inner.append(seg['text'])
    rich.innerText = ''.join(inner)
    return req


def build_message_packet(msg: dict, chat_id: str, request_id: str, root_id: str=None, thread_chat: bool=False,
                         cid: str=None, parent_id: str=None, in_thread: bool=False) -> P.Packet:
    """发消息(PutMessageRequest)的请求包, msg 见上面的消息描述, 回复的参数见 message_request。"""
    return wrap_packet(5, message_request(msg, chat_id, root_id=root_id, thread_chat=thread_chat, cid=cid,
                                          parent_id=parent_id, in_thread=in_thread), request_id)


def build_create_chat_packet(user_id: str, request_id: str) -> P.Packet:
    req = P.PutChatRequest()
    req.type = 1
    req.chatterIds.append(str(user_id))
    return wrap_packet(13, req, request_id)


def build_search_packet(query: str, request_id: str, locale: str='zh_CN') -> P.Packet:
    req = P.UniversalSearchRequest()
    req.header.searchSession = generate_request_cid()
    req.header.sessionSeqId = 1
    req.header.query = query
    req.header.searchContext.tagName = 'SMART_SEARCH'
    item1 = P.EntityItem()
    item1.type = 1
    item2 = P.EntityItem()
    item2.type = 2
    item2.filter.CopyFrom(P.EntityItem.EntityFilter())
    item3 = P.EntityItem()
    item3.type = 3
    item3.filter.groupChatFilter.CopyFrom(P.GroupChatFilter())
    item4 = P.EntityItem()
    item4.type = 10
    item4.filter.CopyFrom(P.EntityItem.EntityFilter())
    req.header.searchContext.entityItems.append(item1)
    req.header.searchContext.entityItems.append(item2)
    req.header.searchContext.entityItems.append(item3)
    req.header.searchContext.entityItems.append(item4)
    req.header.searchContext.commonFilter.includeOuterTenant = 1
    req.header.searchContext.sourceKey = 'messenger'
    req.header.locale = locale
    req.header.extraParam.CopyFrom(P.SearchExtraParam())
    return wrap_packet(11021, req, request_id)


def build_user_info_packet(user_id: str, chat_id: str, request_id: str) -> P.Packet:
    req = P.GetUserInfoRequest()
    req.userId = int(user_id)
    req.chatId = int(chat_id)
    req.userType = 1
    return wrap_packet(5023, req, request_id)


def build_group_info_packet(chat_id: str, request_id: str) -> P.Packet:
    req = P.GetGroupInfoRequest()
    req.chatId = str(chat_id)
    return wrap_packet(64, req, request_id)


def decode_put_message_response(content: bytes) -> str:
    pkt = P.Packet()
    pkt.ParseFromString(content)
    resp = P.PutMessageResponse()
    resp.ParseFromString(pkt.payload)
    return resp.message.id


def decode_put_chat_response(content: bytes):
    pkt = P.Packet()
    pkt.ParseFromString(content)
    if pkt.payload:
        resp = P.PutChatResponse()
        resp.ParseFromString(pkt.payload)
        return resp.chat.id
    return None


def decode_search_response(content: bytes):
    pkt = P.Packet()
    pkt.ParseFromString(content)
    results = []
    if pkt.payload:
        resp = P.UniversalSearchResponse()
        resp.ParseFromString(pkt.payload)
        # summary: 用户是部门, 群是"包含: 某某"; 同名的人靠部门和头像区分
        for r in resp.results:
            kind = {1: 'user', 3: 'group'}.get(r.type)
            if kind:
                results.append({'type': kind, 'id': r.id, 'title': r.titleHighlighted, 'summary': r.summaryHighlighted,
                                'avatar_key': r.avatarKey})
    return results


def decode_user_info_response(content: bytes, locale: str='zh_CN'):
    pkt = P.Packet()
    pkt.ParseFromString(content)
    if not pkt.payload:
        return None
    info = P.UserInfo()
    info.ParseFromString(pkt.payload)
    detail = info.userInfoDetail.detail
    name = None
    for item in detail.locales:
        if item.key_string == locale.lower():
            return item.translation
    if detail.nickname:
        try:
            return detail.nickname.decode('utf-8')
        except Exception:
            return str(detail.nickname)
    return name


def decode_group_info_response(content: bytes):
    pkt = P.Packet()
    pkt.ParseFromString(content)
    if not pkt.payload:
        return None
    info = P.UserInfo()
    info.ParseFromString(pkt.payload)
    detail = info.userInfoDetail.detail
    for field in (detail.nickname1, detail.nickname4):
        if field:
            try:
                return field.decode('utf-8')
            except Exception:
                pass
    return None
