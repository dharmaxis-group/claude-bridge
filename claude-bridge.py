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
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.error import NetworkError
from telegram.request import HTTPXRequest

# ── 路径与常量 ──

CB_HOME = Path(os.environ.get("CB_HOME", str(Path.home() / ".claude-bridge")))
CONFIG_PATH = CB_HOME / "config.json"
DB_PATH = CB_HOME / "data" / "sessions.db"
LOG_PATH = CB_HOME / "logs" / "claude-bridge.log"
IMAGE_DIR = CB_HOME / "data" / "images"
VOICE_DIR = CB_HOME / "data" / "voice"

DEFAULT_MODEL = "opus"
MAX_TURNS = 50  # enough for complex tasks like sync/review
MAX_CONCURRENT_WORKERS = 3   # matches Claude Max real concurrency (~2-3 simultaneous claude -p)
MAX_CONCURRENT_PER_USER = 3
MAX_TOTAL_TASKS = 20
TELEGRAM_MAX_LEN = 4000
SESSION_ROTATE_TURNS = 50
SESSION_ROTATE_COST = 2.0
DAILY_BUDGET_USD = 100.0
CLAUDE_TIMEOUT = 3600  # 1 hour max — prevents runaway sessions; use /cancel for longer tasks
PROGRESS_EDIT_INTERVAL = 3.0  # min seconds between Telegram progress message edits
TZ_OFFSET = "+8 hours"  # UTC+8 for cost_log date queries
DEFAULT_EFFORT = "high"
VALID_EFFORTS = {"low", "medium", "high"}
WAVE_FRAMES = ["◉ ◌ ◌", "◌ ◉ ◌", "◌ ◌ ◉", "◌ ◉ ◌"]
WAVE_INTERVAL = 6.0  # seconds between wave animation frames (reduced from 3s to halve API pressure)

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
    "[业务整理 skill 提示]\n"
    "主人发自然语言含'车牌号/装车/发货/运费/供应商/客户/汇款/付款/收款人'等关键词时，"
    "立即调用 business-info-organizer skill 按标准模板整理（采购17字段 / 销售17字段 / 汇款5字段）。"
    "关键约束：① 逐项追问一个字段 ② 单位/口径含糊必反问 ③ 落到 ~/Private/cases/business/ 单车 md。"
    "本规则覆盖上面的'回复不超过5行'——业务整理的标准模板输出需要完整字段。\n"
    "[业务规则结束]\n\n"
)

# ── P1: 敏感消息关键词 ──
_SENSITIVE_MSG_KEYWORDS = [
    "密码是", "密码为", "密码:", "密码：", "password is", "password:",
    "token是", "token:", "secret:", "凭据是", "pin码",
    "帮我存密码", "加密保管", "保管密码", "存储密码",
]

# ── P0: 业务整理 skill 强制触发关键词（命中即强制调用 Skill 工具）──
_BUSINESS_INFO_KEYWORDS_RE = re.compile(
    r'(车号|车牌|装车|发货|运费每吨|运费每斤|供应商|客户|汇款|付款|收款人|银行帐号|挂车|货主)',
    re.UNICODE
)

BUSINESS_INFO_FORCE_PROMPT = (
    "\n\n[🚨 业务整理强制规则 — 优先级最高 / 覆盖所有简洁规则 / 必须严格执行]\n"
    "检测到业务关键词。**必须立即调用 Skill 工具加载 business-info-organizer**：\n"
    "1. 调用方式：使用 Skill 工具，参数 skill='business-info-organizer'\n"
    "2. 加载后严格按 SKILL.md + templates/ + rules/ 流程：\n"
    "   - 从主人输入提取已知字段（一次性列出，按模板字段顺序）\n"
    "   - 逐项追问缺失字段（一次问一个，每个 Q 必带完整车牌如「吉AW2908 XX 是？」）\n"
    "   - 单位/口径含糊必反问（如 34 吨是理论还是过磅）\n"
    "   - 多商品装车自动展开商品段 ① ② + 合计行\n"
    "3. 输出**明文**：身份证完整 18 位 / 电话完整 11 位 / 银行卡完整 19 位，**禁止遮蔽**\n"
    "   （理由：业务员录入 ERP + 内部记录，需完整字段；覆盖 TG 通用'部分遮蔽'规则）\n"
    "4. 输出**完整模板**（采购 19 字段 / 销售 19 字段 / 汇款 5 字段，公式可验算）\n"
    "   覆盖 TG 通用'5 行限制'——业务模板需要完整字段\n"
    "5. 落到 ~/Private/cases/business/{采购单车,销售单车,汇款记录}/<车牌>_<日期>.md\n"
    "6. 严禁自创简短格式（如「运输信息登记」），严禁省略字段\n"
    "\n"
    "[输出质量硬约束 — 违反即重做]\n"
    "A. 公式精确派生：运费总额必须用件数算出的精确吨数（如 1374 件 × 49.5 斤 / 2000 = 34.9965 吨），\n"
    "   禁用主人口头估算（如'34 吨'）。公式必须标完整单位：\n"
    "   ✅ 360 元/吨 × 34.9965 吨 = 12,598.74 元\n"
    "   ❌ 360 × 34 = 12,240（无单位 + 用了口头吨数）\n"
    "B. 只问模板字段：追问前必须自查字段在 templates/<采购|销售|汇款>.md 字段清单里。\n"
    "   严禁自创字段如'付款方式'/'付款状态'/'支付方式'（模板只有'付款协议'：货到付款/装车付款）\n"
    "C. 简短答案立即接受：主人答'湛江'/'无'/'缝包'等单字单词答案 → 直接填入字段，\n"
    "   禁止反问细化（如不能再问'湛江哪个仓库'）。例外仅 3 种：明显矛盾/不合法/金融红线\n"
    "D. 计款依据括号注用'依实际到货付款'，禁用'待过磅后切换'。\n"
    "   过磅字段标 — 即可，禁加占位文字'（到货过磅后补）'\n"
    "E. 字段顺序按模板严格走。**单商品**（1 个规格）：默认模板，无 ① 编号、无合计行；\n"
    "   **多商品**（≥ 2 个规格）：用'装车商品 ①'/'装车商品 ②'一行格式 + 段尾加'—— 合计 ——'行；\n"
    "   禁用'装车商品：花生米\\n商品 ①'（拆两行格式错）\n"
    "   判定：同品名 + 同规格 + 同单价 = 1 个商品（合并件数）；不同规格 = 多商品\n"
    "[强制规则结束]\n"
)


def _maybe_force_business_skill(text: str) -> str:
    """If business keywords detected, return force-skill prefix; else empty."""
    if _BUSINESS_INFO_KEYWORDS_RE.search(text or ""):
        return BUSINESS_INFO_FORCE_PROMPT
    return ""

TOOL_PROFILES = {
    "readonly": "Read,Grep,Glob,WebSearch,WebFetch",
    "standard": "default",
    "restricted": "Read,Grep,Glob",
}
DEFAULT_TOOL_PROFILE = "readonly"  # kept for reference; --tools no longer passed to claude

