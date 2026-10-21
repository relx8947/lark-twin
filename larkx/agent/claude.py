"""Claude 后端: 一个一直开着的 ClaudeSDKClient。

一次只处理一轮: 发出一条消息后读到 ResultMessage 才算结束。上一轮没结束时再 query,
Claude Code 会把新消息插进当前这一轮(实测), 所以排队由 daemon 负责, 这里用锁保证串行。
"""
import asyncio
import glob
import os
import time
import uuid
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, CLIConnectionError,
                              ProcessError, ResultMessage, SystemMessage, TextBlock, ToolUseBlock)
from loguru import logger

from ..config import ACTOR_ENV
from .base import SessionCrashed, TurnResult, agent_path, resolve_agent_cli
from .hooks import build_hooks

SESSION_KEY = 'main'
# 从另一个 Claude Code 会话里启动 daemon 时会继承这些变量, agent 进程会被当成那个会话的子会话
PARENT_SESSION_VARS = ('CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN',
                       'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_PID',
                       'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_EXECPATH', 'CLAUDE_EFFORT')


class ClaudeSession:
    def __init__(self, cfg: dict, storage):
        self.cfg = cfg
        self.storage = storage
        self.home = Path(cfg['agent_home'])
        self.claude_dir = Path(cfg['agent_claude_dir'])
        self.session_id = ''
        self.busy = False
        self._client = None
        self._queue = None
        self._pump = None
        self._lock = asyncio.Lock()

    def _transcript_exists(self, sid: str) -> bool:
        return bool(sid) and bool(glob.glob(str(self.claude_dir / 'projects' / '*' / f'{sid}.jsonl')))

    def _options(self, resume: bool) -> ClaudeAgentOptions:
        env = {
            'CLAUDE_CONFIG_DIR': str(self.claude_dir),
            'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1',
            # 后台任务结束时会话会自己开新的一轮, 打乱"一条消息一轮"的对应关系
            'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1',
            ACTOR_ENV: 'agent',
            'PATH': agent_path(),
        }
        kw = dict(
            cwd=str(self.home),
            setting_sources=['project'],
            skills=['larkx'],
            system_prompt={'type': 'preset', 'preset': 'claude_code'},
            permission_mode='bypassPermissions',
            env=env,
            hooks=build_hooks(),
        )
        if self.cfg.get('claude_cli'):
            kw['cli_path'] = resolve_agent_cli(self.cfg['claude_cli'])
        if self.cfg.get('agent_model'):
            kw['model'] = self.cfg['agent_model']
        if self.cfg.get('agent_fallback_model'):
            kw['fallback_model'] = self.cfg['agent_fallback_model']
        if resume:
            kw['resume'] = self.session_id
        else:
            kw['session_id'] = self.session_id
        return ClaudeAgentOptions(**kw)

    async def start(self):
        for k in PARENT_SESSION_VARS:
            os.environ.pop(k, None)
        row = self.storage.session_get(SESSION_KEY)
        resume = bool(row and self._transcript_exists(row.agent_session_id))
        if resume:
            self.session_id = row.agent_session_id
        else:
            if row and row.agent_session_id:
                logger.warning(f'找不到上次的会话记录 {row.agent_session_id}, 开新会话')
            self._new_session_id()
        await self._connect(resume)
        logger.info(f'数字人会话已{"恢复" if resume else "创建"}: {self.session_id}')

    def _new_session_id(self):
        self.session_id = str(uuid.uuid4())
        self.storage.session_upsert(SESSION_KEY, '', '', self.session_id, False)

    async def _connect(self, resume: bool):
        self._client = ClaudeSDKClient(self._options(resume))
        await self._client.connect()
        self._queue = asyncio.Queue()
        self._pump = asyncio.create_task(self._pump_messages(self._client, self._queue))

    @staticmethod
    async def _pump_messages(client, queue):
        try:
            async for m in client.receive_messages():
                await queue.put(m)
            await queue.put(SessionCrashed('agent 进程的输出流已结束'))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await queue.put(SessionCrashed(f'agent 进程异常: {e!r}'))

    async def _disconnect(self):
        if self._pump:
            self._pump.cancel()
        if self._client:
            try:
                await asyncio.wait_for(self._client.disconnect(), 15)
            except Exception as e:
                logger.debug(f'断开 agent 会话时出错(忽略): {e!r}')
        self._client = self._pump = self._queue = None

    async def _recover(self):
        await self._disconnect()
        try:
            await self._connect(resume=self._transcript_exists(self.session_id))
            logger.warning(f'agent 进程已重连, 会话 {self.session_id}')
        except Exception as e:
            logger.error(f'agent 进程重连失败, 下一条消息时重试: {e!r}')

    def _drain_stale(self) -> bool:
        """丢弃上一轮之后残留的消息; 发现进程已退出时返回 True。"""
        crashed = False
        while self._queue and not self._queue.empty():
            m = self._queue.get_nowait()
            crashed = crashed or isinstance(m, Exception)
            logger.debug(f'丢弃残留消息: {type(m).__name__}')
        return crashed

    async def ask(self, prompt: str) -> TurnResult:
        async with self._lock:
            if self._client is None or self._drain_stale():
                await self._recover()
            if self._client is None:
                raise SessionCrashed('agent 进程未连接')
            self.busy = True
            started = time.monotonic()
            try:
                await self._client.query(prompt)
                return await self._read_turn(started)
            except SessionCrashed:
                await self._recover()
                raise
            except (CLIConnectionError, ProcessError) as e:
                await self._recover()
                raise SessionCrashed(f'{e!r}') from e
            finally:
                self.busy = False

    async def _read_turn(self, started: float) -> TurnResult:
        texts, tools = [], []
        deadline = started + self.cfg['agent_turn_timeout']
        interrupted = False
        while True:
            try:
                m = await asyncio.wait_for(self._queue.get(), max(deadline - time.monotonic(), 1))
            except asyncio.TimeoutError:
                if interrupted:
                    raise SessionCrashed('中断后 60 秒仍未结束这一轮')
                logger.warning('这一轮处理超时, 中断')
                interrupted = True
                await self._client.interrupt()
                deadline = time.monotonic() + 60
                continue
            if isinstance(m, Exception):
                raise m
            if isinstance(m, SystemMessage):
                self._on_system(m)
            elif isinstance(m, AssistantMessage):
                for b in m.content:
                    if isinstance(b, TextBlock):
                        texts.append(b.text)
                    elif isinstance(b, ToolUseBlock):
                        tools.append(b.name)
            elif isinstance(m, ResultMessage):
                self._mark_active()
                return TurnResult(subtype=m.subtype, is_error=m.is_error, text=''.join(texts).strip(),
                                  duration_s=time.monotonic() - started, tools=tools)

    def _on_system(self, m: SystemMessage):
        data = m.data or {}
        if m.subtype == 'init':
            logger.debug(f'agent init: model={data.get("model")} skills={data.get("skills")}')
        elif m.subtype == 'compact_boundary':
            meta = data.get('compact_metadata') or {}
            logger.info(f'[agent] 上下文已压缩 {meta.get("pre_tokens")} → {meta.get("post_tokens")} tokens ({meta.get("trigger")})')
        elif m.subtype == 'model_refusal_fallback':
            logger.warning(f'[agent] 主模型被安全策略拦截, 整个会话已切到备用模型: {str(data)[:300]}')
        else:
            logger.debug(f'agent system[{m.subtype}]')

    def _mark_active(self):
        row = self.storage.session_get(SESSION_KEY)
        if not (row and row.started and row.agent_session_id == self.session_id):
            self.storage.session_upsert(SESSION_KEY, '', '', self.session_id, True)
        self.storage.session_touch(SESSION_KEY)

    async def context_pressure(self):
        if not self._client or self.busy:
            return None
        try:
            u = await self._client.get_context_usage()
        except Exception as e:
            logger.debug(f'查询上下文用量失败: {e!r}')
            return None
        limit = u.get('autoCompactThreshold') or u.get('maxTokens') or 0
        return (u.get('totalTokens') or 0) / limit if limit else None

    async def interrupt(self) -> bool:
        if self.busy and self._client:
            await self._client.interrupt()
            return True
        return False

    async def reset(self):
        await self.interrupt()
        async with self._lock:
            await self._disconnect()
            self._new_session_id()
            await self._connect(resume=False)
        logger.info(f'数字人会话已重置: {self.session_id}')

    async def close(self):
        await self.interrupt()
        await self._disconnect()
