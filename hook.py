#!/usr/bin/env python3
"""Claude Code hook for Slack approval bridge.

Subcommands:
  approval       -- PreToolUse: ask Slack to approve a tool call
  session-start  -- SessionStart: register this session with the bridge
  session-end    -- SessionEnd: unregister this session

Spawns the bridge process if it isn't already running. Uses only stdlib
so it can run from any Python; the bridge runs from a dedicated venv.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
BRIDGE_SCRIPT = HERE / "slack_bridge.py"
VENV_PYTHON = HERE / ".venv" / "bin" / "python"
LOG_FILE = HERE / "bridge.log"

PORT = int(os.environ.get("CLAUDE_SLACK_BRIDGE_PORT", "3737"))
BRIDGE_URL = f"http://127.0.0.1:{PORT}"
SPAWN_TIMEOUT = 15.0
APPROVAL_WAIT_TIMEOUT = 60
APPROVAL_POLL_INTERVAL = 5

def _read_dotenv() -> dict:
    """Tiny .env parser (stdlib only). Returns {} on any error."""
    env_path = HERE / ".env"
    if not env_path.exists():
        return {}
    result = {}
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        return {}
    return result


def _slack_enabled() -> bool:
    """On/off switch. Reads TO_SLACK from .env (next to this script).

    Default (missing/empty) is enabled. Values off/0/false/no disable.
    The TO_SLACK environment variable overrides .env when explicitly set.
    """
    val = os.environ.get("TO_SLACK")
    if val is None or val == "":
        val = _read_dotenv().get("TO_SLACK", "on")
    return val.strip().lower() not in ("off", "0", "false", "no")


HARD_DENY_BASH = [
    "rm -rf",
    "sudo",
    "curl ",
    "wget ",
    "ssh ",
    "scp ",
    "chmod ",
    "chown ",
    "mkfs",
    "dd if=",
    ".env",
    "id_rsa",
]
HARD_DENY_READ = [".env", "id_rsa"]


def _http_get(path: str, timeout: float = 2.0) -> dict:
    req = urllib.request.Request(f"{BRIDGE_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post(path: str, payload: dict, timeout: float = 5.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BRIDGE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _is_bridge_ready() -> bool:
    try:
        _http_get("/health", timeout=0.4)
        return True
    except (urllib.error.URLError, socket.timeout, ConnectionError):
        return False


def _spawn_bridge() -> bool:
    python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    try:
        log = open(LOG_FILE, "a")
        subprocess.Popen(
            [python, str(BRIDGE_SCRIPT)],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(HERE),
        )
    except Exception as e:
        print(f"[hook] failed to spawn bridge: {e}", file=sys.stderr)
        return False

    deadline = time.time() + SPAWN_TIMEOUT
    while time.time() < deadline:
        if _is_bridge_ready():
            return True
        time.sleep(0.2)
    return False


def _ensure_bridge() -> bool:
    if _is_bridge_ready():
        return True
    return _spawn_bridge()


def _block(reason: str):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _allow(reason: str = "Slackで許可されました"):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _ask(reason: str):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _is_safe_to_auto_allow(tool_name: str, tool_input: dict, cwd: str) -> bool:
    if tool_name == "Read":
        file_path = str(tool_input.get("file_path", "")).lower()
        if any(x in file_path for x in HARD_DENY_READ):
            return False
        return True
    if tool_name in ("Write", "Edit", "MultiEdit"):
        file_path = str(tool_input.get("file_path", ""))
        if not file_path:
            return False
        try:
            abs_path = os.path.realpath(file_path)
            abs_cwd = os.path.realpath(cwd)
        except Exception:
            return False
        return abs_path == abs_cwd or abs_path.startswith(abs_cwd + os.sep)
    return False


def _hard_deny(tool_name: str, tool_input: dict):
    if tool_name == "Bash":
        command = str(tool_input.get("command", "")).lower()
        for needle in HARD_DENY_BASH:
            if needle in command:
                _block(f"危険なBashコマンドのため即時ブロックしました: {tool_input.get('command', '')}")
    if tool_name == "Read":
        file_path = str(tool_input.get("file_path", "")).lower()
        for needle in HARD_DENY_READ:
            if needle in file_path:
                _block(f"機密ファイル読取のため即時ブロックしました: {tool_input.get('file_path', '')}")


def cmd_approval(hook_input: dict):
    if not _slack_enabled():
        _allow()

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    session_id = hook_input.get("session_id", "")
    cwd = hook_input.get("cwd") or os.getcwd()

    _hard_deny(tool_name, tool_input)
    if _is_safe_to_auto_allow(tool_name, tool_input, cwd):
        _allow("自動承認(安全と判定)")

    if not _ensure_bridge():
        _block("Slack承認サーバー(bridge)を起動できませんでした。bridge.log を確認してください。")

    try:
        res = _http_post("/approval/request", {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": cwd,
            "session_id": session_id,
        })
        request_id = res["request_id"]
    except Exception as e:
        _block(f"承認リクエストに失敗しました: {e}")

    deadline = time.time() + APPROVAL_WAIT_TIMEOUT
    while time.time() < deadline:
        try:
            status = _http_get(f"/approval/status/{request_id}")
        except Exception:
            time.sleep(APPROVAL_POLL_INTERVAL)
            continue
        if status.get("status") == "approved":
            _allow()
        if status.get("status") == "denied":
            _block(status.get("reason") or "Slackで拒否されました")
        time.sleep(APPROVAL_POLL_INTERVAL)

    try:
        _http_post("/approval/abandon", {"request_id": request_id})
    except Exception:
        pass
    _ask(f"Slackで{APPROVAL_WAIT_TIMEOUT}秒以内に応答が無かったためIDEで承認してください")


def cmd_session_start(hook_input: dict):
    if not _slack_enabled():
        sys.exit(0)
    session_id = hook_input.get("session_id", "")
    if not session_id:
        sys.exit(0)
    if not _ensure_bridge():
        print("[hook] bridge unavailable, session-start skipped", file=sys.stderr)
        sys.exit(0)
    try:
        _http_post("/session/register", {"session_id": session_id})
    except Exception as e:
        print(f"[hook] session-start register failed: {e}", file=sys.stderr)
    sys.exit(0)


def cmd_session_end(hook_input: dict):
    if not _slack_enabled():
        sys.exit(0)
    session_id = hook_input.get("session_id", "")
    if not session_id or not _is_bridge_ready():
        sys.exit(0)
    try:
        _http_post("/session/unregister", {"session_id": session_id})
    except Exception as e:
        print(f"[hook] session-end unregister failed: {e}", file=sys.stderr)
    sys.exit(0)


def cmd_stop(hook_input: dict):
    """Stop hook: end the current Slack thread so the next turn starts a new
    root message (with a fresh mention). The session stays registered."""
    if not _slack_enabled():
        sys.exit(0)
    session_id = hook_input.get("session_id", "")
    if not session_id or not _is_bridge_ready():
        sys.exit(0)
    try:
        _http_post("/thread/reset", {"session_id": session_id})
    except Exception as e:
        print(f"[hook] stop thread-reset failed: {e}", file=sys.stderr)
    sys.exit(0)


def main():
    if len(sys.argv) < 2:
        print("usage: hook.py {approval|session-start|session-end|stop}", file=sys.stderr)
        sys.exit(2)

    subcommand = sys.argv[1]
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        hook_input = {}

    if subcommand == "approval":
        cmd_approval(hook_input)
    elif subcommand == "session-start":
        cmd_session_start(hook_input)
    elif subcommand == "session-end":
        cmd_session_end(hook_input)
    elif subcommand == "stop":
        cmd_stop(hook_input)
    else:
        print(f"unknown subcommand: {subcommand}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
