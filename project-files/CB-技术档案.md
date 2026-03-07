# Claude Bridge (CB) — 技术档案

> 版本：1.0.0 | 部署日期：2026-03-07 | 维护者：Claude Code + Claude.ai 军师

---

## 一、产品定位

Claude Bridge 是一个 **Telegram Bot ↔ Claude Code CLI 桥接服务**，让用户通过 Telegram 随时随地使用 Claude Code 的全部能力（代码阅读、文件编辑、Shell 执行、Web 搜索等），无需坐在电脑前开终端。

**核心特征**：
- **多项目操作台**：通过 cwd 切换在不同项目间操作，每个项目有独立的 CLAUDE.md 行为定义
- **零额外 API 成本**：复用 Claude Max 订阅（$100/月），不走独立 API 计费
- **薄桥接层**：CB 本身不做 AI 推理，只负责 I/O 转换——所有智能由 `claude -p` 提供
- **独立产品**：不依赖任何项目（包括 OpenClaw），CB 操作项目而非属于项目

**关系模型**：
```
CB（操作者）
├── 操作目标：~/.openclaw/workspace（OpenClaw 基础设施）
├── 操作目标：~/Projects/erp-system（ERP 系统）
└── 操作目标：任何包含 CLAUDE.md 的目录
```

## 二、架构

### 数据流

```
用户 (Telegram App)
    │
    ▼  Telegram Bot API (HTTPS, via proxy)
Claude Bridge (Python, LaunchAgent 常驻)
    │
    ▼  subprocess stdin/stdout (JSON)
Claude Code CLI (`claude -p --output-format json`)
    │
    ▼  cwd = 目标项目目录
项目文件系统 + CLAUDE.md
```

### 核心设计决策

| 决策 | 理由 |
|------|------|
| `claude -p` 而非 API 调用 | Max 订阅包含 -p 使用权，零额外成本；API 调用 Opus $15/$75 per M tokens |
| stdin pipe 传入消息 | 命令行参数传入会被 argparse 误解析（`-` 开头的中文内容报 unknown option） |
| 每用户 FIFO 锁 + 全局 semaphore | 防止同一用户并发请求导致 session 冲突；限制总并发保护系统资源 |
| 项目通过 cwd 切换 | Claude Code 自动读取 cwd 下的 CLAUDE.md，天然支持项目级行为定义 |
| 不预设默认项目 | 通过 /p add 动态管理，不硬编码路径依赖 |
| InlineKeyboard 交互 | Telegram 最佳实践，比命令参数更直观，支持多层菜单+返回按钮 |

## 三、文件结构

```
~/.claude-bridge/                  ← CB 家目录
├── claude-bridge.py               ← 主程序（880 行）
├── config.json                    ← 配置文件
├── CLAUDE.md                      ← 默认 agent 行为定义（无项目 CLAUDE.md 时生效）
├── data/
│   ├── sessions.db                ← SQLite（会话/项目/成本数据）
│   └── images/                    ← 临时图片（处理后自动删除）
└── logs/
    ├── claude-bridge.log          ← 应用日志
    └── claude-bridge.err.log      ← stderr 日志

~/Library/LaunchAgents/
└── ai.claude-bridge.plist         ← macOS LaunchAgent（开机自启、崩溃重启）
```

## 四、配置文件

### config.json

