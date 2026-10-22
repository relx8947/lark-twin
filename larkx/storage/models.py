from datetime import datetime
from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base
Base = declarative_base()


class Message(Base):
    __tablename__ = 'messages'
    id = Column(Integer, primary_key=True, autoincrement=True)
    msg_id = Column(String(32), nullable=False)
    chat_id = Column(String(32), nullable=False, index=True)
    chat_name = Column(String(256), default='')
    chat_type = Column(Integer, default=0)
    scope = Column(String(8), default='chat')
    anchor = Column(String(32), default='', index=True)
    root_id = Column(String(32), default='')
    parent_id = Column(String(32), default='')
    sender_id = Column(String(32), default='')
    sender_name = Column(String(256), default='')
    msg_type = Column(Integer, default=0)
    msg_type_name = Column(String(32), default='')
    content = Column(Text, default='')
    content_data = Column(Text, default='')
    position = Column(Integer, default=0)
    create_time = Column(BigInteger, default=0)
    direction = Column(String(4), default='in')
    stored_at = Column(DateTime, default=datetime.now)
    __table_args__ = (UniqueConstraint('msg_id', 'chat_id', name='uq_msg_chat'), Index('ix_chat_anchor_time', 'chat_id', 'anchor', 'create_time'))


class Chat(Base):
    __tablename__ = 'chats'
    chat_id = Column(String(32), primary_key=True)
    name = Column(String(256), default='')
    chat_type = Column(Integer, default=0)
    last_position = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AgentSession(Base):
    __tablename__ = 'agent_sessions'

    session_key = Column(String(80), primary_key=True)
    chat_id = Column(String(32), default='', index=True)
    anchor = Column(String(32), default='')
    agent_session_id = Column(String(64), default='')
    started = Column(Integer, default=0)
    msg_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    last_active = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Action(Base):
    """审计: 通过 larkx 对外做的每个动作(发消息等), 记录是谁、在哪一轮里做的。"""
    __tablename__ = 'actions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    actor = Column(String(16), default='')
    action = Column(String(32), default='')
    chat_id = Column(String(32), default='')
    msg_id = Column(String(32), default='')
    content = Column(Text, default='')
    ok = Column(Integer, default=1)
    error = Column(Text, default='')
    turn_kind = Column(String(16), default='')
    turn_ref = Column(String(64), default='')


class Task(Base):
    """定时任务和终端指令: 到点后由 daemon 交给数字人处理。"""
    __tablename__ = 'tasks'
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now)
    kind = Column(String(16), default='task')
    due_at = Column(BigInteger, default=0, index=True)
    every_sec = Column(Integer, default=0)
    prompt = Column(Text, default='')
    chat_id = Column(String(32), default='')
    anchor = Column(String(32), default='')
    by_owner = Column(Integer, default=0)
    creator = Column(String(16), default='')
    status = Column(String(16), default='pending', index=True)
    run_count = Column(Integer, default=0)
    last_run_at = Column(BigInteger, default=0)
    result = Column(Text, default='')


class InboxItem(Base):
    """已收到、还没处理完的触发消息。daemon 重启后据此接着处理, 不丢消息。"""
    __tablename__ = 'inbox'
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now)
    msg_id = Column(String(32), default='', index=True)
    chat_id = Column(String(32), default='')
    payload = Column(Text, default='')
    status = Column(String(16), default='pending', index=True)


class Takeover(Base):
    """接管中的会话: 截止前对方的每条消息都直接交给数字人, 不需要触发前缀。"""
    __tablename__ = 'takeovers'
    chat_id = Column(String(32), primary_key=True)
    created_at = Column(DateTime, default=datetime.now)
    until = Column(BigInteger, default=0)
    goal = Column(Text, default='')
    creator = Column(String(16), default='')
    active = Column(Integer, default=1)


class Control(Base):
    """网页等外部入口给 daemon 的控制请求(打断、重置会话), daemon 每秒取一次执行。"""
    __tablename__ = 'controls'
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now)
    action = Column(String(32), default='')
    status = Column(String(16), default='pending', index=True)
    result = Column(Text, default='')



class WhitelistEntry(Base):
    """唤起白名单: 除本人外能叫起数字人的人。只按 user_id 匹配; 名字、部门、头像是添加时的快照, 只用来显示。"""
    __tablename__ = 'whitelist'
    user_id = Column(String(32), primary_key=True)
    name = Column(String(256), default='')
    department = Column(String(512), default='')
    avatar_key = Column(String(256), default='')
    added_by = Column(String(16), default='')
    added_at = Column(DateTime, default=datetime.now)
