# 配套 skill

| 目录 | 给谁用 | 怎么生效 |
|---|---|---|
| [`lark-twin/`](lark-twin/SKILL.md) | 你在本仓库里打开的 Claude Code / Codex | 部署和运维：安装、配置、扫码登录、体检、托管、排错。仓库里的 `.claude/skills/` 和 `.agents/skills/` 都是指向这里的软链接，打开仓库就能用 |
| [`larkx/`](larkx/SKILL.md) | 你平时用的 Claude Code / Codex | 让它们以你的身份操作飞书、指挥数字人。用 `larkx skill install` 装到 `~/.claude/skills/larkx` 和 `~/.codex/skills/larkx`，安装时会把 `{{LARKX}}` 换成本机 larkx 的绝对路径 |
| [`larkx-agent/`](larkx-agent/SKILL.md) | 数字人自己 | 它用 larkx CLI 的手册。daemon 每次启动时都会同步到 `~/.larkx/agent/.claude/skills/larkx`（Codex 后端同步到它专用的 `CODEX_HOME`） |

改 skill 就改这里的文件。`larkx/agent/templates/skills/` 下是指向这里的软链接，打包时会作为普通文件带进去。改完 `larkx/` 要重新执行 `larkx skill install`；`larkx-agent/` 在下次 `larkx service restart` 时生效。
