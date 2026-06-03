# Claude Bridge — Agent Instructions（轻量桥接）

> 本文件是给 Codex / 其他 AI agent 的**入口桥接**，**不是权威源**，不复制长文。
> **项目权威规则 = 本仓 `CLAUDE.md`**；集团级铁律 = `~/.claude/CLAUDE.md`。
> 改规则改 `CLAUDE.md`，**不改本文件**（避免双源漂移）。2026-06-04 三脑部署 v2 Step4 统一补齐。

## 权威与导航

- **项目规则权威**：[`CLAUDE.md`](CLAUDE.md)（本仓根）— 领域知识 / 业务红线 / 项目级流程
- **集团法规**：`~/.claude/CLAUDE.md` — 跨项目铁律（#1 彻底闭环 / 自主 commit / 逐一提问 / 双脑审计后裁决 / 工具路由 RFC v7）
- **项目参数唯一权威源**：`~/Projects/dharmaxis/project-registry.json`（本项目 id=`cb`：路径 / sync_pipeline / doc_mapping）——**禁凭记忆硬编码**

## 统一同步

统一入口：`python3 ~/Projects/dharmaxis/scripts/dx-sync.py sync cb -m "..."`。每 commit 后必 push；敏感信息（密码 / token / 私密路径）不入公开文档。

## Codex 只读审计红线（集团 P0 安全）

- Codex 审计**默认只读**：`codex exec --sandbox read-only`。写仓 / 外发须**主人显式授权**（`touch ~/.claude/.codex-write-authorized`，完后 `rm`）。
- LIVE 守护：`~/.claude/scripts/codex-write-lane-guard.py`（PreToolUse / fail-closed）拦未授权 codex 写车道；双脑审计走 `~/.claude/scripts/dual-brain-runner.py`（codex 只读 + gemini，已豁免）。

## 工具路由

工具选择规则统一在集团 RFC（`~/.claude/CLAUDE.md`「代码图谱工具路由」节 + memory `rfc-tool-routing-2026-05-27`）。本文件不重复。
