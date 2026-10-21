# lark-twin

以你本人的飞书账号常驻在线的数字分身。收到带触发前缀（默认 `/ai`）的消息就处理，记得住事，会按时提醒和跟进，并通过 `larkx` CLI 以你的身份在飞书里操作。驱动它的是你自己的 coding agent：Claude Code 或 Codex。

你也可以在自己平时用的 Claude Code / Codex 里，直接让它操作你的飞书，或者指挥数字人去做事。

> **来源说明**
> 本仓库 fork 自 [cv-cat/LarkAgentX](https://github.com/cv-cat/LarkAgentX)。飞书网页版私有协议层（登录、WS 长连接、网关请求、protobuf 编解码、协议提取流水线）出自上游原作者，位于 `larkx/protocol/` 和 `dev/proto_pipeline/`，上游代码版权归原作者所有。其余部分按本项目的思路重写。上游没有提供许可证，本仓库也不另行授权。
> 同步上游的协议修复：`git fetch upstream && git merge upstream/master`

## 能做什么

- **以你本人的身份在线。** 走飞书网页版私有协议，实时收到你账号收到的所有消息，并以你的身份发出消息。不需要建应用，也不需要机器人。
- **一个统一的"你"。** 所有会话的触发消息都交给同一个 agent 会话处理。用 Claude Code 时，会话一直开着；用 Codex 时，每轮通过 `codex exec resume` 接上同一个会话。daemon 重启、agent 崩溃后都能恢复。
- **长期记忆。** 数字人把人、偏好、约定、待跟进的事写进 `memory/`。上下文快被压缩前会先整理一次记忆，每天还会回顾当天的消息。记忆目录是一个 git 仓库，每次变化都有记录。
- **定时与跟进。** "半小时后提醒我""每天早上汇总一下"会变成定时任务，到点由数字人处理。
- **用你自己的 agent 驱动。** 认证和模型沿用你自己的 Claude Code（`claude` / `ccd` 等启动器）或 Codex（`codex login`）配置，不用另外配 key。数字人有独立的工作目录和配置目录，不会加载你本机 agent 的全局配置、插件、hooks 和 skill。
- **有边界、可审计。** 只有本人发起时，数字人才能给其他会话发消息，这条规则由 CLI 在代码里执行，不只是写在提示词里。通过 `larkx` 发出的每条消息都会记入审计表。

## 部署

最省事的方式：在仓库目录里打开 Claude Code 或 Codex，说一句"帮我部署 lark-twin"。仓库自带运维 skill（`.claude/skills/lark-twin/`），它会按步骤完成安装、配置、扫码登录、体检和 systemd 托管，需要你做决定或扫码时会停下来问你。

手动部署需要 Linux、Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv venv && uv pip install -e .
cp .env.example .env            # 选后端并配置启动命令，见下文
.venv/bin/larkx auth qr         # 用飞书 App 扫码登录
.venv/bin/larkx doctor          # 检查登录、配置、模型调用、服务
.venv/bin/larkx service install && .venv/bin/larkx service start
.venv/bin/larkx skill install   # 让你本机的 Claude Code / Codex 也能操作飞书
```

然后在飞书里发一条 `/ai 你好` 试试。如果是自己给自己发，要把 `LARKX_ALLOW_SELF` 设为 `true`。

### 选择驱动数字人的 agent

| `.env` | Claude Code | Codex |
|---|---|---|
| `LARKX_AGENT_BACKEND` | `claude`（默认） | `codex` |
| 启动命令 | `LARKX_CLAUDE_CLI`：你平时启动 Claude Code 的命令，如 `claude` 或 `ccd`。留空时用 Agent SDK 自带的 claude，认证取 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` | `LARKX_CODEX_CLI`，默认 `codex`。认证沿用 `~/.codex/auth.json` |
| 会话形态 | 一个一直开着的会话 | 每轮 `codex exec resume` 接着同一个会话 |
| 注意 | 设置 `LARKX_AGENT_FALLBACK_MODEL`，否则主模型被安全策略拦截后整个会话会失败 | 需要代理时在 `.env` 写大写的 `HTTPS_PROXY`（Codex 读大写变量）。账号不支持默认模型时，设置 `LARKX_AGENT_MODEL` |

### 数字人目录 `~/.larkx/agent`

| 文件 | 说明 |
|---|---|
| `CLAUDE.md` | 人设和行为准则，首次启动时生成，之后不会被覆盖，可以直接改。`AGENTS.md` 是指向它的软链接，给 Codex 用 |
| `memory/` | 长期记忆，`MEMORY.md` 是索引 |
| `.claude/skills/larkx/` | 数字人用的 CLI 手册，跟着代码版本走 |
| `downloads/` | 下载的附件 |

这个目录本身是一个 git 仓库，每轮结束后的变化会自动提交，可以用 `git log` 查看它的记忆是怎么变的。

## 在飞书里用

| 消息 | 作用 |
|---|---|
| `/ai <任何事>` | 交给数字人处理。别人发的，数字人代表你回应；你自己发的，就是在给它下指令 |
| `/ai /stop` | 打断当前任务，清空排队的消息（只认你本人） |
| `/ai /clear` | 开一个新会话，长期记忆保留（只认你本人） |

## 在终端或你自己的 agent 里用

执行 `larkx skill install` 后，你的 Claude Code 和 Codex 就能直接说"帮我给张三发个消息""看看 X 群今天聊了什么""让数字人整理一下今天谁找过我"。也可以直接用命令：

| 命令 | 作用 |
|---|---|
| `larkx send <chat_id> [--root <anchor>] [--json] -` | 以你的身份发消息，内容从 stdin 读取 |
| `larkx messages <chat_id>` / `larkx history <chat_id>` | 本地消息记录 / 从服务器拉历史 |
| `larkx chats` / `larkx search <关键词>` | 会话列表 / 搜人和群 |
| `larkx download <msg_id> -o <路径>` | 下载图片、文件 |
| `larkx say [--wait] -` | 以本人身份给数字人下指令；加 `--wait` 等它做完并打印回答 |
| `larkx task add --in 30m \| --at "10-22 09:00" [--every 1d] -` | 安排定时任务，到点后由数字人处理 |
| `larkx task list [--all]` / `larkx task cancel <id>` | 查看 / 取消任务 |
| `larkx audit` | 通过 larkx 发出的所有消息：谁发的、在哪一轮、发了什么 |
| `larkx service install\|start\|stop\|restart\|status\|logs` | 用 systemd 用户服务托管 daemon，日志在 `~/.larkx/logs/daemon.log` |
| `larkx doctor [--json]` | 检查登录、配置、模型调用、服务状态、skill 安装情况 |
| `larkx skill install [--target claude\|codex\|all]` | 把 larkx skill 装进你本机的 Claude Code / Codex |
| `larkx api <请求类型> [JSON]` | 调用任意网关接口，类型名见 `larkx/protocol/proto/cmd_map.json` |
| `larkx auth qr\|import\|check\|status` | 登录与凭证 |

`lark` 是 `larkx` 的别名。数字人调用时（`LARKX_ACTOR=agent`），`api`、`auth`、`listen`、`daemon`、`service`、`doctor`、`say`、`skill` 都会被拒绝；回复内容不能以触发前缀开头；没有本人发起，不能给触发这一轮以外的会话发消息或安排任务。

Lark 国际版：登录时加 `--region lark`，之后所有命令自动走凭证里记录的区域。

## 结构

```
larkx daemon（常驻进程）
├─ 监听：WS 长连接收消息，全部入库
├─ 触发：前缀、会话类型、白名单等规则，选出交给数字人的消息
├─ 调度：到点的任务、终端指令、每日记忆回顾
├─ 收件箱：一次只交给 agent 一件事，这一轮结束再交下一件
└─ 数字人会话（Claude Code 或 Codex）
    ├─ 每轮开始前写入"当前这一轮"的上下文，CLI 据此判断有没有越权
    └─ 每轮结束后提交记忆变化；上下文快满时先整理记忆再压缩

larkx/
  protocol/   飞书私有协议层（上游代码，尽量不改）
  daemon/     常驻进程：监听、触发规则、调度、收件箱
  agent/      Claude / Codex 两种后端、工具调用检查、数字人目录模板
  storage/    消息库、审计表、任务表（SQLite / MySQL）
  cli.py      larkx 命令
```

## 风险

- **非官方协议。** 飞书一改协议就可能失效。用非官方方式自动操作账号，有被风控的风险。如果是公司账号，先确认公司的安全规定。
- **凭证就是完整登录态。** `~/.larkx/credentials.json` 一旦泄露，别人就能接管你的账号。
- **规则只能约束，不能完全拦住。** 跨会话发消息、禁用命令这些由 CLI 拦截，但数字人拥有完整的 shell 权限，刻意绕过是做得到的。Claude 后端的工具调用检查按字符串匹配；Codex 后端没有这层检查，只靠 CLI 拦截。"不转述其他会话的内容"这类要求只写在 CLAUDE.md 里，靠模型自觉遵守。
- **签名。** 数字人默认以你的口吻发消息，不加任何标识。需要标明是 AI 代回时，设置 `LARKX_REPLY_SIGNATURE`。

## 开发

```bash
.venv/bin/python -m pytest -q tests                                      # 单元测试
[LARKX_AGENT_BACKEND=codex] .venv/bin/python dev/check_agent_session.py  # 不接飞书，单独检查一个后端（用临时目录）
```

飞书更新协议后，重新提取 schema 的方法见 `dev/proto_pipeline/README.md`。
