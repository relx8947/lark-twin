import asyncio
import threading
import time
from urllib.parse import urlencode
import requests
import websockets
from loguru import logger
from .. import state
from .auth import LarkAuth
from .proto import builders, decoders, gateway, proto_pb2 as P
from .proto.ids import generate_access_key, generate_long_request_id, generate_request_cid, generate_request_id

WS_STATS_INTERVAL = 600


class PushStats:
    """WS 收到的推送统计, 用来查长连接漏推: 新消息(cmd 6)以外的推送每种 cmd 第一次出现时打一条 INFO(带解出来的内容摘要),
    之后每 WS_STATS_INTERVAL 秒汇总一次各 cmd 的次数; 解不开的帧限频打 WARNING。不逐帧打日志。"""

    def __init__(self, interval: int = WS_STATS_INTERVAL, clock=time.monotonic):
        self.interval, self.clock = interval, clock
        self.counts, self.seen = {}, set()
        self.compressed = self.errors = self.muted = 0
        self.last_report = self.last_warn = clock()
        self.warned = False

    def packet(self, cmd, frame: dict, packet: dict):
        cmd = int(cmd or 0)
        self.counts[cmd] = self.counts.get(cmd, 0) + 1
        if frame.get('payloadEncoding') or (frame.get('payload') or b'')[:2] == b'\x1f\x8b':
            self.compressed += 1
        if cmd == 6 or cmd in self.seen:
            return
        self.seen.add(cmd)
        names = '/'.join(gateway.push_types(cmd)) or '未知'
        body = gateway.decode_push(cmd, packet.get('payload') or b'')
        logger.info(f'[WS 推送] 第一次收到 cmd {cmd}({names}), payload {len(packet.get("payload") or b"")} 字节: '
                    f'{str(body)[:300] if body is not None else "解不出来"}')

    def error(self, e, raw: bytes):
        """解不开的帧: 第一次马上打 WARNING, 之后每个周期最多打一条, 带上这期间压下去的条数。"""
        self.errors += 1
        now = self.clock()
        if self.warned and now - self.last_warn < self.interval:
            self.muted += 1
            return
        more = f'(上次告警后还有 {self.muted} 帧也解不开)' if self.muted else ''
        logger.warning(f'[WS 推送] 解不开的帧, {len(raw)} 字节, 已跳过: {e!r}{more}')
        self.warned, self.last_warn, self.muted = True, now, 0

    def maybe_report(self):
        now = self.clock()
        if now - self.last_report < self.interval:
            return
        minutes = (now - self.last_report) / 60
        others = ', '.join(f'cmd {c}({"/".join(gateway.push_types(c)) or "未知"}) {n}'
                           for c, n in sorted(self.counts.items()) if c != 6) or '无'
        logger.info(f'[WS 推送] 近 {minutes:.0f} 分钟: 新消息(cmd 6) {self.counts.get(6, 0)} 帧, 其他推送: {others}; '
                    f'压缩帧 {self.compressed}, 解不开的帧 {self.errors}')
        self.counts, self.compressed, self.errors, self.last_report = {}, 0, 0, now


