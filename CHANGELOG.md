# Changelog

All notable changes to Claude Bridge are documented here.

## [Unreleased]

### Fixed
- **`/cancel` actually stops running tasks** — three bugs prevented `/cancel` from working: (1) normal messages: partial results were still sent after kill; (2) agent mode: subprocess not registered in `_active_procs`, so `proc.kill()` couldn't reach it; (3) agent exec loop continued to next phase after cancel. All three fixed
- **Telegram 429 rate limit loop** — wave animation was editing messages every 0.4s (~150/min), far exceeding Telegram's ~30/min limit. Added exponential backoff (2x, max 30s) on 429 errors, increased base interval to 3s. Also added 429 backoff to `_stream_reply` progressive text reveal
- **Runaway session prevention** — `CLAUDE_TIMEOUT` changed from `None` (unlimited) to `3600` (1 hour max). Observed a 13+ hour stuck session causing 3,000+ rate limit errors

### Security
- **Bot Token log redaction** — httpx logs now mask bot token as `bot****/` via `_GetUpdatesTracker` filter. Cleaned 64,645 historical occurrences from existing log files
- **GitHub repo set to PRIVATE** — temporarily unpublished from public access

### Added
- **`/cancel` command** — terminate running Claude operation or agent (`proc.kill()` + cancel event)
- **Auto-send output files** — images/documents written by Claude are automatically sent to Telegram (PNG/JPG/PDF/CSV etc., 10MB limit)
- **Context buffer** — persists last 8 conversation exchanges per project in SQLite `context_buffer` table; injects last 3 into new sessions for continuity
- **Webhook mode** — add `webhookUrl` to config.json to switch from polling to webhook; defaults to polling when absent
- **Third-layer watchdog** — HTTP-level `getUpdates` activity monitor via httpx log filter; forces restart if no HTTP activity for 2 minutes

### Changed
- **Cost tag includes project name** — footer shows `project | model | effort | $cost | time`

## [1.6.0] — 2026-03-18

### Added
- **Blog Features page** — comprehensive feature listing at `/p/features.html`, organized by category (Core Communication, Voice, Project Management, Automation, Cost, Security, Operations) with full command reference table
- **Navigation update** — added Features link to blog header nav (desktop + mobile): Home / Features / Why CB? / Changelog
- **Wave animation progress** — replaced static "Working..." with animated `◉ ◌ ◌` wave dots (0.6s/frame) during Claude invocation. Tool progress lines appear below the wave
- **Event loop watchdog** — independent OS thread monitors asyncio heartbeat; forces process restart via `os._exit(1)` if event loop freezes for >5 minutes (LaunchAgent auto-restarts)
- **Uptime Kuma heartbeat** — push-type health monitor pings Uptime Kuma every 2 minutes; LaunchAgent restarts automatically on miss
- **Poll monitor watchdog** — second OS-thread watchdog checks `updater.running` every 30s; forces restart if Telegram polling silently dies

### Changed
- **Default model → Opus 4.6** — upgraded from Sonnet for all requests
- **Default effort → high** — raised from medium
- **No timeout** — removed 900s `CLAUDE_TIMEOUT` to support long-running remote maintenance tasks from phone
- **Removed smart model routing** — eliminated auto-downgrade to Sonnet/low-effort for short queries; all requests use active model/effort settings
- **MAX_TURNS 8→50** — prevents complex tasks (sync/review/deploy) from being truncated with empty response when last turn is a tool call
- **6 projects registered** — added <redacted> and <redacted> to CB project registry

### Fixed
- **Model callback validation** — model selection via InlineKeyboard accepted arbitrary values without checking against MODELS whitelist. Now validates `model in MODELS` before writing to DB, consistent with effort/tools handlers
- **ElevenLabs key exception** — `/el` command crashed with unhandled `CalledProcessError` when Keychain entry was missing. Now catches exception and returns user-friendly error message
- **UTC+8 timezone for cost queries** — daily budget check, `/cost` today/7-day stats, and inline cost display all used UTC `date('now')`, causing 0:00–8:00 AM local time to count towards the previous day. Now applies `TZ_OFFSET = "+8 hours"` to both `created_at` and `'now'` in all 4 query sites

## [1.5.0] — 2026-03-13

### Added
- **`/el` command** — ElevenLabs account dashboard: view credits usage/limit, voice slots, clone capability, next billing amount
- **Telegram behavior constraints** — system context injection enforces concise replies (<5 lines), action-first style, no emoji decoration, sensitive data masking
- **Smart model routing** — short simple queries (<80 chars, non-complex) auto-downgrade to Sonnet + low effort for faster responses
- **Sensitive message auto-delete** — messages containing passwords/tokens are automatically deleted from chat after processing
- **21 ElevenLabs voices** — expanded from 5 to full Creator voice library (Sarah, Jessica, Laura, Alice, Matilda, Bella, Lily, River, George, Brian, Adam, Charlie, Roger, Callum, Harry, Liam, Will, Eric, Chris, Daniel, Bill)

### Fixed
- **Task GC leak** — `asyncio.create_task()` for fire-and-forget tasks (cron scheduler, agent loop) had no strong reference, causing "Task was destroyed but it is pending!" errors. New `_create_background_task()` holds references via a set with auto-cleanup done callback
- **Result type safety** — `result.get()` crashed with `'list' object has no attribute 'get'` when Claude CLI returned unexpected types. New `_safe_result()` normalizes all invoke returns to dict
- **Stream buffer overflow** — `readline()` with 4MB limit still overflowed on very large tool results ("Separator/chunk exceed limit"). Replaced with manual chunked `read(256KB)` + newline splitting, no size limit
- **Default voice** — corrected from Adam to Sarah (user preference established in v1.4.0 session)

### Security
- ElevenLabs account upgraded to Creator plan with 2FA enabled

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
