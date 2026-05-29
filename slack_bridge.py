#!/usr/bin/env python3
"""Slack approval bridge daemon.

Holds an outbound Socket Mode WebSocket to Slack and exposes a localhost
HTTP API for the Claude Code hook to (a) submit approval requests and
(b) register/unregister sessions. Auto-exits after IDLE_TIMEOUT seconds
of having zero active sessions.
"""

import json
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_USER_ID = os.environ.get("SLACK_USER_ID", "")
ALLOWED_SLACK_USER_IDS = {
    x.strip()
    for x in os.environ.get("ALLOWED_SLACK_USER_IDS", SLACK_USER_ID).split(",")
    if x.strip()
}

PORT = int(os.environ.get("CLAUDE_SLACK_BRIDGE_PORT", "3737"))
IDLE_TIMEOUT = int(os.environ.get("CLAUDE_SLACK_IDLE_TIMEOUT", "300"))
APPROVAL_TTL = int(os.environ.get("CLAUDE_SLACK_APPROVAL_TTL", "300"))

APPROVALS: dict[str, dict] = {}
ACTIVE_SESSIONS: set[str] = set()
# session_id -> {"channel": str, "ts": str} : the first (root) message per session.
# The root message mentions the user; everything else threads under it.
SESSION_THREADS: dict[str, dict] = {}
LAST_EMPTY_AT: float | None = time.time()
STATE_LOCK = threading.Lock()


def _require_env():
    missing = [
        name
        for name, value in [
            ("SLACK_BOT_TOKEN", SLACK_BOT_TOKEN),
            ("SLACK_APP_TOKEN", SLACK_APP_TOKEN),
            ("SLACK_USER_ID", SLACK_USER_ID),
        ]
        if not value
    ]
    if missing:
        print(f"[bridge] missing env: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def _bind_singleton_port() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        s.bind(("127.0.0.1", PORT))
        return s
    except OSError:
        return None


slack_app = App(token=SLACK_BOT_TOKEN)
flask_app = Flask(__name__)


def _summarize(tool_name: str, tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return str(tool_input)[:1000]
    if tool_name == "Bash":
        return f"Command: `{tool_input.get('command', '')}`"
    if tool_name in ("Write", "Edit", "MultiEdit"):
        return f"File: `{tool_input.get('file_path', '')}`"
    if tool_name == "Read":
        return f"File: `{tool_input.get('file_path', '')}`"
    return json.dumps(tool_input, ensure_ascii=False, indent=2)[:1000]


def _approval_blocks(request_id: str, tool_name: str, tool_input: dict, cwd: str, mention: bool = True) -> tuple[str, list]:
    summary = _summarize(tool_name, tool_input)
    prefix = f"<@{SLACK_USER_ID}>\n" if (mention and SLACK_USER_ID) else ""
    text = (
        f"{prefix}"
        "*Claudeが操作許可を求めています*\n"
        f"*Tool:* `{tool_name}`\n"
        f"*CWD:* `{cwd}`\n"
        f"*内容:*\n{summary}"
    )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
        {
            "type": "actions",
            "block_id": f"approval_{request_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "許可する"},
                    "style": "primary",
                    "value": request_id,
                    "action_id": "approve_tool",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "拒否する"},
                    "style": "danger",
                    "value": request_id,
                    "action_id": "deny_tool",
                },
            ],
        },
    ]
    return text, blocks


def _mark_active():
    global LAST_EMPTY_AT
    with STATE_LOCK:
        LAST_EMPTY_AT = None


def _mark_empty_if_done():
    global LAST_EMPTY_AT
    with STATE_LOCK:
        if not ACTIVE_SESSIONS:
            LAST_EMPTY_AT = time.time()


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "active_sessions": len(ACTIVE_SESSIONS),
        "pending_approvals": sum(1 for a in APPROVALS.values() if a["status"] == "pending"),
    })


@flask_app.route("/session/register", methods=["POST"])
def session_register():
    payload = request.get_json(force=True, silent=True) or {}
    session_id = str(payload.get("session_id", "")).strip()
    if not session_id:
        return jsonify({"error": "session_id required"}), 400
    with STATE_LOCK:
        ACTIVE_SESSIONS.add(session_id)
    _mark_active()
    return jsonify({"active_sessions": len(ACTIVE_SESSIONS)})


@flask_app.route("/session/unregister", methods=["POST"])
def session_unregister():
    payload = request.get_json(force=True, silent=True) or {}
    session_id = str(payload.get("session_id", "")).strip()
    with STATE_LOCK:
        ACTIVE_SESSIONS.discard(session_id)
        SESSION_THREADS.pop(session_id, None)
    _mark_empty_if_done()
    return jsonify({"active_sessions": len(ACTIVE_SESSIONS)})


@flask_app.route("/thread/reset", methods=["POST"])
def thread_reset():
    payload = request.get_json(force=True, silent=True) or {}
    session_id = str(payload.get("session_id", "")).strip()
    with STATE_LOCK:
        existed = SESSION_THREADS.pop(session_id, None) is not None
    return jsonify({"reset": existed})


