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
    # agent 专用的 Claude Code 配置目录, 会话记录写在这里, 不和本机其他 Claude Code 会话混在一起
    "agent_claude_dir": str(DATA_DIR / "claude"),
    # 启动 agent 用的 Claude Code 命令, 即用户平时启动 Claude Code 的方式(如 claude、ccd 这类启动器),
    # 认证沿用用户自己的配置。留空用 Agent SDK 自带的 claude, 认证取当前环境的 ANTHROPIC_* 变量
    "agent_cli": "",
    "agent_model": "",
    # 主模型被安全策略拦截时 Claude Code 会整会话切到备用模型, 必须指定一个当前接入点可用的模型
    "agent_fallback_model": "",
    # 单轮处理超时(秒), 超时后中断这一轮
    "agent_turn_timeout": 1800,
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
    for key in ("mark_read", "allow_self"):
        if isinstance(cfg.get(key), str):
            cfg[key] = _as_bool(cfg[key])
    cfg["agent_turn_timeout"] = int(cfg["agent_turn_timeout"])
    for key in ("agent_home", "agent_claude_dir"):
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
