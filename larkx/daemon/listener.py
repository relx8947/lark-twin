import asyncio
import sys

from loguru import logger

from .. import state
from ..protocol import AuthExpired


class Listener:
    """WS 收消息 → 补全会话/发送人名字 → 入库 → 交给 on_stored。

    on_message 跑在协议层自己的事件循环线程里(LarkClient.loop), on_stored 需要自己切回调用方的循环。
    on_disconnect(异常) 在长连接断开(或重连失败)时调用, on_connect() 在每次连上后调用, 都在 run_forever 的循环里。
    """

    def __init__(self, client, storage, cfg: dict, my_user_id: str, on_stored=None, on_disconnect=None, on_connect=None):
        self.client = client
        self.storage = storage
        self.cfg = cfg
        self.my_user_id = str(my_user_id)
        self.on_stored = on_stored
        self.on_disconnect = on_disconnect
        self.on_connect = on_connect
        self.name_cache = {}

    async def on_message(self, msg) -> bool:
        """返回这条消息是否是第一次入库(重复推送、补收到已有的消息、中转消息返回 False)。"""
        if state.is_relay(msg):
            # 匿名发送(打开 LARKX_KEEP_UNREAD 时)经本人私聊中转的那条: 马上会被撤回, 不入库也不触发,
            # 免得当成本人在私聊里下的指令; 转发到目标会话的那份 cid 不同, 照常处理
            logger.info(f'跳过本人私聊里的中转消息 {msg.get("msg_id")}')
            return False
        client, st, cache = self.client, self.storage, self.name_cache
        chat_id = msg['chat_id']
        chat_type = msg.get('chat_type', 0)
        is_own = str(msg['from_id']) == self.my_user_id
        sender_key = (msg['from_id'], chat_id)
        if sender_key not in cache:
            cache[sender_key] = await asyncio.to_thread(client.get_user_name, msg['from_id'], chat_id)
        sender_name = cache.get(sender_key) or ''
        if chat_id not in cache:
            if chat_type in (2, 3):
                cache[chat_id] = await asyncio.to_thread(client.get_group_name, chat_id)
                if cache[chat_id]:
                    st.update_chat_name(chat_id, cache[chat_id], chat_type)
            elif chat_type == 1 and not is_own and sender_name:
                cache[chat_id] = sender_name
                st.update_chat_name(chat_id, sender_name, chat_type)
        chat_name = cache.get(chat_id) or ''
        saved = st.save_message(msg, sender_name=sender_name, chat_name=chat_name,
                                direction='out' if is_own else 'in')
        if not saved:
            return False
        content = msg['content'] or ''
        shown = content[:80] + ('…' if len(content) > 80 else '')
        line = f"[{chat_name or chat_id}] {sender_name or msg['from_id']}: {shown}"
        if is_own and sys.stderr.isatty():
            line = f'\x1b[36m{line}\x1b[0m'
        logger.info(line)
        if self.cfg.get('mark_read') and not is_own and msg.get('position'):
            await asyncio.to_thread(client.mark_read, chat_id, msg['position'], [msg['msg_id']])
        if self.on_stored:
            await self.on_stored(msg, chat_name, sender_name)
        return True

    async def run_forever(self):
        while True:
            try:
                await self.client.connect_websocket(self.on_message, on_open=self.on_connect)
            except AuthExpired:
                raise
            except Exception as e:
                logger.warning(f'WS 断开, 10s 后重连: {e}')
                if self.on_disconnect:
                    self.on_disconnect(e)
                await asyncio.sleep(10)
