"""larkx daemon: 常驻收消息, 把触发的消息、到点的任务排队交给一直开着的数字人会话。"""
import asyncio
import json
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from loguru import logger

from .. import state
from ..agent import SessionCrashed, create_session, ensure_agent_home
from ..agent.home import commit_agent_home
from ..config import CREDENTIALS_PATH, DATA_DIR, load_config
from ..notify import Notifier
from ..protocol import AuthExpired, LarkAuth, LarkClient
from ..storage import Storage
from .listener import Listener
from .triggers import (build_prompt, build_say_prompt, build_system_prompt, build_task_prompt, parse_command,
                       parse_takeover, should_handle, strip_prefix, takeover_note)

LOG_PATH = DATA_DIR / 'logs' / 'daemon.log'
DAEMON_STATE = state.STATE_DIR / 'daemon.json'
SCHEDULER_INTERVAL = 15
RETRY_NOTE = '上一次处理这条消息时 agent 进程中断过。先用 larkx messages 查一下是否已经回复过, 避免重复回复'
RESTART_NOTE = 'daemon 重启前收到了这条消息但没处理完。先用 larkx messages 查一下是否已经回复过, 避免重复回复'
MSG_FIELDS = ('msg_id', 'chat_id', 'chat_type', 'from_id', 'anchor', 'at_me', 'create_time', 'content')
CATCHUP_DELAY = 5
TAKEOVER_SUMMARY = ('接管「{name}」(chat_id={chat}) 已结束({why})。用 larkx messages {chat} --limit 60 回顾接管期间的对话, '
                    '给本人发一份简短总结: 聊了什么、谈成了什么、还有什么等本人决定或跟进。目标是: {goal}')
RECONNECT_ALERT = (3, 600)  # 10 分钟内断线 3 次就告警
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
    inbox_id: int = 0
    note: str = ''