MODELS = {
    "opus": "Opus 4.7",
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


class _GetUpdatesTracker(logging.Filter):
    """Track actual getUpdates HTTP activity via httpx log messages.
    Also redacts bot token from httpx log output to prevent leaking."""
    _TOKEN_RE = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+/")

    def filter(self, record):
        global _last_getupdate_ts
        msg = record.getMessage()
        if "getUpdates" in msg and "200 OK" in msg:
            _last_getupdate_ts = time.time()
        # Redact bot token in logged URLs
        if "/bot" in msg:
            record.msg = self._TOKEN_RE.sub("/bot****/", record.getMessage())
            record.args = None
        return True

logging.getLogger("httpx").addFilter(_GetUpdatesTracker())

# ── 配置读取 ──

_config_cache: dict | None = None


def load_config(force_reload: bool = False) -> dict:
    """Load config with caching. Shell expansion runs once at first load."""
    global _config_cache
    if _config_cache is not None and not force_reload:
        return _config_cache

    import subprocess as _sp
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    # Power-user feature: config values prefixed with "!" are executed as shell
    # commands and replaced with their stdout. This runs with the privileges of the
    # bot process, so config.json must be owner-writable only (0600/0644).
    for k, v in cfg.items():
        if isinstance(v, str) and v.startswith("!"):
            cmd = v[1:]
            try:
                cfg[k] = _sp.check_output(cmd, shell=True, text=True, timeout=10).strip()
            except _sp.CalledProcessError as e:
                print(f"Shell expansion failed for {k}: {e}", file=sys.stderr)
                sys.exit(1)
            except _sp.TimeoutExpired:
                print(f"Shell expansion timed out for {k}: {cmd}", file=sys.stderr)
                sys.exit(1)
    _config_cache = cfg
    return cfg


def get_claude_bin() -> Path:
    cfg = load_config()
    return Path(cfg.get("claudeBin", "~/.local/bin/claude")).expanduser()


def get_proxy() -> str:
    cfg = load_config()
    return cfg.get("proxy", "") or None


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
        CREATE TABLE IF NOT EXISTS context_buffer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            project TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
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


# ── Task 并发管理 ──

TASK_PRIORITY_QUICK = 0    # queries, status checks — scheduled first
TASK_PRIORITY_NORMAL = 1   # regular conversation
TASK_PRIORITY_HEAVY = 2    # agent loop, long tasks


@dataclass
class Task:
    task_id: str
    chat_id: str
    message_id: int          # 原始消息 ID，用于 reply_to
    label: str               # 消息前 15 字做标签
    status: str = "queued"   # queued → running → done/failed/cancelled
    priority: int = TASK_PRIORITY_NORMAL
    proc: asyncio.subprocess.Process | None = None
    created_at: float = field(default_factory=time.time)
    tool_count: int = 0      # tools used so far (for progress display)
    started_at: float = 0.0  # when status changed to running


class TaskManager:
    def __init__(self, max_per_user: int, max_total: int):
        self._tasks: dict[str, Task] = {}           # task_id → Task
        self._user_tasks: dict[str, list[str]] = {}  # chat_id → [task_ids]
        self._max_per_user = max_per_user
        self._max_total = max_total
        self._effective_workers = MAX_CONCURRENT_WORKERS  # adaptive: lowered on 503
        self._consecutive_503 = 0

    def can_submit(self, chat_id: str) -> tuple[bool, str]:
        active = [tid for tid in self._user_tasks.get(chat_id, [])
                  if self._tasks.get(tid) and self._tasks[tid].status in ("queued", "running")]
        if len(active) >= self._max_per_user:
            return False, f"Concurrent limit ({self._max_per_user}). /tasks to check."
        total_active = sum(1 for t in self._tasks.values() if t.status in ("queued", "running"))
        if total_active >= self._max_total:
            return False, "System busy. Try again later."
        return True, ""

    def submit(self, chat_id: str, message_id: int, label: str,
               priority: int = TASK_PRIORITY_NORMAL) -> Task:
        task_id = uuid.uuid4().hex[:8]
        task = Task(task_id=task_id, chat_id=chat_id, message_id=message_id,
                    label=label, priority=priority)
        self._tasks[task_id] = task
        self._user_tasks.setdefault(chat_id, []).append(task_id)
        return task

    def set_running(self, task_id: str, proc: asyncio.subprocess.Process):
        task = self._tasks.get(task_id)
        if task:
            task.status = "running"
            task.proc = proc
            task.started_at = time.time()

    def complete(self, task_id: str):
        self._remove(task_id, "done")
        # Successful completion: recover from 503 throttle
        if self._consecutive_503 > 0:
            self._consecutive_503 = max(0, self._consecutive_503 - 1)
            if self._consecutive_503 == 0:
                self._effective_workers = MAX_CONCURRENT_WORKERS
                log.info(f"TaskManager: recovered from throttle, workers={self._effective_workers}")

    def fail(self, task_id: str):
        self._remove(task_id, "failed")

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or task.status not in ("queued", "running"):
            return False
        if task.proc:
            try:
                task.proc.kill()
            except ProcessLookupError:
                pass
        self._remove(task_id, "cancelled")
        return True

    def report_throttle(self):
        """Called when Claude returns 503/overloaded. Reduces effective workers."""
        self._consecutive_503 += 1
        new_limit = max(1, self._effective_workers - 1)
        if new_limit != self._effective_workers:
            self._effective_workers = new_limit
            log.warning(f"TaskManager: Claude throttle detected, workers reduced to {self._effective_workers}")

    def has_running(self, chat_id: str) -> bool:
        return len(self.get_user_active(chat_id)) > 0

    def running_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == "running")

    def queued_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == "queued")

    def get_user_active(self, chat_id: str) -> list[Task]:
        return [self._tasks[tid] for tid in self._user_tasks.get(chat_id, [])
                if tid in self._tasks and self._tasks[tid].status in ("queued", "running")]

    def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def _remove(self, task_id: str, final_status: str):
        task = self._tasks.pop(task_id, None)
        if task:
            task.status = final_status
            task.proc = None
            user_list = self._user_tasks.get(task.chat_id, [])
            if task_id in user_list:
                user_list.remove(task_id)
            if not user_list:
                self._user_tasks.pop(task.chat_id, None)


# ── 全局状态 ──

db: sqlite3.Connection = None
worker_semaphore: asyncio.Semaphore = None
task_manager: TaskManager = None  # initialized in main()
agent_running: dict[str, dict] = {}   # chat_id -> {"cancel": Event, ...}
_sensitive_values: dict[str, list[str]] = {}  # chat_id -> [password_value, ...]
_background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks
_watchdog_ts: float = time.time()  # updated by heartbeat; checked by OS thread watchdog
_poll_ts: float = time.time()  # updated by poll monitor; checked by OS thread watchdog
_last_getupdate_ts: float = 0.0  # 0 = never seen; set by first getUpdates 200 OK
_app_ref = None  # set in main(), used by poll monitor
_active_procs: dict[str, asyncio.subprocess.Process] = {}  # chat_id -> proc (agent system only)
_cancelled: dict[str, bool] = {}  # task_id or chat_id -> cancel flag
_typing_chats: dict[int, int] = {}  # chat_id -> active_task_count (shared typing loop)
_typing_loop_task: asyncio.Task | None = None  # single global typing loop
_context_buffer: dict[str, list[dict]] = {}  # "chat_id:project" -> [{user, assistant, ts}]
CONTEXT_BUFFER_SIZE = 8  # keep last N exchanges for session continuity

# ── Reply Chain Index (message thread memory) ──
# Stores recent messages to enable multi-level reply chain traversal.
# Telegram only gives us the immediate parent; this index lets us walk the full chain.
_msg_chain: dict[int, dict[int, dict]] = {}  # chat_id -> {msg_id -> {text, author, reply_to}}
_MSG_CHAIN_MAX = 200  # per chat, evict oldest when exceeded


def _chain_record(chat_id: int, msg_id: int, text: str, author: str, reply_to: int | None = None):
    """Record a message in the reply chain index."""
    chain = _msg_chain.setdefault(chat_id, {})
    chain[msg_id] = {"text": text[:500], "author": author, "reply_to": reply_to}
    # Evict oldest if over limit
    if len(chain) > _MSG_CHAIN_MAX:
        oldest = sorted(chain.keys())[:len(chain) - _MSG_CHAIN_MAX]
        for k in oldest:
            chain.pop(k, None)


def _chain_build_context(chat_id: int, msg_id: int, max_depth: int = 5) -> str:
    """Walk the reply chain backwards, return formatted thread context."""
    chain = _msg_chain.get(chat_id, {})
    thread = []
    current = msg_id
    for _ in range(max_depth):
        entry = chain.get(current)
        if not entry:
            break
        thread.append(entry)
        if not entry.get("reply_to"):
            break
        current = entry["reply_to"]
    if len(thread) <= 1:
        return ""  # no chain to show (single message or not found)
    # Reverse to chronological order, skip the last one (it's the current message)
    thread = list(reversed(thread[1:]))  # exclude current msg, oldest first
    lines = []
    for i, e in enumerate(thread):
        role = "Bot" if e["author"] == "bot" else "User"
        lines.append(f"[Thread {i+1}/{len(thread)}] {role}: {e['text']}")
    return "\n".join(lines)
WATCHDOG_STALE_SEC = 300  # 5 min without heartbeat tick → force restart
POLL_ACTIVITY_STALE_SEC = 300  # 5 min without actual getUpdates → force restart (was 120, raised to match WATCHDOG_STALE_SEC — proxy latency spikes were causing false restarts)

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


# get_user_lock removed — replaced by TaskManager per-user concurrency cap