```json
{
  "botToken": "<Telegram Bot Token>",
  "allowFrom": ["<Telegram User ID>"],
  "proxy": "http://127.0.0.1:1082",
  "dailyBudget": 5.0,
  "defaultModel": "opus",
  "defaultToolProfile": "readonly",
  "claudeBin": "~/.local/bin/claude"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `botToken` | string | Telegram Bot API token（从 @BotFather 获取） |
| `allowFrom` | string[] | 允许使用的 Telegram user ID 白名单 |
| `proxy` | string | HTTP 代理地址（Telegram API 需要翻墙） |
| `dailyBudget` | number | 每日成本上限（USD），达到后拒绝新请求 |
| `defaultModel` | string | 默认模型：`opus` 或 `sonnet` |
| `defaultToolProfile` | string | 默认工具权限：`readonly` / `standard` / `restricted` |
| `claudeBin` | string | claude CLI 路径，支持 `~` 展开 |

### CLAUDE.md（默认 agent 行为）

当通过 CB 操作某个项目时，Claude Code 会读取该项目目录下的 CLAUDE.md。如果项目没有 CLAUDE.md，CB 的 WorkingDirectory（`~/.claude-bridge/`）下的 CLAUDE.md 作为 fallback 生效。

当前默认 CLAUDE.md 内容：
- 告知 Claude 它在 Telegram bot 模式下运行
- 回复保持简洁（Telegram 聊天界面）
- Markdown 格式稀疏使用
- 超长回复自动摘要
- 默认只读操作，修改需确认

## 五、数据库 Schema

SQLite 文件：`~/.claude-bridge/data/sessions.db`（WAL 模式）

### projects — 项目注册表

| 列 | 类型 | 说明 |
|----|------|------|
| name | TEXT PK | 项目短名（如 `openclaw`、`erp`） |
| path | TEXT | 项目绝对路径 |
| description | TEXT | 项目描述 |

### sessions — 会话状态

| 列 | 类型 | 说明 |
|----|------|------|
| chat_id | TEXT | Telegram chat ID |
| project | TEXT | 项目名 |
| session_id | TEXT | Claude Code session ID（用于 --resume） |
| model | TEXT | 使用的模型 |
| turns | INTEGER | 累计对话轮数 |
| cost_usd | REAL | 累计成本 |
| created_at | TEXT | 创建时间 |
| updated_at | TEXT | 最后更新时间 |

PK: (chat_id, project)

### active_project — 用户当前选择

| 列 | 类型 | 说明 |
|----|------|------|
| chat_id | TEXT PK | Telegram chat ID |
| project | TEXT | 当前活跃项目 |
| model | TEXT | 当前模型 |
| tool_profile | TEXT | 当前工具权限 |
| effort | TEXT | 当前思考深度 |

### cost_log — 成本流水

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增 ID |
| chat_id | TEXT | Telegram chat ID |
| project | TEXT | 项目名 |
| cost_usd | REAL | 本次成本 |
| turns | INTEGER | 本次轮数 |
| duration_ms | INTEGER | 本次耗时（毫秒） |
| created_at | TEXT | 记录时间 |

## 六、Telegram 命令参考

### 基础命令

| 命令 | 功能 |
|------|------|
| `/start`、`/help` | 帮助 + InlineKeyboard 快捷操作 |
| `/status` | 当前状态 + 所有快捷操作按钮 |
| `/new` | 重置当前项目的会话（新 session） |
| `/think` | 一键切换 Opus + high effort（深度思考模式） |
| `/cost` | 成本汇总（今日/7日/总计/按项目） |

### 项目管理

| 命令 | 功能 |
|------|------|
| `/p` | InlineKeyboard 项目选择面板 |
| `/p add <name> <path> [desc]` | 注册新项目 |
| `/p rm <name>` | 移除项目（含会话数据） |

### 设置面板

| 命令 | 功能 |
|------|------|
| `/model` | InlineKeyboard 模型选择（Opus 4.6 / Sonnet 4.6） |
| `/effort` | InlineKeyboard 思考深度（Low / Medium / High） |
| `/tools` | InlineKeyboard 工具权限（Read-only / Standard / Restricted） |

### 消息处理

| 消息类型 | 处理方式 |
|----------|----------|
| 纯文本 | 直接作为 prompt 通过 stdin 传给 `claude -p` |
| 图片 | 下载到临时文件 → 构造 "Read the file at {path}" prompt → Claude 用 Read 工具查看 |
| 图片+文字 | 下载图片 + caption 拼接为 prompt |

## 七、工具权限体系

### readonly（默认）
```
Read, Grep, Glob, WebSearch, WebFetch,
Bash(cat *), Bash(head *), Bash(tail *), Bash(ls *), Bash(wc *),
Bash(launchctl list *), Bash(docker ps *), Bash(docker logs *),
Bash(git log *), Bash(git status *), Bash(git diff *),
Bash(df *), Bash(uptime), Bash(date), Bash(which *),
Bash(python3 --version), Bash(pip3 list *), Bash(brew list *)
```

### standard
```
Read, Write, Edit, Grep, Glob, WebSearch, WebFetch, Bash
```

### restricted
```
Read, Grep, Glob, WebSearch, WebFetch
```

## 八、并发与安全模型

### 并发控制
- **per-user FIFO lock**：同一用户的请求串行执行（`asyncio.Lock` per chat_id）
- **global worker semaphore**：最多 2 个 Claude 进程同时运行（`MAX_CONCURRENT_WORKERS=2`）
- 超出并发的请求排队等待，不丢弃

### 安全边界
- **用户白名单**：`config.json` 的 `allowFrom` 严格匹配 Telegram user ID
- **工具白名单**：`--allowedTools` 限制 Claude 可用的工具集，默认 readonly
- **每日预算**：达到 `dailyBudget` 后拒绝新请求
- **会话轮转提醒**：超过 50 轮或 $2 成本时提示开新会话
- **环境隔离**：subprocess 环境移除 `CLAUDECODE` 和 `CLAUDE_PROJECT_DIR`，防止嵌套检测

### 超时与错误
- Claude 调用超时：300 秒，超时后 kill 进程
- 图片处理 try/except 全包裹，失败向用户发送错误消息
- 全局 error handler 注册，未捕获异常写日志（不再静默丢弃）

## 九、LaunchAgent 部署

### plist 文件

路径：`~/Library/LaunchAgents/ai.claude-bridge.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.claude-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3</string>
        <string>/Users/chenmingzhong/.claude-bridge/claude-bridge.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/chenmingzhong/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>/Users/chenmingzhong</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/chenmingzhong/.claude-bridge/logs/claude-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/chenmingzhong/.claude-bridge/logs/claude-bridge.err.log</string>
    <key>WorkingDirectory</key>
    <string>/Users/chenmingzhong/.claude-bridge</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
