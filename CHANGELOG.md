# Changelog

All notable changes to Claude Bridge are documented here.

## [1.4.0] — 2026-03-12

### Added
- **Voice reply** — bot replies with both text and voice message when user sends voice. Supports two TTS engines: edge-tts (free, local) and ElevenLabs (cloud, higher quality)
- **`/voice` command** — interactive voice settings panel: toggle voice reply on/off, switch TTS engine (edge-tts / ElevenLabs), select voice with live preview
- **ElevenLabs integration** — cloud TTS with Sarah v3 voice for natural Chinese speech. API key stored in macOS Keychain
- **8 edge-tts Chinese voices** — Xiaoxiao, Xiaoyi, Yunxi, Yunjian, Yunyang, Yunxia, Xiaobei (Liaoning dialect), Xiaoni (Shaanxi dialect)
- **5 ElevenLabs voices** — Sarah, George, Brian, Jessica, Adam (all with eleven_v3 model)
- **Sensitive data masking** — passwords and tokens in user input are automatically masked in bot responses

### Changed
- Streaming subprocess buffer increased to 4MB (was 64KB default, caused truncation on large tool results)

## [1.3.0] — 2026-03-12

### Added
- **Streaming progress feedback** — real-time tool use progress during Claude operations. Shows which tools Claude is using (Read, Bash, Edit, Search, etc.) instead of blind "typing..." indicator
- **Voice message support** — send voice messages in Telegram, automatically transcribed via Whisper and sent to Claude as text prompt
- **Document/file handling** — send PDF, code files, logs, etc. directly in Telegram. Claude reads and analyzes the file content
- **`/cron` scheduled tasks** — register recurring prompts that run automatically on a schedule. Subcommands: `add`, `list`, `rm`, `pause`, `resume`. Min interval 5 minutes
- **Error notifications** — unhandled exceptions now send error summary back to Telegram chat instead of silently failing

### Fixed
- **Claude CLI JSON array format** — adapted `invoke_claude` to handle new `--output-format json` output (JSON array instead of single object). Extracts `type: "result"` event from array

### Changed
- `send_long_message` refactored to accept `bot` directly (enables cron scheduler to send messages without handler context)
- Main message flow now uses `stream-json` output format for real-time event processing

## [1.2.1] — 2026-03-11

### Security
- **Personal path leak purge**: git filter-repo removed 16 instances of personal filesystem paths (`/Users/<HOME>/`) from public repository history; force-push rewrote all affected commits
- Added `__pycache__/`, `*.pyc`, `*.pyo` to `.gitignore` (prevent bytecode leaking source paths)

### Fixed
- Blog post URL: corrected GitHub repo link from `anthropics/claude-bridge` to `dharmaxis-group/claude-bridge`

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
