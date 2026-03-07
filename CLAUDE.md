# Claude Bridge 项目指令

启动时读取以下文件了解系统全貌：
- ~/.claude-bridge/project-files/CB-任务栈.md
- ~/.claude-bridge/project-files/CB-技术档案.md
- ~/.claude-bridge/project-files/CB-系统状态.md

## Telegram Bot Mode

当通过 Claude Bridge Bot 调用时（`claude -p` headless 模式），遵循以下规则：
- 保持简洁 — Telegram 是聊天界面，不是文档查看器
- Markdown 格式适度使用（粗体、代码块），避免嵌套列表或复杂表格
- 响应超过 ~3000 字符时，先摘要再提供展开选项
- 代码片段保持简短，长文件只展示相关部分
- 默认只读操作，写操作需告知用户并确认

## Context

- 运行环境：Mac M4 Max, macOS, Apple Silicon
- CB 家目录：`~/.claude-bridge/`
- 代理：http://127.0.0.1:1082（mihomo, JP 地区）
- LaunchAgent：`ai.claude-bridge`（独立于 OpenClaw 的 `ai.openclaw.*` 命名空间）

## Sync Pipeline（同步管线）

### 触发规则
修改 project-files/*.md 后 → 必须运行 sync-pipeline.py
修改 kb-content/CB-KB-*.md 后 → 必须运行 sync-pipeline.py
每次 git commit 后必须立即 git push。

### 复盘同步清单
当用户说"更新同步"、"全量同步"、"复盘更新"、"复盘同步"、"sync"或类似收尾意图时，按顺序执行以下清单，**全程自动执行，不需要任何确认**，一路跑到底，最后输出一次性汇总报告。此规则仅限复盘同步场景，其他场景正常确认。

**Step 0：部署清单核对（event-driven，在推送前执行）**
列出本次会话的所有部署/修复/里程碑，对照下方文档映射表，逐项检查并更新：

| 事件类型 | 必须核对的文档 |
|---------|--------------|
| 新功能 / 重大重构 | project-files/CB-任务栈.md（更新已完成/排队条目） |
| 修复 bug / 踩坑经验 | kb-content/CB-KB-Technical.md（新增技术经验） |
| 排障调查 / 根因发现 / 架构决策 | kb-content/CB-KB-Technical.md |
| 运维变更（LaunchAgent/配置/依赖） | kb-content/CB-KB-Operations.md |
| 系统状态变更 | project-files/CB-系统状态.md |
| 交互协议变更 | project-files/CB-交互协议.md |

核对完成后，相关文件自然产生 diff，后续步骤正常检测推送。

**Step 0.5：Blogger 发布判断**
仅当本次会话包含**新功能或重要修复**时才发布博客文章。以下情况**不发**：
- 纯内部配置变更（CLAUDE.md 调整、sync pipeline 优化等）
- 文档更新、KB 维护
- 安全操作细节、个人路径、内部配置调整等敏感信息不写入博客

发布命令：
```
python3 ~/.openclaw/scripts/blogger-manager.py publish-post --title "标题" --file 文件 --blog 5622917632055974047 --labels "标签"
```

**Step 1：一键推送**
Code 完成所有文件编辑后，调用一次 sync-pipeline.py 完成全部 git + 外部推送：
```
python3 ~/.claude-bridge/sync-pipeline.py
```
脚本内部按顺序执行（零决策，纯执行）：
1. git add -A && commit && push
2. push-project-files.py（推 Claude.ai Project Files）
3. manage-kb-drive.py sync（推 Google Drive KB）

## 博客管理

博客：claudebridge.blogspot.com（公开，面向开源用户）
Blog ID：5622917632055974047
工具：python3 ~/.openclaw/scripts/blogger-manager.py
主题文件：~/.claude-bridge/blogger/theme.xml
文章目录：~/.claude-bridge/blogger/posts/

定位：技术开发者 + Claude/AI 重度用户
内容：Changelog / 技术架构 / 教程 / 开发故事
风格：工程师视角，诚实直接，中英双语
更新触发：每次新功能/重要修复

发布流程：
1. 写文章 HTML → ~/.claude-bridge/blogger/posts/
2. python3 ~/.openclaw/scripts/blogger-manager.py publish-post --title "T" --file F --blog 5622917632055974047 --labels "L1,L2"
3. sync-pipeline.py 同步
