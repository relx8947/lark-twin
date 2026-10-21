"""larkx daemon: 常驻收消息, 把触发的消息、到点的任务排队交给一直开着的数字人会话。"""
import asyncio
import json
import signal
import time
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from .. import state
from ..agent import SessionCrashed, create_session, ensure_agent_home
from ..agent.home import commit_agent_home
from ..config import DATA_DIR, load_config
from ..protocol import LarkClient
from ..storage import Storage
from .listener import Listener
from .triggers import (build_prompt, build_say_prompt, build_system_prompt, build_task_prompt, parse_command,
                       should_handle, strip_prefix)

LOG_PATH = DATA_DIR / 'logs' / 'daemon.log'
DAEMON_STATE = state.STATE_DIR / 'daemon.json'
SCHEDULER_INTERVAL = 15
RETRY_NOTE = '上一次处理这条消息时 agent 进程中断过。先用 larkx messages 查一下是否已经回复过, 避免重复回复'
FLUSH_PROMPT = ('上下文快满了, 马上会被压缩, 压缩后早期的细节会丢失。现在把这段时间里值得长期记住、但还没写进 memory/ 的内容整理进去'
                '(人、偏好、约定、待跟进的事、群的背景), 更新 memory/MEMORY.md 索引。不要发飞书消息, 整理完直接结束。')
REVIEW_PROMPT = ('每日回顾: 用 larkx chats 和 larkx messages 看一下最近 24 小时本人参与的会话(重点是本人发的消息和找本人的消息), '
                 '把值得长期记住的新信息整理进 memory/ 并更新索引; 发现记忆里过时或错误的内容就改掉。'
                 '答应过但还没安排的跟进, 用 larkx task add 补上。不要发飞书消息, 整理完直接结束。')


@dataclass
class Event:
    kind: str
    prompt: str
    chat_id: str = ''
    anchor: str = ''
    is_owner: bool = False
    ref: str = ''
    task_id: int = 0
    msg: dict = None
    body: str = ''
    chat_name: str = ''
    sender_name: str = ''


