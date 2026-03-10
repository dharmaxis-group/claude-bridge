# Changelog

All notable changes to Claude Bridge are documented here.

## [1.2.1] — 2026-03-11

### Security
- **Personal path leak purge**: git filter-repo removed 16 instances of personal filesystem paths (`/Users/chenmingzhong/`) from public repository history; force-push rewrote all affected commits
- Added `__pycache__/`, `*.pyc`, `*.pyo` to `.gitignore` (prevent bytecode leaking source paths)

### Fixed
- Blog post URL: corrected GitHub repo link from `anthropics/claude-bridge` to `rssprivacy-commits/claude-bridge`

## [1.2.0] — 2026-03-10

### Added
- `/restart` — restart CB service via Telegram (LaunchAgent KeepAlive auto-respawn)

### Changed
- Removed `--tools` flag from Claude invocation — tool profiles no longer enforced at CLI level; Claude has full tool access in all modes
- `MAX_TURNS` 15 → 8 (reduce runaway sessions)
- `CLAUDE_TIMEOUT` 300s → 900s (allow longer operations)

### Fixed
- **Bootstrap retry loop**: proxy downtime caused 1.5h outage (310 failed restarts). Root cause: `bootstrap_retries=0` (default) exits process on first failure → LaunchAgent blindly respawns → same failure. Fix: `bootstrap_retries=-1` (infinite retry within process)

## [1.1.0] — 2026-03-08

### Added
- `/task` — two-phase task orchestration: readonly analysis → confirm → execute with full tools
- `/budget` — interactive daily budget management via InlineKeyboard (on/off/set amount)
- Budget settings persisted to SQLite (`settings` table), no restart required
- CHANGELOG.md and sync checklist for external-facing docs

### Changed
- Daily budget default raised from $5 to $100
- Budget enforcement moved from JSON config to SQLite, runtime-configurable via Telegram
- `--allowedTools` → `--tools` for tool restriction (security fix: `--allowedTools` is ineffective in `-p` mode)

### Fixed
- Connection pool exhaustion causing bot to stop responding (pool size 1 → main=16, polling=4)

## [1.0.0] — 2026-03-08

First public release.

### Features
- **Telegram ↔ Claude Code bridge** — connect to `claude -p` headless mode via Telegram Bot API
- **Multi-project management** — `/p add/rm`, per-project session state and cost tracking
- **InlineKeyboard UI** — interactive menus for project, model, effort, and tool profile selection
- **Session persistence** — SQLite-backed sessions with `--resume` support, auto-rotate at 50 turns / $2
- **Model switching** — Opus / Sonnet via `/model` or `/think` (one-key Opus + high effort)
- **Tool permission profiles** — readonly (default), standard, restricted via `--tools` flag
- **Image support** — send photos from Telegram, Claude reads them via the Read tool
- **Cost tracking** — daily budget, per-project breakdown, `/cost` summary (today / 7-day / total)
- **`/task` two-phase orchestration** — Phase 1 readonly analysis → Telegram confirm → Phase 2 execute with full tools
- **LaunchAgent integration** — auto-start on boot, auto-restart on crash (macOS)
- **Keychain integration** — bot token stored in macOS Keychain, not plaintext config
- **stdin pipe message delivery** — handles `-` prefixed text that would be parsed as CLI flags
- **Environment isolation** — subprocess unsets `CLAUDECODE` to prevent nested session detection

### Security
- `--tools` flag for tool restriction (`--allowedTools` is ineffective in `-p` mode)
- `/task` Phase 2 uses `--permission-mode bypassPermissions` only after explicit user confirmation

### Bug Fixes
- Image messages: frozen dataclass AttributeError in python-telegram-bot v22
- `-` prefixed messages: parsed as CLI flags → switched to stdin pipe
- Empty responses: `stop_reason=tool_use` returns empty result → fallback message
- Connection pool exhaustion: default pool size 1 → main=16, polling=4
