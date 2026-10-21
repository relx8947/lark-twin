---
name: larkx
description: 以本人的飞书账号（网页登录态）操作飞书 IM：给人或群发消息、查聊天记录和历史、搜人和群、下载附件，以及指挥本人的飞书数字人（lark-twin）去做事、安排定时提醒、查看它发过什么。用户要求"在飞书上给某人发消息""看看某个群最近在聊什么""让我的数字人去……""提醒我……"时使用。飞书文档、表格、日历请用官方 lark-cli，不用本 skill。
---

# larkx：以本人身份操作飞书

命令都以本人的飞书账号执行。别人看到的发送者就是本人，所以发出去之前要和本人确认内容和对象。命令路径是 `{{LARKX}}`，下文简写为 `larkx`。加上 `--json` 后输出 JSON，失败时退出码非零。

## 找到会话

```bash
larkx chats --json                 # 最近活跃的会话：chat_id、name、chat_type（1 私聊，2 群，3 话题群）
larkx search <名字或群名>          # 搜人和群，输出 JSON
```

给某人发私聊时，先用 `search` 找到人。如果 `chats` 里已经有和这个人的私聊，就直接用它的 chat_id。

## 发消息

内容从 stdin 传入。结束标记用带引号的 `'EOF'`，这样内容里的特殊字符不会被 shell 解析：

```bash
larkx send <chat_id> --json - <<'EOF'
消息内容
EOF
```

回复话题时加上 `--root <话题 id>`。**发送前，要先和本人确认发给谁、发什么**，除非本人已经明确给出了对象和原文。

## 看聊天记录

```bash
larkx messages <chat_id> --limit 50 --json    # 本地库，按时间从早到晚排列
larkx history <chat_id> --count 30 --json     # 从服务器拉最近的历史（本地库缺消息时用，加 --save 会存进本地库）
larkx download <msg_id> -o <保存路径>          # 下载图片或文件
```

## 指挥数字人

数字人是一个常驻的 agent 会话。它有长期记忆，以本人身份在飞书里回复带 `/ai` 前缀的消息。

```bash
larkx say --wait - <<'EOF'        # 以本人身份直接给数字人下指令，等它做完并打印回答
帮我看一下今天各个群里有没有人找我，整理成要点
EOF
larkx task add --in 30m - <<'EOF'  # 安排定时任务，到点后由数字人处理
提醒我给张三回电话（在我和自己的私聊里提醒）
EOF
larkx task add --at "10-22 09:00" --every 1d -   # 周期任务，内容同样从 stdin 读
larkx task list [--all] [--json]                  # 查看任务，--all 会包括已完成的
larkx task cancel <id>
larkx audit --limit 30                            # 通过 larkx 发出的所有消息，标明是本人还是数字人发的，以及是哪一轮
```

数字人的人设和行为准则在 `~/.larkx/agent/CLAUDE.md`，长期记忆在 `~/.larkx/agent/memory/`，这两处都可以直接查看和修改。

## 服务运维

```bash
larkx service status | logs -n 100 | restart | stop | start
larkx doctor          # 检查登录、配置、模型调用和服务状态
```

更完整的部署和排错流程，见仓库里的 `lark-twin` 运维 skill。

## 注意

- `larkx api <请求类型> <JSON>` 能调用任意网关接口，威力很大。只在其他命令做不到、并且本人同意时才用。
- 不要读取或输出 `~/.larkx/credentials.json`，那是本人飞书账号的完整登录态。
