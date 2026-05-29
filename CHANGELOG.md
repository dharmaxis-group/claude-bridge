# Changelog

All notable changes to Claude Bridge are documented here.

## [1.8.1] — 2026-05-29

First publicly available release. The repository was previously private; this release strips all personal artifacts from both HEAD and git history before going public.

### Added
- **business-info-organizer skill integration** — when the user's message contains business-domain keywords (车牌 / 装车 / 采购 / 销售 / 汇款 / 供应商 / 客户 / etc.), the bot injects the `business-info-organizer` skill prompt to normalize unstructured Chinese business messages into ERP-ready templates. Includes five quality constraints (A-E: 净重双口径 / 单价校验 / 车牌格式 / 日期标准化 / 字段完整性) and graceful single-item degradation (no auto-numbering, no totals row for single-product entries).
- **`heartbeat-jobs.example.json`** — schema-annotated template showing how to register cron-style background jobs. Copy to `heartbeat-jobs.json` and customize.

### Removed (public release prep)
- **`blogger/`** — the maintainer's personal blog theme and posts; not part of the bridge codebase. The previous theme also contained a client-side SHA-256 gate which was an ineffective access control.
- **`tts-bench/outputs/`** — voice synthesis benchmark samples containing personal voice prints. The `tts-bench/benchmark.py` and `benchmark_clone.py` scripts are retained so users can run their own benchmarks against their own reference audio.
- **`skills`** symlink — pointed to an external skills directory. External users should manage skills via their own `~/.claude/skills/` or equivalent.
- **`heartbeat-jobs.json`** — was a personal jobs file containing maintainer-specific paths; moved to `.gitignore`. Use `heartbeat-jobs.example.json` as a starting point.

### Security
- **Uptime Kuma push token rotation** — the push token previously committed to `claude-bridge.py` (later moved to `config.json` in v1.8.0) has been revoked at the Uptime Kuma server side. Git history was rewritten with `git filter-repo` to remove the token, the associated server IP, the maintainer's home-directory path, and personal voice samples from all previous commits. The repository was not publicly accessible during the period the token was committed; this rewrite is precautionary in advance of going public.
- **Author email normalization** — all historical commit authors were rewritten to `noreply@dharmaxis-group.users.noreply.github.com` so the maintainer's personal email does not appear in `git log`.

### Known limitations (read before deploying publicly)
- **`business-info-organizer` skill is prompt-based, not tool-based** — the bot detects keywords and appends skill instructions to the prompt; it does not invoke the Claude Skill API and does not post-validate model output. A prompt-injection-savvy user could neutralize the skill instruction. The skill is designed for a single-user trusted Bot.
- **PII is preserved in full** — the business-info workflow intentionally keeps full ID numbers / phone numbers / bank cards in responses for accounting purposes. `_sanitize_response()` only masks credentials adjacent to password keywords, not PII. Do not deploy this bot as a service for untrusted users.
- **Webhook mode places the bot token in the URL path** — if you enable `webhookUrl`, the Telegram bot token ends up in reverse-proxy access logs. Use polling (default) unless you fully control the network path.

### Note for existing private-period clones
Earlier commit history was rewritten by `git filter-repo`; all commit hashes prior to v1.8.1 have changed. Existing clones from the private period must re-clone or run `git fetch origin && git reset --hard origin/main` against the new history.

## [1.8.0] — 2026-04-04

### Fixed
- **Watchdog crash loop (root cause)** — `_heartbeat_loop()` referenced undefined `_cfg` variable, causing `NameError` on every start. Since heartbeat was the sole watchdog feeder, `_watchdog_ts` never updated, triggering `os._exit(1)` every 5 minutes. All streaming responses >5min were killed mid-flight. Both CB Bot and secondary instance affected (same codebase). Fix: `_cfg.get(...)` → `load_config().get(...)`

### Added
- **Independent watchdog feeder** — `_watchdog_feeder()` coroutine runs every 30s with zero external dependencies, decoupled from Uptime Kuma heartbeat. Heartbeat failures can no longer kill the bot
- **Reply chain index** — `_msg_chain` stores last 200 messages per chat with reply_to links. Enables multi-level reply chain traversal (up to 5 levels deep) for parallel conversation threading. Bot replies are also indexed. Falls back to single-level quote when chain is not in memory

## [1.7.0] — 2026-03-31

