# lark-twin

**你在飞书上的数字分身。**

它以你本人的账号常驻在线，收到 `/ai` 开头的消息就替你处理。它记得住事，会按时提醒和跟进。驱动它的是你自己的 Claude Code 或 Codex。

> 飞书私有协议层源自 [cv-cat/LarkAgentX](https://github.com/cv-cat/LarkAgentX)（`larkx/protocol/`），版权归原作者所有，其余部分为重写。上游未提供许可证，本仓库亦不另行授权。

## 特性

- **本人身份**：基于飞书网页版协议，实时收发消息，无需创建应用或机器人。
- **一个会话**：所有会话共用同一个"你"，重启或崩溃后自动接上之前的对话。
- **长期记忆**：人物、偏好、约定都会写进记忆，每天回顾一次，每次修改都有 git 记录。
- **定时跟进**：可以安排"半小时后提醒我""每天早上汇总谁找过我"这类任务。
- **替你聊**：接管某个会话一段时间，由它代你和对方往来，结束后给你一份总结。
- **有边界**：没有你的指令，不会去别的会话发消息。它在外面说的话会实时抄送给你，异常时会告警，每次发送都有审计记录。
- **不丢消息**：消息先落库再处理；重启后会接着处理没回完的消息，并从服务器补收长连接漏推的消息。
- **看得见**：网页控制台里能看到它在做什么、和谁聊了什么，也能管理任务、接管、记忆和人设，必要时一键打断。
- **不留已读**：数字人和网页版飞书看消息都不会标为已读，对方看不到已读，你自己的未读提醒也还在。

## 快速开始

在仓库目录里打开 Claude Code 或 Codex，说一句 **"帮我部署 lark-twin"**，内置的运维 skill 会一步步带你完成。

也可以手动部署：

```bash
uv venv && uv pip install -e .
cp .env.example .env          # 选择 claude / codex，并填写启动命令
.venv/bin/larkx auth qr       # 用飞书扫码登录
.venv/bin/larkx doctor        # 自检
.venv/bin/larkx service install && .venv/bin/larkx service start
.venv/bin/larkx skill install # 让你自己的 Claude Code / Codex 也能操作飞书
```

## 使用

**在飞书里**

| 消息 | 作用 |
|---|---|
| `/ai …` | 交给数字人处理。别人发的，它替你回应；你自己发的，它当作指令执行 |
| `@你 /ai …` | 在群里点名叫它 |
| `/ai /takeover 2h 目标` · `/ai /release` | 接管 · 结束接管当前会话。接管期间对方说的话都由它回复 |
| `/ai /stop` · `/ai /clear` | 打断当前任务 · 开启新会话，长期记忆保留 |

以 `/` 开头的命令只认你本人。

**在终端，或者你自己的 agent 里**

```bash
larkx send <chat_id> - <<'EOF'   # 以你的身份发消息
larkx say --wait "…"             # 直接指挥数字人
larkx task add --in 30m -        # 安排定时任务
larkx takeover start <chat_id> --for 2h --goal "…"   # 让它替你聊一段
larkx mentions --since 24h       # 最近谁找过你，有没有回
larkx audit                      # 它发过什么
larkx recall <msg_id>            # 撤回一条
larkx service status | logs -f   # 运行状态
```

**在浏览器里**

`larkx service install` 会同时装上网页控制台（`lark-twin-web.service`，只监听 `127.0.0.1:18341`），对外由你自己的 Nginx 反向代理。`larkx web url` 打印带令牌的登录链接，打开一次即可；`larkx web reset-token` 让所有已登录的浏览器失效。

打开这个地址就是一个照着飞书网页端做的网页版飞书：会话列表、未读数、头像和客户端一致，可以看消息、回复、搜人开聊；左侧的「数字人」标签页就是控制台（也可以直接打开 `/console`）。在这里看消息不会标为已读；但在某个会话发出消息后，飞书会把这个会话之前的消息都算作已读。

执行 `larkx skill install` 之后，直接对你的 Claude Code 或 Codex 说"给张三发个消息""看看某个群在聊什么"就可以。完整命令见 `larkx --help`。

## 配套 skill

`skills/` 下有三个：`lark-twin`（在本仓库里部署运维）、`larkx`（装进你的 Claude Code / Codex，用来操作飞书）、`larkx-agent`（数字人自己用的 CLI 手册）。说明见 [skills/README.md](skills/README.md)。

## 配置

都写在 `.env` 里，完整说明见 `.env.example`。

| 配置项 | 说明 |
|---|---|
| `LARKX_AGENT_BACKEND` | `claude` 或 `codex` |
| `LARKX_CLAUDE_CLI` / `LARKX_CODEX_CLI` | 你平时启动 agent 的命令，比如 `claude`、`ccd`、`codex`，认证沿用你自己的配置 |
| `LARKX_AGENT_FALLBACK_MODEL` | 用 Claude 时，主模型被安全策略拦截后改用的备用模型 |
| `LARKX_TRIGGER_PREFIX` | 触发前缀，默认是 `/ai` |
| `LARKX_REPLY_SIGNATURE` | 每条回复末尾的署名，比如 `🤖 lark-twin · 数字分身`，会和正文空一行、前面加"——" |
| `LARKX_ALERT_CMD` | 飞书登录失效时的备用告警命令，比如用 lark-cli 机器人身份发给你 |
| `LARKX_WEB_URL` | 网页控制台的对外地址（Nginx 入口），用来生成登录链接 |

数字人的人设在 `~/.larkx/agent/CLAUDE.md`，记忆在 `~/.larkx/agent/memory/`，都可以直接修改。

## 注意

- 用的是非官方协议，飞书一升级就可能失效，也有被风控的可能。如果是公司账号，先确认公司规定是否允许。
- `~/.larkx/credentials.json` 就是你账号的完整登录态，务必妥善保管。控制台的令牌 `~/.larkx/web_token` 能看到你的聊天和记忆，同样不要外传。
- 边界由 CLI 在代码里拦截，但数字人拥有完整的 shell 权限，所以这些限制只能防住误操作，不能当作安全隔离。

## 开发

```bash
.venv/bin/python -m pytest -q tests
```

飞书协议更新后，重新提取 schema 的方法见 `dev/proto_pipeline/README.md`。
