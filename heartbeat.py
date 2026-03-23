#!/usr/bin/env python3
"""Claude Bridge Heartbeat — 统一定时任务调度器

KeepAlive 守护进程，每分钟检查任务表，到期任务自动执行，
结果通过 Telegram Bot 推送。与 CB 主进程独立运行。

用法：
  python3 heartbeat.py           # 启动守护进程
  python3 heartbeat.py --test    # 跑一次所有启用的任务（调试）
  python3 heartbeat.py --status  # 查看任务状态
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CB_HOME = Path.home() / ".claude-bridge"
JOBS_FILE = CB_HOME / "heartbeat-jobs.json"
STATE_FILE = CB_HOME / "data" / "heartbeat-state.json"
LOG_FILE = CB_HOME / "logs" / "heartbeat.log"
CONFIG_FILE = CB_HOME / "config.json"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("heartbeat")


# ── Cron 解析 ──

def _field_matches(field: str, value: int) -> bool:
    """Check if a single cron field matches a value."""
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            range_part, step = part.split("/", 1)
            step = int(step)
            if range_part == "*":
                if value % step == 0:
                    return True
            elif "-" in range_part:
                lo, hi = map(int, range_part.split("-", 1))
                if lo <= value <= hi and (value - lo) % step == 0:
                    return True
        elif "-" in part:
            lo, hi = map(int, part.split("-", 1))
            if lo <= value <= hi:
                return True
        else:
            if int(part) == value:
                return True
    return False


def cron_match(expr: str, dt: datetime) -> bool:
    """Check if datetime matches 5-field cron expression (min hour dom month dow)."""
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    # dow: cron uses 0=Sun, Python isoweekday() returns 1=Mon..7=Sun
    dow = dt.isoweekday() % 7  # convert to 0=Sun
    return (
        _field_matches(fields[0], dt.minute)
        and _field_matches(fields[1], dt.hour)
        and _field_matches(fields[2], dt.day)
        and _field_matches(fields[3], dt.month)
        and _field_matches(fields[4], dow)
    )


# ── 状态管理 ──

def load_jobs() -> list[dict]:
    if not JOBS_FILE.exists():
        return []
    try:
        return json.loads(JOBS_FILE.read_text()).get("jobs", [])
    except (json.JSONDecodeError, KeyError):
        log.error(f"Invalid jobs file: {JOBS_FILE}")
        return []


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── Telegram 通知 ──

def send_telegram(message: str) -> bool:
    """Send notification via CB's bot token."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        token_raw = cfg.get("botToken", "")
        if token_raw.startswith("!"):
            r = subprocess.run(token_raw[1:], shell=True, capture_output=True, text=True, timeout=10)
            token = r.stdout.strip()
        else:
            token = token_raw

        chat_id = cfg.get("allowFrom", [""])[0]
        if not token or not chat_id:
            return False

        proxy = cfg.get("proxy", "")
        curl = [
            "curl", "-s", "-X", "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            "-d", f"chat_id={chat_id}",
            "-d", f"text={message}",
            "-d", "parse_mode=Markdown",
        ]
        if proxy:
            curl.extend(["--proxy", proxy])

        r = subprocess.run(curl, capture_output=True, text=True, timeout=15)
        return json.loads(r.stdout).get("ok", False)
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


# ── 任务执行 ──