```

### 管理命令

```bash
# 查看状态
launchctl list ai.claude-bridge

# 重启
launchctl kickstart -k gui/$(id -u)/ai.claude-bridge

# 停止
launchctl bootout gui/$(id -u)/ai.claude-bridge

# 启动
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.claude-bridge.plist

# 查看日志
tail -f ~/.claude-bridge/logs/claude-bridge.log

# 查看错误日志
tail -f ~/.claude-bridge/logs/claude-bridge.err.log
```

## 十、从零部署指南

### 前置条件

1. **macOS**（Apple Silicon）
2. **Python 3.10+**（`/opt/homebrew/bin/python3`）
3. **python-telegram-bot >= 22**：`pip3 install python-telegram-bot --break-system-packages`
4. **Claude Code CLI**：`~/.local/bin/claude`（已登录 Max 订阅）
5. **HTTP 代理**：访问 Telegram API 需要翻墙（如 mihomo 1082 端口）
6. **Telegram Bot Token**：通过 @BotFather 创建 bot 获取

### 部署步骤

```bash
# 1. 创建目录
mkdir -p ~/.claude-bridge/{data/images,logs}

# 2. 放入文件
# - ~/.claude-bridge/claude-bridge.py（主程序）
# - ~/.claude-bridge/config.json（配置）
# - ~/.claude-bridge/CLAUDE.md（默认 agent 行为）

# 3. 编辑 config.json（填入 botToken、allowFrom 等）

# 4. 放入 LaunchAgent plist
cp ai.claude-bridge.plist ~/Library/LaunchAgents/

# 5. 加载并启动
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.claude-bridge.plist

# 6. 验证
launchctl list ai.claude-bridge
tail -5 ~/.claude-bridge/logs/claude-bridge.log
# 应看到 "Claude Bridge started" + "Application started"

