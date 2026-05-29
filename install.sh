#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"

echo "==> claude-slack-approval install"
echo "    source: $HERE"
echo "    settings: $SETTINGS"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

# --- 1. venv & deps ---------------------------------------------------------
if [ ! -d "$HERE/.venv" ]; then
  echo "==> creating venv at $HERE/.venv"
  python3 -m venv "$HERE/.venv"
fi

"$HERE/.venv/bin/pip" install --quiet --upgrade pip
"$HERE/.venv/bin/pip" install --quiet "slack-bolt>=1.18" "flask>=3.0" "python-dotenv>=1.0"
echo "==> deps installed"

# --- 2. .env ---------------------------------------------------------------
if [ ! -f "$HERE/.env" ]; then
  echo "==> creating $HERE/.env from template"
  cp "$HERE/.env.example" "$HERE/.env"
  chmod 600 "$HERE/.env"
  echo ""
  echo "    edit $HERE/.env with your Slack tokens before starting claude."
  echo "    see README.md for how to create the Slack App."
  echo ""
else
  chmod 600 "$HERE/.env" || true
  echo "==> .env already exists, leaving it alone"
  # Backfill TO_SLACK if missing (upgraded from a pre-toggle install).
  if ! grep -q '^TO_SLACK=' "$HERE/.env"; then
    printf '\n# runtime on/off switch (off = bypass all Slack hooks)\nTO_SLACK=on\n' >> "$HERE/.env"
    echo "==> added TO_SLACK=on to existing .env"
  fi
fi

# --- 2b. .gitignore (make sure .env never gets committed) -------------------
GITIGNORE="$HERE/.gitignore"
touch "$GITIGNORE"
for pattern in ".env" "bridge.log" ".venv/"; do
  if ! grep -qxF "$pattern" "$GITIGNORE"; then
    echo "$pattern" >> "$GITIGNORE"
    echo "==> added '$pattern' to $GITIGNORE"
  fi
done

# --- 3. settings.json merge -------------------------------------------------
mkdir -p "$(dirname "$SETTINGS")"
if [ ! -f "$SETTINGS" ]; then
  echo '{}' > "$SETTINGS"
fi

cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"

python3 - "$SETTINGS" "$HERE/hook.py" <<'PY'
import json, sys
from pathlib import Path

settings_path = Path(sys.argv[1])
hook_path = sys.argv[2]
data = json.loads(settings_path.read_text() or "{}")
hooks = data.setdefault("hooks", {})

ENTRIES = {
    "PreToolUse": {
        "matcher": "Write|Edit|MultiEdit|Bash",
        "hooks": [{
            "type": "command",
            "command": f"python3 {hook_path} approval",
            "statusMessage": "⏳🔔 WAITING FOR SLACK APPROVAL — approve in Slack",
        }],
    },
    "SessionStart": {
        "hooks": [{"type": "command", "command": f"python3 {hook_path} session-start"}],
    },
    "SessionEnd": {
        "hooks": [{"type": "command", "command": f"python3 {hook_path} session-end"}],
    },
    "Stop": {
        "hooks": [{"type": "command", "command": f"python3 {hook_path} stop"}],
    },
}

# Strip any existing entries pointing at this hook.py, so re-running install.sh
# replaces them with the latest format (e.g. picks up the TO_SLACK=on prefix).
for event in list(hooks.keys()):
    kept = []
    for entry in hooks[event]:
        sub_hooks = [h for h in entry.get("hooks", []) if hook_path not in h.get("command", "")]
        if sub_hooks:
            entry["hooks"] = sub_hooks
            kept.append(entry)
    if kept:
        hooks[event] = kept
    else:
        del hooks[event]

# Add fresh entries.
for event, entry in ENTRIES.items():
    hooks.setdefault(event, []).append(entry)

settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print(f"==> updated {settings_path}")
PY

# --- 4. summary -------------------------------------------------------------
cat <<EOF

==> install complete.

Next steps:
  1. Create a Slack App (Socket Mode) and fill in $HERE/.env
     - SLACK_BOT_TOKEN (xoxb-): OAuth & Permissions, scopes: chat:write im:write
     - SLACK_APP_TOKEN (xapp-): Basic Information → App-Level Tokens, scope: connections:write
     - SLACK_USER_ID (Uxxx): your Slack member ID (profile → ... → Copy member ID)
     - Enable Socket Mode in the App, and enable Interactivity (Request URL can be left blank under Socket Mode)
  2. Start claude as usual. The bridge will auto-spawn on first SessionStart.

To toggle Slack notification on/off without uninstalling:
  - edit $HERE/.env and flip the TO_SLACK= line (on/off). One line, applies to all 3 hooks.
  - off = hook exits immediately (no Slack call, no bridge spawn).
  - or run ./uninstall.sh to remove the hook entries entirely.

Logs: $HERE/bridge.log
Bridge port: 3737 (override with CLAUDE_SLACK_BRIDGE_PORT)
EOF
