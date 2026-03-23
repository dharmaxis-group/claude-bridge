#!/usr/bin/env python3
"""GitHub Repo Watcher — 定期检查指定 repo 是否有代码发布，通过 Telegram 通知。

用法：python3 github-watch.py [--check-only]
  --check-only  只输出状态，不发 Telegram 通知（调试用）

配置：WATCHLIST 列表定义监控目标，每项包含 repo、描述、检测条件。
状态持久化在 ~/.claude-bridge/data/github-watch-state.json
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# ── 配置 ──

CB_HOME = Path.home() / ".claude-bridge"
STATE_FILE = CB_HOME / "data" / "github-watch-state.json"
CONFIG_FILE = CB_HOME / "config.json"

WATCHLIST = [
    {
        "repo": "EverMind-AI/MSA",
        "description": "Memory Sparse Attention — 100M token 端到端稀疏注意力",
        "detect": "code_released",  # 检测 src/ 或 code/ 目录，或有 release
    },
]

# ── 工具函数 ──

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

def gh_api(endpoint):
    """调用 gh api，返回 JSON 或 None"""
    try:
        result = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "NO_COLOR": "1"}
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return None

def check_repo(repo_info):
    """检查 repo 状态，返回 (changed: bool, details: str)"""
    repo = repo_info["repo"]

    # 获取 repo 基本信息
    info = gh_api(f"repos/{repo}")
    if not info:
        return False, f"无法访问 {repo}"

    pushed_at = info.get("pushed_at", "unknown")
    stars = info.get("stargazers_count", 0)

    # 检查是否有代码目录（src/, code/, model/, scripts/ 等）
    contents = gh_api(f"repos/{repo}/contents")
    code_dirs = []
    code_files = []
    if contents and isinstance(contents, list):
        for item in contents:
            name = item.get("name", "")
            item_type = item.get("type", "")
            if item_type == "dir" and name.lower() in (
                "src", "code", "model", "models", "scripts",
                "msa", "lib", "examples", "training", "inference"
            ):
                code_dirs.append(name)
            if item_type == "file" and name.lower().endswith((".py", ".sh", ".yaml", ".yml")):
                if name.lower() not in ("readme.md",):
                    code_files.append(name)

    # 检查 releases
    releases = gh_api(f"repos/{repo}/releases")
    has_release = bool(releases and len(releases) > 0)
    latest_release = releases[0]["tag_name"] if has_release else None

    # 判断是否有代码
    has_code = bool(code_dirs) or bool(code_files) or has_release

    details = {
        "pushed_at": pushed_at,
        "stars": stars,
        "code_dirs": code_dirs,
        "code_files": code_files,
        "has_release": has_release,
        "latest_release": latest_release,
        "has_code": has_code,
    }

    return has_code, details

def send_telegram(message):
    """通过 CB 的 bot token 发送 Telegram 通知"""
    config = json.loads(CONFIG_FILE.read_text())

    # 解析 bot token（支持 ! 前缀的 keychain 命令）
    token_raw = config.get("botToken", "")
    if token_raw.startswith("!"):
        cmd = token_raw[1:]
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        token = result.stdout.strip()
    else:
        token = token_raw

    chat_id = config.get("allowFrom", [""])[0]
    if not token or not chat_id:
        print("缺少 bot token 或 chat_id，跳过通知")
        return False

    proxy = config.get("proxy", "")

    # 用 curl 发送，避免依赖 python-telegram-bot
    curl_cmd = [
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{token}/sendMessage",
        "-d", f"chat_id={chat_id}",
        "-d", f"text={message}",
        "-d", "parse_mode=Markdown",
    ]
    if proxy:
        curl_cmd.extend(["--proxy", proxy])

    try:
        result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=15)
        resp = json.loads(result.stdout)
        return resp.get("ok", False)
    except Exception as e:
        print(f"发送失败: {e}")
        return False

# ── 主逻辑 ──

def main():
    check_only = "--check-only" in sys.argv

    state = load_state()
    notifications = []

    for item in WATCHLIST:
        repo = item["repo"]
        prev = state.get(repo, {})
        prev_has_code = prev.get("has_code", False)

        has_code, details = check_repo(item)

        if isinstance(details, str):
            # 错误信息
            print(f"[WARN] {repo}: {details}")
            continue

        print(f"[{repo}] stars={details['stars']} pushed={details['pushed_at']} "
              f"code_dirs={details['code_dirs']} code_files={details['code_files']} "
              f"release={details['latest_release']} has_code={has_code}")

        # 状态变化：从无代码→有代码
        if has_code and not prev_has_code:
            msg = (
                f"*GitHub Watch*: `{repo}` 代码已发布\n"
                f"描述: {item['description']}\n"
                f"目录: {', '.join(details['code_dirs']) or '无'}\n"
                f"文件: {', '.join(details['code_files'][:5]) or '无'}\n"
                f"Release: {details['latest_release'] or '无'}\n"
                f"Stars: {details['stars']}\n"
                f"链接: https://github.com/{repo}"
            )
            notifications.append(msg)

        # 更新状态
        state[repo] = {
            "has_code": has_code,
            "pushed_at": details["pushed_at"],
            "stars": details["stars"],
            "latest_release": details.get("latest_release"),
            "checked_at": subprocess.run(
                ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                capture_output=True, text=True
            ).stdout.strip(),
        }

    save_state(state)

    if check_only:
        print(f"\n检查完成，{len(notifications)} 条通知待发送（check-only 模式，不发送）")
        for n in notifications:
            print(n)
        return

    for msg in notifications:
        ok = send_telegram(msg)
        print(f"通知{'成功' if ok else '失败'}: {msg[:60]}...")

    if not notifications:
        print("无新变化，不发送通知")

if __name__ == "__main__":
    main()
