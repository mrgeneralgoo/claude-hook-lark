# claude-hook-lark

把 **Claude Code** 的任务完成事件推送到 **飞书 / Lark**，并把「发飞书通知」封装成一个可以手动调用的 skill。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](#开发)
[![deps](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#开发)

- 🧩 标准 Claude Code plugin，`/plugin` 一键安装
- 🔔 任务完成 / 需要授权 / 等待输入时自动推送飞书卡片
- 🛠️ 附带 `lark-notify` skill，也可以随时让 Claude 主动给你发一条
- 🪶 **零第三方依赖**，只用 Python 标准库，不需要 `pip install`，也不需要 `jq`
- 🏷️ 标题用**会话名**而不是项目名 —— 一个项目并行跑多个任务时，一眼就知道是哪件事
- ✍️ 用飞书卡片 **schema 2.0**，Claude 回复里的标题、列表、代码块、引用都能正常渲染
- 🔕 支持静默时段、最短耗时过滤、事件白名单，不吵人
- 🔒 仓库里的配置文件**不能**指定通知目的地（见[安全设计](#安全设计)）

```
┌────────────────────────────────────────────┐
│ ✅ 任务完成 · 重构登录模块                    │  ← 标题用会话名
├────────────────────────────────────────────┤
│ Claude Code 已完成本轮任务                   │
│ 📋 本轮任务                                 │
│ 把登录接口改成 JWT，顺便补一下测试             │
│ 📝 完成情况                                 │
│ ## 改了什么          ← 标题、列表、代码块      │
│ - 换成 JWT 签发         都能正常渲染           │
│ - 补了 12 个测试                             │
│ 🔧 19 次工具调用 · ⏱️ 用时 6 分 31 秒         │
├────────────────────────────────────────────┤
│ 项目 my-project        分支 feat/jwt        │
│ 主机 dev-macbook       会话 ID 3f2a9c10-…   │
├────────────────────────────────────────────┤
│ Claude Code · Stop · 2026-08-23 00:39       │
└────────────────────────────────────────────┘
```

---

## 快速开始

### 1. 拿到飞书机器人 Webhook

飞书群聊 → 右上角 **设置** → **群机器人** → **添加机器人** → **自定义机器人**，
得到形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-...` 的地址。

若在安全设置里勾选了 **签名校验**，一并记下密钥。

### 2. 安装插件

在 Claude Code 里执行：

```
/plugin marketplace add mrgeneralgoo/claude-hook-lark
/plugin install claude-hook-lark
```

### 3. 配置

三种方式任选其一，优先级从高到低：命令行 `--webhook` > 环境变量 >
`CLAUDE_HOOK_LARK_CONFIG` > 用户级配置 > 项目级配置。

**环境变量**（最快）：

```bash
export LARK_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
export LARK_WEBHOOK_SECRET="..."   # 开启签名校验时才需要
```

**用户级配置**（推荐）：

```bash
mkdir -p ~/.config/claude-hook-lark && chmod 700 ~/.config/claude-hook-lark
cp config.example.json ~/.config/claude-hook-lark/config.json
chmod 600 ~/.config/claude-hook-lark/config.json
$EDITOR ~/.config/claude-hook-lark/config.json
```

> ⚠️ Webhook URL 等同于密码，**不要提交到仓库**。

### 4. 验证

```bash
python3 scripts/lark_notify.py doctor --test
```

飞书里收到测试卡片就说明配好了。

---

## 手动发通知

安装后直接跟 Claude 说「跑完了发个飞书通知给我」，它会调用 `lark-notify` skill。
也可以在终端直接用：

```bash
# 安装后脚本在 ~/.claude/plugins/cache/ 下，用这条命令定位：
NOTIFY=$(find ~/.claude/plugins -name lark_notify.py -path '*claude-hook-lark*' | head -1)

python3 "$NOTIFY" send -t "数据迁移完成" -s success -m "已迁移 12 万条记录。"
python3 "$NOTIFY" send -t "CI 构建失败" -s failed -m "**分支** feat/login 编译不通过" \
                       -d "error[E0308]: mismatched types"
python3 "$NOTIFY" send -t "需要确认" -s decision -m "REST 还是 GraphQL？"
python3 "$NOTIFY" send -t "预览" -s info -m "hello" --dry-run   # 只打印，不发送
```

状态 `-s`：`success` `failed` `running` `decision` `warning` `error` `info`，
分别对应 ✅ ❌ ⏳ 🤔 ⚠️ ⚠️ 💬 和 green / red / blue / orange / yellow / carmine / wathet。
`-c` 可强制指定颜色（飞书支持 blue、wathet、turquoise、green、yellow、orange、red、carmine、violet、purple、indigo、grey）。

卡片标题用**会话名**（取自 Claude Code 自动生成、你也能手动改的那个会话标题），
会话还没生成标题时退回项目名。正文下方自动带上项目、Git 分支、主机名和完整 session ID。
用 `--session-name` / `--session-id` 覆盖，`--no-session` 完全不带会话信息。

---

## 支持的事件

| 事件 | 触发时机 | 卡片 | 默认开启 |
|------|----------|------|:--------:|
| `Stop` | Claude 完成一轮任务 | ✅ 任务完成（绿） | ✔ |
| `Notification` | 需要授权 / 等待输入超时 | 🔐 需要授权（橙）· ⏳ 等待输入（黄） | ✔ |
| `SubagentStop` | 子任务（subagent）结束 | 📦 子任务完成（青） | ✘ |
| `SessionEnd` | 会话退出 | 👋 会话结束（灰） | ✘ |

四种 hook 都已在 `plugin.json` 里注册，实际发不发由配置里的 `events` 决定，改配置不用重装。
只要「任务完成」就写 `{"events": ["Stop"]}`。

> `"events": []` 表示**一个都不发**，而不是「用默认值」。只有这个键完全缺失时才套用默认值。

---

## 配置项

| 键 | 默认值 | 说明 |
|----|--------|------|
| `webhook_url` | — | 飞书自定义机器人 Webhook 地址（必填） |
| `secret` | `""` | 签名校验密钥，机器人未开启签名时留空 |
| `webhooks` | `{}` | 具名地址表，供项目级配置按名字引用 |
| `events` | `["Stop", "Notification"]` | 哪些事件触发推送 |
| `include_summary` | `true` | 是否把任务内容和完成情况写进卡片 |
| `min_duration_seconds` | `0` | 本轮耗时低于该值就不推送，过滤掉几秒钟的琐碎问答 |
| `quiet_hours` | `null` | 静默时段，如 `[22, 8]` 表示 22:00–08:00 不打扰（跨零点自动处理） |
| `timeout` | `5` | 发送超时（秒） |
| `allowed_hosts` | 飞书 / Lark 官方域名 | 允许发往的主机白名单，自建代理时才需要改 |
| `debug` | `false` | 写详细日志到 `~/.config/claude-hook-lark/debug.log` |

### 按项目分流

先在**用户配置**里登记地址：

```json
{
  "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/默认",
  "webhooks": {
    "值班群": {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/aaaa", "secret": ""}
  }
}
```

再在项目的 `.claude/lark.json` 里**按名字引用**（这个文件可以安全地提交）：

```json
{ "webhook": "值班群", "events": ["Stop"] }
```

项目配置只能引用已登记的名字、并把行为收窄，**不能自带 URL**。原因见下。

---

## 安全设计

两点是使用前该知道的：

**卡片默认会把你的原始提问和 Claude 的回复发进群。** 如果你在对话里粘贴过密钥、令牌、客户数据，
它们会随卡片进入群聊并留在消息记录里。需要时用 `{"include_summary": false}` 关掉，
卡片就只剩项目、分支、会话名和耗时这些元信息。

**仓库里的 `.claude/lark.json` 不能指定通知目的地。** 它会随 `git clone` 进入你的机器 ——
如果它能决定「发到哪」，克隆任意第三方仓库都可能让插件把你的会话内容发往仓库作者控制的地址。
所以目的地只认环境变量和用户级配置；项目配置只能按名字引用已登记的地址，并且只能把行为改得更严。

其余设计取舍——单调合并、配置类型校验、hook 时间预算与看门狗、凭据不入日志、
轮次边界与 transcript 解析——都记在 **[docs/DESIGN.md](docs/DESIGN.md)**。

---

## 故障排查

```bash
python3 scripts/lark_notify.py doctor        # 配置从哪来、监听哪些事件、是否在静默时段
python3 scripts/lark_notify.py doctor --test # 发一条测试消息
```

收不到通知时按顺序排查：

1. `doctor` 输出里 `webhook_url` 是不是 `(未配置)`，以及有没有 ⚠️ 告警行。
2. 把 `debug` 设为 `true`，触发一次任务完成，再 `tail -f ~/.config/claude-hook-lark/debug.log`。
   发送失败的记录**无论 debug 开关都会写**。
3. 单独验证 webhook 本身：
   ```bash
   curl -X POST "$LARK_WEBHOOK_URL" -H "Content-Type: application/json" \
     -d '{"msg_type":"text","content":{"text":"test"}}'
   ```
   返回 `{"code":0,...}` 才算通。`code: 19021` 是签名校验失败或时间戳超出 1 小时。
4. `/plugin` 看 claude-hook-lark 是否为 enabled。

**hook 会拖慢会话吗？** 不会。内部有 9 秒总预算和一个墙钟看门狗，超时安静退出 0；
`plugin.json` 另有 15 秒硬超时兜底。实测端到端 0.19 秒（本地）/ 0.56 秒（含真实网络）。

---

## 开发

```bash
python3 -m unittest discover -s tests -v
```

217 个测试，不需要任何依赖。其中两个端到端用例会真的等待数秒来验证看门狗，整轮约 13 秒。

Python 3.9+（3.9.6 与 3.14.7 实测通过）。

欢迎提 Issue 和 PR。改动请附带对应的回归用例 —— 这个项目的绝大多数分支都是某个具体失败场景的产物，
`docs/DESIGN.md` 记录了它们各自的来由。

版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## License

MIT