class Daemon:
    def __init__(self, auth):
        self.cfg = load_config()
        self.auth = auth
        self.client = LarkClient(auth)
        self.storage = Storage()
        self.session = create_session(self.cfg, self.storage)
        self.notifier = Notifier(self.cfg, self.client)
        self.prefix = (self.cfg.get('triggers') or {}).get('prefix') or ''
        self.inbox = asyncio.Queue()
        self.home = None
        self.loop = None
        self.listener = None
        self._disconnects = []

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
        self._restore_inbox()
        self.listener = Listener(self.client, self.storage, self.cfg, self.auth.user_id, on_stored=self._from_listener,
                                 on_disconnect=self._on_disconnect)
        workers = [asyncio.create_task(self._consume()), asyncio.create_task(self._schedule()),
                   asyncio.create_task(self._catch_up_loop()), asyncio.create_task(self._control_loop())]
        try:
            await self.listener.run_forever()
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
        is_owner = str(msg.get('from_id')) == str(self.auth.user_id)
        chat_id = str(msg['chat_id'])
        tk = None if is_owner else self.storage.takeover_get(chat_id, time.time())
        if not tk and not should_handle(msg, self.cfg, self.auth.user_id):
            return
        age = time.time() - float(msg.get('create_time') or time.time())
        if age > self.cfg['inbox_max_age']:
            logger.info(f'跳过 {age / 60:.0f} 分钟前的消息 {msg.get("msg_id")}(超过 LARKX_INBOX_MAX_AGE)')
            return
        if tk:
            # 接管中的会话: 对方说的每句话都交给数字人, 不看触发规则
            return await self._enqueue_message(msg, (msg.get('content') or '').strip(), chat_name, sender_name, False,
                                               note=takeover_note(tk.goal, tk.until))
        body = strip_prefix(msg.get('content'), self.prefix)
        # 主会话是所有会话共用的, 清空、打断、接管只认本人
        cmd, rest = parse_command(body) if is_owner else (None, body)
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
        elif cmd == 'takeover':
            if chat_id == self.notifier.owner_chat():
                self.notifier.send('要接管哪个会话, 就在那个会话里发 /ai /takeover [时长] [目标]; '
                                   '或者在这里直接告诉数字人, 比如"接管和张三的私聊两小时, 帮我约下周二开会"')
                return
            seconds, goal = parse_takeover(rest, self.cfg['takeover_default'])
            until = time.time() + seconds
            self.storage.takeover_start(chat_id, until, goal, 'owner')
            when = datetime.fromtimestamp(until).strftime('%H:%M')
            # 确认发到本人私聊, 不在对方看得到的会话里出现
            self.notifier.send(f'✅ 已接管「{chat_name or chat_id}」到 {when}' + (f', 目标: {goal}' if goal else '')
                               + '。对方的消息都由数字人回复, 它发的每条都会抄送给你; 在那个会话发 /ai /release 或运行 '
                               f'larkx takeover stop {chat_id} 结束。')
            return
        elif cmd == 'release':
            if self.storage.takeover_expire(chat_id, time.time()):
                await self._finish_takeovers()
            else:
                self.notifier.send(f'「{chat_name or chat_id}」当前没有在接管')
            return
        if not rest:
            return
        await self._enqueue_message(msg, rest, chat_name, sender_name, is_owner)

    async def _enqueue_message(self, msg, body, chat_name, sender_name, is_owner, note=''):
        if not body:
            return
        if msg.get('msg_id') and self.storage.inbox_seen(msg['msg_id']):
            return
        # 先落库再排队: daemon 中途重启时, 没处理完的消息下次启动接着处理
        slim = {k: msg.get(k) for k in MSG_FIELDS}
        inbox_id = self.storage.inbox_add(slim['msg_id'] or '', str(msg['chat_id']),
                                          {'msg': slim, 'body': body, 'chat_name': chat_name, 'sender_name': sender_name,
                                           'is_owner': is_owner, 'note': note})
        await self.inbox.put(self._message_event(slim, body, chat_name, sender_name, is_owner, inbox_id, note=note))

    async def _finish_takeovers(self):
        """收尾已到期或被本人结束的接管: 让数字人在本人私聊发一份总结。"""
        owner_chat = self.notifier.owner_chat()
        for tk, stopped in self.storage.takeover_finish_due(time.time()):
            why = '本人结束' if stopped else '到期'
            name = self.storage.get_chat_name(tk.chat_id) or tk.chat_id
            logger.info(f'接管结束({why}): {name}')
            if not owner_chat:
                continue
            prompt = build_system_prompt(TAKEOVER_SUMMARY.format(name=name, chat=tk.chat_id, why=why, goal=tk.goal or '(无)'))
            prompt = prompt.replace('<system>', '<task>').replace('</system>', '</task>') + \
                f"\n\n用 larkx send {owner_chat} - <<'EOF' ... EOF 发给本人。"
            await self.inbox.put(Event('task', prompt, chat_id=owner_chat, is_owner=True, ref=f'takeover-end:{tk.chat_id}'))

    @staticmethod
    def _message_event(msg, body, chat_name, sender_name, is_owner, inbox_id, note=''):
        return Event('message', '', chat_id=str(msg['chat_id']), anchor=msg.get('anchor') or '', is_owner=is_owner,
                     ref=str(msg.get('msg_id') or ''), msg=msg, body=body, chat_name=chat_name, sender_name=sender_name,
                     inbox_id=inbox_id, note=note)

    def _restore_inbox(self):
        restored = expired = 0
        for row in self.storage.inbox_unfinished():
            try:
                p = json.loads(row.payload)
            except json.JSONDecodeError:
                self.storage.inbox_set(row.id, 'failed')
                continue
            age = time.time() - float(p['msg'].get('create_time') or 0)
            if age > self.cfg['inbox_max_age']:
                self.storage.inbox_set(row.id, 'expired')
                expired += 1
                continue
            note = '; '.join(n for n in (p.get('note'), RESTART_NOTE) if n)
            self.inbox.put_nowait(self._message_event(p['msg'], p['body'], p.get('chat_name', ''), p.get('sender_name', ''),
                                                      p.get('is_owner', False), row.id, note=note))
            restored += 1
        if restored or expired:
            logger.info(f'收件箱: {restored} 条重启前没处理完的消息接着处理, {expired} 条太久远已放弃')

    # ---- 外部控制(网页) ----

    async def _control_loop(self):
        while True:
            try:
                for c in self.storage.control_pending():
                    self.storage.control_done(c.id, await self.control(c.action))
            except Exception as e:
                logger.warning(f'处理控制请求失败: {e!r}')
            await asyncio.sleep(1)

    async def control(self, action: str) -> str:
        if action == 'interrupt':
            n = self._drain_inbox()
            stopped = await self.session.interrupt()
            return ('已停止当前任务' if stopped else '当前没有在处理的任务') + (f', 清空 {n} 条排队消息' if n else '')
        if action == 'clear':
            self._drain_inbox()
            await self.session.reset()
            return '已开启新会话(长期记忆保留)'
        return f'未知的控制动作 {action}'

    # ---- 漏收检查与断线告警 ----

    async def _catch_up_loop(self):
        interval = self.cfg.get('catchup_interval') or 0
        await asyncio.sleep(CATCHUP_DELAY)
        first = True
        while True:
            try:
                n = await self.catch_up()
                if n and not first:
                    logger.warning(f'补收到 {n} 条 WS 没推送的消息')
                    self.notifier.alert(f'长连接漏推了 {n} 条消息, 已从服务器补收并处理', key='ws-miss', cooldown=3600)
                elif n:
                    logger.info(f'补收到 {n} 条 daemon 启动前漏收的消息')
            except Exception as e:
                logger.warning(f'核对漏收消息失败: {e!r}')
            first = False
            if not interval:
                return
            await asyncio.sleep(interval)

    async def catch_up(self) -> int:
        """对比最近活跃会话在服务端的最新位置, 把本地没收到的消息补收进来(并照常判断是否触发)。"""
        chats = {c.chat_id: c.last_position or 0 for c in self.storage.recent_chats(datetime.now() - timedelta(days=7))}
        ids = [cid for cid, pos in chats.items() if pos]
        new = 0
        for i in range(0, len(ids), 20):
            resp = await asyncio.to_thread(self.client.api, 'chats.PullChatsByIdsRequest', {'chatIds': ids[i:i + 20]})
            for cid, info in ((resp or {}).get('chats') or {}).items():
                latest = int(info.get('lastMessagePosition') or 0)
                if latest <= chats.get(cid, latest):
                    continue
                positions = list(range(chats[cid] + 1, latest + 1))[-50:]
                for m in await asyncio.to_thread(self.client.pull_history, cid, positions):
                    if m.get('from_id') and await self.listener.on_message(m):
                        new += 1
        return new

    def _on_disconnect(self, error):
        now = time.time()
        times, window = RECONNECT_ALERT
        self._disconnects = [t for t in self._disconnects if now - t < window] + [now]
        if len(self._disconnects) >= times:
            self.notifier.alert(f'飞书长连接 {window // 60} 分钟内断开了 {len(self._disconnects)} 次, 最近一次: {error}',
                                key='ws-unstable', cooldown=3600)

    def _drain_inbox(self) -> int:
        """清空排队的飞书消息; 定时任务和终端指令放回队列。"""
        kept, n = [], 0
        while not self.inbox.empty():
            ev = self.inbox.get_nowait()
            if ev.kind == 'message':
                n += 1
                if ev.inbox_id:
                    self.storage.inbox_set(ev.inbox_id, 'cancelled')
            else:
                kept.append(ev)
        for ev in kept:
            self.inbox.put_nowait(ev)
        return n

    async def _reply(self, chat_id, text, root_id=None):
        if self.cfg.get('reply_signature'):
            text = f"{text}\n{self.cfg['reply_signature']}"
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
                await self._finish_takeovers()
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
        note, result = ev.note, None
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
        if ev.inbox_id:
            self.storage.inbox_set(ev.inbox_id, 'done' if result else 'failed')
        self._report(ev, result)
        if result:
            logger.info(f'[agent] {ev.kind} {ev.ref} 结束: {result.subtype} {result.duration_s:.1f}s 工具调用 {len(result.tools)} 次'
                        + (f' | 输出: {result.text[:200]}' if result.text else ''))
        if ev.task_id:
            self._finish_task(ev, result)
        await asyncio.to_thread(commit_agent_home, self.home, f'{ev.kind} {ev.ref}'.strip())

    def _report(self, ev: Event, result):
        """把这一轮里值得本人知道的异常发给本人。"""
        for note in getattr(self.session, 'events', [])[:]:
            self.notifier.alert(note, key=f'session:{note[:30]}', cooldown=1800)
        if hasattr(self.session, 'events'):
            self.session.events.clear()
        if result is None:
            self.notifier.alert(f'处理 {ev.kind} {ev.ref} 时 agent 两次中断, 这一条没有处理完', key='crash', cooldown=900)
        elif result.is_error and result.subtype != 'interrupted' and ev.kind != 'system':
            self.notifier.alert(f'处理 {ev.kind} {ev.ref} 失败({result.subtype}): {result.text[:150]}',
                                key=f'error:{result.text[:30]}', cooldown=1800)

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


