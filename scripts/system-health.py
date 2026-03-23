#!/usr/bin/env python3
"""系统健康检查 — 输出简洁的 Mac 状态报告"""

import subprocess
import shutil
import os


def run(cmd: str) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return ""


def main():
    lines = []

    # CPU load
    load = run("sysctl -n vm.loadavg").strip("{ }")
    lines.append(f"CPU Load: {load}")

    # Memory
    mem_info = run("vm_stat")
    if mem_info:
        pages = {}
        for line in mem_info.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                v = v.strip().rstrip(".")
                try:
                    pages[k.strip()] = int(v)
                except ValueError:
                    pass
        page_size = 16384  # Apple Silicon
        free = pages.get("Pages free", 0) * page_size
        active = pages.get("Pages active", 0) * page_size
        inactive = pages.get("Pages inactive", 0) * page_size
        wired = pages.get("Pages wired down", 0) * page_size
        used_gb = (active + wired) / (1024**3)
        total_gb = (free + active + inactive + wired) / (1024**3)
        lines.append(f"Memory: {used_gb:.1f}G / {total_gb:.1f}G used")

    # Disk
    total, used, free = shutil.disk_usage("/")
    pct = used / total * 100
    lines.append(f"Disk: {used/(1024**3):.0f}G / {total/(1024**3):.0f}G ({pct:.0f}%)")

    # Battery
    batt = run("pmset -g batt")
    if "InternalBattery" in batt:
        for line in batt.splitlines():
            if "InternalBattery" in line:
                parts = line.split("\t")
                if len(parts) >= 2:
                    lines.append(f"Battery: {parts[1].strip()}")
                break
    elif "AC Power" in batt:
        lines.append("Battery: AC Power (desktop)")

    # Uptime
    uptime = run("uptime | sed 's/.*up /up /' | sed 's/,.*//'")
    lines.append(f"Uptime: {uptime}")

    # Key processes
    processes = {
        "CB": "claude-bridge.py",
        "Heartbeat": "heartbeat.py",
        "mihomo": "mihomo",
    }
    proc_status = []
    for name, pattern in processes.items():
        pid = run(f"pgrep -f '{pattern}' | head -1")
        proc_status.append(f"{name}:{'OK' if pid else 'DOWN'}")
    lines.append(f"Services: {' | '.join(proc_status)}")

    # Network (proxy check)
    proxy_ok = run("curl -s --proxy http://127.0.0.1:1082 --max-time 5 -o /dev/null -w '%{http_code}' https://api.telegram.org")
    lines.append(f"Proxy: {'OK' if proxy_ok == '200' else 'DOWN'}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
