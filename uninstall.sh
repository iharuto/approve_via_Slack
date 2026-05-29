#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"

if [ ! -f "$SETTINGS" ]; then
  echo "no settings.json at $SETTINGS, nothing to remove"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found; please remove hook entries manually from $SETTINGS" >&2
  exit 1
fi

cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"

python3 - "$SETTINGS" "$HERE/hook.py" <<'PY'
import json, sys
from pathlib import Path

settings_path = Path(sys.argv[1])
hook_path = sys.argv[2]
data = json.loads(settings_path.read_text())

hooks = data.get("hooks", {})

def strip(event):
    entries = hooks.get(event, [])
    kept = []
    for entry in entries:
        sub_hooks = [
            h for h in entry.get("hooks", [])
            if hook_path not in h.get("command", "")
        ]
        if sub_hooks:
            entry["hooks"] = sub_hooks
            kept.append(entry)
    if kept:
        hooks[event] = kept
    elif event in hooks:
        del hooks[event]

for event in ("PreToolUse", "SessionStart", "SessionEnd"):
    strip(event)

if not hooks:
    data.pop("hooks", None)
else:
    data["hooks"] = hooks

settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print(f"removed claude-slack hook entries from {settings_path}")
PY

echo "done. bridge will exit on its own (or kill the running process if you want it gone now):"
echo "  pkill -f 'slack_bridge.py' || true"
