---
name: lark-twin
description: 部署和运维 lark-twin 飞书数字人（larkx daemon）：安装、配置 .env、选择 Claude Code 或 Codex 作为驱动、扫码登录、体检、用 systemd 托管、把 larkx skill 装进本机 agent、查看日志、重启，以及排查"不回复"等问题。用户要求部署、启动、停止、升级、切换后端或排查 lark-twin / larkx 数字人时使用。
---

# 部署和运维 lark-twin

lark-twin 以用户本人的飞书账号常驻运行。它由两部分组成：`larkx daemon` 负责收消息和调度定时任务；驱动数字人的 coding agent（Claude Code 或 Codex）负责处理，需要时用 `larkx` 命令在飞书里操作。下面所有命令都在仓库根目录执行。

## 首次部署

按顺序执行。每一步确认成功后再进行下一步。

### 1. 安装

```bash
uv venv && uv pip install -e .
```

如果 pypi.org 连不上，加 `--index-url <可用的镜像>`。需要 Python 3.10 以上。

### 2. 配置 `.env`

`.env` 不存在时执行 `cp .env.example .env`。已经存在就不要覆盖，只改需要改的项。下面几项要先问用户，不要自己猜：

| 配置项 | 怎么定 |
|---|---|
| `LARKX_AGENT_BACKEND` | 用户想用 Claude Code（`claude`）还是 Codex（`codex`）驱动数字人 |
| `LARKX_CLAUDE_CLI` | 选 claude 时填：用户平时用什么命令启动 Claude Code，就填什么，比如 `claude`，或 `ccd` 这类启动器。数字人会用它启动 agent，认证沿用用户自己的配置。如果用户没有这样的命令，就不填，改为填 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN` |
| `LARKX_AGENT_FALLBACK_MODEL` | 选 claude 时填：用户的模型接入点上确实可用的模型。主模型被安全策略拦截时，整个会话会切到这个模型 |
| `LARKX_CODEX_CLI` | 选 codex 时用，默认就是 `codex`。用户要先在终端里 `codex login` 过 |
| `HTTPS_PROXY` / `HTTP_PROXY` | 选 codex 并且需要代理时填。Codex 读的是大写变量，所以要确认大写变量指向一个能访问 OpenAI 的代理 |
| `LARKX_AGENT_MODEL` | 一般留空，用默认模型。Codex 账号不支持默认模型时再设置 |
| `LARKX_TRIGGER_PREFIX` | 触发前缀，默认 `/ai` |
| `LARKX_ALLOW_SELF` | 默认 `true`：用户自己发的 `/ai` 也会交给数字人，这是给它下指令、`/stop`、`/clear`、`/takeover` 的前提。没有触发前缀时，用户自己的消息一律不触发 |
| `LARKX_REPLY_SIGNATURE` | 用户希望数字人发出的消息带上 AI 标识时填，比如 `（AI 代回）`。默认不加 |

`.env` 里写了 key 时，执行 `chmod 600 .env`。不要把 key 的内容输出到对话里。

### 3. 飞书扫码登录（需要用户本人操作）

```bash
.venv/bin/larkx auth qr
```

二维码会打印在终端里，同时保存为 `~/.larkx/login_qr.png`。把图片路径告诉用户，请用户用飞书 App 扫码。每张二维码约 45 秒失效，会自动刷新。看到"登录成功"才算完成。已经登录过的话，用 `.venv/bin/larkx auth check` 确认凭证有效即可。

### 4. 体检

```bash
.venv/bin/larkx doctor
```

除了"后台服务""本机 agent 的 larkx skill"这两项之外，其余项都应该是 ✅。出现 ❌ 或 ⚠️ 时，按每项下面的 → 提示处理，再重新运行。这一步会真实调用一次模型。

### 5. 交给 systemd 托管

```bash
.venv/bin/larkx service install   # 写入 systemd 用户服务并设为开机自启
.venv/bin/larkx service start
.venv/bin/larkx service status
```

`install` 会记下当前的 PATH，用来在服务里找到 agent 的启动命令。所以要在能正常运行该命令的终端里执行。服务进程不会继承终端里的代理等环境变量，agent 需要的变量要写进 `.env`。

如果希望用户退出登录后服务继续运行，需要开启 linger：`loginctl show-user $USER -p Linger` 应该显示 `Linger=yes`。如果不是，告诉用户执行 `loginctl enable-linger $USER`，这一步可能需要管理员权限。

### 6. 让本机的 Claude Code / Codex 也能操作飞书

```bash
.venv/bin/larkx skill install       # 装到 ~/.claude/skills/larkx 和 ~/.codex/skills/larkx
```

装好后，用户在自己平时的 Claude Code 或 Codex 里就能说"给某人发消息""看看某个群在聊什么""让数字人去做……"。

### 7. 验证

请用户在飞书里给数字人能看到的会话发 `/ai 你好`，然后查看日志：

```bash
.venv/bin/larkx service logs -n 50
```

看到 `[agent] message ... 结束: success` 并且飞书里收到回复，部署就完成了。也可以直接用 `.venv/bin/larkx say --wait "你好，介绍一下你自己"` 从终端测试。

## 日常运维

| 要做的事 | 命令 |
|---|---|
| 看状态 | `.venv/bin/larkx service status` |
| 看日志 | `.venv/bin/larkx service logs -n 100`（持续跟踪加 `-f`；日志文件是 `~/.larkx/logs/daemon.log`） |
| 改了 `.env`、升级了代码 | `.venv/bin/larkx service restart`。升级后也要 `larkx skill install` 刷新本机 agent 的 skill |
| 切换后端 | 改 `LARKX_AGENT_BACKEND` 和对应的启动命令，执行 `doctor`，再 `service restart`。两种后端各有各的会话，但长期记忆是共用的 |
| 暂停 / 恢复 | `.venv/bin/larkx service stop` / `start` |
| 看数字人发过什么 | `.venv/bin/larkx audit --limit 50` |
| 看定时任务 | `.venv/bin/larkx task list --all` |
| 卸载服务 | `.venv/bin/larkx service uninstall` |
| 全面检查 | `.venv/bin/larkx doctor` |

数字人的人设和行为准则在 `~/.larkx/agent/CLAUDE.md`，长期记忆在 `~/.larkx/agent/memory/`，两处都可以直接修改。这个目录是一个 git 仓库，每轮的变化都会自动提交，改坏了可以用 git 回退。修改人设后，新会话一定会读到新内容：用户在飞书发 `/ai /clear` 即可开新会话，长期记忆会保留。`/ai /stop` 用来打断当前任务。这两个命令只有用户本人发才生效。

## 排查

| 现象 | 处理 |
|---|---|
| 发了消息没反应 | 先看 `service logs`。日志里没有这条消息，说明监听有问题，检查登录和网络。有这条消息但没有"结束"，说明 agent 在处理或卡住了。消息没有被交给数字人，检查触发前缀；如果是自己发给自己，还要检查 `LARKX_ALLOW_SELF` |
| 日志里有"凭证已过期"或"会话已被服务端注销" | 请用户重新执行 `.venv/bin/larkx auth qr`，然后 `service restart` |
| doctor 显示"模型调用"失败 | Claude：检查 `LARKX_CLAUDE_CLI` 指定的命令在终端里能否正常启动 Claude Code。Codex：检查 `codex` 在终端里能否正常对话；如果报 "workspace routing discovery" 或连接超时，多半是代理问题，在 `.env` 设置大写的 `HTTPS_PROXY`；如果报模型不支持，设置 `LARKX_AGENT_MODEL`。改完先 `service install` 刷新 PATH，再 `restart` |
| doctor 或 status 显示"重复进程" | 有不归 systemd 管的 `larkx daemon` 在运行，同一账号会出现两个数字人。确认后结束那些进程 |
| 日志里有"主模型被安全策略拦截" | 整个会话已经切到备用模型。确认 `LARKX_AGENT_FALLBACK_MODEL` 可用；必要时请用户发 `/ai /clear` 开新会话 |
| 审计里有"只能发到触发这一轮的会话" | 别人想让数字人给第三方发消息，被拦下了，这是预期行为。如果是本人想让它跨会话发消息，本人自己发 `/ai` 即可 |

## 注意

- `~/.larkx/credentials.json` 是用户飞书账号的完整登录态。不要读取它的内容，不要输出，也不要复制到别处。
- 不要把 `.env` 或 `~/.larkx/` 下的任何文件提交到 git。
- 同一个飞书账号只能运行一个 daemon。
