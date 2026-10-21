---
name: larkx
description: larkx 命令行，以本人身份操作飞书：发消息、查本地消息记录和服务器历史、列会话、搜人和群、下载消息附件。需要在飞书里回复、查上下文或找人时使用。
---

# larkx

所有命令都以本人的飞书身份执行。加上 `--json` 后输出 JSON。失败时退出码非零，带 `--json` 时输出 `{"ok": false, "error": "..."}`。

## 发消息

内容一律从 stdin 传入。结束标记用带引号的 `'EOF'`，这样内容里的 `$`、引号、反引号都不会被 shell 解析：

```bash
larkx send <chat_id> --json - <<'EOF'
第一行
第二行
EOF
```

- 回复话题里的消息时，加上 `--root <anchor>`。anchor 就是 `<chat>` 里的 anchor。
- 成功时输出 `{"ok": true, "msg_id": "...", "chat_id": "..."}`。
- 内容不能以触发前缀（如 `/ai`）开头，否则会被拒绝。

## 查上下文

```bash
larkx messages <chat_id> --limit 30 --json            # 本地库里的消息，按时间从早到晚排列
larkx messages <chat_id> --anchor <anchor> --json     # 只看某个话题
larkx history <chat_id> --count 20 --json             # 从服务器拉最近的历史（本地库缺消息时用）
larkx chats --json                                    # 最近活跃的会话
```

消息字段：`sender_id`、`sender_name`、`content`、`create_time`（Unix 秒）、`direction`（`in` 表示收到，`out` 表示本人发出），以及 `anchor`（话题 id）。

## 找人和群

```bash
larkx search <关键词>        # 搜用户和群，输出 JSON，里面有 id
```

## 下载附件

```bash
larkx download <msg_id> -o <保存路径>
```

适用于图片和文件消息。保存路径要放在当前工作目录下。

## 不能用的命令

`larkx api`、`larkx auth`、`larkx listen`、`larkx daemon` 都会被拒绝。需要的能力如果上面没有，就告诉本人。
