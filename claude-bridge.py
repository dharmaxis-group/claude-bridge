#!/usr/bin/env python3
"""Claude Bridge — Telegram <-> Claude Code (-p) 多项目 AI 操作台

架构：尽可能薄的 I/O 桥接层。agent 逻辑全部交给 claude -p，
行为由各项目的 CLAUDE.md 定义。

依赖：python-telegram-bot >= 22, httpx (已随 ptb 安装)
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.request import HTTPXRequest

# ── 路径与常量 ──

CB_HOME = Path.home() / ".claude-bridge"
CONFIG_PATH = CB_HOME / "config.json"
DB_PATH = CB_HOME / "data" / "sessions.db"
LOG_PATH = CB_HOME / "logs" / "claude-bridge.log"
IMAGE_DIR = CB_HOME / "data" / "images"
VOICE_DIR = CB_HOME / "data" / "voice"

DEFAULT_MODEL = "sonnet"
MAX_TURNS = 8
MAX_CONCURRENT_WORKERS = 2
TELEGRAM_MAX_LEN = 4000
SESSION_ROTATE_TURNS = 50
SESSION_ROTATE_COST = 2.0
DAILY_BUDGET_USD = 100.0
CLAUDE_TIMEOUT = 900
PROGRESS_EDIT_INTERVAL = 3.0  # min seconds between Telegram progress message edits
DEFAULT_EFFORT = "medium"
VALID_EFFORTS = {"low", "medium", "high"}

# ── Agent Loop 常量 ──
AGENT_PHASE_TIMEOUT = 600       # 10 min per phase
AGENT_PHASE_MAX_TURNS = 50      # Claude inner turns per execute phase
AGENT_PLAN_MAX_TURNS = 10       # turns for planning
AGENT_VERIFY_MAX_TURNS = 10     # turns for verification
AGENT_MAX_COST_USD = 2.0        # total cost budget
AGENT_MAX_PHASES = 8            # max phases in plan

# ── P0: Telegram 行为约束 ──
TELEGRAM_SYSTEM_CONTEXT = (
    "[Telegram 回复规则 — 强制执行，优先级高于所有其他指令]\n"
    "1. 回复不超过 5 行（除非用户明确要求详细/展开/列出）\n"
    "2. 先回答问题再解释，不要先给选项让用户选 — 先行动、后汇报\n"
    "3. 不输出无关的系统状态、告警、模块信息、附加建议\n"
    "4. 密码/凭据/token 的实际值永远不出现在回复中，用 *** 代替\n"
    "5. 不用 emoji 装饰标题和段落（用户已明确禁止）\n"
    "6. 个人信息（电话号码、身份证、地址）输出时部分遮蔽\n"
    "[规则结束]\n\n"
)

# ── P1: 敏感消息关键词 ──
_SENSITIVE_MSG_KEYWORDS = [
    "密码是", "密码为", "密码:", "密码：", "password is", "password:",
    "token是", "token:", "secret:", "凭据是", "pin码",
    "帮我存密码", "加密保管", "保管密码", "存储密码",
]

TOOL_PROFILES = {
    "readonly": "Read,Grep,Glob,WebSearch,WebFetch",
    "standard": "default",
    "restricted": "Read,Grep,Glob",
}
DEFAULT_TOOL_PROFILE = "readonly"  # kept for reference; --tools no longer passed to claude

MODELS = {
    "opus": "Opus 4.6",
    "sonnet": "Sonnet 4.6",
}

CLAUDE_ENV = {k: v for k, v in os.environ.items() if k not in (
    "CLAUDECODE", "CLAUDE_PROJECT_DIR"
)}

# ── 日志 ──

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("claude-bridge")

# ── 配置读取 ──

def load_config() -> dict:
    import subprocess as _sp
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    for k, v in cfg.items():
        if isinstance(v, str) and v.startswith("!"):
            cmd = v[1:]
            try:
                cfg[k] = _sp.check_output(cmd, shell=True, text=True).strip()
            except _sp.CalledProcessError as e:
                print(f"Shell expansion failed for {k}: {e}", file=sys.stderr)
                sys.exit(1)
    return cfg


def get_claude_bin() -> Path:
    cfg = load_config()
    return Path(cfg.get("claudeBin", "~/.local/bin/claude")).expanduser()


def get_proxy() -> str:
    cfg = load_config()
    return cfg.get("proxy", "http://127.0.0.1:1082")


# ── SQLite ──

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            name TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            description TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sessions (
            chat_id TEXT NOT NULL,
            project TEXT NOT NULL,
            session_id TEXT NOT NULL,
            model TEXT DEFAULT 'sonnet',
            turns INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (chat_id, project)
        );
        CREATE TABLE IF NOT EXISTS active_project (
            chat_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            model TEXT DEFAULT 'sonnet',
            tool_profile TEXT DEFAULT 'readonly',
            effort TEXT DEFAULT 'medium'
        );
        CREATE TABLE IF NOT EXISTS cost_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            project TEXT NOT NULL,
            cost_usd REAL NOT NULL,
            turns INTEGER NOT NULL,
            duration_ms INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cron_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            project TEXT NOT NULL,
            prompt TEXT NOT NULL,
            interval_sec INTEGER NOT NULL,
            model TEXT DEFAULT 'sonnet',
            effort TEXT DEFAULT 'medium',
            enabled INTEGER DEFAULT 1,
            last_run TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    try:
        conn.execute("ALTER TABLE active_project ADD COLUMN effort TEXT DEFAULT 'medium'")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return conn


def get_setting(key: str, default: str = None) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()


# ── 全局状态 ──

db: sqlite3.Connection = None
user_locks: dict[str, asyncio.Lock] = {}
worker_semaphore: asyncio.Semaphore = None
agent_running: dict[str, dict] = {}   # chat_id -> {"cancel": Event, ...}
_sensitive_values: dict[str, list[str]] = {}  # chat_id -> [password_value, ...]

# ── 敏感信息防护 ──

_SENSITIVE_KW_RE = re.compile(
    r'(?:密码|password|passwd|secret|token|credential|凭据|口令|pin码?)'
    r'[\s:：=是为]*'
    r'[`"\']?'
    r'([^\s`"\'，。,.\n]{4,64})'
    r'[`"\']?',
    re.IGNORECASE,
)


def _extract_sensitive_from_input(chat_id: str, text: str):
    """Extract and store potential passwords/credentials from user input."""
    matches = _SENSITIVE_KW_RE.findall(text)
    if matches:
        vals = _sensitive_values.setdefault(chat_id, [])
        for m in matches:
            if m not in vals:
                vals.append(m)
        _sensitive_values[chat_id] = vals[-20:]  # keep last 20


def _mask_value(val: str) -> str:
    if len(val) <= 3:
        return "***"
    return val[0] + "*" * (len(val) - 2) + val[-1]


def _sanitize_response(chat_id: str, text: str) -> str:
    """Mask sensitive values in outgoing response text. Two layers:
    1. Exact-match: mask values previously extracted from user input.
    2. Keyword-proximity: mask values adjacent to password keywords in the response."""
    # Layer 1: exact match from tracked user input
    for val in _sensitive_values.get(chat_id, []):
        if val in text:
            text = text.replace(val, _mask_value(val))
    # Layer 2: keyword-proximity in response text
    def _kw_mask(m):
        val = m.group(1)
        return m.group(0).replace(val, _mask_value(val))
    text = _SENSITIVE_KW_RE.sub(_kw_mask, text)
    return text


def get_user_lock(chat_id: str) -> asyncio.Lock:
    if chat_id not in user_locks:
        user_locks[chat_id] = asyncio.Lock()
    return user_locks[chat_id]


# ── 数据库操作 ──

def get_active_project(chat_id: str) -> dict | None:
    row = db.execute(
        "SELECT a.project, a.model, a.tool_profile, p.path, a.effort "
        "FROM active_project a JOIN projects p ON a.project = p.name "
        "WHERE a.chat_id = ?", (chat_id,)
    ).fetchone()
    if row:
        return {"project": row[0], "model": row[1], "tool_profile": row[2],
                "path": row[3], "effort": row[4] or DEFAULT_EFFORT}
    return None


def set_active_project(chat_id: str, project: str, model: str = None,
                       tool_profile: str = None, effort: str = None):
    cfg = load_config()
    m = model or cfg.get("defaultModel", DEFAULT_MODEL)
    tp = tool_profile or cfg.get("defaultToolProfile", DEFAULT_TOOL_PROFILE)
    ef = effort or DEFAULT_EFFORT
    db.execute(
        "INSERT INTO active_project (chat_id, project, model, tool_profile, effort) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET project=?, "
        "model=COALESCE(?, model), tool_profile=COALESCE(?, tool_profile), "
        "effort=COALESCE(?, effort)",
        (chat_id, project, m, tp, ef, project, model, tool_profile, effort),
    )
    db.commit()


def get_session(chat_id: str, project: str) -> dict | None:
    row = db.execute(
        "SELECT session_id, model, turns, cost_usd FROM sessions WHERE chat_id=? AND project=?",
        (chat_id, project),
    ).fetchone()
    if row:
        return {"session_id": row[0], "model": row[1], "turns": row[2], "cost_usd": row[3]}
    return None


def upsert_session(chat_id: str, project: str, session_id: str, model: str,
                   add_turns: int = 0, add_cost: float = 0.0):
    db.execute(
        "INSERT INTO sessions (chat_id, project, session_id, model, turns, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id, project) DO UPDATE SET "
        "session_id=?, model=?, turns=turns+?, cost_usd=cost_usd+?, updated_at=datetime('now')",
        (chat_id, project, session_id, model, add_turns, add_cost,
         session_id, model, add_turns, add_cost),
    )
    db.commit()


def reset_session(chat_id: str, project: str):
    db.execute("DELETE FROM sessions WHERE chat_id=? AND project=?", (chat_id, project))
    db.commit()


def log_cost(chat_id: str, project: str, cost: float, turns: int, duration_ms: int):
    db.execute(
        "INSERT INTO cost_log (chat_id, project, cost_usd, turns, duration_ms) VALUES (?,?,?,?,?)",
        (chat_id, project, cost, turns, duration_ms),
    )
    db.commit()


def get_budget() -> tuple[bool, float]:
    """Return (enabled, amount). enabled=False means budget checking is off."""
    enabled = get_setting("budget_enabled", "1")
    amount = float(get_setting("budget_amount", str(DAILY_BUDGET_USD)))
    return enabled == "1", amount


def get_daily_cost(chat_id: str) -> float:
    row = db.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_log "
        "WHERE chat_id=? AND date(created_at)=date('now')", (chat_id,),
    ).fetchone()
    return row[0] if row else 0.0


def list_projects() -> list[dict]:
    rows = db.execute("SELECT name, path, description FROM projects ORDER BY name").fetchall()
    return [{"name": r[0], "path": r[1], "description": r[2]} for r in rows]


def _parse_interval(s: str) -> int | None:
    """Parse '5m', '1h', '6h', '1d' to seconds. Min 5 minutes."""
    m = re.match(r'^(\d+)(m|h|d)$', s.strip().lower())
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    sec = val * {'m': 60, 'h': 3600, 'd': 86400}[unit]
    return sec if sec >= 300 else None


def _format_interval(sec: int) -> str:
    if sec >= 86400 and sec % 86400 == 0:
        return f"{sec // 86400}d"
    if sec >= 3600 and sec % 3600 == 0:
        return f"{sec // 3600}h"
    return f"{sec // 60}m"


# ── 鉴权 ──

def is_allowed(chat_id: int) -> bool:
    cfg = load_config()
    allow = cfg.get("allowFrom", [])
    return str(chat_id) in [str(a) for a in allow]


# ── InlineKeyboard 构建器 ──

def make_keyboard(items: list[tuple[str, str]], columns: int = 2,
                   back_to: str = None) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text, callback_data=data) for text, data in items]
    rows = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
    if back_to:
        rows.append([InlineKeyboardButton("<< Back", callback_data=back_to)])
    return InlineKeyboardMarkup(rows)


def status_text(active: dict, session: dict | None, daily: float) -> str:
    parts = [f"{active['project']}  |  {MODELS.get(active['model'], active['model'])}  |  {active['effort']}"]
    if session:
        parts.append(f"{session['turns']}t  ${session['cost_usd']:.3f}")
    enabled, amount = get_budget()
    if enabled:
        parts.append(f"Today ${daily:.3f} / ${amount:.0f}")
    else:
        parts.append(f"Today ${daily:.3f} (no limit)")
    return "\n".join(parts)


# ── Claude Invoker ──

async def invoke_claude(message: str, project_path: str, session_id: str | None,
                        model: str, tool_profile: str, effort: str = "medium",
                        bypass_permissions: bool = False) -> dict:
    claude_bin = get_claude_bin()
    cmd = [
        str(claude_bin), "-p",
        "--output-format", "json",
        "--max-turns", str(MAX_TURNS),
        "--model", model,
        "--effort", effort,
    ]
    if bypass_permissions:
        cmd.extend(["--permission-mode", "bypassPermissions"])
    if session_id:
        cmd.extend(["--resume", session_id])

    log.info(f"invoke: model={model} effort={effort} project={project_path} resume={session_id is not None}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_path,
            env=CLAUDE_ENV,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=message.encode("utf-8")), timeout=CLAUDE_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"error": f"Claude timeout ({CLAUDE_TIMEOUT}s)", "result": None}

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            log.error(f"claude exit {proc.returncode}: {err}")
            return {"error": f"Claude exit {proc.returncode}: {err[:200]}", "result": None}

        raw = stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            return {"error": "Claude returned empty output", "result": None}
        parsed = json.loads(raw)
        # claude CLI --output-format json now returns a JSON array of events;
        # extract the {"type": "result", ...} element
        if isinstance(parsed, list):
            for item in reversed(parsed):
                if isinstance(item, dict) and item.get("type") == "result":
                    return item
            return {"error": "No result event in Claude output", "result": None}
        return parsed

    except json.JSONDecodeError as e:
        log.error(f"JSON parse error: {e}, raw={raw[:200]}")
        return {"error": f"JSON parse error: {e}", "result": raw[:500]}
    except Exception as e:
        log.error(f"invoke error: {e}")
        return {"error": str(e), "result": None}


def _format_tool_progress(name: str, input_data: dict) -> str:
    """Format a tool_use event into a concise progress line."""
    if name == "Read":
        p = input_data.get("file_path", "")
        return f"Read {Path(p).name}" if p else "Read"
    if name == "Bash":
        cmd = input_data.get("command", "")
        return f"$ {cmd[:40]}..." if len(cmd) > 40 else f"$ {cmd}"
    if name in ("Edit", "Write"):
        p = input_data.get("file_path", "")
        return f"Edit {Path(p).name}" if p else name
    if name == "Grep":
        pat = input_data.get("pattern", "")
        return f"Search: {pat[:25]}..." if len(pat) > 25 else f"Search: {pat}"
    if name == "Glob":
        return f"Find: {input_data.get('pattern', '')[:30]}"
    if name == "WebSearch":
        q = input_data.get("query", "")
        return f"Web: {q[:30]}..." if len(q) > 30 else f"Web: {q}"
    if name == "WebFetch":
        url = input_data.get("url", "")
        return f"Fetch: {url[:30]}..." if len(url) > 30 else f"Fetch: {url}"
    return name


async def invoke_claude_streaming(message: str, project_path: str, session_id: str | None,
                                   model: str, tool_profile: str, effort: str = "medium",
                                   bypass_permissions: bool = False,
                                   on_tool_use=None) -> dict:
    """Stream claude -p output via stream-json, calling on_tool_use(name, input) for progress.
    Returns the final result dict (same schema as invoke_claude)."""
    claude_bin = get_claude_bin()
    cmd = [
        str(claude_bin), "-p",
        "--output-format", "stream-json",
        "--max-turns", str(MAX_TURNS),
        "--model", model,
        "--effort", effort,
    ]
    if bypass_permissions:
        cmd.extend(["--permission-mode", "bypassPermissions"])
    if session_id:
        cmd.extend(["--resume", session_id])

    log.info(f"invoke_stream: model={model} effort={effort} project={project_path} resume={session_id is not None}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_path,
            env=CLAUDE_ENV,
            limit=4 * 1024 * 1024,  # 4MB line buffer (default 64KB too small for large tool results)
        )
        proc.stdin.write(message.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        result = {"error": "No result received", "result": None}
        deadline = time.monotonic() + CLAUDE_TIMEOUT

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                await proc.wait()
                return {"error": f"Claude timeout ({CLAUDE_TIMEOUT}s)", "result": None}

            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=min(remaining, 30))
            except asyncio.TimeoutError:
                if time.monotonic() >= deadline:
                    proc.kill()
                    await proc.wait()
                    return {"error": f"Claude timeout ({CLAUDE_TIMEOUT}s)", "result": None}
                continue

            if not line:
                break

            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")
            if etype == "result":
                result = event
            elif etype == "assistant" and on_tool_use:
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        await on_tool_use(block.get("name", ""), block.get("input", {}))

        await proc.wait()

        if proc.returncode != 0 and not result.get("result"):
            stderr_data = await proc.stderr.read()
            err = stderr_data.decode("utf-8", errors="replace").strip()
            log.error(f"claude exit {proc.returncode}: {err}")
            return {"error": f"Claude exit {proc.returncode}: {err[:200]}", "result": None}

        return result

    except Exception as e:
        log.error(f"invoke_stream error: {e}")
        return {"error": str(e), "result": None}


# ── Telegram 消息处理 ──

async def send_typing_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def send_long_message(bot, chat_id: int, text: str):
    """Split and send a long message. Accepts Bot instance directly."""
    # Sanitize sensitive data at the final output gate
    text = _sanitize_response(str(chat_id), text)
    chunks = []
    while len(text) > TELEGRAM_MAX_LEN:
        split_pos = text.rfind("\n", 0, TELEGRAM_MAX_LEN)
        if split_pos == -1:
            split_pos = TELEGRAM_MAX_LEN
        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")
    chunks.append(text)

    for chunk in chunks:
        if not chunk.strip():
            continue
        try:
            await bot.send_message(
                chat_id=chat_id, text=chunk, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            await bot.send_message(chat_id=chat_id, text=chunk)


STREAM_INTERVAL = 0.35       # seconds between edits
STREAM_INITIAL_CHUNK = 20    # chars for first reveal
STREAM_ACCEL = 1.4           # chunk growth factor per step
STREAM_MAX_CHUNK = 200       # max chars per edit step
STREAM_MSG_LIMIT = 3800      # start new message before hitting Telegram 4096 limit


async def _stream_reply(bot, chat_id: int, text: str, reuse_msg=None):
    """Progressively reveal text in a Telegram message (typing effect).
    If reuse_msg is provided, edits that message; otherwise creates a new one."""
    if not text or not text.strip():
        if reuse_msg:
            try:
                await reuse_msg.delete()
            except Exception:
                pass
        return

    cursor = "▍"
    pos = 0
    chunk_size = float(STREAM_INITIAL_CHUNK)
    msg = reuse_msg

    # First edit: clear progress content, show cursor
    if msg:
        try:
            await msg.edit_text(cursor)
        except Exception:
            msg = None

    if not msg:
        msg = await bot.send_message(chat_id=chat_id, text=cursor)

    while pos < len(text):
        step = int(chunk_size)
        # Snap to word/line boundary for natural reveals
        target = min(pos + step, len(text))
        if target < len(text):
            # Try to break at newline first, then space
            nl = text.rfind("\n", pos, target + 1)
            sp = text.rfind(" ", pos, target + 1)
            if nl > pos:
                target = nl + 1
            elif sp > pos:
                target = sp + 1
        pos = target

        # Check if we need to start a new message (approaching Telegram limit)
        if pos > STREAM_MSG_LIMIT and pos < len(text):
            # Finalize current message with text so far (no cursor)
            try:
                await msg.edit_text(text[:pos], parse_mode=ParseMode.MARKDOWN)
            except Exception:
                try:
                    await msg.edit_text(text[:pos])
                except Exception:
                    pass
            # Start new message for remaining text
            text = text[pos:]
            pos = 0
            chunk_size = float(STREAM_INITIAL_CHUNK)
            msg = await bot.send_message(chat_id=chat_id, text=cursor)
            continue

        display = text[:pos] + (cursor if pos < len(text) else "")
        try:
            await msg.edit_text(display, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            try:
                await msg.edit_text(display)
            except Exception:
                pass

        chunk_size = min(chunk_size * STREAM_ACCEL, STREAM_MAX_CHUNK)
        if pos < len(text):
            await asyncio.sleep(STREAM_INTERVAL)

    # Final edit without cursor (if cursor was shown)
    if cursor in (text[:pos] + cursor):
        try:
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            try:
                await msg.edit_text(text)
            except Exception:
                pass


async def _invoke_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            text: str):
    """共享的 Claude 调用 + 回复逻辑，供 handle_message 和 handle_photo 使用"""
    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)

    # Extract sensitive values from user input for response masking
    _extract_sensitive_from_input(chat_id_str, text)

    # P1: Auto-delete user messages containing passwords/credentials
    text_lower = text.lower()
    is_sensitive_msg = any(kw in text_lower for kw in _SENSITIVE_MSG_KEYWORDS)
    if is_sensitive_msg:
        try:
            await update.message.delete()
            log.info(f"Auto-deleted sensitive message from chat {chat_id_str}")
        except Exception as e:
            log.warning(f"Failed to delete sensitive message: {e}")

    # P0: Inject Telegram behavior constraints
    augmented_text = TELEGRAM_SYSTEM_CONTEXT + text

    # P2: Smart model routing — downgrade simple queries for speed
    effective_model = None  # None = use active["model"]
    effective_effort = None
    text_len = len(text)
    if text_len < 80 and not any(kw in text_lower for kw in [
        "部署", "deploy", "修复", "fix", "重构", "refactor", "分析", "analyze",
        "调研", "research", "设计", "design", "实现", "implement", "编写", "write",
        "创建", "create", "搭建", "build", "迁移", "migrate",
    ]):
        effective_model = "sonnet"
        effective_effort = "low"

    active = get_active_project(chat_id_str)
    if not active:
        projects = list_projects()
        if not projects:
            await update.message.reply_text("No projects. Use /p add <name> <path>")
            return
        set_active_project(chat_id_str, projects[0]["name"])
        active = get_active_project(chat_id_str)

    if not Path(active["path"]).exists():
        await update.message.reply_text(f"Path not found: {active['path']}")
        return

    daily_cost = get_daily_cost(chat_id_str)
    budget_enabled, budget_amount = get_budget()
    if budget_enabled and daily_cost >= budget_amount:
        await update.message.reply_text(
            f"Daily budget reached (${daily_cost:.2f} / ${budget_amount:.0f}). "
            f"Use /budget to adjust.")
        return

    session = get_session(chat_id_str, active["project"])
    session_id = session["session_id"] if session else None

    if session and (session["turns"] >= SESSION_ROTATE_TURNS or session["cost_usd"] >= SESSION_ROTATE_COST):
        kb = make_keyboard([("New session", "cmd:new"), ("Continue", "cmd:dismiss")], columns=2)
        await update.message.reply_text(
            f"Session: {session['turns']}t, ${session['cost_usd']:.2f}. Start fresh?",
            reply_markup=kb,
        )

    # Progress message for streaming feedback
    progress_msg = await update.message.reply_text("Working...")
    progress_lines = []
    last_edit = 0.0

    async def on_tool_use(tool_name: str, tool_input: dict):
        nonlocal last_edit
        progress_lines.append(_format_tool_progress(tool_name, tool_input))
        now = time.monotonic()
        if now - last_edit >= PROGRESS_EDIT_INTERVAL:
            last_edit = now
            display = progress_lines[-6:]
            progress_text = "Working...\n" + "\n".join(f"`> {l}`" for l in display)
            try:
                await progress_msg.edit_text(progress_text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

    lock = get_user_lock(chat_id_str)
    async with lock:
        async with worker_semaphore:
            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(send_typing_loop(context, chat_id, stop_typing))
            try:
                use_model = effective_model or active["model"]
                use_effort = effective_effort or active["effort"]
                result = await invoke_claude_streaming(
                    message=augmented_text,
                    project_path=active["path"],
                    session_id=session_id,
                    model=use_model,
                    tool_profile=active["tool_profile"],
                    effort=use_effort,
                    on_tool_use=on_tool_use,
                )
            finally:
                stop_typing.set()
                await typing_task

    if result.get("error") and not result.get("result"):
        try:
            await progress_msg.delete()
        except Exception:
            pass
        await update.message.reply_text(f"Error: {result['error'][:500]}")
        return None

    reply_text = result.get("result", "")
    if not reply_text:
        stop = result.get("stop_reason", "unknown")
        if stop == "tool_use":
            reply_text = "(Claude used tools but didn't produce a text response. The operation may have completed silently.)"
        elif stop == "max_turns":
            reply_text = "(Reached max turns limit)"
        else:
            reply_text = f"(empty response, stop_reason={stop})"
    new_session_id = result.get("session_id", session_id)
    cost = result.get("total_cost_usd", 0.0)
    turns = result.get("num_turns", 1)
    duration = result.get("duration_ms", 0)

    # Sanitize sensitive data before sending to Telegram
    reply_text = _sanitize_response(chat_id_str, reply_text)

    cost_tag = f"\n\n`{use_model} | {use_effort} | ${cost:.4f} | {duration/1000:.1f}s`"
    full_reply = reply_text + cost_tag

    upsert_session(chat_id_str, active["project"], new_session_id, active["model"], turns, cost)
    log_cost(chat_id_str, active["project"], cost, turns, duration)

    # Streaming typing effect: progressively reveal text in the progress message
    await _stream_reply(context.bot, chat_id, full_reply, progress_msg)
    return result.get("result", "")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理图片消息：下载图片 → 构造 prompt → 调用 Claude"""
    if not update.message or not update.message.photo:
        return
    if not is_allowed(update.effective_chat.id):
        return

    try:
        photo = update.message.photo[-1]
        caption = (update.message.caption or "").strip()

        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        file = await context.bot.get_file(photo.file_id)
        img_path = IMAGE_DIR / f"{photo.file_unique_id}.jpg"
        await file.download_to_drive(str(img_path))
        log.info(f"photo downloaded: {img_path} ({photo.width}x{photo.height})")

        prompt = f"I'm sending you an image. Use the Read tool to view the file at {img_path} first, then respond."
        if caption:
            prompt += f"\n\nUser message: {caption}"
        else:
            prompt += "\n\nDescribe what you see and ask if I need help with anything."

        await _invoke_and_reply(update, context, prompt)

        try:
            img_path.unlink(missing_ok=True)
        except Exception:
            pass

    except Exception as e:
        log.error(f"handle_photo failed: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"Image processing failed: {e}")
        except Exception:
            pass


TTS_MAX_CHARS = 800  # TTS 文本上限，超长只语音前 800 字

# ── TTS 引擎配置 ──
EDGE_TTS_VOICES = {
    "Xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "Xiaoyi": "zh-CN-XiaoyiNeural",
    "Yunxi": "zh-CN-YunxiNeural",
    "Yunjian": "zh-CN-YunjianNeural",
    "Yunyang": "zh-CN-YunyangNeural",
    "Yunxia": "zh-CN-YunxiaNeural",
    "Xiaobei": "zh-CN-liaoning-XiaobeiNeural",
    "Xiaoni": "zh-CN-shaanxi-XiaoniNeural",
}
ELEVENLABS_VOICES = {
    "Sarah": "EXAVITQu4vr4xnSDxMaL",
    "George": "JBFqnCBsd6RMkjVDRZzb",
    "Brian": "nPczCjzI2devNBz1zQrb",
    "Jessica": "cgSgspJ2msm6clMCkdW9",
    "Adam": "pNInz6obpgDQGcFmaJgB",
}
ELEVENLABS_MODEL = "eleven_v3"


def _get_voice_settings() -> dict:
    """Get voice TTS settings from DB. Returns {enabled, engine, voice}."""
    return {
        "enabled": get_setting("voice_enabled", "1") == "1",
        "engine": get_setting("voice_engine", "edge"),  # "edge" or "eleven"
        "voice": get_setting("voice_name", "Xiaoxiao"),
    }


def _get_elevenlabs_key() -> str:
    """Read ElevenLabs API key from Keychain."""
    import subprocess as _sp
    return _sp.check_output(
        ["security", "find-generic-password", "-s", "elevenlabs-api-key", "-a", "elevenlabs", "-w"],
        text=True,
    ).strip()


async def _tts_edge(clean: str, mp3_path, ogg_path, voice_name: str):
    """Generate voice via edge-tts."""
    voice_id = EDGE_TTS_VOICES.get(voice_name, "zh-CN-XiaoxiaoNeural")
    proc = await asyncio.create_subprocess_exec(
        "edge-tts", "--voice", voice_id, "--text", clean,
        "--write-media", str(mp3_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode != 0 or not mp3_path.exists():
        return False
    # ffmpeg MP3 → OGG Opus
    proc2 = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(mp3_path),
        "-c:a", "libopus", "-b:a", "48k", str(ogg_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc2.communicate(), timeout=30)
    return proc2.returncode == 0 and ogg_path.exists()


async def _tts_elevenlabs(clean: str, mp3_path, ogg_path, voice_name: str):
    """Generate voice via ElevenLabs API."""
    import httpx
    voice_id = ELEVENLABS_VOICES.get(voice_name, "EXAVITQu4vr4xnSDxMaL")
    api_key = _get_elevenlabs_key()
    async with httpx.AsyncClient(proxy="http://127.0.0.1:1082", timeout=30) as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={"text": clean, "model_id": ELEVENLABS_MODEL},
        )
    if resp.status_code != 200:
        log.error(f"ElevenLabs API error: {resp.status_code} {resp.text[:200]}")
        return False
    mp3_path.write_bytes(resp.content)
    # ffmpeg MP3 → OGG Opus
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(mp3_path),
        "-c:a", "libopus", "-b:a", "48k", str(ogg_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=30)
    return proc.returncode == 0 and ogg_path.exists()


async def _send_voice_reply(bot, chat_id: int, text: str):
    """Convert text to voice via configured TTS engine → send_voice."""
    vs = _get_voice_settings()
    if not vs["enabled"]:
        return
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    # Strip markdown/code formatting for cleaner speech
    clean = re.sub(r'```[\s\S]*?```', '', text)  # remove code blocks
    clean = re.sub(r'`[^`]+`', '', clean)  # remove inline code
    clean = re.sub(r'[*_~\[\]()#>]', '', clean).strip()  # remove markdown chars
    if not clean:
        return
    if len(clean) > TTS_MAX_CHARS:
        clean = clean[:TTS_MAX_CHARS] + "……后续请看文字回复"

    mp3_path = VOICE_DIR / f"tts_{chat_id}_{int(time.time())}.mp3"
    ogg_path = mp3_path.with_suffix(".ogg")

    try:
        if vs["engine"] == "eleven":
            ok = await _tts_elevenlabs(clean, mp3_path, ogg_path, vs["voice"])
        else:
            ok = await _tts_edge(clean, mp3_path, ogg_path, vs["voice"])
        if not ok:
            return
        with open(ogg_path, "rb") as f:
            await bot.send_voice(chat_id=chat_id, voice=f)
    except Exception as e:
        log.error(f"TTS failed: {e}")
    finally:
        try:
            mp3_path.unlink(missing_ok=True)
            ogg_path.unlink(missing_ok=True)
        except Exception:
            pass


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理语音消息：下载 → Whisper 转录 → Claude → 文字+语音回复"""
    if not update.message or not update.message.voice:
        return
    if not is_allowed(update.effective_chat.id):
        return

    try:
        voice = update.message.voice
        VOICE_DIR.mkdir(parents=True, exist_ok=True)

        file = await context.bot.get_file(voice.file_id)
        voice_path = VOICE_DIR / f"{voice.file_unique_id}.ogg"
        await file.download_to_drive(str(voice_path))
        log.info(f"voice downloaded: {voice_path} ({voice.duration}s)")

        status_msg = await update.message.reply_text("Transcribing...")
        proc = await asyncio.create_subprocess_exec(
            "whisper", str(voice_path),
            "--model", "base",
            "--output_format", "txt",
            "--output_dir", str(VOICE_DIR),
            "--language", "zh",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:200]
            await status_msg.edit_text(f"Transcription failed: {err}")
            return

        txt_path = voice_path.with_suffix(".txt")
        if not txt_path.exists():
            await status_msg.edit_text("Transcription produced no output")
            return

        transcript = txt_path.read_text().strip()
        if not transcript:
            await status_msg.edit_text("Empty transcription (audio too quiet?)")
            return

        await status_msg.edit_text(f"🎤 {transcript}")
        log.info(f"voice transcribed: {transcript[:100]}")

        # Invoke Claude and get reply text
        reply_text = await _invoke_and_reply(update, context, transcript)

        # Voice reply: convert Claude's response to speech
        if reply_text:
            await _send_voice_reply(context.bot, update.effective_chat.id, reply_text)

        # Cleanup
        try:
            voice_path.unlink(missing_ok=True)
            txt_path.unlink(missing_ok=True)
        except Exception:
            pass

    except asyncio.TimeoutError:
        await update.message.reply_text("Transcription timed out")
    except Exception as e:
        log.error(f"handle_voice failed: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"Voice processing failed: {e}")
        except Exception:
            pass


DOCUMENT_DIR = CB_HOME / "data" / "documents"

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文档消息：下载文件 → 构造 prompt → Claude Read"""
    if not update.message or not update.message.document:
        return
    if not is_allowed(update.effective_chat.id):
        return

    try:
        doc = update.message.document
        caption = (update.message.caption or "").strip()
        file_name = doc.file_name or "unknown"

        # 20MB Telegram bot API limit
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text(f"File too large ({doc.file_size // 1024 // 1024}MB). Max 20MB.")
            return

        DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
        file = await context.bot.get_file(doc.file_id)
        doc_path = DOCUMENT_DIR / f"{doc.file_unique_id}_{file_name}"
        await file.download_to_drive(str(doc_path))
        log.info(f"document downloaded: {doc_path} ({doc.file_size} bytes)")

        prompt = f"I'm sending you a file: {file_name}\nUse the Read tool to view the file at {doc_path} first, then respond."
        if caption:
            prompt += f"\n\nUser message: {caption}"
        else:
            prompt += "\n\nAnalyze this file and summarize what you see."

        await _invoke_and_reply(update, context, prompt)

        try:
            doc_path.unlink(missing_ok=True)
        except Exception:
            pass

    except Exception as e:
        log.error(f"handle_document failed: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"Document processing failed: {e}")
        except Exception:
            pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if not is_allowed(update.effective_chat.id):
        return

    text = update.message.text.strip()
    if not text:
        return

    # Intercept budget amount input
    if context.user_data.get("awaiting_budget"):
        del context.user_data["awaiting_budget"]
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
            set_setting("budget_amount", str(amount))
            set_setting("budget_enabled", "1")
            await update.message.reply_text(f"Daily budget set to ${amount:.0f}.")
        except ValueError:
            await update.message.reply_text("Invalid amount. Use /budget to try again.")
        return

    await _invoke_and_reply(update, context, text)


# ── 命令处理（InlineKeyboard 交互式） ──

async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/p — 项目选择面板"""
    if not is_allowed(update.effective_chat.id):
        return
    chat_id_str = str(update.effective_chat.id)
    args = context.args or []

    if args and args[0].lower() == "add" and len(args) >= 3:
        name, path = args[1], args[2]
        resolved = Path(path).expanduser()
        if not resolved.exists():
            await update.message.reply_text(f"Path not found: {path}")
            return
        db.execute(
            "INSERT OR REPLACE INTO projects (name, path, description) VALUES (?, ?, ?)",
            (name, str(resolved), " ".join(args[3:]) if len(args) > 3 else ""),
        )
        db.commit()
        await update.message.reply_text(f"Added: {name}")
        return

    if args and args[0].lower() == "rm" and len(args) >= 2:
        name = args[1]
        db.execute("DELETE FROM projects WHERE name=?", (name,))
        db.execute("DELETE FROM sessions WHERE project=?", (name,))
        db.execute("DELETE FROM active_project WHERE project=?", (name,))
        db.commit()
        await update.message.reply_text(f"Removed: {name}")
        return

    active = get_active_project(chat_id_str)
    projects = list_projects()
    items = []
    for p in projects:
        session = get_session(chat_id_str, p["name"])
        marker = ">> " if (active and active["project"] == p["name"]) else ""
        info = f" ({session['turns']}t)" if session else ""
        items.append((f"{marker}{p['name']}{info}", f"project:{p['name']}"))

    kb = make_keyboard(items, columns=2)
    text = "Select a project:"
    if active:
        daily = get_daily_cost(chat_id_str)
        session = get_session(chat_id_str, active["project"])
        text = status_text(active, session, daily)
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/model — 模型选择面板"""
    if not is_allowed(update.effective_chat.id):
        return
    active = get_active_project(str(update.effective_chat.id))
    items = []
    for key, name in MODELS.items():
        marker = ">> " if (active and active["model"] == key) else ""
        items.append((f"{marker}{name}", f"model:{key}"))
    kb = make_keyboard(items, columns=2)
    await update.message.reply_text("Select model:", reply_markup=kb)


async def cmd_effort_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/effort — 思考深度选择面板"""
    if not is_allowed(update.effective_chat.id):
        return
    active = get_active_project(str(update.effective_chat.id))
    labels = {"low": "Low (fast)", "medium": "Medium", "high": "High (deep)"}
    items = []
    for key, name in labels.items():
        marker = ">> " if (active and active["effort"] == key) else ""
        items.append((f"{marker}{name}", f"effort:{key}"))
    kb = make_keyboard(items, columns=3)
    await update.message.reply_text("Select effort level:", reply_markup=kb)


async def cmd_tools_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tools — 工具权限选择面板"""
    if not is_allowed(update.effective_chat.id):
        return
    active = get_active_project(str(update.effective_chat.id))
    labels = {"readonly": "Read-only", "standard": "Standard (R/W)", "restricted": "Restricted"}
    items = []
    for key, name in labels.items():
        marker = ">> " if (active and active["tool_profile"] == key) else ""
        items.append((f"{marker}{name}", f"tools:{key}"))
    kb = make_keyboard(items, columns=3)
    await update.message.reply_text("Select tool access:", reply_markup=kb)


async def cmd_think(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/think — 一键切换 opus + high effort"""
    if not is_allowed(update.effective_chat.id):
        return
    chat_id_str = str(update.effective_chat.id)
    active = get_active_project(chat_id_str)
    if not active:
        await update.message.reply_text("No active project.")
        return
    set_active_project(chat_id_str, active["project"], model="opus", effort="high")
    await update.message.reply_text("Thinking mode: Opus + high effort")


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/new — 开新会话"""
    if not is_allowed(update.effective_chat.id):
        return
    chat_id_str = str(update.effective_chat.id)
    active = get_active_project(chat_id_str)
    if not active:
        await update.message.reply_text("No active project.")
        return
    reset_session(chat_id_str, active["project"])
    await update.message.reply_text(f"New session: {active['project']}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status — 当前状态 + 快捷操作按钮"""
    if not is_allowed(update.effective_chat.id):
        return
    chat_id_str = str(update.effective_chat.id)
    active = get_active_project(chat_id_str)
    if not active:
        await update.message.reply_text("No active project. Use /p")
        return

    session = get_session(chat_id_str, active["project"])
    daily = get_daily_cost(chat_id_str)
    text = status_text(active, session, daily)

    kb = make_keyboard([
        ("Switch Project", "menu:project"),
        ("Switch Model", "menu:model"),
        ("Effort", "menu:effort"),
        ("Tools", "menu:tools"),
        ("New Session", "cmd:new"),
        ("Cost", "cmd:cost"),
    ], columns=2)
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cost — 成本汇总"""
    if not is_allowed(update.effective_chat.id):
        return
    chat_id_str = str(update.effective_chat.id)
    today = db.execute(
        "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(turns),0) FROM cost_log "
        "WHERE chat_id=? AND date(created_at)=date('now')", (chat_id_str,)
    ).fetchone()
    week = db.execute(
        "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(turns),0) FROM cost_log "
        "WHERE chat_id=? AND created_at >= datetime('now', '-7 days')", (chat_id_str,)
    ).fetchone()
    total = db.execute(
        "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(turns),0) FROM cost_log "
        "WHERE chat_id=?", (chat_id_str,)
    ).fetchone()
    by_project = db.execute(
        "SELECT project, SUM(cost_usd), SUM(turns) FROM cost_log "
        "WHERE chat_id=? GROUP BY project ORDER BY SUM(cost_usd) DESC", (chat_id_str,)
    ).fetchall()

    lines = [
        f"Today:  ${today[0]:.4f} ({today[1]} turns)",
        f"7 days: ${week[0]:.4f} ({week[1]} turns)",
        f"Total:  ${total[0]:.4f} ({total[1]} turns)",
    ]
    if by_project:
        lines.append("\nBy project:")
        for p, c, t in by_project:
            lines.append(f"  {p}: ${c:.4f} ({int(t)}t)")
    await update.message.reply_text("\n".join(lines))


def _voice_panel_text(vs: dict) -> str:
    """Build voice settings display text."""
    status = "ON" if vs["enabled"] else "OFF"
    engine = "edge-tts" if vs["engine"] == "edge" else "ElevenLabs"
    return f"Voice Reply: {status}\nEngine: {engine}\nVoice: {vs['voice']}"


def _voice_panel_kb(vs: dict):
    """Build voice settings InlineKeyboard."""
    items = []
    if vs["enabled"]:
        items.append(("Turn Off", "voice:off"))
    else:
        items.append(("Turn On", "voice:on"))
    engine_label = "Switch → ElevenLabs" if vs["engine"] == "edge" else "Switch → edge-tts"
    items.append((engine_label, "voice:toggle_engine"))
    items.append(("Change Voice", "voice:pick"))
    return make_keyboard(items, columns=2)


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/voice — 语音回复设置"""
    if not is_allowed(update.effective_chat.id):
        return
    vs = _get_voice_settings()
    await update.message.reply_text(_voice_panel_text(vs), reply_markup=_voice_panel_kb(vs))


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/budget — 每日预算管理"""
    if not is_allowed(update.effective_chat.id):
        return
    chat_id_str = str(update.effective_chat.id)
    enabled, amount = get_budget()
    daily_cost = get_daily_cost(chat_id_str)
    status = "ON" if enabled else "OFF"
    text = f"Daily Budget: {status}\nLimit: ${amount:.0f}\nUsed today: ${daily_cost:.2f}"
    items = []
    if enabled:
        items.append(("Turn Off", "budget:off"))
    else:
        items.append(("Turn On", "budget:on"))
    items.append(("Set Amount", "budget:set"))
    kb = make_keyboard(items, columns=2)
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_chat.id):
        return
    kb = make_keyboard([
        ("Projects", "menu:project"),
        ("Models", "menu:model"),
        ("Effort", "menu:effort"),
        ("Tools", "menu:tools"),
        ("Status", "cmd:status"),
        ("Cost", "cmd:cost"),
    ], columns=2)
    await update.message.reply_text(
        "Claude Bridge\n\nSend any message to chat with Claude.\nUse buttons or commands:",
        reply_markup=kb,
    )


# ── Callback Query 处理（按钮点击） ──

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    chat_id = query.from_user.id
    if not is_allowed(chat_id):
        await query.answer("Unauthorized")
        return

    await query.answer()
    chat_id_str = str(chat_id)
    data = query.data
    active = get_active_project(chat_id_str)

    def _status_panel(active_now=None):
        a = active_now or get_active_project(chat_id_str)
        if not a:
            return "No active project.", None
        s = get_session(chat_id_str, a["project"])
        d = get_daily_cost(chat_id_str)
        text = status_text(a, s, d)
        kb = make_keyboard([
            ("Switch Project", "menu:project"),
            ("Switch Model", "menu:model"),
            ("Effort", "menu:effort"),
            ("Tools", "menu:tools"),
            ("New Session", "cmd:new"),
            ("Cost", "cmd:cost"),
        ], columns=2)
        return text, kb

    # ── project:<name> ──
    if data.startswith("project:"):
        name = data.split(":", 1)[1]
        row = db.execute("SELECT name FROM projects WHERE name=?", (name,)).fetchone()
        if not row:
            await query.edit_message_text(f"Unknown project: {name}")
            return
        set_active_project(chat_id_str, name)
        text, kb = _status_panel()
        await query.edit_message_text(text, reply_markup=kb)

    # ── model:<name> ──
    elif data.startswith("model:"):
        model = data.split(":", 1)[1]
        if active:
            set_active_project(chat_id_str, active["project"], model=model)
        text, kb = _status_panel()
        await query.edit_message_text(text, reply_markup=kb)

    # ── effort:<level> ──
    elif data.startswith("effort:"):
        level = data.split(":", 1)[1]
        if level in VALID_EFFORTS and active:
            set_active_project(chat_id_str, active["project"], effort=level)
        text, kb = _status_panel()
        await query.edit_message_text(text, reply_markup=kb)

    # ── tools:<profile> ──
    elif data.startswith("tools:"):
        profile = data.split(":", 1)[1]
        if profile in TOOL_PROFILES and active:
            set_active_project(chat_id_str, active["project"], tool_profile=profile)
        text, kb = _status_panel()
        await query.edit_message_text(text, reply_markup=kb)

    # ── menu:<target> — 子菜单（都带返回按钮） ──
    elif data.startswith("menu:"):
        target = data.split(":", 1)[1]

        if target == "status":
            text, kb = _status_panel()
            if kb:
                await query.edit_message_text(text, reply_markup=kb)
            else:
                await query.edit_message_text(text)

        elif target == "project":
            projects = list_projects()
            items = []
            for p in projects:
                session = get_session(chat_id_str, p["name"])
                marker = ">> " if (active and active["project"] == p["name"]) else ""
                info = f" ({session['turns']}t)" if session else ""
                items.append((f"{marker}{p['name']}{info}", f"project:{p['name']}"))
            kb = make_keyboard(items, columns=2, back_to="menu:status")
            await query.edit_message_text("Select project:", reply_markup=kb)

        elif target == "model":
            items = []
            for key, name in MODELS.items():
                marker = ">> " if (active and active["model"] == key) else ""
                items.append((f"{marker}{name}", f"model:{key}"))
            kb = make_keyboard(items, columns=2, back_to="menu:status")
            await query.edit_message_text("Select model:", reply_markup=kb)

        elif target == "effort":
            labels = {"low": "Low (fast)", "medium": "Medium", "high": "High (deep)"}
            items = []
            for key, name in labels.items():
                marker = ">> " if (active and active["effort"] == key) else ""
                items.append((f"{marker}{name}", f"effort:{key}"))
            kb = make_keyboard(items, columns=3, back_to="menu:status")
            await query.edit_message_text("Select effort:", reply_markup=kb)

        elif target == "tools":
            labels = {"readonly": "Read-only", "standard": "Standard (R/W)", "restricted": "Restricted"}
            items = []
            for key, name in labels.items():
                marker = ">> " if (active and active["tool_profile"] == key) else ""
                items.append((f"{marker}{name}", f"tools:{key}"))
            kb = make_keyboard(items, columns=3, back_to="menu:status")
            await query.edit_message_text("Select tool access:", reply_markup=kb)

    # ── voice:<action> ──
    elif data.startswith("voice:"):
        action = data.split(":", 1)[1]
        vs = _get_voice_settings()

        if action == "off":
            set_setting("voice_enabled", "0")
            vs["enabled"] = False
            await query.edit_message_text(_voice_panel_text(vs), reply_markup=_voice_panel_kb(vs))

        elif action == "on":
            set_setting("voice_enabled", "1")
            vs["enabled"] = True
            await query.edit_message_text(_voice_panel_text(vs), reply_markup=_voice_panel_kb(vs))

        elif action == "toggle_engine":
            new_engine = "eleven" if vs["engine"] == "edge" else "edge"
            set_setting("voice_engine", new_engine)
            # Reset voice to first available for new engine
            if new_engine == "edge":
                default_voice = list(EDGE_TTS_VOICES.keys())[0]
            else:
                default_voice = list(ELEVENLABS_VOICES.keys())[0]
            set_setting("voice_name", default_voice)
            vs["engine"] = new_engine
            vs["voice"] = default_voice
            await query.edit_message_text(_voice_panel_text(vs), reply_markup=_voice_panel_kb(vs))

        elif action == "pick":
            voices = EDGE_TTS_VOICES if vs["engine"] == "edge" else ELEVENLABS_VOICES
            rows = []
            for name in voices:
                marker = ">> " if name == vs["voice"] else ""
                rows.append([
                    InlineKeyboardButton(f"{marker}{name}", callback_data=f"voice:set:{name}"),
                    InlineKeyboardButton("Preview", callback_data=f"voice:preview:{name}"),
                ])
            rows.append([InlineKeyboardButton("<< Back", callback_data="voice:back")])
            kb = InlineKeyboardMarkup(rows)
            engine_label = "edge-tts" if vs["engine"] == "edge" else "ElevenLabs"
            await query.edit_message_text(f"Select voice ({engine_label}):", reply_markup=kb)

        elif action.startswith("preview:"):
            voice_name = action.split(":", 1)[1]
            preview_text = "你好，我是你的智能助手，有什么可以帮你的吗？"
            mp3_path = VOICE_DIR / f"preview_{int(time.time())}.mp3"
            ogg_path = mp3_path.with_suffix(".ogg")
            try:
                if vs["engine"] == "eleven":
                    ok = await _tts_elevenlabs(preview_text, mp3_path, ogg_path, voice_name)
                else:
                    ok = await _tts_edge(preview_text, mp3_path, ogg_path, voice_name)
                if ok:
                    engine_label = "edge-tts" if vs["engine"] == "edge" else "ElevenLabs"
                    with open(ogg_path, "rb") as f:
                        await query.message.chat.send_voice(
                            voice=f, caption=f"Preview: {voice_name} ({engine_label})"
                        )
                else:
                    await query.message.chat.send_message("Preview failed.")
            except Exception as e:
                log.error(f"Voice preview failed: {e}")
                await query.message.chat.send_message(f"Preview error: {e}")
            finally:
                try:
                    mp3_path.unlink(missing_ok=True)
                    ogg_path.unlink(missing_ok=True)
                except Exception:
                    pass

        elif action.startswith("set:"):
            voice_name = action.split(":", 1)[1]
            set_setting("voice_name", voice_name)
            vs["voice"] = voice_name
            await query.edit_message_text(_voice_panel_text(vs), reply_markup=_voice_panel_kb(vs))

        elif action == "back":
            await query.edit_message_text(_voice_panel_text(vs), reply_markup=_voice_panel_kb(vs))

    # ── cmd:<action> ──
    elif data.startswith("cmd:"):
        action = data.split(":", 1)[1]
        if action == "new" and active:
            reset_session(chat_id_str, active["project"])
            text, kb = _status_panel()
            await query.edit_message_text(f"New session started.\n\n{text}", reply_markup=kb)
        elif action == "cost":
            today = db.execute(
                "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(turns),0) FROM cost_log "
                "WHERE chat_id=? AND date(created_at)=date('now')", (chat_id_str,)
            ).fetchone()
            kb = make_keyboard([], back_to="menu:status")
            await query.edit_message_text(
                f"Today: ${today[0]:.4f} ({today[1]} turns)", reply_markup=kb)
        elif action == "dismiss":
            await query.edit_message_text("Continuing session.")

    # ── task_exec:<session_id> / task_cancel ──
    elif data.startswith("task_exec:"):
        target_session = data.split(":", 1)[1]
        active = get_active_project(chat_id_str)
        if not active:
            await query.edit_message_text("No active project.")
            return
        await query.edit_message_text("Executing...")
        lock = get_user_lock(chat_id_str)
        async with lock:
            async with worker_semaphore:
                stop_typing = asyncio.Event()
                typing_task = asyncio.create_task(
                    send_typing_loop(context, chat_id, stop_typing))
                try:
                    result = await invoke_claude(
                        message="User confirmed. Execute the operations described above.",
                        project_path=active["path"],
                        session_id=target_session,
                        model=active["model"],
                        tool_profile="standard",
                        effort=active["effort"],
                        bypass_permissions=True,
                    )
                finally:
                    stop_typing.set()
                    await typing_task
        if result.get("error") and not result.get("result"):
            await context.bot.send_message(
                chat_id=chat_id, text=f"Error: {result['error'][:500]}")
            return
        new_sid = result.get("session_id", target_session)
        cost = result.get("total_cost_usd", 0.0)
        turns = result.get("num_turns", 1)
        duration = result.get("duration_ms", 0)
        upsert_session(chat_id_str, active["project"], new_sid, active["model"], turns, cost)
        log_cost(chat_id_str, active["project"], cost, turns, duration)
        reply = result.get("result", "") or "(no output)"
        cost_tag = f"\n\n`standard | {active['model']} | ${cost:.4f} | {duration/1000:.1f}s`"
        await send_long_message(context.bot, chat_id, reply + cost_tag)

    elif data == "task_cancel":
        await query.edit_message_text("Task cancelled.")

    # ── budget:<action> ──
    elif data.startswith("budget:"):
        action = data.split(":", 1)[1]
        if action == "off":
            set_setting("budget_enabled", "0")
            await query.edit_message_text("Budget checking disabled.")
        elif action == "on":
            set_setting("budget_enabled", "1")
            _, amount = get_budget()
            await query.edit_message_text(f"Budget checking enabled (${amount:.0f}/day).")
        elif action == "set":
            context.user_data["awaiting_budget"] = True
            await query.edit_message_text("Enter new daily budget amount (e.g. 50, 100):")


# ── /task 拦截式编排 ──

async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/task — readonly 分析 → 确认 → standard 执行"""
    if not is_allowed(update.effective_chat.id):
        return
    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)

    task_text = " ".join(context.args) if context.args else ""
    if update.message.reply_to_message and update.message.reply_to_message.text:
        task_text = update.message.reply_to_message.text + ("\n\n" + task_text if task_text else "")
    if not task_text.strip():
        await update.message.reply_text("Usage: /task <description> or reply to a message with /task")
        return

    active = get_active_project(chat_id_str)
    if not active:
        await update.message.reply_text("No active project. Use /p")
        return
    if not Path(active["path"]).exists():
        await update.message.reply_text(f"Path not found: {active['path']}")
        return

    daily_cost = get_daily_cost(chat_id_str)
    budget_enabled, budget_amount = get_budget()
    if budget_enabled and daily_cost >= budget_amount:
        await update.message.reply_text(
            f"Daily budget reached (${daily_cost:.2f} / ${budget_amount:.0f}). "
            f"Use /budget to adjust.")
        return

    session = get_session(chat_id_str, active["project"])
    session_id = session["session_id"] if session else None

    # Phase 1: readonly analysis
    lock = get_user_lock(chat_id_str)
    async with lock:
        async with worker_semaphore:
            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(send_typing_loop(context, chat_id, stop_typing))
            try:
                result = await invoke_claude(
                    message=task_text,
                    project_path=active["path"],
                    session_id=session_id,
                    model=active["model"],
                    tool_profile="readonly",
                    effort=active["effort"],
                )
            finally:
                stop_typing.set()
                await typing_task

    if result.get("error") and not result.get("result"):
        await update.message.reply_text(f"Error: {result['error'][:500]}")
        return

    new_session_id = result.get("session_id", session_id)
    cost = result.get("total_cost_usd", 0.0)
    turns = result.get("num_turns", 1)
    duration = result.get("duration_ms", 0)

    upsert_session(chat_id_str, active["project"], new_session_id, active["model"], turns, cost)
    log_cost(chat_id_str, active["project"], cost, turns, duration)

    analysis = result.get("result", "") or "(empty analysis)"
    cost_tag = f"\n\n`readonly | {active['model']} | ${cost:.4f} | {duration/1000:.1f}s`"

    # Phase 2: send analysis + confirm/cancel buttons
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Execute", callback_data=f"task_exec:{new_session_id}"),
         InlineKeyboardButton("Cancel", callback_data="task_cancel")],
    ])
    await send_long_message(context.bot, chat_id, analysis + cost_tag)
    await context.bot.send_message(
        chat_id=chat_id, text="Execute the suggested operations?", reply_markup=kb)


# ── Agent Loop ──

def _extract_json(text: str) -> dict | None:
    """Extract first JSON object from Claude's text response."""
    m = re.search(r'```(?:json)?\s*(\{.+?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


async def _agent_invoke(message: str, project_path: str, session_id: str | None,
                        model: str, tool_profile: str, effort: str = "high",
                        max_turns: int = 50, timeout: int = 600) -> dict:
    """Claude invocation with agent-specific limits. Uses stdin + JSON array parsing."""
    claude_bin = get_claude_bin()
    cmd = [
        str(claude_bin), "-p",
        "--output-format", "json",
        "--max-turns", str(max_turns),
        "--model", model,
        "--effort", effort,
    ]
    # Agent execute phases with standard profile get bypassPermissions
    if tool_profile == "standard":
        cmd.extend(["--permission-mode", "bypassPermissions"])
    if session_id:
        cmd.extend(["--resume", session_id])

    log.info(f"agent_invoke: model={model} tp={tool_profile} max_turns={max_turns} "
             f"resume={bool(session_id)}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_path,
            env=CLAUDE_ENV,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=message.encode("utf-8")), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"error": f"Timeout ({timeout}s)", "result": None}

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            return {"error": f"Exit {proc.returncode}: {err[:300]}", "result": None}

        raw = stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            return {"error": "Empty output", "result": None}

        parsed = json.loads(raw)
        if isinstance(parsed, list):
            for item in reversed(parsed):
                if isinstance(item, dict) and item.get("type") == "result":
                    return item
            return {"error": "No result event in output", "result": None}
        return parsed

    except json.JSONDecodeError as e:
        return {"error": f"JSON parse: {e}", "result": raw[:500] if raw else None}
    except Exception as e:
        log.error(f"agent_invoke error: {e}")
        return {"error": str(e), "result": None}


async def run_agent_loop(chat_id_str: str, project_path: str, model: str,
                         objective: str, context: ContextTypes.DEFAULT_TYPE):
    """Core agent: Plan → Execute phases → Verify."""
    chat_id = int(chat_id_str)
    cancel = agent_running.get(chat_id_str, {}).get("cancel")
    total_cost = 0.0
    total_turns = 0
    phase_results = []

    try:
        # ── PLAN ──
        await context.bot.send_message(
            chat_id=chat_id, text="🎯 *Agent 启动*\n目标: " + objective + "\n\n规划中...",
            parse_mode=ParseMode.MARKDOWN)

        plan_prompt = (
            "你是任务规划专家。分析目标，输出 JSON 执行计划。\n\n"
            f"目标：{objective}\n\n"
            "可用工具：文件读写(Read/Write/Edit)、搜索(Grep/Glob)、"
            "终端(Bash)、网络搜索(WebSearch)、网页抓取(WebFetch)\n\n"
            "严格输出以下 JSON（无其他文本）：\n"
            "```json\n"
            '{"phases": [{"id": 1, "title": "标题", '
            '"objective": "具体目标", '
            '"tool_profile": "readonly或standard", '
            '"estimated_turns": 10}], '
            '"summary": "一句话计划摘要"}\n'
            "```\n\n"
            "规则：最多5个phase | 只有修改文件才用standard | estimated_turns合计≤50"
        )

        plan_result = await _agent_invoke(
            plan_prompt, project_path, None, model, "readonly",
            effort="high", max_turns=AGENT_PLAN_MAX_TURNS, timeout=120)

        if plan_result.get("error"):
            await context.bot.send_message(
                chat_id=chat_id, text=f"❌ 规划失败: {plan_result['error'][:300]}")
            return

        total_cost += plan_result.get("total_cost_usd", 0)
        total_turns += plan_result.get("num_turns", 0)

        plan_text = plan_result.get("result", "")
        plan = _extract_json(plan_text)

        if not plan or "phases" not in plan:
            await context.bot.send_message(
                chat_id=chat_id, text=f"❌ 无法解析计划\n\n{str(plan_text)[:500]}")
            return

        phases = plan["phases"][:AGENT_MAX_PHASES]
        summary = plan.get("summary", "")

        plan_lines = ["📋 *计划* (" + str(len(phases)) + " 阶段): " + summary + "\n"]
        for p in phases:
            tp_tag = "📝" if p.get("tool_profile") == "standard" else "👁"
            plan_lines.append(f"  {p['id']}. {tp_tag} {p['title']}")
        try:
            await context.bot.send_message(
                chat_id=chat_id, text="\n".join(plan_lines),
                parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id, text="\n".join(plan_lines))

        # ── EXECUTE ──
        session_id = None

        for i, phase in enumerate(phases):
            if cancel and cancel.is_set():
                await context.bot.send_message(chat_id=chat_id, text="⏹ Agent 已停止")
                return

            if total_cost >= AGENT_MAX_COST_USD:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ 成本上限 ${AGENT_MAX_COST_USD}，已停止 ({i}/{len(phases)})")
                break

            phase_num = i + 1
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚡ Phase {phase_num}/{len(phases)}: {phase['title']}")

            exec_prompt = (
                f"执行多步任务的第 {phase_num}/{len(phases)} 阶段。\n\n"
                f"整体目标：{objective}\n"
                f"当前阶段：{phase['title']}\n"
                f"阶段目标：{phase['objective']}\n\n"
                "立即开始执行。完成后简要说明完成了什么、产出物路径（如有）。"
            )

            tp = phase.get("tool_profile", "readonly")
            if tp not in TOOL_PROFILES:
                tp = "readonly"
            est = phase.get("estimated_turns", 20)

            exec_result = await _agent_invoke(
                exec_prompt, project_path, session_id, model, tp,
                effort="high",
                max_turns=min(est * 2, AGENT_PHASE_MAX_TURNS),
                timeout=AGENT_PHASE_TIMEOUT)

            if exec_result.get("error"):
                phase_results.append({
                    "phase": phase_num, "title": phase["title"],
                    "status": "failed", "error": exec_result["error"][:200]})
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Phase {phase_num} 失败: {exec_result['error'][:200]}")
                continue

            session_id = exec_result.get("session_id", session_id)
            cost = exec_result.get("total_cost_usd", 0)
            turns = exec_result.get("num_turns", 0)
            total_cost += cost
            total_turns += turns

            result_text = exec_result.get("result", "")
            phase_results.append({
                "phase": phase_num, "title": phase["title"],
                "status": "done", "summary": result_text[:300]})

            short = result_text[:500] + "..." if len(result_text) > 500 else result_text
            await send_long_message(
                context.bot, chat_id,
                f"✅ Phase {phase_num} 完成 (${cost:.4f}, {turns}t)\n\n{short}")

        # ── VERIFY (独立 session，不受执行上下文污染) ──
        if cancel and cancel.is_set():
            return

        if phase_results:
            await context.bot.send_message(chat_id=chat_id, text="🔍 独立验证中...")

            results_summary = "\n".join([
                f"Phase {r['phase']} ({r['title']}): {r['status']}"
                + (f" - {r.get('summary', '')[:100]}" if r['status'] == 'done'
                   else f" - {r.get('error', '')}")
                for r in phase_results
            ])

            verify_prompt = (
                "你是独立审查员。评估任务执行结果。\n\n"
                f"原始目标：{objective}\n"
                f"计划摘要：{summary}\n"
                f"各阶段结果：\n{results_summary}\n\n"
                "评估：1. 目标达成度(pass/partial/fail) "
                "2. 质量(1-5) 3. 遗漏 4. 最终摘要（一段话）"
            )

            verify_result = await _agent_invoke(
                verify_prompt, project_path, None, model, "readonly",
                effort="medium", max_turns=AGENT_VERIFY_MAX_TURNS, timeout=120)

            total_cost += verify_result.get("total_cost_usd", 0)
            total_turns += verify_result.get("num_turns", 0)
            verify_text = verify_result.get("result", "(验证无输出)")
        else:
            verify_text = "(无阶段完成，跳过验证)"

        # ── FINAL REPORT ──
        done_count = len([r for r in phase_results if r['status'] == 'done'])
        final = (
            "🏁 *Agent 完成*\n\n"
            f"目标: {objective}\n"
            f"阶段: {done_count}/{len(phases)} 成功\n"
            f"总计: ${total_cost:.4f}, {total_turns} turns\n\n"
            f"验证:\n{verify_text}")

        await send_long_message(context.bot, chat_id, final)
        log_cost(chat_id_str, "agent", total_cost, total_turns, 0)

    except asyncio.CancelledError:
        await context.bot.send_message(chat_id=chat_id, text="⏹ Agent 已取消")
    except Exception as e:
        log.error(f"Agent loop error: {e}", exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=f"❌ Agent 错误: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        agent_running.pop(chat_id_str, None)


async def cmd_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/agent <objective> | stop | status"""
    if not is_allowed(update.effective_chat.id):
        return
    chat_id_str = str(update.effective_chat.id)
    args = context.args or []

    if args and args[0].lower() == "stop":
        running = agent_running.get(chat_id_str)
        if running:
            running["cancel"].set()
            await update.message.reply_text("⏹ 正在停止 Agent...")
        else:
            await update.message.reply_text("没有运行中的 Agent")
        return

    if args and args[0].lower() == "status":
        running = agent_running.get(chat_id_str)
        if running:
            elapsed = int(time.time() - running["started"])
            await update.message.reply_text(
                f"🔄 Agent 运行中 ({elapsed}s)\n目标: {running['objective']}")
        else:
            await update.message.reply_text("没有运行中的 Agent")
        return

    if chat_id_str in agent_running:
        await update.message.reply_text("⚠️ Agent 已在运行，/agent stop 先停止")
        return

    objective = " ".join(args).strip()
    if not objective:
        await update.message.reply_text(
            "用法: /agent <目标>\n\n"
            "示例:\n"
            "/agent 调研 X 上 Claude Code 最新讨论并写报告\n"
            "/agent 扫描所有 LaunchAgent 生成健康报告\n\n"
            "/agent status — 查看进度\n"
            "/agent stop — 停止")
        return

    active = get_active_project(chat_id_str)
    if not active:
        await update.message.reply_text("先用 /p 选择项目")
        return

    daily_cost = get_daily_cost(chat_id_str)
    budget_enabled, budget_amount = get_budget()
    if budget_enabled and daily_cost >= budget_amount:
        await update.message.reply_text(f"每日预算已满 ${daily_cost:.2f}")
        return

    cancel_event = asyncio.Event()
    agent_running[chat_id_str] = {
        "cancel": cancel_event,
        "objective": objective,
        "started": time.time(),
    }

    asyncio.create_task(
        run_agent_loop(chat_id_str, active["path"], "opus", objective, context))


# ── /restart ──

async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/restart — restart CB service via launchd (KeepAlive auto-respawn)"""
    if not is_allowed(update.effective_chat.id):
        return
    await update.message.reply_text("Restarting CB service...")
    log.info("Restart requested via /restart command")
    # Delay exit to let the reply reach Telegram
    await asyncio.sleep(1)
    os._exit(0)


# ── /cron 定时任务 ──

async def cmd_cron(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cron — 管理定时任务"""
    if not is_allowed(update.effective_chat.id):
        return
    chat_id_str = str(update.effective_chat.id)
    args = context.args or []

    if not args or args[0] == "list":
        rows = db.execute(
            "SELECT id, project, prompt, interval_sec, enabled FROM cron_jobs WHERE chat_id=?",
            (chat_id_str,)
        ).fetchall()
        if not rows:
            await update.message.reply_text(
                "No cron jobs.\n\n"
                "Usage:\n"
                "`/cron add <interval> <prompt>`\n"
                "`/cron rm <id>`\n"
                "`/cron pause <id>` / `/cron resume <id>`\n\n"
                "Intervals: 5m, 30m, 1h, 6h, 1d",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        lines = []
        for r in rows:
            status = "ON" if r[4] else "OFF"
            lines.append(f"#{r[0]} [{status}] {r[1]} | every {_format_interval(r[3])}\n  `{r[2][:60]}`")
        await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    elif args[0] == "add" and len(args) >= 3:
        interval = _parse_interval(args[1])
        if not interval:
            await update.message.reply_text("Invalid interval (min 5m). Examples: 5m, 1h, 6h, 1d")
            return
        prompt = " ".join(args[2:])
        active = get_active_project(chat_id_str)
        if not active:
            await update.message.reply_text("No active project. Use /p first.")
            return
        db.execute(
            "INSERT INTO cron_jobs (chat_id, project, prompt, interval_sec, model, effort) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id_str, active["project"], prompt, interval, active["model"], active["effort"]),
        )
        db.commit()
        job_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        await update.message.reply_text(
            f"Cron #{job_id} added: every {_format_interval(interval)} on `{active['project']}`\n`{prompt}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif args[0] == "rm" and len(args) >= 2:
        try:
            job_id = int(args[1])
        except ValueError:
            await update.message.reply_text("Usage: /cron rm <id>")
            return
        deleted = db.execute(
            "DELETE FROM cron_jobs WHERE id=? AND chat_id=?", (job_id, chat_id_str)
        ).rowcount
        db.commit()
        await update.message.reply_text(f"Cron #{job_id} {'deleted' if deleted else 'not found'}.")

    elif args[0] == "pause" and len(args) >= 2:
        try:
            job_id = int(args[1])
        except ValueError:
            await update.message.reply_text("Usage: /cron pause <id>")
            return
        db.execute("UPDATE cron_jobs SET enabled=0 WHERE id=? AND chat_id=?", (job_id, chat_id_str))
        db.commit()
        await update.message.reply_text(f"Cron #{job_id} paused.")

    elif args[0] == "resume" and len(args) >= 2:
        try:
            job_id = int(args[1])
        except ValueError:
            await update.message.reply_text("Usage: /cron resume <id>")
            return
        db.execute("UPDATE cron_jobs SET enabled=1 WHERE id=? AND chat_id=?", (job_id, chat_id_str))
        db.commit()
        await update.message.reply_text(f"Cron #{job_id} resumed.")

    else:
        await update.message.reply_text(
            "Usage:\n"
            "`/cron add <interval> <prompt>`\n"
            "`/cron list`\n"
            "`/cron rm <id>`\n"
            "`/cron pause <id>` / `/cron resume <id>`",
            parse_mode=ParseMode.MARKDOWN,
        )


async def _cron_scheduler(bot):
    """Background task: check and run due cron jobs every 60s."""
    await asyncio.sleep(10)  # initial delay after startup
    while True:
        try:
            rows = db.execute(
                "SELECT id, chat_id, project, prompt, interval_sec, model, effort, last_run "
                "FROM cron_jobs WHERE enabled=1"
            ).fetchall()

            for job_id, chat_id, project, prompt, interval_sec, model, effort, last_run in rows:
                # Check if due
                if last_run:
                    row = db.execute(
                        "SELECT datetime(?, '+' || ? || ' seconds') <= datetime('now')",
                        (last_run, interval_sec)
                    ).fetchone()
                    if not row or not row[0]:
                        continue

                project_row = db.execute("SELECT path FROM projects WHERE name=?", (project,)).fetchone()
                if not project_row or not Path(project_row[0]).exists():
                    continue

                # Mark as running
                db.execute("UPDATE cron_jobs SET last_run=datetime('now') WHERE id=?", (job_id,))
                db.commit()

                log.info(f"cron #{job_id} running: {prompt[:50]}")
                result = await invoke_claude(
                    message=prompt,
                    project_path=project_row[0],
                    session_id=None,
                    model=model,
                    tool_profile="readonly",
                    effort=effort,
                )

                reply = result.get("result", "") or result.get("error", "No output")
                cost = result.get("total_cost_usd", 0.0)
                duration = result.get("duration_ms", 0)
                header = f"*Cron #{job_id}* (`{_format_interval(interval_sec)}`)\n\n"
                cost_tag = f"\n\n`cron | {model} | {effort} | ${cost:.4f} | {duration/1000:.1f}s`"

                try:
                    await send_long_message(bot, int(chat_id), header + reply + cost_tag)
                except Exception as e:
                    log.error(f"cron #{job_id} send failed: {e}")

                if cost > 0:
                    log_cost(chat_id, project, cost, result.get("num_turns", 1), duration)

        except Exception as e:
            log.error(f"cron scheduler error: {e}", exc_info=True)

        await asyncio.sleep(60)


# ── Error Handler ──

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error(f"Unhandled exception: {context.error}", exc_info=context.error)
    # 把错误摘要发回 Telegram，让用户立刻知道
    if update and hasattr(update, "effective_chat") and update.effective_chat:
        err_name = type(context.error).__name__
        err_msg = str(context.error)[:300]
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ Bot Error: `{err_name}`\n```\n{err_msg}\n```",
                parse_mode="Markdown",
            )
        except Exception:
            pass  # 通知本身失败则放弃，避免死循环


# ── 启动 ──

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("p", "Projects"),
        BotCommand("model", "Select model"),
        BotCommand("effort", "Thinking depth"),
        BotCommand("think", "Opus + deep thinking"),
        BotCommand("tools", "Tool access"),
        BotCommand("new", "New session"),
        BotCommand("status", "Status & settings"),
        BotCommand("cost", "Cost summary"),
        BotCommand("task", "Readonly analyze, then execute"),
        BotCommand("budget", "Daily budget settings"),
        BotCommand("voice", "Voice reply settings"),
        BotCommand("restart", "Restart CB service"),
        BotCommand("agent", "Autonomous agent loop"),
        BotCommand("cron", "Scheduled tasks"),
        BotCommand("help", "Help"),
    ])
    # Start cron scheduler background task
    asyncio.create_task(_cron_scheduler(app.bot))
    log.info("Claude Bridge started")


def main():
    global db, worker_semaphore

    if not CONFIG_PATH.exists():
        print(f"Config not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    token = cfg.get("botToken", "")
    if not token:
        print("No botToken in config.json", file=sys.stderr)
        sys.exit(1)

    proxy = get_proxy()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = init_db()
    worker_semaphore = asyncio.Semaphore(MAX_CONCURRENT_WORKERS)

    app = (
        Application.builder()
        .token(token)
        .request(HTTPXRequest(connection_pool_size=16, pool_timeout=30.0, proxy=proxy))
        .get_updates_request(HTTPXRequest(connection_pool_size=4, pool_timeout=10.0, proxy=proxy))
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("p", cmd_project))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("effort", cmd_effort_menu))
    app.add_handler(CommandHandler("think", cmd_think))
    app.add_handler(CommandHandler("tools", cmd_tools_menu))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("agent", cmd_agent))
    app.add_handler(CommandHandler("cron", cmd_cron))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info(f"Starting with proxy={proxy}, allowed={cfg.get('allowFrom', [])}")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True,
                     bootstrap_retries=-1)


if __name__ == "__main__":
    main()
