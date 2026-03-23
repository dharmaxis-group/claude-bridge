#!/usr/bin/env python3
"""CB Watchdog — 检测 claude-bridge.py 进程是否存活且日志有活动。

由 heartbeat 每 5 分钟调用。检测到异常时：
1. 尝试 launchctl kickstart 重启
2. 通过 Telegram 通知用户
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

CB_HOME = Path.home() / ".claude-bridge"
LOG_FILE = CB_HOME / "logs" / "claude-bridge.log"
CONFIG_FILE = CB_HOME / "config.json"
LABEL = "ai.claude-bridge"
# 如果日志超过 10 分钟没有新写入，认为 CB 可能僵死
STALE_THRESHOLD = 600


def send_telegram(message: str):
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        token_raw = cfg.get("botToken", "")
        if token_raw.startswith("!"):
            r = subprocess.run(token_raw[1:], shell=True, capture_output=True, text=True, timeout=10)
            token = r.stdout.strip()
        else:
            token = token_raw
        chat_id = cfg.get("allowFrom", [""])[0]
        proxy = cfg.get("proxy", "")
        if not token or not chat_id:
            return
        curl = [
            "curl", "-s", "-X", "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            "-d", f"chat_id={chat_id}",
            "-d", f"text={message}",
        ]
        if proxy:
            curl.extend(["--proxy", proxy])
        subprocess.run(curl, capture_output=True, text=True, timeout=15)
    except Exception:
        pass


def main():
    # 检查进程是否存在
    r = subprocess.run(
        ["pgrep", "-f", "claude-bridge.py"],
        capture_output=True, text=True
    )
    pids = [p for p in r.stdout.strip().split("\n") if p]

    if not pids:
        print("CB process not found, attempting restart")
        send_telegram("CB Watchdog: claude-bridge.py 进程不存在，正在重启...")
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"],
            capture_output=True, text=True
        )
        time.sleep(3)
        # 验证重启
        r2 = subprocess.run(["pgrep", "-f", "claude-bridge.py"], capture_output=True, text=True)
        if r2.stdout.strip():
            send_telegram("CB Watchdog: 重启成功")
        else:
            send_telegram("CB Watchdog: 重启失败，需要手动检查")
        sys.exit(1)

    # 检查日志活跃度
    if LOG_FILE.exists():
        mtime = LOG_FILE.stat().st_mtime
        stale = time.time() - mtime
        if stale > STALE_THRESHOLD:
            print(f"CB log stale for {stale:.0f}s, may be frozen")
            send_telegram(f"CB Watchdog: 日志 {stale/60:.0f} 分钟无更新，可能僵死，尝试重启...")
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"],
                capture_output=True, text=True
            )
            sys.exit(1)

    print(f"CB OK: pid={pids[0]}")

    # Uptime Kuma push 心跳
    try:
        subprocess.run(
            ["curl", "-s", "--connect-timeout", "5",
             "http://<REDACTED_HOST>:3001/api/push/<REDACTED_TOKEN>?status=up&msg=OK&ping="],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
