# Claude Bridge 任务栈

> 用途：军师每次新对话读取，记住当前开发进度
> 最后更新：2026-03-07

---

## ✅ 已完成

1. **v1.0.0 首版开发 + 部署**（2026-03-07）
   - 多项目管理（SQLite projects 表，/p add 注册）
   - InlineKeyboard 交互式 UI（项目/模型/effort/工具权限四层菜单）
   - 会话管理（session_id --resume 续接，50轮/$2 轮转提醒）
   - 成本追踪（cost_log 流水，每日预算熔断）
   - 图片支持（下载 → Read 工具路径 → Claude 查看）
   - LaunchAgent ai.claude-bridge（KeepAlive + RunAtLoad）

2. **Bug Fix #1：图片静默无响应**（2026-03-07）
   - 根因：python-telegram-bot v22 Message 是 frozen dataclass，直接赋值 AttributeError
   - 修复：handle_photo 独立实现完整调用链 + try/except + error_handler 注册

3. **Bug Fix #2：`-` 开头消息报 unknown option**（2026-03-07）
   - 根因：消息作为 `claude -p <msg>` 位置参数，argparse 将 `-` 解析为 flag
   - 修复：改为 stdin pipe 传入（`proc.communicate(input=msg.encode())`）

4. **架构独立：从 OpenClaw 分离**（2026-03-07）
   - 从 `~/.openclaw/scripts/` 迁移到 `~/.claude-bridge/`
   - 消除所有 OpenClaw 依赖（openclaw_utils、telegram_message_log 等）
   - 家目录：`~/.claude-bridge/`，完全独立运行

5. **Claude Bridge Project 创建 + 基础设施初始化**（2026-03-07）
   - claude.ai Project 创建（ID: 019cc6a8-2b99-7052-bc59-63ddae533682）
   - 4个 Project Files 设计完成（CB-交互协议/技术档案/系统状态/任务栈）
   - Code 任务文件生成，待执行

6. **P0：Bot Token 安全迁移**（2026-03-07）
   - `load_config()` 加 `!` 前缀 Keychain 解析
   - `config.json` 的 botToken 改为 `!security find-generic-password -s claude-bridge-bot-token -a claude-bridge -w`
   - LaunchAgent kickstart 重启验证通过

7. **Bug Fix #3：Empty Response（空回复）**（2026-03-07）
   - 根因：stop_reason=tool_use 时 Claude 返回的 result 为空字符串，Bot 将空字符串发送给用户导致静默
   - 修复：检测空 result 时返回兜底提示

---

## 🔥 下一个（P0 优先）

**P3：UTC+8 时区修复**
- cost_log 查询加 `datetime(created_at, '+8 hours')`
- 影响：每日预算统计、cost 查询命令的时间范围

---

## 🔜 排队

| # | 任务 | 说明 |
|---|------|------|
| 1 | **CB 基础设施完整初始化** | git init、project-files/ 目录、Drive KB 创建、sync-pipeline.py —— 由 Code 根据任务文件执行 |
| 2 | **P2：Document 消息处理** | PDF/代码文件 → 下载到临时目录 → 构造 Read 工具 prompt |
| 3 | **P2：Forward 消息测试** | `filters.FORWARDED` 是否能触发 text handler |
| 4 | **评估 MAX_CONCURRENT_WORKERS 2→4** | M4 Max 16核，资源充足，需压测验证 |
| 5 | **多用户权限体系（长期）** | 当前 allowFrom 白名单，未来可能需要角色/权限分层 |

---

## 🧊 低优先级

- MarkdownV2 兼容性改进（当前 fallback 纯文本已可用）
- Voice 消息支持（whisper 转录 → text prompt）
- 成本周报自动推送（每周一 Telegram 发送上周汇总）
