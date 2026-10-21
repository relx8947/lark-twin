"""larkx daemon: 常驻收消息, 把触发的消息排队交给一直开着的数字人会话。"""
import asyncio
import signal

from loguru import logger

from ..agent import AgentSession, SessionCrashed, ensure_agent_home
from ..config import load_config
from ..protocol import LarkClient
from ..storage import Storage
from .listener import Listener
from .triggers import build_prompt, parse_command, should_handle, strip_prefix

RETRY_NOTE = '上一次处理这条消息时 agent 进程中断过。先用 larkx messages 查一下是否已经回复过, 避免重复回复'


class Daemon:
    def __init__(self, auth):
        self.cfg = load_config()
        self.auth = auth
        self.client = LarkClient(auth)
        self.storage = Storage()
        self.session = AgentSession(self.cfg, self.storage)
        self.prefix = (self.cfg.get('triggers') or {}).get('prefix') or ''
        self.inbox = asyncio.Queue()
        self.loop = None

    def _banner(self, home):
        t = self.cfg.get('triggers') or {}
        print('─' * 56, flush=True)
        print(f'larkx daemon  region={self.auth.region.name}  user={self.auth.user_id}', flush=True)
        print(f'  数字人目录: {home}', flush=True)
        print(f'  触发前缀: {t.get("prefix") or "(全部消息)"}  允许自己触发: {self.cfg.get("allow_self")}', flush=True)
        print('─' * 56, flush=True)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        home = ensure_agent_home(self.cfg)
        self._banner(home)
        await self.session.start()
        consumer = asyncio.create_task(self._consume())
        listener = Listener(self.client, self.storage, self.cfg, self.auth.user_id, on_stored=self._from_listener)
        try:
            await listener.run_forever()
        finally:
            consumer.cancel()
            await self.session.close()

    async def _from_listener(self, msg, chat_name, sender_name):
        # 监听回调跑在协议层的事件循环线程里, 切回 daemon 主循环处理, 队列和会话只在主循环里使用
        fut = asyncio.run_coroutine_threadsafe(self.on_message(msg, chat_name, sender_name), self.loop)
        await asyncio.wrap_future(fut)

    async def on_message(self, msg: dict, chat_name: str = '', sender_name: str = ''):
        if not should_handle(msg, self.cfg, self.auth.user_id):
            return
        is_owner = str(msg.get('from_id')) == str(self.auth.user_id)
        body = strip_prefix(msg.get('content'), self.prefix)
        # 主会话是所有会话共用的, 清空和打断只认本人
        cmd, rest = parse_command(body) if is_owner else (None, body)
        chat_id = str(msg['chat_id'])
        root_id = msg.get('anchor') or None
        if cmd == 'stop':
            n = self._drain_inbox()
            stopped = await self.session.interrupt()
            if not rest:
                parts = ['⏹ 已停止当前任务' if stopped else '⏹ 当前没有在处理的任务']
                if n:
                    parts.append(f'清空 {n} 条排队消息')
                await self._reply(chat_id, ', '.join(parts), root_id)
                return
        elif cmd == 'clear':
            self._drain_inbox()
            await self.session.reset()
            if not rest:
                await self._reply(chat_id, '✅ 已开启新会话', root_id)
                return
        if not rest:
            return
        await self.inbox.put((msg, rest, chat_name, sender_name, is_owner))

    def _drain_inbox(self) -> int:
        n = 0
        while not self.inbox.empty():
            self.inbox.get_nowait()
            n += 1
        return n

    async def _reply(self, chat_id, text, root_id=None):
        msg_id = await asyncio.to_thread(self.client.send_msg, text, chat_id, root_id)
        if not msg_id:
            logger.error(f'控制回执发送失败 [{chat_id}]: {text}')

    async def _consume(self):
        while True:
            msg, body, chat_name, sender_name, is_owner = await self.inbox.get()
            note = ''
            for attempt in (1, 2):
                prompt = build_prompt(msg, body, chat_name, sender_name, is_owner=is_owner, note=note)
                try:
                    r = await self.session.ask(prompt)
                except SessionCrashed as e:
                    logger.error(f'agent 会话中断({attempt}/2): {e}')
                    note = RETRY_NOTE
                    continue
                except Exception as e:
                    logger.exception(f'处理消息异常: {e!r}')
                    break
                logger.info(f'[agent] 本轮结束: {r.subtype} {r.duration_s:.1f}s 工具调用 {len(r.tools)} 次'
                            + (f' | 输出(未发送): {r.text[:200]}' if r.text else ''))
                break


async def _run_until_signal(coro):
    task = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        return await task
    except asyncio.CancelledError:
        logger.info('收到停止信号, 已退出')


def run_daemon(auth):
    return asyncio.run(_run_until_signal(Daemon(auth).run()))


def run_listen(auth):
    """只收消息入库, 不接数字人。"""
    cfg = load_config()
    listener = Listener(LarkClient(auth), Storage(), cfg, auth.user_id)
    return asyncio.run(_run_until_signal(listener.run_forever()))