def _log_to_file():
    # 自己写日志文件: 有的机器上普通用户读不到自己的 journal
    logger.add(LOG_PATH, level='INFO', rotation='10 MB', retention=5, encoding='utf-8')


def run_daemon(auth):
    _log_to_file()
    return asyncio.run(_run_until_signal(Daemon(auth).run()))


def _credentials_mtime() -> float:
    try:
        return CREDENTIALS_PATH.stat().st_mtime
    except FileNotFoundError:
        return 0


def run_service(wait_interval: int = 60):
    """无人值守运行(systemd): 飞书登录失效时用备用通道告警, 等本人重新登录后自动恢复。"""
    _log_to_file()
    cfg = load_config()
    notifier = Notifier(cfg)
    while True:
        auth = LarkAuth()
        ok, msg = auth.validate(fetch_profile=True)
        if ok and not auth.user_id:
            ok, msg = False, '拿不到当前账号的 user_id'
        if ok:
            auth.save()
            try:
                return asyncio.run(_run_until_signal(Daemon(auth).run()))
            except AuthExpired as e:
                msg = str(e)
        logger.error(f'飞书登录不可用: {msg}')
        notifier.fallback_alert(f'飞书登录已失效, 数字人暂停工作。在服务器上运行 larkx auth qr 重新登录后会自动恢复。({msg[:120]})',
                                key='auth-expired', cooldown=6 * 3600)
        seen = _credentials_mtime()
        while _credentials_mtime() == seen:
            time.sleep(wait_interval)
        logger.info('检测到凭证更新, 重新连接')


def run_listen(auth):
    """只收消息入库, 不接数字人。"""
    cfg = load_config()
    listener = Listener(LarkClient(auth), Storage(), cfg, auth.user_id)
    return asyncio.run(_run_until_signal(listener.run_forever()))
