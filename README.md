# lark-twin

以你本人的飞书账号常驻在线的数字分身。它一直开着一个 agent 会话，收到带触发前缀（默认 `/ai`）的消息就处理，并通过 `larkx` CLI 以你的身份在飞书里操作。

> **来源说明**
> 本仓库 fork 自 [cv-cat/LarkAgentX](https://github.com/cv-cat/LarkAgentX)。飞书网页版私有协议层（登录、WS 长连接、网关请求、protobuf 编解码、协议提取流水线）出自上游原作者，位于 `larkx/protocol/` 和 `dev/proto_pipeline/`，上游代码版权归原作者所有。其余部分按本项目的思路重写。上游没有提供许可证，本仓库也不另行授权。
> 同步上游的协议修复：`git fetch upstream && git merge upstream/master`

## 它是什么

- **身份是你本人。** 它走飞书网页版私有协议，登录态就是你的账号，能实时收到你账号收到的所有消息。不需要建应用，也不需要机器人。
- **一个一直开着的会话。** 用 [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) 维持一个常驻的 Claude Code 会话，所有会话的触发消息都进这一个"你"，它对你的事情有统一的认知。daemon 重启或 agent 进程崩溃后，会按原会话 id 恢复。
- **通过 CLI 操作飞书。** agent 用 `larkx` 命令收发消息、查记录、找人。CLI 同时也给你本人用，它怎么操作的，你在终端里敲一遍就能复现。
- **和本机其他 Claude Code 环境隔离。** 数字人只加载自己工作目录里的 CLAUDE.md 和 skill，不读你的全局配置、插件和 hooks，会话记录也单独存放。

## 结构

```
larkx daemon（常驻进程）
├─ 监听：WS 长连接收消息，全部入库
├─ 触发：前缀、会话类型、白名单等规则，选出交给数字人的消息
├─ 收件箱：排队，一次只交给 agent 一条，这一轮结束再发下一条
└─ 数字人会话：一个常驻 ClaudeSDKClient
    ├─ 工作目录 ~/.larkx/agent：CLAUDE.md（人设和准则）、.claude/skills/larkx（CLI 手册）
    └─ 工具调用前检查：禁止原始接口、登录凭证等

larkx/
  protocol/   飞书私有协议层（上游代码，尽量不改）
  daemon/     常驻进程：监听、触发规则、收件箱
  agent/      常驻会话、工具调用检查、工作目录模板
  storage/    消息库（SQLite / MySQL）
  cli.py      larkx 命令
```

## 快速开始

最省事的方式：在仓库目录里打开 Claude Code，说一句"帮我部署 lark-twin"。仓库自带运维 skill（`.claude/skills/lark-twin/`），它会按步骤完成安装、配置、扫码登录、体检和 systemd 托管，需要你做决定或扫码时会停下来问你。

手动部署需要 Linux、Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv venv && uv pip install -e .
cp .env.example .env            # 至少设置 LARKX_AGENT_CLI 和 LARKX_AGENT_FALLBACK_MODEL
.venv/bin/larkx auth qr         # 用飞书 App 扫码登录
.venv/bin/larkx doctor          # 检查登录、配置、模型调用
.venv/bin/larkx service install && .venv/bin/larkx service start
```

然后在飞书里发一条 `/ai 你好` 试试。如果是自己给自己发，要把 `LARKX_ALLOW_SELF` 设为 `true`。

**数字人用你自己的 coding agent。** `LARKX_AGENT_CLI` 填你平时启动 Claude Code 的命令（`claude`，或者 `ccd` 这类启动器），数字人就用它启动 agent，认证和模型选择都沿用你自己的配置，不用另外配 key。没有这样的命令时，也可以在 `.env` 里直接写 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`。目前只支持 Claude Code。

首次启动会生成 `~/.larkx/agent/CLAUDE.md`，这是数字人的人设和行为准则，之后不会被覆盖，可以直接修改。`.claude/skills/larkx/SKILL.md` 跟着代码版本走，每次启动都会同步。

运维命令：`larkx service status|logs|restart|stop`，日志文件在 `~/.larkx/logs/daemon.log`。

## 控制命令

只有你本人发出的才生效，别人发的会被当成普通消息。

| 命令 | 作用 |
|---|---|
| `/ai /stop` | 打断当前任务，清空排队的消息 |
| `/ai /clear` | 丢弃当前会话，开一个全新的会话 |

## larkx CLI

| 命令 | 作用 |
|---|---|
| `larkx send <chat_id> [--root <anchor>] [--json] -` | 发消息，内容从 stdin 读取（也可以直接写在参数里） |
| `larkx messages <chat_id> [--anchor <id>] [--json]` | 本地库里的消息记录 |
| `larkx history <chat_id> [--count N] [--json]` | 从服务器拉历史 |
| `larkx chats [--json]` / `larkx search <关键词>` | 会话列表 / 搜人和群 |
| `larkx download <msg_id> -o <路径>` | 下载图片、文件 |
| `larkx api <请求类型> [JSON]` | 调用任意网关接口，类型名见 `larkx/protocol/proto/cmd_map.json` |
| `larkx auth qr\|import\|check\|status` | 登录与凭证 |
| `larkx listen` / `larkx daemon` | 只收消息入库 / 运行数字人 |
| `larkx service install\|start\|stop\|restart\|status\|logs` | 用 systemd 用户服务托管 daemon |
| `larkx doctor [--json]` | 检查登录、配置、模型调用、服务状态 |

`lark` 是 `larkx` 的别名，兼容旧脚本。数字人调用时（环境变量 `LARKX_ACTOR=agent`）不能用 `api`、`auth`、`listen`、`daemon`、`service`、`doctor`，回复内容也不能以触发前缀开头，否则 CLI 会拒绝。

Lark 国际版：登录时加 `--region lark`，之后所有命令自动走凭证里记录的区域。

## 风险

- **非官方协议。** 飞书一改协议就可能失效，客户端版本号写死在协议层里。用非官方方式自动操作账号，有被风控的风险。如果是公司账号，先确认公司的安全规定。
- **凭证就是完整登录态。** `~/.larkx/credentials.json` 一旦泄露，别人就能接管你的账号。
- **规则只能约束，不能完全拦住。** 工具调用检查按字符串匹配，可以被刻意绕过。不跨会话透露信息这类要求只写在 CLAUDE.md 里，靠模型自觉遵守。

## 开发

```bash
.venv/bin/python -m pytest -q tests          # 单元测试
.venv/bin/python dev/check_agent_session.py  # 不接飞书，单独检查常驻会话（用临时目录）
```

飞书更新协议后，重新提取 schema 的方法见 `dev/proto_pipeline/README.md`。
