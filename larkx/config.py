import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_home_env = os.environ.get("LARKX_HOME") or str(Path.home() / ".larkx")
DATA_DIR = Path(os.path.expanduser(_home_env))
CREDENTIALS_PATH = DATA_DIR / "credentials.json"
DB_DEFAULT_PATH = DATA_DIR / "messages.db"

# 数字人的 agent 进程由 daemon 以这个值启动; larkx CLI 据此识别调用方是 agent 还是人
ACTOR_ENV = "LARKX_ACTOR"

DEFAULTS = {
    "region": "feishu",
    "storage_url": f"sqlite:///{DB_DEFAULT_PATH}",
    "mark_read": True,
    # 允许自己账号发的消息也触发数字人(单账号自测用)。安全前提是触发前缀: agent 的回复被 CLI 禁止以前缀开头
    "allow_self": False,
    # 数字人工作目录: CLAUDE.md、记忆、skill 都在这里, agent 只加载这里的配置
    "agent_home": str(DATA_DIR / "agent"),
    # 用哪种 coding agent 驱动数字人: claude(Claude Code) | codex
    "agent_backend": "claude",
    # agent 专用的 Claude Code 配置目录, 会话记录写在这里, 不和本机其他 Claude Code 会话混在一起
    "agent_claude_dir": str(DATA_DIR / "claude"),
    # agent 专用的 CODEX_HOME: 认证软链到用户自己的 auth.json, 配置由 larkx 生成
    "agent_codex_dir": str(DATA_DIR / "codex"),
    # 启动 agent 的命令, 即用户平时启动 coding agent 的方式, 认证沿用用户自己的配置。
    # claude 后端: 如 claude、ccd 这类启动器; 留空用 Agent SDK 自带的 claude, 认证取当前环境的 ANTHROPIC_* 变量
    "claude_cli": "",
    # codex 后端
    "codex_cli": "codex",
    "agent_model": "",
    # 主模型被安全策略拦截时 Claude Code 会整会话切到备用模型, 必须指定一个当前接入点可用的模型
    "agent_fallback_model": "",
    # 单轮处理超时(秒), 超时后中断这一轮
    "agent_turn_timeout": 1800,
    # 每天几点(本地时间)让数字人回顾当天的对话和消息, 整理长期记忆; 设为 -1 关闭
    "memory_review_hour": 4,
    # 上下文用到自动压缩阈值的这个比例时, 先让数字人把要记的内容写进记忆(仅 claude 后端)
    "memory_flush_ratio": 0.85,
    # 数字人发出的消息末尾自动加上的签名, 如 "（AI 代回）"; 留空不加
    "reply_signature": "",
    # 数字人在别的会话以本人名义发消息时, 抄送一份到本人私聊
    "mirror": True,
    # 通知和告警发往的会话; 留空 = 本人和自己的私聊
    "notify_chat": "",
    # 本人账号用不了(如飞书登录失效)时的备用通知命令, {text} 替换成通知内容; 例:
    # lark-cli im +messages-send --as bot --user-id ou_xxx --text {text}
    "alert_cmd": "",
    # 数字人 10 分钟内最多发多少条消息, 超过后拒绝发送并告警
    "send_rate_limit": 20,
    # 重启后补处理的消息最多是多久之前的(秒), 更早的不再回复
    "inbox_max_age": 7200,
    # 多久核对一次最近活跃会话有没有漏收的消息(秒), 0 关闭
    "catchup_interval": 900,
    "triggers": {
        "prefix": "",
        "chat_types": [],
        "include_chats": [],
        "exclude_chats": [],
        "include_senders": [],
        "keywords": [],
        "group_at_only": False,
    },
}

TRIGGER_ENV = {
    "prefix": "LARKX_TRIGGER_PREFIX",
    "chat_types": "LARKX_TRIGGER_CHAT_TYPES",
    "include_chats": "LARKX_TRIGGER_INCLUDE_CHATS",
    "exclude_chats": "LARKX_TRIGGER_EXCLUDE_CHATS",
    "include_senders": "LARKX_TRIGGER_INCLUDE_SENDERS",
    "keywords": "LARKX_TRIGGER_KEYWORDS",
    "group_at_only": "LARKX_TRIGGER_GROUP_AT_ONLY",
}


def _as_list(v: str):
    v = v.strip()
    if v.startswith("["):
        try:
            return json.loads(v)
        except Exception:
            return []
    return [x.strip() for x in v.split(",") if x.strip()]


def _as_bool(v: str):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for key in cfg:
        if key == "triggers":
            continue
        v = os.environ.get("LARKX_" + key.upper())
        if v:
            cfg[key] = v
    for key in ("mark_read", "allow_self", "mirror"):
        if isinstance(cfg.get(key), str):
            cfg[key] = _as_bool(cfg[key])
    # LARKX_AGENT_CLI 是 claude_cli 的旧名
    if not cfg["claude_cli"] and os.environ.get("LARKX_AGENT_CLI"):
        cfg["claude_cli"] = os.environ["LARKX_AGENT_CLI"]
    cfg["agent_turn_timeout"] = int(cfg["agent_turn_timeout"])
    cfg["memory_review_hour"] = int(cfg["memory_review_hour"])
    cfg["memory_flush_ratio"] = float(cfg["memory_flush_ratio"])
    for key in ("send_rate_limit", "inbox_max_age", "catchup_interval"):
        cfg[key] = int(cfg[key])
    for key in ("agent_home", "agent_claude_dir", "agent_codex_dir"):
        cfg[key] = os.path.expanduser(cfg[key])
    triggers = dict(DEFAULTS["triggers"])
    for key, env_name in TRIGGER_ENV.items():
        v = os.environ.get(env_name)
        if v is None:
            continue
        if key == "group_at_only":
            triggers[key] = _as_bool(v)
        elif isinstance(DEFAULTS["triggers"][key], list):
            triggers[key] = _as_list(v)
        else:
            triggers[key] = v
    cfg["triggers"] = triggers
    return cfg


def is_agent_actor() -> bool:
    return os.environ.get(ACTOR_ENV) == "agent"