### Added
- **Multi-agent parallel processing** — `concurrent_updates` enabled, 3 concurrent workers per user. Send multiple messages simultaneously and they process in parallel
- **Task priority system** — auto-detects quick queries vs normal conversations vs heavy agent tasks. Quick tasks get priority scheduling
- **503 adaptive throttle** — Claude Max rate limiting auto-detected, workers dynamically reduced then recovered
- **Shared typing loop** — single global typing indicator per chat replaces per-task typing, halving Telegram API pressure
- **Wave animation degradation** — secondary concurrent tasks reduce animation frequency (18s vs 6s) to avoid Pool timeout
- **Reply-to threading** — concurrent task responses link back to original message via reply_to
- **Enhanced /tasks** — shows worker utilization, priority labels, tool count, actual runtime
- **secondary bot instance** — the secondary bot Telegram Bot migrated to CB architecture via CB_HOME isolation
- **`/cancel` command** — terminate running Claude operation or agent (`proc.kill()` + cancel event)
- **Auto-send output files** — images/documents written by Claude are automatically sent to Telegram (PNG/JPG/PDF/CSV etc., 10MB limit)
- **Context buffer** — persists last 8 conversation exchanges per project in SQLite `context_buffer` table; injects last 3 into new sessions for continuity
- **Webhook mode** — add `webhookUrl` to config.json to switch from polling to webhook; defaults to polling when absent
- **Third-layer watchdog** — HTTP-level `getUpdates` activity monitor via httpx log filter; forces restart if no HTTP activity for 2 minutes
- **Protected directory guard for headless mode** — `~/.claude/` is a hardcoded protected directory where Edit/Write tools block in `-p` mode waiting for interactive approval. New two-layer system: (1) CLAUDE.md rule instructs Claude to use Bash tools instead; (2) `protected-dir-guard.py` PreToolUse hook intercepts Edit/Write targeting `~/.claude/*` and returns actionable Bash alternatives (exit 2 block + stderr guidance). Enables full Telegram Bot operation without `--dangerously-skip-permissions`

### Fixed
- **Pool Timeout eliminated** — connection pool 32→48, pool_timeout 60→90s, shared typing loop reduces concurrent API calls from N*3 to N+1
- **Pool Timeout + asyncio crash root cause fix** — roundtable diagnosis identified 3-layer cascade: (1) empty `proxy` config caused httpx to bypass mihomo proxy, relying on TUN which can silently hang connections; (2) `POLL_ACTIVITY_STALE_SEC=120` too short, causing false restarts on proxy latency spikes; (3) `RuntimeError: no running event loop` during `run_polling()` shutdown. Fixes: explicit proxy in config, stale threshold raised to 300s, RuntimeError catch for clean exit
- **`/cancel` actually stops running tasks** — three bugs prevented `/cancel` from working: (1) normal messages: partial results were still sent after kill; (2) agent mode: subprocess not registered in `_active_procs`, so `proc.kill()` couldn't reach it; (3) agent exec loop continued to next phase after cancel. All three fixed
- **Telegram 429 rate limit loop** — wave animation was editing messages every 0.4s (~150/min), far exceeding Telegram's ~30/min limit. Added exponential backoff (2x, max 30s) on 429 errors, increased base interval to 3s. Also added 429 backoff to `_stream_reply` progressive text reveal
- **Runaway session prevention** — `CLAUDE_TIMEOUT` changed from `None` (unlimited) to `3600` (1 hour max). Observed a 13+ hour stuck session causing 3,000+ rate limit errors

### Changed
- **MAX_CONCURRENT_WORKERS 6→3** — matches Claude Max actual concurrency limit (~2-3 simultaneous claude -p)
- **cb-watchdog orphan process prevention** — watchdog now checks if launchd manages the process before attempting restart, preventing duplicate instances from competing for Telegram getUpdates
- **launchd KeepAlive restored** — `ai.claude-bridge` plist existed but wasn't bootstrapped; process had no automatic restart supervision
- **Cost tag includes project name** — footer shows `project | model | effort | $cost | time`

### Security
- **Bot Token log redaction** — httpx logs now mask bot token as `bot****/` via `_GetUpdatesTracker` filter. Cleaned 64,645 historical occurrences from existing log files
- **GitHub repo set to PRIVATE** — temporarily unpublished from public access
- **Uptime Kuma push URL moved to config** — hardcoded external IP and push token replaced with `uptimeKumaPushUrl` config field; watchdog updated to match

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
- **6 projects registered** — added two additional projects to CB project registry

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
