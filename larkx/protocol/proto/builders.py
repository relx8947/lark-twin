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
                              sync_to_chat: bool=False) -> P.Packet:
    """sync_to_chat: 以话题回复发出, 同时在主会话显示一份(飞书的"同时发送到会话")。
    这样发出的消息不会推进本人在这个会话的已读位置, 之前的未读消息仍是未读; 普通发送会把它们都算作已读。"""
    cid_1 = generate_request_cid()
    cid_2 = generate_request_cid()
    req = P.PutMessageRequest()
    req.type = 4
    req.chatId = str(chat_id)
    req.cid = cid_1
    req.isNotified = 1
    req.version = 1
    if root_id:
        req.rootId = str(root_id)
        req.parentId = str(root_id)
        # 普通群缺 isReplyInThread 会落进主会话;话题群反而拒收它(replyInThread not support chat)
        if not thread_chat:
            req.isReplyInThread = True
    req.content.richText.elementIds.append(cid_2)
    req.content.richText.innerText = text
    req.content.richText.elements.dictionary[cid_2].tag = 1
    tp = P.TextProperty()
    tp.content = str(text)
    req.content.richText.elements.dictionary[cid_2].property = tp.SerializeToString()
    if sync_to_chat and root_id and not thread_chat:
        # 旧的 proto_pb2 里没有 syncToChat, 换成完整定义(线格式兼容)再设置
        from . import lark_all_pb2 as LA
        full = LA.messages.PutMessageRequest()
        full.ParseFromString(req.SerializeToString())
        full.isReplyInThread = True
        full.syncToChat = True
        req = full
    return wrap_packet(5, req, request_id)


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
        for r in resp.results:
            if r.type == 1:
                results.append({'type': 'user', 'id': r.id, 'title': r.titleHighlighted})
            elif r.type == 3:
                results.append({'type': 'group', 'id': r.id, 'title': r.titleHighlighted})
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
