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
import sqlite3
import sys
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

DEFAULT_MODEL = "sonnet"
MAX_TURNS = 8
MAX_CONCURRENT_WORKERS = 2
TELEGRAM_MAX_LEN = 4000
SESSION_ROTATE_TURNS = 50
SESSION_ROTATE_COST = 2.0
DAILY_BUDGET_USD = 100.0
CLAUDE_TIMEOUT = 900
DEFAULT_EFFORT = "medium"
VALID_EFFORTS = {"low", "medium", "high"}

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
        return json.loads(raw)

    except json.JSONDecodeError as e:
        log.error(f"JSON parse error: {e}, raw={raw[:200]}")
        return {"error": f"JSON parse error: {e}", "result": raw[:500]}
    except Exception as e:
        log.error(f"invoke error: {e}")
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


async def send_long_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
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
            await context.bot.send_message(
                chat_id=chat_id, text=chunk, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=chunk)


async def _invoke_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            text: str):
    """共享的 Claude 调用 + 回复逻辑，供 handle_message 和 handle_photo 使用"""
    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)

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

    lock = get_user_lock(chat_id_str)
    async with lock:
        async with worker_semaphore:
            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(send_typing_loop(context, chat_id, stop_typing))
            try:
                result = await invoke_claude(
                    message=text,
                    project_path=active["path"],
                    session_id=session_id,
                    model=active["model"],
                    tool_profile=active["tool_profile"],
                    effort=active["effort"],
                )
            finally:
                stop_typing.set()
                await typing_task

    if result.get("error") and not result.get("result"):
        await update.message.reply_text(f"Error: {result['error'][:500]}")
        return

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

    cost_tag = f"\n\n`{active['model']} | {active['effort']} | ${cost:.4f} | {duration/1000:.1f}s`"
    reply_text += cost_tag

    upsert_session(chat_id_str, active["project"], new_session_id, active["model"], turns, cost)
    log_cost(chat_id_str, active["project"], cost, turns, duration)
    await send_long_message(context, chat_id, reply_text)


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
        await send_long_message(context, chat_id, reply + cost_tag)

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
    await send_long_message(context, chat_id, analysis + cost_tag)
    await context.bot.send_message(
        chat_id=chat_id, text="Execute the suggested operations?", reply_markup=kb)


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


# ── Error Handler ──

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error(f"Unhandled exception: {context.error}", exc_info=context.error)


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
        BotCommand("restart", "Restart CB service"),
        BotCommand("help", "Help"),
    ])
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
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info(f"Starting with proxy={proxy}, allowed={cfg.get('allowFrom', [])}")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True,
                     bootstrap_retries=-1)


if __name__ == "__main__":
    main()