@flask_app.route("/approval/request", methods=["POST"])
def approval_request():
    payload = request.get_json(force=True)
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    cwd = payload.get("cwd", "")
    session_id = payload.get("session_id", "")

    request_id = str(uuid.uuid4())
    APPROVALS[request_id] = {
        "status": "pending",
        "created_at": time.time(),
        "session_id": session_id,
    }
    _mark_active()

    with STATE_LOCK:
        root = SESSION_THREADS.get(session_id)
    is_first = root is None
    text, blocks = _approval_blocks(request_id, tool_name, tool_input, cwd, mention=is_first)

    post_kwargs = {
        "channel": root["channel"] if root else SLACK_USER_ID,
        "text": text[:2900],
        "blocks": blocks,
    }
    if root:
        post_kwargs["thread_ts"] = root["ts"]

    try:
        result = slack_app.client.chat_postMessage(**post_kwargs)
        channel = result.get("channel")
        ts = result.get("ts")
        APPROVALS[request_id]["channel"] = channel
        APPROVALS[request_id]["ts"] = ts
        APPROVALS[request_id]["original_text"] = text
        if is_first and session_id:
            root = {"channel": channel, "ts": ts}
            with STATE_LOCK:
                SESSION_THREADS[session_id] = root
        # Decisions reply into the session's root thread (or this message itself).
        thread = root or {"channel": channel, "ts": ts}
        APPROVALS[request_id]["thread_channel"] = thread["channel"]
        APPROVALS[request_id]["thread_ts"] = thread["ts"]
    except Exception as e:
        APPROVALS[request_id]["status"] = "denied"
        APPROVALS[request_id]["reason"] = f"Slack投稿に失敗: {e}"
        return jsonify({"request_id": request_id, "status": "denied", "reason": str(e)}), 502

    return jsonify({"request_id": request_id, "status": "pending"})


@flask_app.route("/approval/abandon", methods=["POST"])
def approval_abandon():
    payload = request.get_json(force=True, silent=True) or {}
    request_id = str(payload.get("request_id", "")).strip()
    item = APPROVALS.get(request_id)
    if not item:
        return jsonify({"status": "unknown"}), 404
    if item["status"] != "pending":
        return jsonify({"status": item["status"]})
    item["status"] = "abandoned"
    channel = item.get("channel")
    ts = item.get("ts")
    if channel and ts:
        original = item.get("original_text", "")
        updated_text = (original + "\n\n⏱ *Slackタイムアウト: IDE側での承認に切り替えました*")[:2900]
        try:
            slack_app.client.chat_update(
                channel=channel,
                ts=ts,
                text=updated_text,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": updated_text}}],
            )
        except Exception as e:
            print(f"[bridge] chat_update failed: {e}", file=sys.stderr)
    _mark_empty_if_done()
    return jsonify({"status": "abandoned"})


@flask_app.route("/approval/status/<request_id>", methods=["GET"])
def approval_status(request_id):
    item = APPROVALS.get(request_id)
    if not item:
        return jsonify({"status": "denied", "reason": "unknown request"}), 404
    if item["status"] == "pending" and time.time() - item["created_at"] > APPROVAL_TTL:
        item["status"] = "denied"
        item["reason"] = "承認タイムアウト"
        _mark_empty_if_done()
    return jsonify({"status": item["status"], "reason": item.get("reason", "")})


def _resolve_action(body: dict) -> tuple[str, str, str]:
    user_id = body.get("user", {}).get("id", "")
    actions = body.get("actions", [])
    if not actions:
        return user_id, "", ""
    return user_id, actions[0].get("action_id", ""), actions[0].get("value", "")


def _handle_decision(body, client, decision: str, label: str):
    user_id, _, request_id = _resolve_action(body)
    if ALLOWED_SLACK_USER_IDS and user_id not in ALLOWED_SLACK_USER_IDS:
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=user_id,
            text="このユーザーは承認権限がありません。",
        )
        return

    item = APPROVALS.get(request_id)
    if not item:
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=user_id,
            text="承認リクエストが見つかりません (期限切れ?)",
        )
        return

    if item["status"] == "abandoned":
        client.chat_postEphemeral(
            channel=body["channel"]["id"],
            user=user_id,
            text="このリクエストはタイムアウト済みで、IDE側に切り替わっています。",
        )
        return
    if item["status"] != "pending":
        return

    item["status"] = decision
    item["decided_by"] = user_id
    if decision == "denied":
        item["reason"] = "Slackで拒否されました"

    channel = item.get("thread_channel") or body.get("channel", {}).get("id")
    ts = item.get("thread_ts") or body.get("message", {}).get("ts")
    if channel and ts:
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            reply_broadcast=False,
            text=label,
        )
    _mark_empty_if_done()


@slack_app.action("approve_tool")
def on_approve(ack, body, client):
    ack()
    _handle_decision(body, client, "approved", "✅ 許可")


@slack_app.action("deny_tool")
def on_deny(ack, body, client):
    ack()
    _handle_decision(body, client, "denied", "❌ 拒否")


def _idle_watchdog():
    while True:
        time.sleep(15)
        with STATE_LOCK:
            empty_at = LAST_EMPTY_AT
            pending = any(a["status"] == "pending" for a in APPROVALS.values())
        if empty_at is None or pending:
            continue
        if time.time() - empty_at >= IDLE_TIMEOUT:
            print(f"[bridge] idle {IDLE_TIMEOUT}s, exiting", file=sys.stderr)
            os._exit(0)


def _run_flask():
    flask_app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


def main():
    _require_env()

    singleton = _bind_singleton_port()
    if singleton is None:
        print(f"[bridge] port {PORT} already bound; another bridge is running", file=sys.stderr)
        sys.exit(0)
    singleton.close()

    threading.Thread(target=_run_flask, daemon=True).start()
    threading.Thread(target=_idle_watchdog, daemon=True).start()
    print(f"[bridge] listening on 127.0.0.1:{PORT}, idle_timeout={IDLE_TIMEOUT}s", file=sys.stderr)
    SocketModeHandler(slack_app, SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
