import asyncio
import threading
import time
from urllib.parse import urlencode
import requests
import websockets
from loguru import logger
from .auth import LarkAuth
from .proto import builders, decoders, proto_pb2 as P
from .proto.ids import generate_access_key, generate_long_request_id, generate_request_id


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
                        _, packet = decoders.parse_ws_frame(raw)
                        sid, cmd = (packet.get('sid'), packet.get('cmd'))
                        if sid is not None:
                            await self.send_ack(ws, sid)
                        if cmd != 6:
                            continue
                        for msg in decoders.decode_push_messages(raw):
                            if not msg.get('from_id'):
                                continue
                            asyncio.run_coroutine_threadsafe(self._dispatch(msg, on_message), self.loop)
                    except Exception as e:
                        logger.debug(f'跳过异常帧: {e}')
                        continue
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
        """返回服务端分配的消息 id;未能确认发送成功(请求失败,或响应里拿不到 id)时返回 None。

        keep_unread: 会话里还有未读时, 以"回复话题并同时发送到会话"的方式发出, 对方在主会话照常看到这条消息,
        但本人的已读位置不动。被回复的那条会被标成已读, 所以没指定 root_id 时挂在不会泄露的消息下面
        (见 _unread_anchor); 指定了 root_id 就回复那条, 那一条对对方显示已读, 这是"回复"本身的意思。
        """
        chat_id = str(chat_id)
        thread_chat = (bool(root_id) or keep_unread) and self.is_thread_chat(chat_id)
        sync = False
        if keep_unread and not thread_chat:
            if not root_id:
                root_id, leaks = self._unread_anchor(chat_id)
                if leaks:
                    logger.warning(f'会话 {chat_id} 最近没有自己发的或已读过的消息可挂靠, 这次挂在对方的 {root_id} 下面, '
                                   '这一条会对对方显示已读')
            sync = bool(root_id)
        pkt = builders.build_send_message_packet(text, chat_id, generate_long_request_id(), root_id=root_id,
                                                 thread_chat=thread_chat, sync_to_chat=sync)
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
            # 拒收时(比如回复了不能当话题根的消息)返回的 id 是 -1, 主会话里也没有这条
            logger.error(f'服务端已接受发送请求,但响应里没有有效的消息 id({msg_id or "空"}),消息没有发出')
            return None
        return msg_id

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

    def _unread_anchor(self, chat_id: str):
        """会话里有未读时, 匿名发送挂在哪条消息下面: 返回 (msg_id, 这次是否会让一条别人的未读消息变成已读)。

        实测被回复的那条会被服务端标成"我已读", 所以依次找: 本人自己最近发的一条 > 已经是"我已读"的最近一条
        (已读位置以下的也要逐条查, 实测有的仍是未读) > 实在没有才挂在对方最近的一条下面, 这一条会泄露已读。
        没有未读返回空, 照常发送不会改变任何已读状态。
        """
        read, last = self.read_state(chat_id)
        if read >= last:
            return '', False
        me = str(self.auth.user_id)

        def pull(start, end):
            return [m for m in self.pull_history(chat_id, range(max(1, start), end + 1))
                    if m.get('msg_id') and m.get('msg_type') != 6 and not m.get('recalled')]

        def target(m):
            # "同时发送到会话"在主会话的那份不能当话题根(服务端拒收), 要挂在它所在话题的根上
            return str(m.get('sync_root_id') or m['msg_id'])

        # 本身是回复的(带 rootId)不挑, 免得连带把它回复的那条标成已读。自己那份的话题根是上次挂靠的那条, 已经读过
        recent = pull(last - 49, last)
        own = [m for m in recent if str(m.get('from_id')) == me and not m.get('root_id')]
        if own:
            return target(own[-1]), False
        # 最近没有自己发的: 连同已读位置附近的几条, 挑已经是"我已读"的; 别人"同时发送到会话"的话题根不一定读过, 不挑
        cands = (pull(read - 19, read) if 0 < read < last - 49 else []) + recent
        plain = [m for m in cands if not m.get('root_id') and (str(m.get('from_id')) == me or not m.get('sync_root_id'))]
        try:
            done = self.my_read_status([m['msg_id'] for m in plain])
        except Exception as e:
            logger.warning(f'查询消息已读状态失败, 按已读位置估计: {e}')
            done = {str(m['msg_id']): int(m.get('position') or 0) <= read for m in plain}
        safe = [m for m in plain if str(m.get('from_id')) == me or done.get(str(m['msg_id']))]
        if safe:
            return target(safe[-1]), False
        pick = ([m for m in cands if str(m.get('from_id')) != me] or cands)[-1:]
        return (target(pick[0]), True) if pick else ('', False)

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
            message_type = decoders.enum_to_int(v.get('type', 0))
            chat_type = decoders.enum_to_int(v.get('chatType', 0))
            summary, data = decoders.decode_message_content(message_type, v.get('content', b''))
            root_id = v.get('rootId') or ''
            scope, anchor = decoders.resolve_scope(v.get('id'), message_type, chat_type, root_id, v.get('threadId') or '', thread_chat)
            msg = {'msg_id': v.get('id'), 'msg_type': message_type, 'msg_type_name': decoders.MSG_TYPE_NAMES.get(message_type, str(message_type)), 'from_id': v.get('fromId'), 'chat_id': v.get('chatId') or v.get('channelId') or str(chat_id), 'chat_type': chat_type, 'chat_type_name': decoders.CHAT_TYPE_NAMES.get(chat_type, str(chat_type)), 'scope': scope, 'anchor': anchor, 'at_me': False, 'root_id': root_id, 'parent_id': v.get('parentId') or '', 'cid': v.get('cid') or '', 'position': v.get('position') or int(pos_key), 'create_time': v.get('createTime'), 'content': summary, 'content_data': data, 'recalled': v.get('status') == 2 or bool(v.get('isRemoved')), 'sync_root_id': str(v.get('syncToChatThreadRootId') or '')}
            out.append(msg)
            if save_storage is not None:
                try:
                    save_storage.save_message(msg)
                except Exception:
                    pass
        return out
