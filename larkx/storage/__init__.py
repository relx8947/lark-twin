import json
from datetime import datetime
from sqlalchemy import create_engine, desc, func, or_
from sqlalchemy.orm import sessionmaker
from ..config import load_config
from .models import Action, AgentSession, Base, Chat, Control, InboxItem, Message, Takeover, Task


def _json_default(o):
    # 内联缩略图等原始字节对入库无用,转成字符串会撑爆长度上限、截断出非法 JSON
    if isinstance(o, (bytes, bytearray)):
        return f'<{len(o)} bytes>'
    return str(o)


class Storage:
    def __init__(self, url: str=None):
        cfg = load_config()
        self.url = url or cfg['storage_url']
        self.engine = create_engine(self.url, pool_pre_ping=True)
        Base.metadata.create_all(self.engine)
        # 方法返回的对象在会话关闭后还要读属性, 提交时不让它们过期
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def save_message(self, msg: dict, sender_name='', chat_name='', direction='in') -> bool:
        s = self.Session()
        try:
            fields = dict(msg_id=str(msg['msg_id']), chat_id=str(msg['chat_id']), chat_name=chat_name or '', chat_type=msg.get('chat_type', 0), scope=msg.get('scope', 'chat'), anchor=msg.get('anchor', '') or '', root_id=msg.get('root_id', '') or '', parent_id=msg.get('parent_id', '') or '', sender_id=str(msg.get('from_id') or ''), sender_name=sender_name or '', msg_type=msg.get('msg_type', 0), msg_type_name=msg.get('msg_type_name', ''), content=msg.get('content', '') or '', content_data=json.dumps(msg.get('content_data'), ensure_ascii=False, default=_json_default)[:20000] if msg.get('content_data') is not None else '', position=msg.get('position') or 0, create_time=msg.get('create_time') or 0, direction=direction)
            row = s.query(Message).filter_by(msg_id=fields['msg_id'], chat_id=fields['chat_id']).first()
            if row:
                # lark send 先落的记录没有 position;WS 回显到达时用回显补全,并视作首次入库
                if row.position or not fields['position']:
                    return False
                for k, v in fields.items():
                    setattr(row, k, v)
            else:
                row = Message(**fields)
                s.add(row)
            self._touch_chat(s, row)
            s.commit()
            return True
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def _touch_chat(self, s, row: Message):
        chat = s.query(Chat).filter_by(chat_id=row.chat_id).first()
        if not chat:
            chat = Chat(chat_id=row.chat_id, chat_type=row.chat_type)
            s.add(chat)
        if row.chat_name and (not chat.name):
            chat.name = row.chat_name
        if row.position and row.position > (chat.last_position or 0):
            chat.last_position = row.position
        chat.updated_at = datetime.now()

    def update_chat_name(self, chat_id: str, name: str, chat_type: int=0):
        s = self.Session()
        try:
            chat = s.query(Chat).filter_by(chat_id=str(chat_id)).first()
            if not chat:
                chat = Chat(chat_id=str(chat_id), chat_type=chat_type)
                s.add(chat)
            chat.name = name
            s.commit()
        finally:
            s.close()

    def list_chats(self, limit=50):
        s = self.Session()
        try:
            return s.query(Chat).order_by(desc(Chat.updated_at)).limit(limit).all()
        finally:
            s.close()

    def get_messages(self, chat_id: str, anchor: str=None, limit=50, before_ts: int=None):
        s = self.Session()
        try:
            q = s.query(Message).filter(Message.chat_id == str(chat_id))
            if anchor is not None:
                q = q.filter(Message.anchor == anchor)
            if before_ts:
                q = q.filter(Message.create_time < before_ts)
            rows = q.order_by(desc(Message.create_time)).limit(limit).all()
            rows.reverse()
            return rows
        finally:
            s.close()

    def get_chat_name(self, chat_id: str) -> str:
        s = self.Session()
        try:
            chat = s.query(Chat).filter_by(chat_id=str(chat_id)).first()
            return chat.name if chat else ''
        finally:
            s.close()

    def session_get(self, session_key: str):
        s = self.Session()
        try:
            return s.query(AgentSession).filter_by(session_key=session_key).first()
        finally:
            s.close()

    def session_upsert(self, session_key: str, chat_id: str, anchor: str, agent_session_id: str, started: bool):
        s = self.Session()
        try:
            row = s.query(AgentSession).filter_by(session_key=session_key).first()
            if not row:
                row = AgentSession(session_key=session_key, chat_id=chat_id, anchor=anchor or '')
                s.add(row)
            row.agent_session_id = agent_session_id
            row.started = 1 if started else 0
            s.commit()
        finally:
            s.close()

    def session_touch(self, session_key: str):
        s = self.Session()
        try:
            row = s.query(AgentSession).filter_by(session_key=session_key).first()
            if row:
                row.msg_count = (row.msg_count or 0) + 1
                row.last_active = datetime.now()
                s.commit()
        finally:
            s.close()

    def list_sessions(self, limit=50):
        s = self.Session()
        try:
            return s.query(AgentSession).order_by(desc(AgentSession.last_active)).limit(limit).all()
        finally:
            s.close()

    def log_action(self, **fields):
        s = self.Session()
        try:
            if 'content' in fields:
                fields['content'] = (fields['content'] or '')[:2000]
            s.add(Action(**fields))
            s.commit()
        finally:
            s.close()

    def list_actions(self, limit=50):
        s = self.Session()
        try:
            rows = s.query(Action).order_by(desc(Action.id)).limit(limit).all()
            rows.reverse()
            return rows
        finally:
            s.close()

    def add_task(self, **fields) -> int:
        s = self.Session()
        try:
            row = Task(**fields)
            s.add(row)
            s.commit()
            return row.id
        finally:
            s.close()

    def get_task(self, task_id: int):
        s = self.Session()
        try:
            return s.get(Task, task_id)
        finally:
            s.close()

    def list_tasks(self, include_finished=False, limit=100):
        s = self.Session()
        try:
            q = s.query(Task)
            if not include_finished:
                q = q.filter(Task.status.in_(('pending', 'running')))
            return q.order_by(Task.due_at).limit(limit).all()
        finally:
            s.close()

    def due_tasks(self, now: float):
        s = self.Session()
        try:
            return s.query(Task).filter(Task.status == 'pending', Task.due_at <= now).order_by(Task.due_at).all()
        finally:
            s.close()

    def update_task(self, task_id: int, **fields):
        s = self.Session()
        try:
            row = s.get(Task, task_id)
            if row:
                for k, v in fields.items():
                    setattr(row, k, v)
                s.commit()
        finally:
            s.close()

    def reset_running_tasks(self) -> int:
        """daemon 重启时, 上次没跑完的任务重新排队。"""
        s = self.Session()
        try:
            n = s.query(Task).filter(Task.status == 'running').update({'status': 'pending'})
            s.commit()
            return n
        finally:
            s.close()

    def count_actions(self, actor: str, action: str, since: datetime) -> int:
        s = self.Session()
        try:
            return s.query(Action).filter(Action.actor == actor, Action.action == action, Action.ok == 1,
                                          Action.created_at >= since).count()
        finally:
            s.close()

    def find_action(self, actor: str, action: str, msg_id: str):
        s = self.Session()
        try:
            return s.query(Action).filter_by(actor=actor, action=action, msg_id=str(msg_id), ok=1).first()
        finally:
            s.close()

    def sender_names(self, user_ids) -> dict:
        """消息库里见过的发送人名字: {user_id: name}。"""
        ids = [str(u) for u in user_ids if u]
        if not ids:
            return {}
        s = self.Session()
        try:
            rows = (s.query(Message.sender_id, Message.sender_name)
                    .filter(Message.sender_id.in_(ids), Message.sender_name != '').distinct().all())
            return {sid: name for sid, name in rows if name}
        finally:
            s.close()

    def recalled_ids(self, msg_ids) -> set:
        """这些消息里已经成功撤回的。"""
        ids = [str(m) for m in msg_ids if m]
        if not ids:
            return set()
        s = self.Session()
        try:
            rows = s.query(Action.msg_id).filter(Action.action == 'recall', Action.ok == 1, Action.msg_id.in_(ids)).all()
            return {r[0] for r in rows}
        finally:
            s.close()

    def recent_chats(self, since: datetime, limit=30):
        s = self.Session()
        try:
            return s.query(Chat).filter(Chat.updated_at >= since).order_by(desc(Chat.updated_at)).limit(limit).all()
        finally:
            s.close()

    def inbox_add(self, msg_id: str, chat_id: str, payload: dict) -> int:
        s = self.Session()
        try:
            row = InboxItem(msg_id=str(msg_id), chat_id=str(chat_id), payload=json.dumps(payload, ensure_ascii=False, default=_json_default))
            s.add(row)
            s.commit()
            return row.id
        finally:
            s.close()

    def inbox_seen(self, msg_id: str) -> bool:
        s = self.Session()
        try:
            return s.query(InboxItem).filter_by(msg_id=str(msg_id)).first() is not None
        finally:
            s.close()

    def inbox_set(self, item_id: int, status: str):
        s = self.Session()
        try:
            row = s.get(InboxItem, item_id)
            if row:
                row.status = status
                s.commit()
        finally:
            s.close()

    def inbox_unfinished(self):
        s = self.Session()
        try:
            return s.query(InboxItem).filter(InboxItem.status == 'pending').order_by(InboxItem.id).all()
        finally:
            s.close()

    def takeover_start(self, chat_id: str, until: int, goal: str = '', creator: str = ''):
        s = self.Session()
        try:
            row = s.get(Takeover, str(chat_id)) or Takeover(chat_id=str(chat_id))
            row.until, row.goal, row.creator, row.active, row.created_at = int(until), goal or '', creator, 1, datetime.now()
            s.merge(row)
            s.commit()
        finally:
            s.close()

    def takeover_get(self, chat_id: str, now: float):
        s = self.Session()
        try:
            row = s.get(Takeover, str(chat_id))
            return row if row and row.active and row.until > now else None
        finally:
            s.close()

    def takeover_list(self):
        s = self.Session()
        try:
            return s.query(Takeover).filter(Takeover.active == 1).order_by(Takeover.until).all()  # 不含正在收尾的
        finally:
            s.close()

    def takeover_expire(self, chat_id: str, now: float):
        """本人提前结束: 立即停止接管(active=2), 由 daemon 在下一次调度时收尾。"""
        s = self.Session()
        try:
            row = s.get(Takeover, str(chat_id))
            if row and row.active == 1:
                row.until, row.active = int(now), 2
                s.commit()
                return True
            return False
        finally:
            s.close()

    def takeover_finish_due(self, now: float):
        """取出已到期或被本人结束的接管并标记为结束, 每条只会被取出一次。返回 [(接管, 是否本人结束)]。"""
        s = self.Session()
        try:
            rows = s.query(Takeover).filter(Takeover.active.in_((1, 2)), Takeover.until <= now).all()
            out = [(r, r.active == 2) for r in rows]
            for r in rows:
                r.active = 0
            s.commit()
            return out
        finally:
            s.close()

    def owner_name(self, user_id: str) -> str:
        s = self.Session()
        try:
            row = (s.query(Message.sender_name, func.count()).filter(Message.sender_id == str(user_id),
                   Message.sender_name.notin_(('', '我'))).group_by(Message.sender_name).order_by(desc(func.count())).first())
            return row[0] if row else ''
        finally:
            s.close()

    def mentions(self, user_id: str, name: str, since_ts: int, limit=200):
        """since_ts 之后找本人的消息: 别人发来的私聊, 以及 @ 到本人的群消息; 附带本人之后有没有在该会话回过话。"""
        s = self.Session()
        try:
            q = s.query(Message).filter(Message.create_time >= since_ts, Message.sender_id != str(user_id),
                                        Message.direction == 'in')
            cond = [Message.chat_type == 1]
            if name:
                cond.append(Message.content.like(f'%@{name}%'))
            rows = q.filter(or_(*cond)).order_by(Message.create_time).limit(limit).all()
            out = []
            for r in rows:
                replied = s.query(Message.id).filter(Message.chat_id == r.chat_id, Message.sender_id == str(user_id),
                                                     Message.create_time >= r.create_time).first() is not None
                out.append({'msg_id': r.msg_id, 'chat_id': r.chat_id, 'chat_name': r.chat_name, 'chat_type': r.chat_type,
                            'sender_name': r.sender_name, 'sender_id': r.sender_id, 'create_time': r.create_time,
                            'content': r.content, 'kind': 'p2p' if r.chat_type == 1 else 'at', 'replied': replied})
            return out
        finally:
            s.close()

    def control_add(self, action: str) -> int:
        s = self.Session()
        try:
            row = Control(action=action)
            s.add(row)
            s.commit()
            return row.id
        finally:
            s.close()

    def control_get(self, control_id: int):
        s = self.Session()
        try:
            return s.get(Control, control_id)
        finally:
            s.close()

    def control_pending(self):
        s = self.Session()
        try:
            return s.query(Control).filter(Control.status == 'pending').order_by(Control.id).all()
        finally:
            s.close()

    def control_done(self, control_id: int, result: str, status: str = 'done'):
        s = self.Session()
        try:
            row = s.get(Control, control_id)
            if row:
                row.status, row.result = status, result
                s.commit()
        finally:
            s.close()

    def list_inbox(self, limit=50):
        s = self.Session()
        try:
            rows = s.query(InboxItem).order_by(desc(InboxItem.id)).limit(limit).all()
            return rows
        finally:
            s.close()

    def actions_for_turns(self, refs):
        s = self.Session()
        try:
            return s.query(Action).filter(Action.turn_ref.in_([str(r) for r in refs])).order_by(Action.id).all() if refs else []
        finally:
            s.close()

    def count_actions_since(self, since: datetime, actor: str = 'agent', action: str = 'send') -> int:
        return self.count_actions(actor, action, since)