def _create_background_task(coro, *, name: str = None) -> asyncio.Task:
    """Create a background task with strong reference to prevent GC destruction."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _safe_result(result) -> dict:
    """Ensure invoke result is always a dict, guarding against unexpected types."""
    if isinstance(result, dict):
        return result
    log.warning(f"invoke returned non-dict type: {type(result).__name__}")
    if isinstance(result, list):
        # Try to find a result event in the list
        for item in reversed(result):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
        return {"error": "Unexpected list response from Claude", "result": None}
    return {"error": f"Unexpected {type(result).__name__} response", "result": None}


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
        f"WHERE chat_id=? AND date(created_at, '{TZ_OFFSET}')=date('now', '{TZ_OFFSET}')", (chat_id,),
    ).fetchone()
    return row[0] if row else 0.0


def list_projects() -> list[dict]:
    rows = db.execute("SELECT name, path, description FROM projects ORDER BY name").fetchall()
    return [{"name": r[0], "path": r[1], "description": r[2]} for r in rows]


def _load_context_buffers():
    """Load context buffers from DB into memory on startup."""
    rows = db.execute(
        "SELECT chat_id, project, content FROM context_buffer ORDER BY created_at ASC"
    ).fetchall()
    for chat_id, project, content in rows:
        key = f"{chat_id}:{project}"
        try:
            entry = json.loads(content)
            _context_buffer.setdefault(key, []).append(entry)
        except json.JSONDecodeError:
            continue
    for key in _context_buffer:
        if len(_context_buffer[key]) > CONTEXT_BUFFER_SIZE:
            _context_buffer[key] = _context_buffer[key][-CONTEXT_BUFFER_SIZE:]


def _save_context_entry(chat_id: str, project: str, entry: dict):
    """Save a context buffer entry and trim old entries."""
    db.execute(
        "INSERT INTO context_buffer (chat_id, project, content) VALUES (?, ?, ?)",
        (chat_id, project, json.dumps(entry, ensure_ascii=False)),
    )
    count = db.execute(
        "SELECT COUNT(*) FROM context_buffer WHERE chat_id=? AND project=?",
        (chat_id, project),
    ).fetchone()[0]
    if count > CONTEXT_BUFFER_SIZE:
        db.execute(
            "DELETE FROM context_buffer WHERE id IN ("
            "  SELECT id FROM context_buffer WHERE chat_id=? AND project=? "
            "  ORDER BY created_at ASC LIMIT ?)",
            (chat_id, project, count - CONTEXT_BUFFER_SIZE),
        )
    db.commit()


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
            if CLAUDE_TIMEOUT:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=message.encode("utf-8")), timeout=CLAUDE_TIMEOUT
                )
            else:
                stdout, stderr = await proc.communicate(input=message.encode("utf-8"))
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
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
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
                                   on_tool_use=None, chat_id: str = None,
                                   task_id: str = None) -> dict:
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
        )
        if task_id and task_manager:
            task_manager.set_running(task_id, proc)
        elif chat_id:
            _active_procs[chat_id] = proc
        proc.stdin.write(message.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        result = {"error": "No result received", "result": None}
        deadline = (time.monotonic() + CLAUDE_TIMEOUT) if CLAUDE_TIMEOUT else None
        buf = b""

        while True:
            if deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    await proc.wait()
                    return {"error": f"Claude timeout ({CLAUDE_TIMEOUT}s)", "result": None}
                read_timeout = min(remaining, 30)
            else:
                read_timeout = 30  # still check periodically for EOF

            try:
                chunk = await asyncio.wait_for(
                    proc.stdout.read(256 * 1024), timeout=read_timeout)
            except asyncio.TimeoutError:
                if deadline and time.monotonic() >= deadline:
                    proc.kill()
                    await proc.wait()
                    return {"error": f"Claude timeout ({CLAUDE_TIMEOUT}s)", "result": None}
                continue

            if not chunk:
                # EOF — process remaining buffer
                if buf:
                    line_str = buf.decode("utf-8", errors="replace").strip()
                    buf = b""
                    if line_str:
                        try:
                            event = json.loads(line_str)
                            if isinstance(event, dict) and event.get("type") == "result":
                                result = event
                        except (json.JSONDecodeError, AttributeError):
                            pass
                break

            buf += chunk
            # Split on newlines, process complete lines
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                line_str = line_bytes.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                etype = event.get("type", "")
                if etype == "result":
                    result = event
                elif etype == "assistant" and on_tool_use:
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            await on_tool_use(block.get("name", ""), block.get("input", {}))

        await proc.wait()
        cancel_key = task_id or chat_id
        if task_id and task_manager:
            t = task_manager.get_task(task_id)
            if t:
                t.proc = None  # proc ended; TaskManager.complete() will remove it
        elif chat_id:
            _active_procs.pop(chat_id, None)

        # Check if cancelled by user — always honour cancel regardless of partial result
        if cancel_key and _cancelled.pop(cancel_key, False):
            return {"error": "Cancelled by user", "result": None}

        if proc.returncode != 0 and not result.get("result"):
            stderr_data = await proc.stderr.read()
            err = stderr_data.decode("utf-8", errors="replace").strip()
            log.error(f"claude exit {proc.returncode}: {err}")
            return {"error": f"Claude exit {proc.returncode}: {err[:200]}", "result": None}

        return result

    except Exception as e:
        cancel_key = task_id or chat_id
        if task_id and task_manager:
            t = task_manager.get_task(task_id)
            if t:
                t.proc = None
        elif chat_id:
            _active_procs.pop(chat_id, None)
        if cancel_key:
            _cancelled.pop(cancel_key, None)
        # Kill leaked subprocess to prevent zombie accumulation
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        log.error(f"invoke_stream error: {e}")
        return {"error": str(e), "result": None}


# ── Telegram 消息处理 ──

async def _shared_typing_loop(bot):
    """Single global typing loop for all active chats. Reduces Telegram API pressure
    from N concurrent send_chat_action calls to 1 per chat every 5s."""
    while True:
        chats = dict(_typing_chats)  # snapshot
        for chat_id, count in chats.items():
            if count > 0:
                try:
                    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                except Exception:
                    pass
        await asyncio.sleep(5.0)


def _typing_register(chat_id: int, bot):
    """Register a chat as needing typing indicator. Starts global loop if needed."""
    global _typing_loop_task
    _typing_chats[chat_id] = _typing_chats.get(chat_id, 0) + 1
    if _typing_loop_task is None or _typing_loop_task.done():
        _typing_loop_task = _create_background_task(_shared_typing_loop(bot), name="shared-typing")


def _typing_unregister(chat_id: int):
    """Unregister a chat from typing indicator."""
    count = _typing_chats.get(chat_id, 0)
    if count <= 1:
        _typing_chats.pop(chat_id, None)
    else:
        _typing_chats[chat_id] = count - 1


async def send_typing_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int, stop_event: asyncio.Event):
    """Legacy per-task typing — now delegates to shared loop."""
    _typing_register(chat_id, context.bot)
    try:
        await stop_event.wait()
    finally:
        _typing_unregister(chat_id)


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


def _is_msg_gone(e: Exception) -> bool:
    """Check if a Telegram error means the message no longer exists."""
    s = str(e).lower()
    return "message to edit not found" in s or "message can't be edited" in s or "message not found" in s


def _is_network_error(e: Exception) -> bool:
    """Check if the error is a transient network issue."""
    s = str(e).lower()
    return "connecterror" in s or "networkerror" in s or "timed out" in s or "timeout" in s


class RetryHTTPXRequest(HTTPXRequest):
    """HTTPXRequest with transparent retry for transient network errors.

    Retries ConnectError / RemoteProtocolError / timeout up to 3 times
    with exponential backoff (1s → 2s → 4s). All other errors pass through.
    """

    _MAX_RETRIES = 3
    _BASE_DELAY = 1.0

    async def do_request(self, url, method, request_data=None,
                         read_timeout=None, write_timeout=None,
                         connect_timeout=None, pool_timeout=None):
        last_exc = None
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                return await super().do_request(
                    url, method, request_data,
                    read_timeout, write_timeout, connect_timeout, pool_timeout)
            except NetworkError as e:
                last_exc = e
                cause = str(e).lower()
                retryable = ("connecterror" in cause or "disconnected" in cause
                             or "timed out" in cause or "timeout" in cause
                             or "reset" in cause)
                if not retryable or attempt == self._MAX_RETRIES:
                    raise
                delay = self._BASE_DELAY * (2 ** attempt)
                log.warning(f"RetryHTTPXRequest: retry {attempt+1}/{self._MAX_RETRIES} "
                            f"in {delay}s — {e}")
                await asyncio.sleep(delay)
        raise last_exc


async def _stream_reply(bot, chat_id: int, text: str, reuse_msg=None):
    """Progressively reveal text in a Telegram message (typing effect).
    If reuse_msg is provided, edits that message; otherwise creates a new one.
    On persistent failures, falls back to send_long_message for delivery guarantee."""
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
    _consecutive_failures = 0
    _MAX_CONSECUTIVE_FAILURES = 5  # after this many, abandon streaming and send plainly

    # First edit: clear progress content, show cursor
    if msg:
        try:
            await msg.edit_text(cursor)
        except Exception as e:
            if _is_msg_gone(e):
                msg = None  # message deleted by user, create new one
            else:
                msg = None

    if not msg:
        try:
            msg = await bot.send_message(chat_id=chat_id, text=cursor)
        except Exception as e:
            # Cannot even send a new message — direct fallback
            log.warning(f"_stream_reply: cannot create message: {e}")
            await asyncio.sleep(3.0)
            await send_long_message(bot, chat_id, text)
            return

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
            _consecutive_failures = 0
            try:
                msg = await bot.send_message(chat_id=chat_id, text=cursor)
            except Exception:
                await asyncio.sleep(3.0)
                await send_long_message(bot, chat_id, text)
                return
            continue

        display = text[:pos] + (cursor if pos < len(text) else "")
        try:
            await msg.edit_text(display, parse_mode=ParseMode.MARKDOWN)
            _consecutive_failures = 0
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "Too Many Requests" in err_str or "Flood" in err_str:
                await asyncio.sleep(5.0)  # back off on rate limit
            elif _is_msg_gone(e):
                # Message deleted by user — send remaining text as new message
                log.info(f"_stream_reply: message deleted by user, fallback to send_long_message")
                remaining = text[pos:] if pos < len(text) else text
                await send_long_message(bot, chat_id, remaining)
                return
            elif _is_network_error(e):
                _consecutive_failures += 1
                log.warning(f"_stream_reply: network error ({_consecutive_failures}/{_MAX_CONSECUTIVE_FAILURES}): {e}")
                if _consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    log.warning("_stream_reply: too many network failures, fallback to send_long_message")
                    await asyncio.sleep(5.0)
                    await send_long_message(bot, chat_id, text)
                    return
                await asyncio.sleep(3.0)
            else:
                try:
                    await msg.edit_text(display)
                    _consecutive_failures = 0
                except Exception as inner_e:
                    if _is_msg_gone(inner_e):
                        remaining = text[pos:] if pos < len(text) else text
                        await send_long_message(bot, chat_id, remaining)
                        return
                    _consecutive_failures += 1

        chunk_size = min(chunk_size * STREAM_ACCEL, STREAM_MAX_CHUNK)
        if pos < len(text):
            await asyncio.sleep(STREAM_INTERVAL)

    # Final edit without cursor (if cursor was shown)
    if cursor in (text[:pos] + cursor):
        for _attempt in range(3):
            try:
                await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
                break
            except Exception as e:
                if "429" in str(e) or "Too Many Requests" in str(e) or "Flood" in str(e):
                    await asyncio.sleep(5.0)
                elif _is_msg_gone(e):
                    await send_long_message(bot, chat_id, text)
                    break
                elif _is_network_error(e) and _attempt == 2:
                    await asyncio.sleep(5.0)
                    await send_long_message(bot, chat_id, text)
                    break
                else:
                    try:
                        await msg.edit_text(text)
                    except Exception:
                        if _attempt == 2:
                            await send_long_message(bot, chat_id, text)
                    break


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

    # P0: Inject Telegram behavior constraints (skip if systemContext disabled in config)
    # Resumed sessions already have the rules in conversation history
    _inject_ctx = load_config().get("injectSystemContext", True)
    augmented_text = text  # default: no prefix for resumed sessions

    effective_model = None  # None = use active["model"]
    effective_effort = None

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

    # New session: inject Telegram rules + context buffer (rules cached in conversation history for subsequent turns)
    if not session_id:
        ctx_key = f"{chat_id_str}:{active['project']}"
        recent = _context_buffer.get(ctx_key, [])
        ctx_prefix = TELEGRAM_SYSTEM_CONTEXT if _inject_ctx else ""
        if recent:
            ctx_lines = []
            for entry in recent[-3:]:
                ctx_lines.append(f"- User: {entry['user']}")
                ctx_lines.append(f"  Assistant: {entry['assistant']}")
            augmented_text = (
                ctx_prefix
                + "[Previous conversation context:\n" + "\n".join(ctx_lines) + "]\n\n"
                + text
            )
        else:
            augmented_text = ctx_prefix + text

    if session and (session["turns"] >= SESSION_ROTATE_TURNS or session["cost_usd"] >= SESSION_ROTATE_COST):
        kb = make_keyboard([("New session", "cmd:new"), ("Continue", "cmd:dismiss")], columns=2)
        await update.message.reply_text(
            f"Session: {session['turns']}t, ${session['cost_usd']:.2f}. Start fresh?",
            reply_markup=kb,
        )

    # ── Multi-agent concurrency: per-user cap check ──
    can, reject_reason = task_manager.can_submit(chat_id_str)
    if not can:
        await update.message.reply_text(reject_reason)
        return

    # Determine session strategy:
    # If user already has running tasks → independent session (task mode)
    # If no concurrent tasks → resume session (conversation mode)
    has_concurrent = task_manager.has_running(chat_id_str)
    if has_concurrent:
        session_id = None  # Force new session — avoid --resume conflict
        augmented_text = (TELEGRAM_SYSTEM_CONTEXT if _inject_ctx else "") + text

    # ── 强制业务整理 skill 调用（每条消息都注入，包括 resumed session） ──
    # 关键词命中 → 强制 prompt 注入到 augmented_text 末尾，让 Claude 必须调用 Skill 工具
    augmented_text = augmented_text + _maybe_force_business_skill(text)

    # Auto-detect task priority from content
    _quick_patterns = re.compile(
        r'^(/status|/cost|/health|/tasks|查|看|是什么|多少|几个|什么时候|状态|帮我查)',
        re.IGNORECASE)
    task_priority = TASK_PRIORITY_QUICK if _quick_patterns.search(text) else TASK_PRIORITY_NORMAL

    # Register task
    task_label = text[:15].replace("\n", " ").strip()
    task = task_manager.submit(chat_id_str, update.message.message_id, task_label,
                               priority=task_priority)

    # Show queue status when multiple tasks are active
    running_n = task_manager.running_count()
    queued_n = task_manager.queued_count()
    if has_concurrent and queued_n > 0:
        queue_hint = f" (#{running_n + queued_n} in queue)"
    else:
        queue_hint = ""

    # Progress message: reply_to original message for multi-task traceability
    progress_prefix = f"「{task_label}」" if has_concurrent else ""
    progress_msg = None
    reply_kwargs = {}
    if has_concurrent:
        reply_kwargs["reply_to_message_id"] = update.message.message_id
    for _retry in range(3):
        try:
            progress_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"{progress_prefix}{WAVE_FRAMES[0]}{queue_hint}",
                **reply_kwargs)
            break
        except Exception as e:
            if _retry < 2:
                log.warning(f"reply_text retry {_retry+1}/3: {e}")
                await asyncio.sleep(2.0)
            else:
                task_manager.fail(task.task_id)
                raise
    progress_lines = []
    stop_wave = asyncio.Event()

    async def _wave_animation():
        """Animate wave dots + tool progress. Degrades to static when ≥2 concurrent tasks
        to halve Telegram API pressure (only first task gets animation)."""
        is_primary = (running_n == 0)  # first task gets full animation
        frame_idx = 0
        backoff = WAVE_INTERVAL
        while not stop_wave.is_set():
            frame_idx += 1
            if is_primary or frame_idx % 3 == 0:
                # Primary task: animate every frame. Secondary: every 3rd frame (18s)
                wave = WAVE_FRAMES[frame_idx % len(WAVE_FRAMES)]
                if progress_lines:
                    display = progress_lines[-6:]
                    anim_text = f"{progress_prefix}{wave}\n" + "\n".join(f"`> {l}`" for l in display)
                else:
                    tools_n = task.tool_count
                    anim_text = f"{progress_prefix}{wave}" + (f" ({tools_n} tools)" if tools_n else "")
                try:
                    await progress_msg.edit_text(anim_text, parse_mode=ParseMode.MARKDOWN)
                    backoff = WAVE_INTERVAL
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "Too Many Requests" in err_str or "Flood" in err_str:
                        backoff = min(backoff * 2, 30.0)
                    # else: transient error, silently skip (non-critical path)
            try:
                await asyncio.wait_for(stop_wave.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass

    output_files = []

    async def on_tool_use(tool_name: str, tool_input: dict):
        task.tool_count += 1
        progress_lines.append(_format_tool_progress(tool_name, tool_input))
        if tool_name == "Write" and tool_input.get("file_path"):
            output_files.append(tool_input["file_path"])

    # Concurrency controlled by TaskManager cap + global semaphore
    if worker_semaphore._value == 0:
        try:
            await progress_msg.edit_text(f"{progress_prefix}System busy, waiting for a slot...")
        except Exception:
            pass
        log.info(f"user {chat_id_str} task {task.task_id} queued (priority={task_priority}, "
                 f"workers={task_manager._effective_workers})")
    async with worker_semaphore:
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(send_typing_loop(context, chat_id, stop_typing))
        wave_task = asyncio.create_task(_wave_animation())
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
                chat_id=chat_id_str,
                task_id=task.task_id,
            )
        finally:
            stop_typing.set()
            stop_wave.set()
            await typing_task
            await wave_task

    result = _safe_result(result)

    # ── 503/overloaded detection: adaptive throttle ──
    err_text = result.get("error", "")
    if err_text and ("503" in err_text or "overloaded" in err_text.lower()):
        task_manager.report_throttle()

    if err_text and not result.get("result"):
        task_manager.fail(task.task_id)
        try:
            await progress_msg.delete()
        except Exception:
            pass
        for _r in range(3):
            try:
                await update.message.reply_text(f"Error: {err_text[:500]}")
                break
            except Exception:
                if _r < 2:
                    await asyncio.sleep(2.0)
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

    # Cost tag with tool count for multi-task awareness
    tools_tag = f" | {task.tool_count}t" if task.tool_count else ""
    cost_tag = f"\n\n`{active['project']} | {use_model} | {use_effort} | ${cost:.4f} | {duration/1000:.1f}s{tools_tag}`"
    full_reply = reply_text + cost_tag

    task_manager.complete(task.task_id)
    upsert_session(chat_id_str, active["project"], new_session_id, active["model"], turns, cost)
    log_cost(chat_id_str, active["project"], cost, turns, duration)

    # Streaming typing effect: progressively reveal text in the progress message
    await _stream_reply(context.bot, chat_id, full_reply, progress_msg)

    # Record bot reply in chain index for multi-level reply tracking
    if progress_msg:
        bot_reply_to = update.message.message_id if has_concurrent else None
        _chain_record(chat_id, progress_msg.message_id, reply_text, "bot", bot_reply_to)

    # Auto-send output files (images, small documents)
    _SENDABLE_IMAGES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
    _SENDABLE_DOCS = {'.pdf', '.csv', '.xlsx', '.json', '.html'}
    for fpath in output_files:
        p = Path(fpath)
        if not p.exists() or p.stat().st_size > 10 * 1024 * 1024:
            continue
        try:
            if p.suffix.lower() in _SENDABLE_IMAGES:
                with open(p, 'rb') as f:
                    await context.bot.send_photo(chat_id=chat_id, photo=f, caption=p.name)
            elif p.suffix.lower() in _SENDABLE_DOCS:
                with open(p, 'rb') as f:
                    await context.bot.send_document(chat_id=chat_id, document=f, caption=p.name)
        except Exception as e:
            log.warning(f"Failed to send output file {p.name}: {e}")

    # Save to context buffer for session continuity
    raw_reply = result.get("result", "")
    if raw_reply:
        ctx_key = f"{chat_id_str}:{active['project']}"
        entry = {"user": text[:300], "assistant": raw_reply[:300], "ts": time.time()}
        buf = _context_buffer.setdefault(ctx_key, [])
        buf.append(entry)
        if len(buf) > CONTEXT_BUFFER_SIZE:
            _context_buffer[ctx_key] = buf[-CONTEXT_BUFFER_SIZE:]
        _save_context_entry(chat_id_str, active["project"], entry)

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

        # Prepend quoted message context if replying to a previous message
        if update.message.reply_to_message:
            quoted = update.message.reply_to_message
            quoted_text = quoted.text or quoted.caption or ""
            if quoted_text:
                quoted_author = "bot" if quoted.from_user and quoted.from_user.is_bot else "user"
                prompt = f"[Replying to {quoted_author}'s message: {quoted_text}]\n\n{prompt}"

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
    # Female
    "Sarah": "EXAVITQu4vr4xnSDxMaL",       # Mature, Reassuring
    "Jessica": "cgSgspJ2msm6clMCkdW9",      # Playful, Bright
    "Laura": "FGY2WhTYpPnrIDTdsKH5",        # Enthusiast, Quirky
    "Alice": "Xb7hH8MSUJpSbSDYk0k2",        # Clear, Educator
    "Matilda": "XrExE9yKIg1WjnnlVkGX",      # Professional
    "Bella": "hpp4J3VqNfWAUOO0d1Us",         # Professional, Bright
    "Lily": "pFZP5JQG7iQjIQuC4Bku",         # Velvety Actress
    "River": "SAz9YHcvj6GT2YYXdXww",        # Relaxed, Neutral
    # Male
    "George": "JBFqnCBsd6RMkjVDRZzb",       # Warm Storyteller
    "Brian": "nPczCjzI2devNBz1zQrb",        # Deep, Comforting
    "Adam": "pNInz6obpgDQGcFmaJgB",         # Dominant, Firm
    "Charlie": "IKne3meq5aSn9XLyUdCD",      # Deep, Confident
    "Roger": "CwhRBWXzGAHq8TQ4Fs17",        # Laid-Back, Casual
    "Callum": "N2lVS1w4EtoT3dr4eOWO",       # Husky Trickster
    "Harry": "SOYHLrjzK2X1ezoPC6cr",        # Fierce Warrior
    "Liam": "TX3LPaxmHKxFdv7VOQHJ",        # Energetic
    "Will": "bIHbv24MWmeRgasZH58o",         # Relaxed Optimist
    "Eric": "cjVigY5qzO86Huf0OWal",         # Smooth, Trustworthy
    "Chris": "iP95p4xoKVk53GoZ742B",        # Charming
    "Daniel": "onwK4e9ZLuTAKqWW03F9",       # Steady Broadcaster
    "Bill": "pqHfZKP75CvOlQylNhV4",         # Wise, Mature
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
    """Read ElevenLabs API key from config (if present) or Keychain."""
    _tts_cfg = load_config().get("tts", {}).get("elevenlabs", {})
    if _tts_cfg.get("apiKey"):
        return _tts_cfg["apiKey"]
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
    # Check for custom voice ID (e.g. FC Berty voice) before standard lookup
    custom_id = get_setting("voice_custom_id", "")
    voice_id = custom_id if custom_id else ELEVENLABS_VOICES.get(voice_name, "EXAVITQu4vr4xnSDxMaL")
    api_key = _get_elevenlabs_key()
    async with httpx.AsyncClient(proxy=get_proxy(), timeout=30) as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={"text": clean, "model_id": load_config().get("tts", {}).get("elevenlabs", {}).get("modelId", ELEVENLABS_MODEL)},
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

        # Extract quoted message when user replies to a previous message
        if update.message.reply_to_message:
            quoted = update.message.reply_to_message
            quoted_text = quoted.text or quoted.caption or ""
            if quoted_text:
                quoted_author = "bot" if quoted.from_user and quoted.from_user.is_bot else "user"
                transcript = f"[Replying to {quoted_author}'s message: {quoted_text}]\n\n{transcript}"
                log.info(f"handle_voice: reply context prepended ({len(quoted_text)} chars from {quoted_author})")

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

        # Extract quoted message when user replies to a previous message
        if update.message.reply_to_message:
            quoted = update.message.reply_to_message
            quoted_text = quoted.text or quoted.caption or ""
            if quoted_text:
                quoted_author = "bot" if quoted.from_user and quoted.from_user.is_bot else "user"
                prompt = f"[Replying to {quoted_author}'s message: {quoted_text}]\n\n{prompt}"
                log.info(f"handle_document: reply context prepended ({len(quoted_text)} chars from {quoted_author})")

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


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward unregistered /commands to Claude Code (gstack skills, etc.)."""
    if not update.message or not update.message.text:
        return
    if not is_allowed(update.effective_chat.id):
        return
    text = update.message.text.strip()
    # Telegram bot commands use _ but gstack skills use - (e.g. /office_hours → /office-hours)
    if text.startswith("/"):
        parts = text.split(None, 1)
        cmd = parts[0].replace("_", "-")
        text = cmd if len(parts) == 1 else f"{cmd} {parts[1]}"
    log.info(f"handle_unknown_command: forwarding '{text[:50]}' to Claude Code")
    await _invoke_and_reply(update, context, text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info(f"handle_message: chat_id={update.effective_chat.id if update.effective_chat else 'None'}, "
             f"has_message={bool(update.message)}, has_text={bool(update.message and update.message.text)}")
    if not update.message or not update.message.text:
        log.info("handle_message: no message or text, returning")
        return
    if not is_allowed(update.effective_chat.id):
        log.info(f"handle_message: chat_id {update.effective_chat.id} not allowed")
        return

    text = update.message.text.strip()
    if not text:
        log.info("handle_message: empty text after strip")
        return

    chat_id = update.effective_chat.id
    msg_id = update.message.message_id
    reply_to_id = update.message.reply_to_message.message_id if update.message.reply_to_message else None

    # Record user message in chain index
    _chain_record(chat_id, msg_id, text, "user", reply_to_id)

    # Also record the quoted message if we haven't seen it
    if update.message.reply_to_message:
        quoted = update.message.reply_to_message
        quoted_text = quoted.text or quoted.caption or ""
        quoted_author = "bot" if quoted.from_user and quoted.from_user.is_bot else "user"
        q_reply_to = quoted.reply_to_message.message_id if quoted.reply_to_message else None
        _chain_record(chat_id, quoted.message_id, quoted_text, quoted_author, q_reply_to)

    # Build thread context from reply chain (multi-level)
    if reply_to_id:
        thread_ctx = _chain_build_context(chat_id, msg_id)
        if thread_ctx:
            text = f"[Conversation thread (oldest first):\n{thread_ctx}]\n\n{text}"
            log.info(f"handle_message: thread context prepended ({thread_ctx.count(chr(10))+1} messages)")
        else:
            # Fallback: single-level quote (chain not in index)
            quoted = update.message.reply_to_message
            quoted_text = quoted.text or quoted.caption or ""
            if quoted_text:
                quoted_author = "bot" if quoted.from_user and quoted.from_user.is_bot else "user"
                text = f"[Replying to {quoted_author}'s message: {quoted_text}]\n\n{text}"
                log.info(f"handle_message: reply context prepended ({len(quoted_text)} chars from {quoted_author})")

    log.info(f"handle_message: processing text='{text[:50]}...' from {chat_id}")

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
    # Auto-review: consolidate memories before resetting session
    cfg = load_config()
    if cfg.get("autoReview", {}).get("enabled", False):
        project_row = db.execute(
            "SELECT path FROM projects WHERE name=?", (active["project"],)
        ).fetchone()
        if project_row and Path(project_row[0]).exists():
            key = f"{chat_id_str}:{active['project']}"
            entries = _context_buffer.get(key, [])
            if entries and len(entries) >= 2:
                await update.message.reply_text("📝 整理记忆中...")
                result = await _auto_review_session(
                    chat_id_str, active["project"], project_row[0]
                )
                if result:
                    from datetime import datetime, timezone
                    ts_key = f"auto_review_ts:{key}"
                    db.execute(
                        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                        (ts_key, datetime.now(timezone.utc).isoformat()),
                    )
                    db.commit()
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
        f"WHERE chat_id=? AND date(created_at, '{TZ_OFFSET}')=date('now', '{TZ_OFFSET}')", (chat_id_str,)
    ).fetchone()
    week = db.execute(
        "SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(turns),0) FROM cost_log "
        f"WHERE chat_id=? AND datetime(created_at, '{TZ_OFFSET}') >= datetime('now', '{TZ_OFFSET}', '-7 days')", (chat_id_str,)
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


async def cmd_el(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/el — ElevenLabs account management"""
    if not is_allowed(update.effective_chat.id):
        return
    import httpx
    try:
        api_key = _get_elevenlabs_key()
    except Exception:
        api_key = None
    if not api_key:
        await update.message.reply_text("ElevenLabs API key not found in Keychain.")
        return
    try:
        async with httpx.AsyncClient(proxy=get_proxy(), timeout=15) as client:
            resp = await client.get(
                "https://api.elevenlabs.io/v1/user/subscription",
                headers={"xi-api-key": api_key},
            )
        if resp.status_code != 200:
            await update.message.reply_text(f"API error: {resp.status_code}")
            return
        sub = resp.json()
        used = sub.get("character_count", 0)
        limit = sub.get("character_limit", 0)
        pct = (used / limit * 100) if limit else 0
        reset_ts = sub.get("next_character_count_reset_unix", 0)
        from datetime import datetime, timezone, timedelta
        tz8 = timezone(timedelta(hours=8))
        reset_str = datetime.fromtimestamp(reset_ts, tz=tz8).strftime("%Y-%m-%d") if reset_ts else "N/A"
        voice_used = sub.get("voice_slots_used", 0)
        voice_limit = sub.get("voice_limit", 0)
        pro_limit = sub.get("professional_voice_limit", 0)
        inv = sub.get("next_invoice", {})
        next_amount = inv.get("amount_due_cents", 0) / 100
        status_icon = "✅" if sub.get("status") == "active" else "⚠️"
        text = (
            f"*ElevenLabs {sub.get('tier', 'unknown').title()}* {status_icon}\n\n"
            f"Credits: `{used:,}` / `{limit:,}` ({pct:.1f}%)\n"
            f"Reset: {reset_str}\n"
            f"Voice slots: {voice_used} / {voice_limit} (Pro: {pro_limit})\n"
            f"Clone: {'✅ Instant + Pro' if sub.get('can_use_professional_voice_cloning') else '✅ Instant' if sub.get('can_use_instant_voice_cloning') else '❌'}\n"
            f"Next bill: ${next_amount:.2f}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.error(f"cmd_el failed: {e}")
        await update.message.reply_text(f"Error: {e}")


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
        if model in MODELS and active:
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
                f"WHERE chat_id=? AND date(created_at, '{TZ_OFFSET}')=date('now', '{TZ_OFFSET}')", (chat_id_str,)
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
        result = _safe_result(result)
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

    # ── cancel_task:<task_id> or cancel_task:all ──
    elif data.startswith("cancel_task:"):
        target = data.split(":", 1)[1]
        if target == "all":
            active_tasks = task_manager.get_user_active(chat_id_str)
            for t in active_tasks:
                _cancelled[t.task_id] = True
                task_manager.cancel(t.task_id)
            await query.edit_message_text(f"Cancelled {len(active_tasks)} tasks.")
        else:
            t = task_manager.get_task(target)
            if t:
                _cancelled[t.task_id] = True
                task_manager.cancel(t.task_id)
                await query.edit_message_text(f"Cancelled「{t.label}」")
            else:
                await query.edit_message_text("Task not found or already finished.")

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

    result = _safe_result(result)
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
                        max_turns: int = 50, timeout: int = 600,
                        chat_id: str = None) -> dict:
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
        if chat_id:
            _active_procs[chat_id] = proc
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=message.encode("utf-8")), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"error": f"Timeout ({timeout}s)", "result": None}
        finally:
            if chat_id:
                _active_procs.pop(chat_id, None)

        # Honour cancel — process was killed by /cancel
        if chat_id and _cancelled.pop(chat_id, False):
            return {"error": "Cancelled by user", "result": None}

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
        if chat_id:
            _active_procs.pop(chat_id, None)
            _cancelled.pop(chat_id, None)
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

        plan_result = _safe_result(await _agent_invoke(
            plan_prompt, project_path, None, model, "readonly",
            effort="high", max_turns=AGENT_PLAN_MAX_TURNS, timeout=120,
            chat_id=chat_id_str))

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

            exec_result = _safe_result(await _agent_invoke(
                exec_prompt, project_path, session_id, model, tp,
                effort="high",
                max_turns=min(est * 2, AGENT_PHASE_MAX_TURNS),
                timeout=AGENT_PHASE_TIMEOUT,
                chat_id=chat_id_str))

            if exec_result.get("error"):
                # If cancelled, stop immediately — don't continue to next phase
                if "Cancelled by user" in exec_result["error"]:
                    await context.bot.send_message(chat_id=chat_id, text="⏹ Agent 已停止")
                    return
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

            verify_result = _safe_result(await _agent_invoke(
                verify_prompt, project_path, None, model, "readonly",
                effort="medium", max_turns=AGENT_VERIFY_MAX_TURNS, timeout=120,
                chat_id=chat_id_str))

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

    _create_background_task(
        run_agent_loop(chat_id_str, active["path"], "opus", objective, context),
        name=f"agent-{chat_id_str}")


# ── /health, /sleep, /work, /cleanup ── Scene Commands ──

async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/health — system health report"""
    if not is_allowed(update.effective_chat.id):
        return
    try:
        r = subprocess.run(
            [sys.executable, str(CB_HOME / "scripts" / "system-health.py")],
            capture_output=True, text=True, timeout=15,
        )
        output = r.stdout.strip() or r.stderr.strip() or "No output"
        await update.message.reply_text(f"```\n{output}\n```", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Health check failed: {e}")


async def cmd_sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sleep — lock screen + lower volume"""
    if not is_allowed(update.effective_chat.id):
        return
    cmds = [
        "osascript -e 'set volume output volume 30'",
        "pmset displaysleepnow",
    ]
    for cmd in cmds:
        subprocess.run(cmd, shell=True, capture_output=True)
    await update.message.reply_text("Good night. Screen locked, volume 30%.")


async def cmd_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/work — open work apps"""
    if not is_allowed(update.effective_chat.id):
        return
    apps = ["Google Chrome", "Visual Studio Code", "Telegram"]
    for app in apps:
        subprocess.run(["open", "-a", app], capture_output=True)
    await update.message.reply_text(f"Work mode. Opened: {', '.join(apps)}")


async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cleanup — auto-organize desktop files"""
    if not is_allowed(update.effective_chat.id):
        return
    await update.message.reply_text("Scanning Desktop...")
    desktop = Path.home() / "Desktop"
    categories = {
        "Screenshots": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"],
        "Documents": [".pdf", ".doc", ".docx", ".txt", ".rtf", ".pages", ".xlsx", ".csv"],
        "Archives": [".zip", ".tar", ".gz", ".rar", ".7z", ".dmg"],
        "Videos": [".mp4", ".mov", ".avi", ".mkv"],
        "Code": [".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".yml", ".sh"],
    }
    moved = {}
    for f in desktop.iterdir():
        if f.name.startswith(".") or f.is_dir():
            continue
        ext = f.suffix.lower()
        target_cat = None
        for cat, exts in categories.items():
            if ext in exts:
                target_cat = cat
                break
        if not target_cat:
            continue
        target_dir = desktop / target_cat
        target_dir.mkdir(exist_ok=True)
        dest = target_dir / f.name
        if dest.exists():
            dest = target_dir / f"{f.stem}_{int(time.time())}{f.suffix}"
        f.rename(dest)
        moved[target_cat] = moved.get(target_cat, 0) + 1

    if moved:
        report = "\n".join(f"  {cat}: {n} files" for cat, n in sorted(moved.items()))
        await update.message.reply_text(f"Cleanup done:\n```\n{report}\n```", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("Desktop clean, nothing to organize.")


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


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tasks — list active tasks with progress details"""
    if not is_allowed(update.effective_chat.id):
        return
    chat_id_str = str(update.effective_chat.id)
    active_tasks = task_manager.get_user_active(chat_id_str)

    # Also check agent system
    agent = agent_running.get(chat_id_str)

    if not active_tasks and not agent:
        await update.message.reply_text("No active tasks.")
        return

    # System status header
    running = task_manager.running_count()
    eff_w = task_manager._effective_workers
    header = f"Workers: {running}/{eff_w}"
    if eff_w < MAX_CONCURRENT_WORKERS:
        header += " (throttled)"

    lines = [header, ""]
    prio_labels = {TASK_PRIORITY_QUICK: "Q", TASK_PRIORITY_NORMAL: "N", TASK_PRIORITY_HEAVY: "H"}
    for i, t in enumerate(active_tasks, 1):
        if t.status == "running" and t.started_at:
            elapsed = int(time.time() - t.started_at)
        else:
            elapsed = int(time.time() - t.created_at)
        status_icon = "🟢" if t.status == "running" else "🟡"
        prio = prio_labels.get(t.priority, "?")
        tools = f" {t.tool_count}tools" if t.tool_count else ""
        lines.append(f"{i}. {status_icon} [{prio}]「{t.label}」{t.status} {elapsed}s{tools}")
    if agent:
        ag_elapsed = int(time.time() - agent.get("started", time.time()))
        lines.append(f"{len(active_tasks)+1}. 🔵 Agent {ag_elapsed}s: {agent.get('objective', '')[:30]}")
    await update.message.reply_text("\n".join(lines))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel — cancel running Claude operation or agent"""
    if not is_allowed(update.effective_chat.id):
        return
    chat_id_str = str(update.effective_chat.id)

    # Cancel agent if running
    running = agent_running.get(chat_id_str)
    if running:
        running["cancel"].set()

    # Check TaskManager for active tasks
    active_tasks = task_manager.get_user_active(chat_id_str)

    if len(active_tasks) == 1:
        # Single task — cancel directly
        t = active_tasks[0]
        _cancelled[t.task_id] = True
        task_manager.cancel(t.task_id)
        await update.message.reply_text(f"Cancelling「{t.label}」...")
    elif len(active_tasks) > 1:
        # Multiple tasks — show inline keyboard to pick
        buttons = [(f"「{t.label}」", f"cancel_task:{t.task_id}") for t in active_tasks]
        buttons.append(("Cancel all", "cancel_task:all"))
        kb = make_keyboard(buttons, columns=1)
        await update.message.reply_text("Which task to cancel?", reply_markup=kb)
    elif running:
        await update.message.reply_text("Stopping agent...")
    else:
        # Fallback: check legacy _active_procs (for agent_invoke)
        proc = _active_procs.get(chat_id_str)
        if proc:
            _cancelled[chat_id_str] = True
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await update.message.reply_text("Cancelling...")
        else:
            await update.message.reply_text("Nothing running.")


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


async def _watchdog_feeder():
    """Independent watchdog feeder — zero external dependencies.
    Separated from heartbeat so Uptime Kuma failures can't kill the bot."""
    global _watchdog_ts
    await asyncio.sleep(2)
    while True:
        _watchdog_ts = time.time()
        await asyncio.sleep(30)


async def _heartbeat_loop():
    """Background task: push heartbeat to Uptime Kuma every 120s."""
    import urllib.request
    _HB_URL = load_config().get("uptimeKumaPushUrl", "")
    await asyncio.sleep(5)
    while True:
        if _HB_URL:
            try:
                urllib.request.urlopen(_HB_URL, timeout=5)
            except Exception:
                pass
        await asyncio.sleep(120)


async def _poll_monitor():
    """Check updater health every 30s, update _poll_ts if alive."""
    global _poll_ts
    await asyncio.sleep(30)
    while True:
        try:
            if _app_ref and hasattr(_app_ref, 'updater') and _app_ref.updater:
                if _app_ref.updater.running:
                    _poll_ts = time.time()
                else:
                    log.error("Poll monitor: updater.running=False, forcing restart")
                    os._exit(1)
        except Exception:
            pass
        await asyncio.sleep(30)


def _start_watchdog_thread():
    """OS thread watchdog: kills process if event loop or polling dies.
    Three independent checks — any one stale triggers restart:
    1. _watchdog_ts: heartbeat loop (event loop alive)
    2. _poll_ts: poll monitor (updater.running property)
    3. _last_getupdate_ts: actual getUpdates HTTP calls (ground truth)
    """
    def _watchdog():
        while True:
            time.sleep(60)
            now = time.time()
            loop_stale = now - _watchdog_ts
            poll_stale = now - _poll_ts
            http_stale = now - _last_getupdate_ts
            if loop_stale > WATCHDOG_STALE_SEC:
                sys.stderr.write(f"[Watchdog] event loop frozen for {loop_stale:.0f}s, forcing restart\n")
                sys.stderr.flush()
                os._exit(1)
            if poll_stale > WATCHDOG_STALE_SEC:
                sys.stderr.write(f"[Watchdog] polling dead for {poll_stale:.0f}s, forcing restart\n")
                sys.stderr.flush()
                os._exit(1)
            if _last_getupdate_ts > 0 and http_stale > POLL_ACTIVITY_STALE_SEC:
                sys.stderr.write(f"[Watchdog] no getUpdates HTTP for {http_stale:.0f}s, forcing restart\n")
                sys.stderr.flush()
                os._exit(1)
    t = threading.Thread(target=_watchdog, daemon=True, name="watchdog")
    t.start()


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
                async with worker_semaphore:
                    result = _safe_result(await invoke_claude(
                        message=prompt,
                        project_path=project_row[0],
                        session_id=None,
                        model=model,
                        tool_profile="readonly",
                        effort=effort,
                    ))

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


# ── Auto Review (Memory Consolidation) ──

_AUTO_REVIEW_PROMPT = """自动记忆整理任务（系统定期执行，不需要回复用户）。

以下是最近的对话摘要：
{context}

请检查这些对话内容，如果有值得长期记住的信息，例如：
- 主人提到的偏好、习惯、计划
- 重要的决定或待办事项
- 主人的情绪状态或生活事件
- 需要后续跟进的事情

如果有值得记住的，请更新 memory/MEMORY.md（在「索引」部分之前添加条目）。
如果没有需要记住的内容，回复"无需更新"即可。

注意：只保存对未来对话有用的信息，不要保存闲聊内容。保持简洁。"""


async def _auto_review_session(chat_id: str, project: str, project_path: str) -> dict | None:
    """Run memory consolidation on recent context buffer entries."""
    key = f"{chat_id}:{project}"
    entries = _context_buffer.get(key, [])
    if not entries or len(entries) < 2:
        return None  # too few exchanges to review

    lines = []
    for e in entries:
        ts = e.get("ts", "")
        lines.append(f"[{ts}] 用户: {e.get('user', '')}")
        lines.append(f"  助手: {e.get('assistant', '')}")

    prompt = _AUTO_REVIEW_PROMPT.format(context="\n".join(lines))

    try:
        result = await invoke_claude(
            message=prompt,
            project_path=project_path,
            session_id=None,
            model="sonnet",
            tool_profile="default",
            effort="low",
            bypass_permissions=True,
        )
        cost = result.get("total_cost_usd", 0.0)
        log.info(f"auto-review completed for {project}: cost=${cost:.4f}")
        return result
    except Exception as e:
        log.error(f"auto-review failed for {project}: {e}")
        return None


async def _auto_review_loop(bot):
    """Background task: periodically consolidate memories for instances with autoReview enabled."""
    await asyncio.sleep(120)  # wait 2 min after startup
    while True:
        try:
            cfg = load_config()
            ar_cfg = cfg.get("autoReview", {})
            if not ar_cfg.get("enabled", False):
                await asyncio.sleep(3600)
                continue

            interval = ar_cfg.get("intervalHours", 6) * 3600

            for key, entries in list(_context_buffer.items()):
                if len(entries) < 2:
                    continue
                parts = key.split(":", 1)
                if len(parts) != 2:
                    continue
                chat_id, project = parts

                # Check last auto-review time
                ts_key = f"auto_review_ts:{key}"
                last = db.execute(
                    "SELECT value FROM settings WHERE key=?", (ts_key,)
                ).fetchone()

                if last:
                    from datetime import datetime, timezone
                    try:
                        last_dt = datetime.fromisoformat(last[0])
                        now = datetime.now(timezone.utc)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        if (now - last_dt).total_seconds() < interval:
                            continue
                    except (ValueError, TypeError):
                        pass

                project_row = db.execute(
                    "SELECT path FROM projects WHERE name=?", (project,)
                ).fetchone()
                if not project_row or not Path(project_row[0]).exists():
                    continue

                log.info(f"auto-review starting for {project} (periodic)")
                result = await _auto_review_session(chat_id, project, project_row[0])

                if result:
                    # Update last review timestamp
                    from datetime import datetime, timezone
                    now_iso = datetime.now(timezone.utc).isoformat()
                    db.execute(
                        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                        (ts_key, now_iso),
                    )
                    db.commit()

                    # Optionally notify user
                    if ar_cfg.get("notify", False):
                        text = result.get("result", "") or ""
                        if text and "无需更新" not in text:
                            cost = result.get("total_cost_usd", 0.0)
                            try:
                                await bot.send_message(
                                    chat_id=int(chat_id),
                                    text=f"🐾 记忆整理完成\n\n{text[:500]}\n\n`auto-review | ${cost:.4f}`",
                                    parse_mode="Markdown",
                                )
                            except Exception as e:
                                log.error(f"auto-review notify failed: {e}")

        except Exception as e:
            log.error(f"auto-review loop error: {e}", exc_info=True)

        await asyncio.sleep(3600)  # check every hour


# ── Error Handler ──

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    # NetworkError: transient proxy blips — already retried by RetryHTTPXRequest,
    # suppress to avoid spamming user with "Bot Error" on every hiccup
    if isinstance(context.error, NetworkError):
        log.warning(f"NetworkError (suppressed): {context.error}")
        return
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
        # ── 高频：核心工作流 ──
        BotCommand("p", "Projects"),
        BotCommand("new", "New session"),
        BotCommand("cancel", "Cancel running operation"),
        BotCommand("status", "Status & settings"),
        BotCommand("model", "Select model"),
        BotCommand("task", "Readonly analyze, then execute"),
        BotCommand("tasks", "Active tasks & queue"),
        # ── 中频：调优 ──
        BotCommand("think", "Opus + deep thinking"),
        BotCommand("effort", "Thinking depth"),
        BotCommand("tools", "Tool access"),
        BotCommand("cost", "Cost summary"),
        BotCommand("agent", "Autonomous agent loop"),
        # ── 低频：设置/监控 ──
        BotCommand("health", "System health"),
        BotCommand("budget", "Daily budget settings"),
        BotCommand("cron", "Scheduled tasks"),
        BotCommand("voice", "Voice reply settings"),
        BotCommand("el", "ElevenLabs account"),
        # ── 偶尔：Mac 控制/元操作 ──
        BotCommand("sleep", "Lock screen + lower volume"),
        BotCommand("work", "Open work apps"),
        BotCommand("cleanup", "Organize desktop"),
        BotCommand("restart", "Restart CB service"),
        BotCommand("help", "Help"),
        # ── gstack 技能（转发给 Claude Code）──
        BotCommand("office_hours", "gstack: YC 需求澄清"),
        BotCommand("plan_ceo_review", "gstack: CEO 产品评审"),
        BotCommand("plan_eng_review", "gstack: 架构评审"),
        BotCommand("review", "gstack: 代码审查"),
        BotCommand("ship", "gstack: 发 PR"),
        BotCommand("qa", "gstack: QA 测试"),
        BotCommand("retro", "gstack: 周回顾"),
        BotCommand("investigate", "gstack: 根因调查"),
        BotCommand("cso", "gstack: 安全审计"),
    ])
    # Start cron scheduler background task
    _create_background_task(_cron_scheduler(app.bot), name="cron-scheduler")
    # Start watchdog feeder (independent, zero-dependency)
    _create_background_task(_watchdog_feeder(), name="watchdog-feeder")
    # Start Uptime Kuma heartbeat
    _create_background_task(_heartbeat_loop(), name="uptime-kuma-heartbeat")
    # Start poll health monitor
    _create_background_task(_poll_monitor(), name="poll-monitor")
    # Start auto-review loop (memory consolidation, controlled by config.autoReview)
    _create_background_task(_auto_review_loop(app.bot), name="auto-review")
    # Auto-configure TTS from config.json if present
    _cfg = load_config()
    tts_cfg = _cfg.get("tts", {})
    if tts_cfg.get("provider") == "elevenlabs" and tts_cfg.get("auto") == "inbound":
        el_cfg = tts_cfg.get("elevenlabs", {})
        if el_cfg.get("voiceId"):
            set_setting("voice_enabled", "1")
            set_setting("voice_engine", "eleven")
            # Store custom voice ID for direct lookup
            set_setting("voice_custom_id", el_cfg["voiceId"])
            log.info(f"TTS auto-configured: ElevenLabs, voiceId={el_cfg['voiceId']}")
    # Auto-register default project from config.json if present
    default_proj = _cfg.get("defaultProject")
    if default_proj and default_proj.get("name") and default_proj.get("path"):
        existing = [p["name"] for p in list_projects()]
        if default_proj["name"] not in existing:
            db.execute(
                "INSERT OR REPLACE INTO projects (name, path, description) VALUES (?, ?, ?)",
                (default_proj["name"], default_proj["path"], default_proj.get("description", "")),
            )
            db.commit()
            log.info(f"Auto-registered default project: {default_proj['name']} → {default_proj['path']}")
    log.info("Claude Bridge started")


def main():
    global db, worker_semaphore, task_manager, _app_ref

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
    _load_context_buffers()
    worker_semaphore = asyncio.Semaphore(MAX_CONCURRENT_WORKERS)
    task_manager = TaskManager(MAX_CONCURRENT_PER_USER, MAX_TOTAL_TASKS)

    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(MAX_TOTAL_TASKS)  # P0: enable concurrent handler dispatch
        .request(RetryHTTPXRequest(connection_pool_size=48, pool_timeout=90.0,
                                connect_timeout=15.0, read_timeout=15.0, write_timeout=15.0,
                                proxy=proxy))
        .get_updates_request(RetryHTTPXRequest(connection_pool_size=8, pool_timeout=30.0,
                                               connect_timeout=15.0, read_timeout=15.0, write_timeout=15.0,
                                               proxy=proxy))
        .post_init(post_init)
        .build()
    )

    _app_ref = app
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
    app.add_handler(CommandHandler("el", cmd_el))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("agent", cmd_agent))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("cron", cmd_cron))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("sleep", cmd_sleep))
    app.add_handler(CommandHandler("work", cmd_work))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # ── 万能兜底：未注册的 /command 转发给 Claude Code（gstack 等技能）──
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))

    _start_watchdog_thread()

    webhook_url = cfg.get("webhookUrl")
    if webhook_url:
        webhook_port = int(cfg.get("webhookPort", 8443))
        webhook_secret = cfg.get("webhookSecret", "")
        log.info(f"Starting webhook mode on port {webhook_port}, url={webhook_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=webhook_port,
            url_path=token,
            webhook_url=f"{webhook_url}/{token}",
            secret_token=webhook_secret,
            drop_pending_updates=True,
        )
    else:
        log.info(f"Starting polling mode, proxy={proxy}, allowed={cfg.get('allowFrom', [])}")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True,
                         bootstrap_retries=-1)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        # python-telegram-bot shutdown race: event loop destroyed while
        # bootstrap_retries sleep is pending. Safe to exit cleanly —
        # launchd KeepAlive will restart the process.
        import sys
        sys.stderr.write(f"[CB] clean exit on RuntimeError during shutdown: {e}\n")
        sys.stderr.flush()
        sys.exit(0)