class LarkClient:
    def __init__(self, auth: LarkAuth):
        self.auth = auth
        self.auth.require_valid()
        if not self.auth.app_key or not self.auth.user_id:
            self.auth.validate(fetch_profile=True)
            self.auth.save()
        self.loop = asyncio.new_event_loop()
        self._loop_started = False
        self._thread_chats = {}
        self._bots = {}
        self._self_chat = ''
        self.push_stats = PushStats()

    def build_ws_url(self) -> str:
        device_id = self.auth.device_id
        access_key = generate_access_key(f'2{self.auth.app_key}{device_id}f8a69f1719916z')
        ticket = self.auth.get_ticket()
        params = {
            'accept_encoding': 'gzip',
            'sdk_version': '7.72.8',
            'version_code': '7.72.8',
            'type': 'sdk_main',
            'platform': '',
            'terminal_type': '2',
            'device_model': '',
            'lark_version': '7.72.0',
            'net_status': '5',
            'lark_env': 'online',
            'request_id': generate_request_id(),
            'aid': '1',
            'fpid': '2',
            'device_id': device_id,
            'access_key': access_key,
            'ticket': ticket,
        }
        return f'{self.auth.region.ws_url}?{urlencode(params)}'

    @staticmethod
    def _wrap_frame(packet: P.Packet) -> bytes:
        frame = P.Frame()
        seconds = int(time.time())
        frame.seqid = seconds
        frame.logid = seconds
        frame.service = 1
        frame.method = 1
        entry = P.ExtendedEntry()
        entry.key = 'x-request-time'
        entry.value = str(int(time.time() * 1000))
        frame.headers.append(entry)
        frame.payloadType = 'pb'
        frame.payload = packet.SerializeToString()
        return frame.SerializeToString()

    async def send_ack(self, ws, sid: str):
        pkt = P.Packet()
        pkt.cmd = 1
        pkt.payloadType = 1
        pkt.sid = sid
        pkt.payload = b'\x08\x00'
        await ws.send(self._wrap_frame(pkt))

    async def send_heartbeat(self, ws):
        pkt = P.Packet()
        pkt.cmd = 4
        pkt.payloadType = 1
        await ws.send(self._wrap_frame(pkt))

    async def heartbeat_loop(self, ws, interval=30):
        try:
            while True:
                await asyncio.sleep(interval)
                await self.send_heartbeat(ws)
        except Exception as e:
            logger.warning(f'心跳停止: {e}')

    def _ensure_loop(self):
        if not self._loop_started:
            def run_loop(loop):
                asyncio.set_event_loop(loop)
                loop.run_forever()
            threading.Thread(target=run_loop, args=(self.loop,), daemon=True).start()
            self._loop_started = True

    async def connect_websocket(self, on_message):
        self._ensure_loop()
        url = self.build_ws_url()
        async with websockets.connect(url) as ws:
            logger.info('WS 已连接')
            heartbeat_task = asyncio.create_task(self.heartbeat_loop(ws))
            try:
                async for raw in ws:
                    try:
                        frame, packet = decoders.parse_ws_frame(raw)
                        sid, cmd = (packet.get('sid'), packet.get('cmd'))
                        if sid is not None:
                            await self.send_ack(ws, sid)
                        if cmd == 6:
                            for msg in decoders.decode_push_messages(raw):
                                if not msg.get('from_id'):
                                    continue
                                asyncio.run_coroutine_threadsafe(self._dispatch(msg, on_message), self.loop)
                        self.push_stats.packet(cmd, frame, packet)
                    except Exception as e:
                        self.push_stats.error(e, raw)
                    self.push_stats.maybe_report()
            finally:
                heartbeat_task.cancel()

    async def _dispatch(self, msg, on_message):
        try:
            # 推送里没有会话模式,话题群的根消息要查过 chatMode 才能归到它自己的话题
            if msg['scope'] == 'chat' and msg.get('chat_type') == 2 and await asyncio.to_thread(self.is_thread_chat, msg['chat_id']):
                msg['scope'], msg['anchor'] = decoders.resolve_scope(msg['msg_id'], msg['msg_type'], 2, '', '', thread_chat=True)
            await on_message(msg)
        except Exception as e:
            logger.error(f'消息回调异常: {e}')

    def _gateway_post(self, packet: P.Packet) -> bytes:
        headers = {
            'content-type': 'application/x-protobuf',
            'accept': '*/*',
            'origin': self.auth.region.web_origin,
            'referer': self.auth.region.web_origin + '/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
            'x-appid': self.auth.gateway_app_id,
            'x-command': str(packet.cmd),
            'x-command-version': '5.7.0',
            'x-lgw-os-type': '1',
            'x-lgw-terminal-type': '2',
            'x-source': 'web',
            'x-web-version': '3.9.32',
            'x-request-id': generate_long_request_id(),
        }
        resp = requests.post(self.auth.region.gateway_url, headers=headers, cookies=self.auth.cookies, data=packet.SerializeToString(), timeout=15)
        resp.raise_for_status()
        return resp.content

    def send_msg(self, text: str, chat_id: str, root_id: str=None, keep_unread: bool=False):
        """发一条纯文字消息, 见 send_message。"""
        return self.send_message({'kind': 'text', 'text': text}, chat_id, root_id=root_id, keep_unread=keep_unread)

    def send_markdown(self, text: str, chat_id: str, root_id: str=None, keep_unread: bool=False):
        """发一条 markdown 富文本(代码块、列表、标题、引用、加粗/斜体、链接), 见 send_message。"""
        return self.send_message({'kind': 'markdown', 'text': text}, chat_id, root_id=root_id, keep_unread=keep_unread)

    def send_message(self, msg: dict, chat_id: str, root_id: str=None, keep_unread: bool=False, fallback: bool=True,
                     parent_id: str=None, in_thread: bool=None):
        """发一条消息(文字、带 @ 的文字、图片、文件, 格式见 builders 里的消息描述)。
        返回服务端分配的消息 id;未能确认发送成功(请求失败,或响应里拿不到 id)时返回 None。

        普通发送只把本人在这个会话的已读位置推到最新(清掉本人的未读), 不会给别人的消息产生已读回执。
        root_id 是回复, 被回复的那条会对对方显示已读, 这是"回复"本身的意思。普通群和私聊里默认是主会话里的引用回复,
        回的那条已经在话题里时回进那个话题; 话题群里回进话题(见 _reply_target)。parent_id、in_thread 见 _reply_target。
        keep_unread(默认关, 对应配置 LARKX_KEEP_UNREAD): 关着时就是普通发送, 直接发到目标会话。打开后, 会话里还有
        本人的未读、又不是回复时, 经本人私聊转发过来(见 _send_via_relay), 目标会话里是一条普通消息, 本人的已读位置不动;
        转发不成就退回普通发送, 只清掉本人的未读, 对方照样看不到已读。
        fallback=False 时转发不成就不发了(返回 None), 绝不清掉本人的未读。
        各种类型的消息都走同一套流程。
        """
        chat_id = str(chat_id)
        if keep_unread and not root_id:
            read, last = self.read_state(chat_id)
            if self.relays(chat_id, read, last):
                msg_id = self._send_via_relay(msg, chat_id)
                if msg_id or not fallback:
                    return msg_id
                logger.warning(f'经本人私聊转发到会话 {chat_id} 没成功, 已退回普通发送: 本人在这个会话的未读会被清掉, 对方看不到已读')
        if not root_id:
            return self._put_message(msg, chat_id)
        thread_chat = self.is_thread_chat(chat_id)
        root_id, parent_id, in_thread = self._reply_target(root_id, parent_id, in_thread, thread_chat)
        return self._put_message(msg, chat_id, root_id=root_id, thread_chat=thread_chat, parent_id=parent_id, in_thread=in_thread)

    def _reply_target(self, root_id: str, parent_id: str=None, in_thread: bool=None, thread_chat: bool=False) -> tuple:
        """回复的 (rootId, parentId, 是否回进话题)。parent_id 是直接回的那条(上方显示的引用), 不给就是 root_id。

        话题群: 回进话题, rootId 就是 root_id(话题的根消息)。
        普通群和私聊: 默认是主会话里的引用回复, 和飞书客户端点"回复"一样, 不会新开话题; 只有回的那条已经在一个话题里
        (它是话题的根消息, 或者本身是话题里的回复)时才回进那个话题, 普通群话题里的对话(比如数字人在话题里答复)
        不会被拽到主会话。要判断时只读地查一下回的那条(PullMessagesByIdsRequest), 查不到就按引用回复发。
        in_thread 和 parent_id 都给了就照给的发, 不再查。
        """
        target = str(parent_id or root_id)
        if thread_chat:
            return str(root_id), target, False
        if in_thread is not None and parent_id:
            return str(root_id), target, bool(in_thread)
        v = {}
        try:
            got = (self.api('messages.PullMessagesByIdsRequest', {'messageIds': [target]}) or {}).get('messages') or {}
            v = next((m for m in got.values() if str(m.get('id')) == target), {})
        except Exception as e:
            logger.warning(f'查询被回复的消息 {target} 失败, 按引用回复发出: {e}')
        # Message.threadMessageType: 1 是话题的根消息, 2 是话题里的回复, 0 是普通消息(包括主会话里的引用回复)
        kind = v.get('threadMessageType') if isinstance(v.get('threadMessageType'), int) else 0
        if in_thread is None:
            in_thread = kind in (1, 2)
        if in_thread:
            # 回进话题: rootId 是话题的根消息(threadId); 那条还没有话题时(明确要求回进话题)以 root_id 为根新开一个
            return (str(v.get('threadId') or target) if kind else str(root_id)), target, True
        root = str(v.get('rootId') or '')
        return (root if root and root != '0' else target), target, False

    def relays(self, chat_id: str, read: int, last: int, root_id: str=None) -> bool:
        """keep_unread 时这条会不会经本人私聊转发: 会话里还有本人的未读(read < last)、不是回复、目标也不是本人私聊
        (本人私聊里只有本人, 普通发送不涉及别人)。"""
        return not root_id and read < last and str(chat_id) != self._relay_chat()

    def _put_message(self, msg: dict, chat_id: str, root_id: str=None, thread_chat: bool=False, cid: str=None,
                     parent_id: str=None, in_thread: bool=False):
        pkt = builders.build_message_packet(msg, chat_id, generate_long_request_id(), root_id=root_id,
                                            thread_chat=thread_chat, cid=cid, parent_id=parent_id, in_thread=in_thread)
        try:
            content = self._gateway_post(pkt)
        except Exception as e:
            logger.error(f'发送消息失败: {e}')
            return None
        try:
            msg_id = builders.decode_put_message_response(content)
        except Exception as e:
            logger.error(f'服务端已接受发送请求,但响应解析失败,无法确认是否发出: {e}')
            return None
        if not msg_id or msg_id.startswith('-'):
            # 拒收时(比如回复了不能当话题根的消息)返回的 id 是 -1, 会话里也没有这条
            logger.error(f'服务端已接受发送请求,但响应里没有有效的消息 id({msg_id or "空"}),消息没有发出')
            return None
        return msg_id

    def _relay_chat(self) -> str:
        """本人和自己的私聊(成员只有本人), 匿名发送(keep_unread)的中转站。"""
        if not self._self_chat:
            try:
                self._self_chat = str(self.create_chat(self.auth.user_id) or '')
            except Exception as e:
                logger.warning(f'获取本人私聊失败: {e}')
                return ''
        return self._self_chat

    def _send_via_relay(self, msg: dict, chat_id: str):
        """先发到本人私聊, 再转发到 chat_id, 然后撤回中转的那条。返回目标会话里那条的 id, 不成返回 None。

        实测转发不动本人在目标会话的已读位置, 也不给任何人的消息产生已读回执, 目标会话里是一条普通消息;
        图片、文件也一样转发(撤回中转消息不影响转发出去的那份)。
        @ 转发不过去: 实测转发(以及 BatchCopyMessagesRequest 复制)都会把 AT 元素变成普通文字、去掉 atUserIdList,
        对方收不到 @ 提醒。所以中转那条直接把 @ 写成文字发出, 不在本人私聊里 @ 别人。
        中转消息发出前先登记它的 cid(state.mark_relay), daemon 收到它时跳过, 不会当成本人在私聊里下的指令。
        """
        relay = self._relay_chat()
        if not relay:
            return None
        cid = generate_request_cid()
        state.mark_relay(cid, relay)
        relay_id = self._put_message(builders.without_mentions(msg), relay, cid=cid)
        if not relay_id:
            return None
        msg_id = ''
        try:
            resp = self.api('messages.PutTransmitMessageRequest',
                            {'chatIds': [chat_id], 'messageId': relay_id, 'cids': {chat_id: generate_request_cid()}}) or {}
            msg_id = str((((resp.get('messages') or {}).get(chat_id)) or {}).get('id') or '')
        except Exception as e:
            logger.error(f'转发到会话 {chat_id} 失败: {e}')
        if msg_id.startswith('-'):
            msg_id = ''
        if not msg_id:
            logger.error(f'转发到会话 {chat_id} 没有拿到有效的消息 id, 消息没有发出')
        try:
            self.api('messages.DeleteMessageRequest', {'id': relay_id})
        except Exception as e:
            logger.warning(f'撤回本人私聊里的中转消息 {relay_id} 失败: {e}')
        return msg_id or None

    def read_state(self, chat_id: str):
        """(本人已读到的 position, 会话最新 position)。"""
        info = self.get_chat_info(chat_id) or {}
        return int(info.get('readPosition') or 0), int(info.get('lastMessagePosition') or 0)

    def my_read_status(self, message_ids) -> dict:
        """本人是否已读这些消息, {msg_id: bool}。只读查询, 不改变已读状态。"""
        ids = [str(m) for m in message_ids if m]
        out = {}
        for i in range(0, len(ids), 50):
            r = self.api('messages.PullMessageMeReadStateRequest', {'messageIds': ids[i:i + 50]}) or {}
            out.update({str(k): bool(v) for k, v in (r.get('readStatus') or {}).items()})
        return out

    def search(self, query: str):
        pkt = builders.build_search_packet(query, generate_long_request_id(), self.auth.region.locale)
        return builders.decode_search_response(self._gateway_post(pkt))

    def create_chat(self, user_id: str):
        pkt = builders.build_create_chat_packet(str(user_id), generate_long_request_id())
        return builders.decode_put_chat_response(self._gateway_post(pkt))

    def get_user_name(self, user_id: str, chat_id: str):
        try:
            pkt = builders.build_user_info_packet(str(user_id), str(chat_id), generate_long_request_id())
            return builders.decode_user_info_response(self._gateway_post(pkt), self.auth.region.locale)
        except Exception:
            return None

    def mark_read(self, chat_id: str, max_position: int, message_ids=None) -> bool:
        data = {'chatId': str(chat_id), 'maxPosition': int(max_position)}
        if message_ids:
            data['messageIds'] = [str(m) for m in message_ids]
        try:
            self.api('messages.PutReadMessagesRequest', data)
            return True
        except Exception as e:
            logger.debug(f'标记已读失败: {e}')
            return False

    def get_group_name(self, chat_id: str):
        try:
            pkt = builders.build_group_info_packet(str(chat_id), generate_long_request_id())
            return builders.decode_group_info_response(self._gateway_post(pkt))
        except Exception:
            return None

    def api(self, type_or_cmd, data: dict=None, raw: bytes=None):
        from .proto import gateway
        cmd, req_cls, resp_cls = gateway.resolve_cmd(type_or_cmd)
        req_msg = None
        if req_cls is not None:
            req_msg = gateway.request_from_dict(req_cls, data or {})
        pkt = gateway.build_packet(cmd, generate_long_request_id(), req_msg=req_msg, req_bytes=raw if req_msg is None else None)
        content = self._gateway_post(pkt)
        resp_pkt = P.Packet()
        resp_pkt.ParseFromString(content)
        if resp_cls is not None and resp_pkt.payload:
            return gateway.response_to_dict(resp_cls, resp_pkt.payload)
        from protobuf_to_dict import protobuf_to_dict
        return protobuf_to_dict(resp_pkt)

    def get_chat_info(self, chat_id: str):
        resp = self.api('chats.PullChatsByIdsRequest', {'chatIds': [str(chat_id)]})
        chats = (resp or {}).get('chats') or {}
        for _, chat in chats.items():
            return chat
        return None

    def feed_chats(self, since: float, max_pages: int = 20) -> list:
        """消息列表(feed)里 since(unix 秒)之后有更新的会话 id, 新的在前。
        只读地一页页拉收件箱的 feed 卡片(feed.PullFeedCardsRequest, 实测不改变已读状态), 卡片按更新时间从新到旧排,
        翻到比 since 早就停; 只取会话卡片, 折叠在盒子里的会话也展开取出来, 话题、文档等别的卡片不要。"""
        def chats(cards):
            return [str(c['id']) for c in cards if c.get('type') in (1, 'CHAT') and int(c.get('updateTime') or 0) >= since]

        out, cursor = [], 0
        for _ in range(max_pages):
            data = {'type': 'INBOX', 'pullType': 'LOAD_MORE' if cursor else 'REFRESH', 'count': 100}
            if cursor:
                data['cursor'] = cursor
            resp = self.api('feed.PullFeedCardsRequest', data) or {}
            cards = resp.get('cards') or []
            out += chats(cards)
            for box in cards:
                if box.get('type') in (5, 'BOX') and int(box.get('updateTime') or 0) >= since:
                    inner = self.api('feed.PullFeedCardsRequest', {'type': 'INBOX', 'pullType': 'REFRESH', 'count': 100,
                                                                   'parentCardId': int(box['id'])}) or {}
                    out += chats(inner.get('cards') or [])
            cursor = int(resp.get('nextCursor') or 0)
            if not cards or not cursor or min(int(c.get('updateTime') or 0) for c in cards) < since:
                break
        return list(dict.fromkeys(out))

    def is_bot(self, user_id: str) -> bool:
        """发消息的这个 id 是不是机器人(应用、AI 助手)。消息上的 fromType 不可靠(机器人发的私聊也是 USER),
        只读地查一下 chatter 的类型(chatters.PullChattersByIdsRequest), 查过的记住; 查不到时当成不是。"""
        uid = str(user_id)
        if uid not in self._bots:
            try:
                resp = self.api('chatters.PullChattersByIdsRequest', {'chatterIds': [uid]}) or {}
            except Exception as e:
                logger.warning(f'查询 {uid} 是不是机器人失败, 按真人处理: {e}')
                return False
            v = (resp.get('chatters') or {}).get(uid)
            if not v:
                return False
            self._bots[uid] = decoders.chatter_is_bot(v.get('type'))
        return self._bots[uid]

    def is_thread_chat(self, chat_id: str) -> bool:
        chat_id = str(chat_id)
        if chat_id not in self._thread_chats:
            try:
                info = self.get_chat_info(chat_id)
            except Exception as e:
                logger.debug(f'查询会话模式失败,按普通会话处理: {e}')
                return False
            if info is None:
                return False
            self._thread_chats[chat_id] = decoders.is_thread_chat(info)
        return self._thread_chats[chat_id]

    def pull_history(self, chat_id: str, positions, save_storage=None):
        resp = self.api('messages.PullMessagesByPositionsRequest', {'chatId': str(chat_id), 'positions': [int(p) for p in positions]})
        messages = (resp or {}).get('messages') or {}
        thread_chat = self.is_thread_chat(chat_id)
        out = []
        for pos_key, v in sorted(messages.items(), key=lambda kv: int(kv[0])):
            msg = self._message_dict(v, chat_id, thread_chat, int(pos_key))
            out.append(msg)
            if save_storage is not None:
                try:
                    save_storage.save_message(msg)
                except Exception:
                    pass
        return out

    @staticmethod
    def _message_dict(v: dict, chat_id: str, thread_chat: bool, position: int) -> dict:
        message_type = decoders.enum_to_int(v.get('type', 0))
        chat_type = decoders.enum_to_int(v.get('chatType', 0))
        summary, data = decoders.decode_message_content(message_type, v.get('content', b''))
        root_id = v.get('rootId') or ''
        scope, anchor = decoders.resolve_scope(v.get('id'), message_type, chat_type, root_id, v.get('threadId') or '', thread_chat)
        return {'msg_id': v.get('id'), 'msg_type': message_type, 'msg_type_name': decoders.MSG_TYPE_NAMES.get(message_type, str(message_type)),
                'from_id': v.get('fromId'), 'from_type': decoders.from_type(v.get('fromType')),
                'chat_id': v.get('chatId') or v.get('channelId') or str(chat_id), 'chat_type': chat_type,
                'chat_type_name': decoders.CHAT_TYPE_NAMES.get(chat_type, str(chat_type)), 'scope': scope, 'anchor': anchor, 'at_me': False,
                'root_id': root_id, 'parent_id': v.get('parentId') or '', 'cid': v.get('cid') or '', 'position': v.get('position') or position,
                'create_time': v.get('createTime'), 'content': summary, 'content_data': data,
                'recalled': v.get('status') == 2 or bool(v.get('isRemoved'))}