# 7. Telegram 测试
# 搜索 bot 用户名 → 发送 /start → 发送任意文本消息
```

### 注册项目

在 Telegram 中：
```
/p add openclaw ~/.openclaw/workspace OpenClaw infrastructure
/p add erp ~/Projects/erp-system ERP system
```

## 十一、系统依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | 3.14.3（/opt/homebrew/bin/python3） | 运行时 |
| python-telegram-bot | >= 22.6 | Telegram Bot API 客户端 |
| httpx | 随 ptb 安装 | HTTP 客户端（代理支持） |
| Claude Code CLI | 最新稳定版 | AI 推理引擎 |
| SQLite | 内置 | 数据存储 |
| mihomo | 1082 端口 | HTTP 代理（访问 Telegram API） |

**关键约束**：
- LaunchAgent plist 必须用 `/opt/homebrew/bin/python3`，不能用 `/usr/bin/python3`（系统 Python 3.9 无第三方包，import 阶段静默崩溃）
- `claude -p` subprocess 必须 unset `CLAUDECODE` 环境变量，否则被检测为嵌套会话报错
- 消息必须通过 stdin 传入（不是命令行参数），否则 `-` 开头的中文内容被误解析为 CLI option

## 十二、已知问题与已修复 Bug

### 已修复

| Bug | 根因 | 修复 | 日期 |
|-----|------|------|------|
| 图片消息静默无响应 | python-telegram-bot v22 的 Message 是 frozen dataclass，`update.message.text = prompt` 抛 AttributeError，无 error handler 导致静默 | handle_photo 独立实现完整调用链 + try/except + error_handler 注册 | 2026-03-07 |
| `-` 开头消息报 unknown option | 消息作为 `claude -p <msg>` 的位置参数传入，argparse 将 `-` 解析为 flag | 改为 stdin pipe 传入（`proc.communicate(input=msg)`) | 2026-03-07 |

### 已知待改进

| 项目 | 说明 | 优先级 |
|------|------|--------|
| Forward 消息处理 | Telegram forward 消息可能不触发 text handler（需测试 filters.FORWARDED） | P2 |
| 文档/文件消息 | 当前不处理 document 类型消息（PDF、代码文件等） | P3 |
| Markdown 渲染失败 fallback | Claude 的 Markdown 输出可能不兼容 Telegram 的 MarkdownV2，当前降级为纯文本 | P3 |
| 成本统计时区 | SQLite `date('now')` 使用 UTC，与用户本地时间（UTC+8）不一致 | P3 |
| 多用户支持 | 当前 allowFrom 是白名单，未来可能需要角色/权限体系 | P4 |

## 十三、竞品分析（设计参考）

CB 在设计时调研了两个开源项目：

### cc-connect（Go 实现）
- **优点**：流式输出（stream-json stdin 协议）
- **缺点**：依赖自定义协议，与 Claude Code 版本强耦合
- **CB 的超越**：使用官方 `-p` + `--output-format json`，不依赖内部协议

### afk-code（TypeScript 实现）
- **优点**：PTY 模拟完整终端体验
- **缺点**：PTY 解析复杂、容易因终端转义序列出错、依赖 JSONL 格式
- **CB 的超越**：不模拟终端，直接用 headless JSON 输出，更简洁稳定

### CB 的独特优势
- **零额外成本**：复用 Max 订阅
- **多项目支持**：cwd 切换 + 项目级 CLAUDE.md
- **InlineKeyboard UI**：交互式操作面板
- **图片支持**：下载 → Read 工具查看
- **独立部署**：不依赖任何项目框架

## 十四、开发历程

| 日期 | 事件 |
|------|------|
| 2026-03-07 AM | 架构设计：调研 cc-connect/afk-code → 确定 `claude -p` stdin 方案 |
| 2026-03-07 AM | 首版开发：多项目、InlineKeyboard、会话管理、成本追踪 |
| 2026-03-07 AM | 部署验证：LaunchAgent + Telegram 端到端测试通过 |
| 2026-03-07 12:04 | Bug 修复 #1：frozen Message 导致图片静默失败 → 独立 handle_photo |
| 2026-03-07 12:12 | Bug 修复 #2：`-` 开头消息报 unknown option → stdin pipe |
| 2026-03-07 12:34 | 架构独立：从 OpenClaw 搬出到 `~/.claude-bridge/`，消除全部 OpenClaw 依赖 |
| 2026-03-07 15:30 | 博客初始化：claudebridge.blogspot.com 主题部署 + 首篇 Changelog 发布 |

## 十五、博客

博客地址：https://claudebridge.blogspot.com
Blog ID：`5622917632055974047`
管理工具：`~/.openclaw/scripts/blogger-manager.py`（复用 OpenClaw 工具链）
主题文件：`~/.claude-bridge/blogger/theme.xml`
文章目录：`~/.claude-bridge/blogger/posts/`

### 博客定位

- 受众：技术开发者（自部署）+ Claude/AI 重度用户（直接使用）
- 内容：Changelog / 技术架构深度文章 / 使用教程 / 开发故事
- 风格：工程师视角，诚实直接，有技术深度，不写营销软文
- 更新触发：每次新功能/重要修复即发布
- 语言：中英双语

### 主题设计

- 暗色系科技感，蓝紫色系（区别于 OpenClaw 的 Apple HIG 浅色风格）
- 根路径显示产品主页（Hero + 核心特性卡片 + 最新文章列表）
- 文章页独立排版，响应式移动端友好
- 主题通过 CDP 推送（push-blogger-theme.py，`--blog-id` 参数指定博客）

### 发布命令

```bash
# 发布文章
python3 ~/.openclaw/scripts/blogger-manager.py publish-post \
  --title "Title" --file ~/.claude-bridge/blogger/posts/xxx.html \
  --labels "changelog,release" --blog 5622917632055974047

