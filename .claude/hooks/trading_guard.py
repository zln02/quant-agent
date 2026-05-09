#!/usr/bin/env python3
"""PreToolUse trading guard — block live trading commands.

Reads PreToolUse JSON from stdin. Exit 0 = allow, exit 2 = deny
(per Anthropic PreToolUse spec). Stdlib only.
"""
import json
import re
import sys

RISKY_PATTERNS = ["place_order", "execute_trade", "withdraw", "transfer", "sell_all"]
RISKY_FLAGS = ["--live", "--real"]
WHITELIST = ["--dry-run", "--simulate", "--paper", "--backtest", "pytest"]
READ_ONLY_CMDS = {"grep", "find", "cat", "less", "head", "tail", "wc", "ls", "file"}

RISKY_PATTERN_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in RISKY_PATTERNS) + r")\b"
)


def first_command(cmd):
    parts = cmd.strip().split(None, 1)
    if not parts:
        return ""
    return parts[0].rsplit("/", 1)[-1]


def find_match(command):
    m = RISKY_PATTERN_RE.search(command)
    if m:
        return m.group(1)
    for flag in RISKY_FLAGS:
        if flag in command:
            return flag
    return None


def find_whitelist(command):
    for w in WHITELIST:
        if w in command:
            return w
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    if data.get("tool_name") != "Bash":
        return 0

    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return 0

    if first_command(command) in READ_ONLY_CMDS:
        return 0

    matched = find_match(command)
    if not matched:
        return 0

    if find_whitelist(command):
        return 0

    sys.stderr.write(
        f"🚫 BLOCKED: 트레이딩 위험 패턴 감지: {matched}\n"
        f"  명령: {command}\n"
        f"  우회: --dry-run / --simulate / --backtest 추가 또는 hook 비활성화\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
