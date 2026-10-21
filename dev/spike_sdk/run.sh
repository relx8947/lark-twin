#!/usr/bin/env bash
# 去掉父 Claude Code 会话注入的变量, 避免验证进程被当成子会话; 认证用当前环境的 ANTHROPIC_*
set -euo pipefail
cd "$(dirname "$0")/../.."
exec env -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_MESSAGING_SOCKET -u CLAUDE_CODE_MESSAGING_TOKEN \
  -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_SESSION_ATTENDED -u CLAUDE_PID -u CLAUDE_CODE_ENTRYPOINT \
  -u CLAUDE_CODE_EXECPATH -u CLAUDE_EFFORT \
  .venv/bin/python -u dev/spike_sdk/spike.py "$@"