def run_job(job: dict) -> dict:
    """Execute a job, return {success, stdout, stderr, returncode}."""
    job_type = job.get("type", "script")
    timeout = job.get("timeout", 300)
    env = {**os.environ, "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:" + os.environ.get("PATH", "")}

    if job_type == "script":
        cmd = os.path.expanduser(job["command"])
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
            return {"success": r.returncode == 0, "stdout": r.stdout[-3000:], "stderr": r.stderr[-1000:], "returncode": r.returncode}
        except subprocess.TimeoutExpired:
            return {"success": False, "stdout": "", "stderr": "Timeout", "returncode": -1}

    elif job_type == "claude":
        claude_bin = os.path.expanduser("~/.local/bin/claude")
        project = os.path.expanduser(job.get("project", "~/.claude-bridge"))
        env.pop("CLAUDECODE", None)
        try:
            r = subprocess.run(
                [claude_bin, "-p", "--output-format", "text",
                 "--model", job.get("model", "sonnet"), "--project", project],
                input=job["prompt"], capture_output=True, text=True, timeout=timeout, env=env,
            )
            return {"success": r.returncode == 0, "stdout": r.stdout[-3000:], "stderr": r.stderr[-500:], "returncode": r.returncode}
        except subprocess.TimeoutExpired:
            return {"success": False, "stdout": "", "stderr": "Timeout", "returncode": -1}

    return {"success": False, "stdout": "", "stderr": f"Unknown type: {job_type}", "returncode": -1}


def notify_result(job: dict, result: dict):
    """Send notification based on job config."""
    notify_always = job.get("notify", False)
    notify_error = job.get("notify_on_error", True)

    if result["success"] and not notify_always:
        return
    if not result["success"] and not notify_error:
        return

    name = job.get("name", job["id"])
    if result["success"]:
        output = result["stdout"].strip()
        # For script jobs that handle their own notifications, skip empty output
        if not output and job.get("self_notify", False):
            return
        msg = f"*Heartbeat | {name}*\n{output}" if output else f"*Heartbeat | {name}* done"
    else:
        msg = f"*Heartbeat | {name}* FAILED\nExit: {result['returncode']}\n```\n{result['stderr'][:500]}\n```"

    send_telegram(msg[:4000])


# ── 主循环 ──

def main_loop():
    log.info("Heartbeat daemon started")
    last_alive = time.time()

    while True:
        now = datetime.now()
        jobs = load_jobs()
        state = load_state()

        for job in jobs:
            if not job.get("enabled", True):
                continue

            jid = job["id"]
            schedule = job.get("schedule", "")
            if not schedule or not cron_match(schedule, now):
                continue

            # 同一分钟不重复执行
            current_min = now.strftime("%Y-%m-%d %H:%M")
            if state.get(jid, {}).get("last_min") == current_min:
                continue

            log.info(f"Running: {jid}")
            state.setdefault(jid, {})["last_min"] = current_min
            state[jid]["last_run"] = now.isoformat()
            state[jid]["status"] = "running"
            save_state(state)

            result = run_job(job)

            state[jid]["status"] = "ok" if result["success"] else "error"
            state[jid]["exit_code"] = result["returncode"]
            save_state(state)

            log.info(f"Done: {jid} -> {'ok' if result['success'] else 'error'}")
            notify_result(job, result)

        # 自我存活检测：5 分钟无迭代则退出（KeepAlive 会重启）
        last_alive = time.time()

        # 睡到下一分钟
        sleep_sec = 60 - datetime.now().second
        time.sleep(max(sleep_sec, 1))


# ── CLI 入口 ──

def main():
    if "--status" in sys.argv:
        state = load_state()
        jobs = load_jobs()
        for job in jobs:
            jid = job["id"]
            s = state.get(jid, {})
            enabled = "ON" if job.get("enabled", True) else "OFF"
            status = s.get("status", "never")
            last = s.get("last_run", "never")
            print(f"[{enabled}] {jid}: {status} (last: {last})")
        return

    if "--test" in sys.argv:
        jobs = load_jobs()
        for job in jobs:
            if not job.get("enabled", True):
                continue
            print(f"Testing: {job['id']} ({job.get('name', '')})")
            result = run_job(job)
            print(f"  OK={result['success']} exit={result['returncode']}")
            if result["stdout"]:
                print(f"  stdout: {result['stdout'][:300]}")
            if result["stderr"]:
                print(f"  stderr: {result['stderr'][:200]}")
        return

    main_loop()


if __name__ == "__main__":
    main()
