---
name: lark-twin
description: 部署和运维 lark-twin 飞书数字人（larkx daemon）：安装、配置 .env、扫码登录、体检、用 systemd 托管、查看日志、重启、排查"不回复"等问题。用户要求部署、启动、停止、升级或排查 lark-twin / larkx 数字人时使用。
---

# 部署和运维 lark-twin

lark-twin 以用户本人的飞书账号常驻运行。它由两部分组成：`larkx daemon` 负责收消息，一个一直开着的 Claude Code 会话负责处理消息，需要时用 `larkx` 命令在飞书里操作。下面所有命令都在仓库根目录执行。

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
| `LARKX_AGENT_CLI` | 用户平时用什么命令启动 Claude Code，就填什么，比如 `claude`，或 `ccd` 这类启动器。数字人会用它启动 agent，认证沿用用户自己的配置。如果用户没有这样的命令，就不填，改为填 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN` |
| `LARKX_AGENT_FALLBACK_MODEL` | 用户的模型接入点上确实可用的模型。主模型被安全策略拦截时，整个会话会切到这个模型 |
| `LARKX_TRIGGER_PREFIX` | 触发前缀，默认 `/ai` |
| `LARKX_ALLOW_SELF` | 只有用户想自己给自己发消息测试时才设为 `true`，而且必须同时设置触发前缀 |

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

除了"后台服务：未安装"之外，其余项都应该是 ✅。出现 ❌ 或 ⚠️ 时，按每项下面的 → 提示处理，再重新运行。这一步会真实调用一次模型。

### 5. 交给 systemd 托管

```bash
.venv/bin/larkx service install   # 写入 systemd 用户服务并设为开机自启
.venv/bin/larkx service start
.venv/bin/larkx service status
```

`install` 会记下当前的 PATH，用来在服务里找到 `LARKX_AGENT_CLI` 指定的命令。所以要在能正常运行该命令的终端里执行 `install`。

如果希望用户退出登录后服务继续运行，需要开启 linger：`loginctl show-user $USER -p Linger` 应该显示 `Linger=yes`。如果不是，告诉用户执行 `loginctl enable-linger $USER`，这一步可能需要管理员权限。

### 6. 验证

请用户在飞书里给数字人能看到的会话发 `/ai 你好`，然后查看日志：

```bash
.venv/bin/larkx service logs -n 50
```

看到 `[agent] 本轮结束: success` 并且飞书里收到回复，部署就完成了。

## 日常运维

| 要做的事 | 命令 |
|---|---|
| 看状态 | `.venv/bin/larkx service status` |
| 看日志 | `.venv/bin/larkx service logs -n 100`（持续跟踪加 `-f`；日志文件是 `~/.larkx/logs/daemon.log`） |
| 改了 `.env`、升级了代码 | `.venv/bin/larkx service restart` |
| 暂停 / 恢复 | `.venv/bin/larkx service stop` / `start` |
| 卸载服务 | `.venv/bin/larkx service uninstall` |
| 全面检查 | `.venv/bin/larkx doctor` |

数字人的人设和行为准则在 `~/.larkx/agent/CLAUDE.md`，可以直接修改。修改后，新会话一定会读到新内容：用户在飞书发 `/ai /clear` 即可开新会话，但当前的对话上下文会被丢掉。`/ai /stop` 用来打断当前任务。这两个命令只有用户本人发才生效。

## 排查

| 现象 | 处理 |
|---|---|
| 发了消息没反应 | 先看 `service logs`。日志里没有这条消息，说明监听有问题，检查登录和网络。有这条消息但没有"本轮结束"，说明 agent 在处理或卡住了。消息没有被交给数字人，检查触发前缀；如果是自己发给自己，还要检查 `LARKX_ALLOW_SELF` |
| 日志里有"凭证已过期"或"会话已被服务端注销" | 请用户重新执行 `.venv/bin/larkx auth qr`，然后 `service restart` |
| doctor 显示"模型调用"失败 | 检查 `LARKX_AGENT_CLI` 指定的命令在终端里能否正常启动 Claude Code，或检查 `ANTHROPIC_*` 是否正确。改完后先 `service install` 刷新 PATH，再 `restart` |
| doctor 或 status 显示"重复进程" | 有不归 systemd 管的 `larkx daemon` 在运行，同一账号会出现两个数字人。确认后结束那些进程 |
| 日志里有"主模型被安全策略拦截" | 整个会话已经切到备用模型。确认 `LARKX_AGENT_FALLBACK_MODEL` 可用；必要时请用户发 `/ai /clear` 开新会话 |

## 注意

- `~/.larkx/credentials.json` 是用户飞书账号的完整登录态。不要读取它的内容，不要输出，也不要复制到别处。
- 不要把 `.env` 或 `~/.larkx/` 下的任何文件提交到 git。
- 同一个飞书账号只能运行一个 daemon。