# 更新主题
python3 ~/.openclaw/scripts/blogger-manager.py update-theme \
  --file ~/.claude-bridge/blogger/theme.xml --blog 5622917632055974047
```

## 十六、维护指南

### 日常运维

```bash
# 检查运行状态
launchctl list ai.claude-bridge

# 查看最近日志
tail -20 ~/.claude-bridge/logs/claude-bridge.log

# 查看成本
sqlite3 ~/.claude-bridge/data/sessions.db \
  "SELECT date(created_at), SUM(cost_usd), SUM(turns) FROM cost_log GROUP BY date(created_at) ORDER BY 1 DESC LIMIT 7;"

# 查看注册项目
sqlite3 ~/.claude-bridge/data/sessions.db "SELECT * FROM projects;"
```

### 代码修改后重启

```bash
# 修改 claude-bridge.py 后
launchctl kickstart -k gui/$(id -u)/ai.claude-bridge
# 3 秒后检查日志确认启动成功
sleep 3 && tail -3 ~/.claude-bridge/logs/claude-bridge.log
```

### 备份

```bash
# 备份会话数据和配置
cp ~/.claude-bridge/data/sessions.db ~/.claude-bridge/data/sessions.db.bak
cp ~/.claude-bridge/config.json ~/.claude-bridge/config.json.bak
```

### Bot Token 轮换

1. 在 @BotFather 中 revoke 旧 token，生成新 token
2. 更新 `~/.claude-bridge/config.json` 的 `botToken`
3. `launchctl kickstart -k gui/$(id -u)/ai.claude-bridge`

## 十六、源代码

主程序 `claude-bridge.py` 共 880 行，结构如下：

| 区域 | 行号 | 功能 |
|------|------|------|
| 常量与路径 | 1-65 | CB_HOME、配置常量、工具权限定义、CLAUDE_ENV |
| 日志 | 67-78 | 文件+控制台双输出 |
| 配置读取 | 80-94 | load_config / get_claude_bin / get_proxy |
| SQLite | 96-143 | init_db、schema 创建、migration |
| 全局状态 | 146-156 | db 连接、用户锁、worker semaphore |
| 数据库操作 | 159-236 | CRUD 函数（project/session/cost） |
| 鉴权 | 239-244 | is_allowed（白名单） |
| UI 构建器 | 247-264 | make_keyboard / status_text |
| Claude 调用器 | 267-318 | invoke_claude（subprocess + stdin + timeout） |
| 消息处理 | 321-477 | typing loop / send_long_message / _invoke_and_reply / handle_photo / handle_message |
| 命令处理 | 480-670 | /p /model /effort /tools /think /new /status /cost /help |
| 回调处理 | 673-805 | InlineKeyboard 按钮点击（project/model/effort/tools/menu/cmd） |
| Error Handler | 808-811 | 全局异常捕获 |
| 启动 | 814-879 | post_init（注册命令菜单）/ main（构建 Application + 注册 handlers） |
