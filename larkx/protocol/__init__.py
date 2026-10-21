"""飞书网页版私有协议层(源自 cv-cat/LarkAgentX)。

登录、WS 长连接、网关请求、protobuf 编解码都在这里。上层只通过本包导出的接口使用,
尽量不改这里的代码, 以便飞书更新协议后跟进上游修复。
"""
from .auth import AuthExpired, LarkAuth, QrLogin
from .client import LarkClient
from .region import REGIONS, get_region

__all__ = ["AuthExpired", "LarkAuth", "LarkClient", "QrLogin", "REGIONS", "get_region"]