class Daemon:
    def __init__(self, auth):
        self.cfg = load_config()
        self.auth = auth
        self.client = LarkClient(auth)
        self.storage = Storage()
        self.session = create_session(self.cfg, self.storage)
        self.prefix = (self.cfg.get('triggers') or {}).get('prefix') or ''
        self.inbox = asyncio.Queue()
        self.home = None
        self.loop = None

    def _banner(self):
        t = self.cfg.get('triggers') or {}
        print('─' * 56, flush=True)
        print(f'larkx daemon  region={self.auth.region.name}  user={self.auth.user_id}  agent={self.cfg["agent_backend"]}', flush=True)
        print(f'  数字人目录: {self.home}', flush=True)
        print(f'  触发前缀: {t.get("prefix") or "(全部消息)"}  允许自己触发: {self.cfg.get("allow_self")}', flush=True)
        print('─' * 56, flush=True)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.home = ensure_agent_home(self.cfg)
        self._banner()
        n = self.storage.reset_running_tasks()
        if n:
            logger.info(f'{n} 个上次没跑完的任务已重新排队')
        await self.session.start()
        workers = [asyncio.create_task(self._consume()), asyncio.create_task(self._schedule())]
        listener = Listener(self.client, self.storage, self.cfg, self.auth.user_id, on_stored=self._from_listener)
        try:
            await listener.run_forever()
        finally:
            for w in workers:
                w.cancel()
            state.clear_turn()
            await self.session.close()

    # ---- 飞书消息 ----

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
                await self._reply(chat_id, '✅ 已开启新会话(长期记忆保留)', root_id)
                return
        if not rest:
            return
        await self.inbox.put(Event('message', '', chat_id=chat_id, anchor=msg.get('anchor') or '', is_owner=is_owner,
                                   ref=str(msg.get('msg_id') or ''), msg=msg, body=rest, chat_name=chat_name,
                                   sender_name=sender_name))

    def _drain_inbox(self) -> int:
        """清空排队的飞书消息; 定时任务和终端指令放回队列。"""
        kept, n = [], 0
        while not self.inbox.empty():
            ev = self.inbox.get_nowait()
            if ev.kind == 'message':
                n += 1
            else:
                kept.append(ev)
        for ev in kept:
            self.inbox.put_nowait(ev)
        return n

    async def _reply(self, chat_id, text, root_id=None):
        msg_id = await asyncio.to_thread(self.client.send_msg, text, chat_id, root_id)
        if not msg_id:
            logger.error(f'控制回执发送失败 [{chat_id}]: {text}')

    # ---- 定时任务与每日回顾 ----

    def _load_state(self) -> dict:
        try:
            return json.loads(DAEMON_STATE.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self, data: dict):
        DAEMON_STATE.parent.mkdir(parents=True, exist_ok=True)
        DAEMON_STATE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    async def _schedule(self):
        while True:
            try:
                now = time.time()
                for task in self.storage.due_tasks(now):
                    self.storage.update_task(task.id, status='running')
                    if task.kind == 'say':
                        ev = Event('say', build_say_prompt(task.prompt), is_owner=True, ref=f'say#{task.id}', task_id=task.id)
                    else:
                        ev = Event('task', build_task_prompt(task, now), chat_id=task.chat_id, anchor=task.anchor,
                                   is_owner=bool(task.by_owner), ref=f'task#{task.id}', task_id=task.id)
                    await self.inbox.put(ev)
                hour = self.cfg.get('memory_review_hour', -1)
                st = self._load_state()
                today = datetime.now().strftime('%Y-%m-%d')
                if 0 <= hour <= 23 and datetime.now().hour >= hour and st.get('last_review') != today:
                    st['last_review'] = today
                    self._save_state(st)
                    await self.inbox.put(Event('system', build_system_prompt(REVIEW_PROMPT), ref='daily-review'))
            except Exception as e:
                logger.exception(f'调度异常: {e!r}')
            await asyncio.sleep(SCHEDULER_INTERVAL)

    def _finish_task(self, ev: Event, result):
        task = self.storage.get_task(ev.task_id)
        if not task:
            return
        fields = {'run_count': (task.run_count or 0) + 1, 'last_run_at': int(time.time()),
                  'result': (result.text if result else '')[:4000]}
        if task.every_sec and task.status != 'cancelled':
            fields.update(status='pending', due_at=int(state.next_due(task.due_at, task.every_sec, time.time())))
        elif task.status != 'cancelled':
            fields['status'] = 'done' if result and not result.is_error else 'failed'
        self.storage.update_task(task.id, **fields)

    # ---- 交给数字人 ----

    async def _run_turn(self, ev: Event, prompt: str):
        state.write_turn(ev.kind, ev.chat_id, ev.anchor, ev.is_owner, ev.ref)
        try:
            return await self.session.ask(prompt)
        finally:
            state.clear_turn()

    async def _handle(self, ev: Event):
        note, result = '', None
        for attempt in (1, 2):
            prompt = ev.prompt
            if ev.kind == 'message':
                prompt = build_prompt(ev.msg, ev.body, ev.chat_name, ev.sender_name, is_owner=ev.is_owner, note=note)
            try:
                result = await self._run_turn(ev, prompt)
                break
            except SessionCrashed as e:
                logger.error(f'agent 会话中断({attempt}/2): {e}')
                note = RETRY_NOTE
        if result:
            logger.info(f'[agent] {ev.kind} {ev.ref} 结束: {result.subtype} {result.duration_s:.1f}s 工具调用 {len(result.tools)} 次'
                        + (f' | 输出: {result.text[:200]}' if result.text else ''))
        if ev.task_id:
            self._finish_task(ev, result)
        await asyncio.to_thread(commit_agent_home, self.home, f'{ev.kind} {ev.ref}'.strip())

    async def _maybe_flush_memory(self):
        pressure = await self.session.context_pressure()
        if pressure is None or pressure < self.cfg['memory_flush_ratio']:
            return
        logger.info(f'[agent] 上下文已用到压缩阈值的 {pressure:.0%}, 先整理记忆再压缩')
        ev = Event('system', build_system_prompt(FLUSH_PROMPT), ref='memory-flush')
        try:
            await self._run_turn(ev, ev.prompt)
            await asyncio.to_thread(commit_agent_home, self.home, 'system memory-flush')
            await self._run_turn(Event('system', '/compact', ref='compact'), '/compact')
        except SessionCrashed as e:
            logger.error(f'整理记忆时 agent 会话中断: {e}')

    async def _consume(self):
        while True:
            ev = await self.inbox.get()
            try:
                await self._handle(ev)
                await self._maybe_flush_memory()
            except Exception as e:
                logger.exception(f'处理 {ev.kind} {ev.ref} 异常: {e!r}')


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
    # 自己写日志文件: 有的机器上普通用户读不到自己的 journal
    logger.add(LOG_PATH, level='INFO', rotation='10 MB', retention=5, encoding='utf-8')
    return asyncio.run(_run_until_signal(Daemon(auth).run()))


def run_listen(auth):
    """只收消息入库, 不接数字人。"""
    cfg = load_config()
    listener = Listener(LarkClient(auth), Storage(), cfg, auth.user_id)
    return asyncio.run(_run_until_signal(listener.run_forever()))
