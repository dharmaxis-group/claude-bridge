# Claude Bridge

**Telegram <-> Claude Code CLI bridge** — control Claude Code from anywhere via your phone.

A lightweight bridge service that connects Telegram to `claude -p` (Claude Code's headless mode), giving you full Claude Code capabilities from any device. No extra API costs — reuses your Claude Max subscription.

> Blog & Changelog: [claudebridge.blogspot.com](https://claudebridge.blogspot.com)

## Key Features

- **Zero extra cost** — reuses Claude Max subscription ($100/mo). The bridge is pure I/O; all intelligence comes from `claude -p`
- **Multi-project management** — switch between projects via `/p`. Each project has its own CLAUDE.md, session state, and cost tracking
- **Session persistence** — SQLite-backed sessions with `--resume` support
- **Image support** — send photos from Telegram, Claude reads them via the Read tool
- **Cost tracking** — daily budget (configurable via `/budget`), per-project cost breakdown, `/cost` command
- **`/task` orchestration** — two-phase execution: readonly analysis first, then confirm to execute with full tools
- **InlineKeyboard UI** — interactive buttons for project selection, model switching, tool permissions
- **Tool permission profiles** — readonly (default), standard, restricted
- **LaunchAgent integration** — auto-start on boot, auto-restart on crash (macOS)

## Architecture

```
User (Telegram App)
    |
    v  Telegram Bot API (HTTPS)
Claude Bridge (Python, ~900 lines)
    |
    v  subprocess stdin/stdout (JSON)
Claude Code CLI (claude -p --output-format json)
    |
    v  cwd = target project directory
Project filesystem + CLAUDE.md
```

Design decisions:
- **stdin pipe** for message delivery (not CLI args) — handles `-` prefixed text that would be parsed as flags
- **Per-user FIFO lock + global semaphore** — prevents session conflicts, limits concurrent workers
- **Environment isolation** — subprocess unsets `CLAUDECODE` to prevent nested session detection

## Requirements

- **macOS** (Apple Silicon or Intel)
- **Python 3.10+** with `python-telegram-bot >= 22`
- **Claude Code CLI** (`claude`) — logged in with a Max subscription
- **Telegram Bot Token** — from [@BotFather](https://t.me/BotFather)
- **HTTP proxy** (optional) — required if Telegram API is blocked in your region

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/user/claude-bridge.git ~/.claude-bridge

# 2. Create data directories
mkdir -p ~/.claude-bridge/{data/images,logs}

# 3. Install dependencies
pip3 install python-telegram-bot

# 4. Configure
cp ~/.claude-bridge/config.example.json ~/.claude-bridge/config.json
# Edit config.json — fill in botToken, allowFrom, etc.

# 5. Run directly
python3 ~/.claude-bridge/claude-bridge.py

# Or install as macOS LaunchAgent (auto-start + auto-restart)
# See "LaunchAgent Setup" below
```

## Configuration

Copy `config.example.json` to `config.json` and fill in your values:

| Field | Type | Description |
|-------|------|-------------|
| `botToken` | string | Telegram Bot API token from @BotFather. Supports Keychain: `"!security find-generic-password -s SERVICE -a ACCOUNT -w"` |
| `allowFrom` | string[] | Telegram user ID whitelist (find yours via [@userinfobot](https://t.me/userinfobot)) |
| `proxy` | string | HTTP proxy URL (e.g. `http://127.0.0.1:1082`). Remove if not needed |
| `dailyBudget` | number | Initial daily budget in USD (default: 100). Managed at runtime via `/budget` |
| `defaultModel` | string | Default model: `opus` or `sonnet` |
| `defaultToolProfile` | string | Default tool permission: `readonly` / `standard` / `restricted` |
| `claudeBin` | string | Path to `claude` CLI binary |

### Keychain Integration (Recommended)

Store your bot token in macOS Keychain instead of plaintext:

```bash
# Store token
security add-generic-password -s "claude-bridge-bot-token" -a "claude-bridge" -w "YOUR_BOT_TOKEN"

# Reference in config.json
{
  "botToken": "!security find-generic-password -s claude-bridge-bot-token -a claude-bridge -w",
  ...
}
```

## Usage

### Telegram Commands

| Command | Description |
|---------|-------------|
| `/start`, `/help` | Help + quick action buttons |
| `/status` | Current status + all action buttons |
| `/p` | Project selection panel |
| `/p add <name> <path> [desc]` | Register a new project |
| `/p rm <name>` | Remove a project |
| `/model` | Switch model (Opus / Sonnet) |
| `/effort` | Set thinking depth (Low / Medium / High) |
| `/tools` | Set tool permissions (Readonly / Standard / Restricted) |
| `/think` | One-key switch to Opus + High effort |
| `/task <desc>` | Two-phase task: readonly analysis → confirm → execute |
| `/budget` | Daily budget settings (on/off/set amount) |
| `/new` | Reset current session |
| `/cost` | Cost summary (today / 7-day / by project) |
| `/restart` | Restart CB service (LaunchAgent auto-respawn) |

### Message Types

| Type | Handling |
|------|----------|
| Text | Sent as prompt to `claude -p` via stdin |
| Image | Downloaded, then Claude reads it via the Read tool |
| Image + Caption | Image path + caption combined as prompt |

### Tool Permissions

As of v1.2.0, Claude has full tool access in all modes. The `/tools` menu remains for future use but `--tools` is no longer passed to the CLI.

## LaunchAgent Setup (macOS)

Create `~/Library/LaunchAgents/ai.claude-bridge.plist`:

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
        <string>/Users/YOUR_USERNAME/.claude-bridge/claude-bridge.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/YOUR_USERNAME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>/Users/YOUR_USERNAME</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/YOUR_USERNAME/.claude-bridge/logs/claude-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USERNAME/.claude-bridge/logs/claude-bridge.err.log</string>
    <key>WorkingDirectory</key>
    <string>/Users/YOUR_USERNAME/.claude-bridge</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
```

```bash
# Load and start
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.claude-bridge.plist

# Check status
launchctl list ai.claude-bridge

# Restart after code changes
launchctl kickstart -k gui/$(id -u)/ai.claude-bridge

# View logs
tail -f ~/.claude-bridge/logs/claude-bridge.log
```

## Database

SQLite at `~/.claude-bridge/data/sessions.db` (WAL mode):

- **projects** — registered project name, path, description
- **sessions** — per-project session state (session_id, turns, cost)
- **active_project** — current project/model/tools per user
- **cost_log** — cost history for `/cost` reports
- **settings** — key-value store for runtime config (budget, etc.)

## License

[MIT](LICENSE)
