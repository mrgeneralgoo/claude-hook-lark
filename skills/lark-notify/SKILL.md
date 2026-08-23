---
name: lark-notify
description: 发送一条飞书(Lark)通知卡片。当用户要求「发个飞书/lark 通知」「推送到飞书」「告诉我一声」，或需要在长任务的关键节点（完成、失败、需要决策、需要授权）主动提醒用户时使用。也用于排查飞书通知配置问题。
---

# 飞书 / Lark 通知

通过飞书自定义机器人 Webhook 发送一张卡片消息。纯 Python 标准库实现，无需安装任何依赖。

## 脚本位置

脚本在本插件的 `scripts/lark_notify.py`。按以下顺序确定路径：

1. 环境变量可用时：`"${CLAUDE_PLUGIN_ROOT}/scripts/lark_notify.py"`
2. 否则用本 SKILL.md 所在目录向上两级：`<skill 目录>/../../scripts/lark_notify.py`

下文用 `$NOTIFY` 指代该路径。

## 发送通知

```bash
python3 "$NOTIFY" send -t "标题" -s <状态> -m "正文" [-d "补充详情"] [-c 颜色]
```

参数：

| 参数 | 说明 |
|------|------|
| `-t, --title` | 标题，显示在卡片头部 |
| `-s, --status` | `success` / `failed` / `running` / `decision` / `warning` / `error` / `info`，决定图标与默认颜色 |
| `-m, --message` | 正文，支持飞书 lark_md 语法（`**粗体**`、`代码`、列表等） |
| `-d, --detail` | 补充详情，显示在分隔线下方 |
| `-c, --color` | 强制指定卡片颜色，覆盖状态默认值 |
| `--session-name` | 覆盖会话名，默认自动从 transcript 的 `ai-title` 记录读取 |
| `--session-id` | 覆盖会话 ID，默认取环境变量 `CLAUDE_CODE_SESSION_ID` |
| `--no-session` | 标题退回项目名，且不附带会话 ID |
| `--dry-run` | 只打印卡片 JSON，不真正发送 |

状态与默认样式：

| 状态 | 显示 | 颜色 |
|------|------|------|
| `success` | ✅ 成功 | green |
| `failed` | ❌ 失败 | red |
| `running` | ⏳ 进行中 | blue |
| `decision` | 🤔 待决策 | orange |
| `warning` | ⚠️ 警告 | yellow |
| `error` | ⚠️ 异常 | carmine |
| `info` | 💬 信息 | wathet |

卡片**标题**会用当前会话名（来自 transcript 的 `ai-title` 记录），会话还没生成标题时退回项目名；
正文下方自动附带项目名、Git 分支、主机名和完整 session ID（来自 `CLAUDE_CODE_SESSION_ID`）。
这些都不需要手动填。只有在用户明确要求换个称呼、或要求不要暴露会话信息时，
才用 `--session-name` / `--no-session` 覆盖。

## 示例

```bash
# 任务完成
python3 "$NOTIFY" send -t "数据迁移完成" -s success -m "已迁移 12 万条记录，耗时 8 分钟。"

# 构建失败
python3 "$NOTIFY" send -t "CI 构建失败" -s failed \
  -m "**分支** feat/login 编译不通过" \
  -d '```\nerror[E0308]: mismatched types\n  --> src/auth.rs:42\n```'

# 需要用户拍板
python3 "$NOTIFY" send -t "需要你确认" -s decision \
  -m "接口风格选 REST 还是 GraphQL？我倾向 REST，等你一句话。"
```

## 检查配置

```bash
python3 "$NOTIFY" doctor          # 打印配置来源、监听事件、静默时段等
python3 "$NOTIFY" doctor --test   # 顺便发一条测试消息
```

`doctor` 退出码 `2` 表示没找到 webhook_url。此时引导用户任选一种方式配置：

- 环境变量：`export LARK_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"`
- 用户级配置：`~/.config/claude-hook-lark/config.json`，内容 `{"webhook_url": "..."}`

机器人若开启了「签名校验」，同时配置 `secret`（或 `LARK_WEBHOOK_SECRET`）。

**不要**建议把 webhook URL 写进项目里的 `.claude/lark.json` —— 该文件会随 git clone 传播，
所以插件只允许它按名字引用用户配置里已登记的地址，自带 URL 会被忽略并告警。
需要按项目分流时，先在用户配置的 `webhooks` 里登记，再在项目配置里写 `{"webhook": "群名"}`。

## 注意

- **不要**把真实 webhook URL 写进仓库里的文件或提交记录。
- 发送失败时脚本返回非 0 并在 stderr 打印原因；hook 模式下则永远返回 0，不会打断会话。
- 目的地只允许 https 且必须是飞书/Lark 官方域名。报错信息里不会出现完整 URL（里面的 token 等同密码），
  所以看到「主机 xxx 不在允许列表内」时不要试图打印 URL 排查，改用 `doctor`。
- 不要为了「显得勤快」而频繁发通知。只在用户明确要求，或任务确实到了值得打扰对方的节点时发。
