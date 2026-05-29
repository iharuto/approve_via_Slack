# claude-slack-approval

Approve Claude Code tool calls from Slack — including from your phone — without
exposing your server to the public internet. Falls back to Claude Code's
built-in IDE permission prompt if Slack doesn't respond in time.

When Claude Code is about to run `Bash`, `Write`, `Edit`, or `MultiEdit`, a
`PreToolUse` hook DMs you on Slack with **Approve / Deny** buttons. The hook
blocks for **60 seconds** waiting for your response. If you don't answer in
time, the hook escalates: the Slack message is updated to say "switched to
IDE", the buttons are removed, and Claude Code's normal IDE prompt appears so
you can decide from the desktop. Slack connectivity uses Socket Mode
(outbound WebSocket), so the host running Claude does **not** need an inbound
port, a public domain, ngrok, or TLS certificates.

## Hybrid approval flow

```
PreToolUse hook fires
        │
        ▼
hard-deny / auto-allow checks  ─────►  done (no Slack DM)
        │
        ▼
post Slack DM with [Approve][Deny] buttons
        │
        ▼
poll bridge every 5s, for up to 60s
        │
        ├─► Slack Approve  ─►  permissionDecision: "allow"  ─► tool runs
        ├─► Slack Deny     ─►  permissionDecision: "deny"   ─► tool blocked
        │
        └─► 60s elapsed
                │
                ▼
        bridge updates Slack DM → "⏱ switched to IDE", buttons removed
                │
                ▼
        permissionDecision: "ask"
                │
                ▼
        Claude Code's IDE permission prompt appears
                │
                ▼
        you approve/deny from the desktop
```

## Why not truly simultaneous IDE + Slack?

The hybrid above is the best you can get on Claude Code's hook architecture.
We can't have the IDE prompt AND the Slack buttons both live at the same time
("whichever you press first wins"). The reasons:

1. **`PreToolUse` is blocking.** While the hook process is running, Claude
   Code does **not** display the IDE permission dialog. The dialog only
   appears after the hook exits with `permissionDecision: "ask"` (or no
   decision at all).
2. **No external API to dismiss an in-flight IDE dialog.** Once the IDE
   dialog is on screen, there is no documented way for an outside process
   (like our Slack bridge) to programmatically click Approve/Deny for the
   user. Approval can only come from the user clicking inside the IDE.
3. **`PermissionRequest` hook has the same constraint.** Claude Code does
   ship a `PermissionRequest` hook event, but it also fires *before* the
   dialog is shown and is also blocking. It doesn't give the bridge any
   ability to influence the dialog after it appears.

So we pick an order. We pick **Slack first**, because that's the direction
where the hook can still fall back: the hook blocks waiting on Slack, and if
nothing happens in 60s it gives up and lets the IDE dialog take over. The
reverse ("IDE first, then Slack fallback") is unimplementable: by the time
we'd know the IDE dialog has been ignored, our hook process is already gone
and Slack has no way to retroactively approve.

## Process architecture

```
[your phone / laptop]            [Slack cloud]            [your server]
                                       ▲ │
        tap "Approve" in Slack ────────┘ │ Socket Mode WebSocket
                                         │ (server → Slack, outbound only)
                                         ▼
                                   slack_bridge.py  (daemon)
                                         ▲
                            HTTP @ 127.0.0.1:3737
                                         │
                                  hook.py (PreToolUse)
                                         ▲
                                         │ blocks 60s, polls every 5s
                                   claude session
```

Two processes:

- **`hook.py`** — short-lived, invoked by Claude Code on every `PreToolUse`,
  `SessionStart`, and `SessionEnd`. Stdlib-only so it runs under whatever
  Python `claude` happens to launch with.
- **`slack_bridge.py`** — long-lived daemon. Holds the Socket Mode connection,
  stores pending approvals, listens on `127.0.0.1:3737` for the hook.
  Auto-spawned by the first hook invocation; auto-exits after 5 minutes with
  zero active Claude sessions.

## Prerequisites

- Python 3.10+
- A Slack workspace where you can install a custom app
- Claude Code CLI (`claude` on `$PATH`)

## 1. Create the Slack App

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
   Pick any name; choose your workspace.
2. **Socket Mode** → enable it. When prompted, create an **App-Level Token**
   with the `connections:write` scope. Save the token (`xapp-...`) — this is
   your `SLACK_APP_TOKEN`.
3. **OAuth & Permissions** → under *Bot Token Scopes*, add:
   - `chat:write`
   - `im:write`

   Then click **Install to Workspace**. The resulting **Bot User OAuth Token**
   (`xoxb-...`) is your `SLACK_BOT_TOKEN`.
4. **Interactivity & Shortcuts** → toggle **Interactivity** on. Under Socket
   Mode the *Request URL* field can be left blank.
5. Find your **Slack User ID** (`U0...`): in Slack, click your profile → the
   `⋯` menu → **Copy member ID**. This is your `SLACK_USER_ID`.

## 2. Install

**Decide first: user-level or project-level?** The installer writes hook
entries into a Claude Code `settings.json`. Pick which one:

- **User-level (default)** — `~/.claude/settings.json`. Slack approval will
  apply to **every** `claude` session you start, in any directory.
- **Project-level** — `<project>/.claude/settings.json`. Slack approval only
  fires when `claude` is started inside that project. Set `CLAUDE_SETTINGS`
  before running the installer.

```sh
git clone <this repo>
cd <repo>

# user-level (default)
./install.sh

# OR project-level
CLAUDE_SETTINGS=/path/to/project/.claude/settings.json ./install.sh
```

`install.sh` will:

- create a `.venv` next to the scripts and install `slack-bolt`, `flask`,
  `python-dotenv`
- copy `.env.example` to `.env` (mode 600) if missing
- append `.env`, `bridge.log`, and `.venv/` to `.gitignore`
- merge three hook entries into the chosen `settings.json` (with a timestamped
  backup)

Then edit `.env` with the three tokens from step 1.

> **Installed to the wrong settings file?** Run `./uninstall.sh` (or with
> `CLAUDE_SETTINGS=...` pointing at the file you installed into) to strip the
> hook entries, then re-run `./install.sh` with the correct value.

## 3. Use

Just start `claude` as you normally would. The first `SessionStart` lazily
spawns the bridge in the background.

When Claude tries to run a tool that needs approval, you receive a Slack DM
like:

> **Claude is requesting tool permission**
> Tool: `Bash`
> CWD: `/home/you/project`
> Content: `npm test`
>
> \[Approve\] \[Deny\]

You have **60 seconds** to tap a button from any Slack client (laptop, phone,
web). If you do, Claude unblocks immediately. If 60 seconds pass with no
response, the Slack DM is updated and Claude's normal IDE permission prompt
appears so you can decide from the desktop instead.

### What auto-allows and what asks for approval

The hook is opinionated about safety. Edit the constants near the top of
`hook.py` to taste.

| Case                                                        | Behavior              |
| ----------------------------------------------------------- | --------------------- |
| `Read` of any file (except `.env` / `id_rsa`)               | auto-allow            |
| `Write` / `Edit` / `MultiEdit` of a path inside `cwd`       | auto-allow            |
| `Bash` containing `rm -rf`, `sudo`, `curl `, `.env`, `id_rsa`, etc. | hard deny (no prompt) |
| Anything else matching `Write\|Edit\|MultiEdit\|Bash`       | Slack approval → IDE fallback |

`Read` of `.env` or `id_rsa` is hard-denied with no Slack prompt.

## 4. Turn it off

There's a single switch in `.env`: the `TO_SLACK` line.

- **Temporarily disable**: edit `.env` and change `TO_SLACK=on` to
  `TO_SLACK=off`. Re-enable by flipping back. Takes effect on the next hook
  invocation — no Claude restart needed. With `off`, the hook exits
  immediately and Claude Code's default permission flow applies as if this
  tool were not installed. The bridge, if running, exits on its own after 5
  idle minutes.

  ```diff
  - TO_SLACK=on
  + TO_SLACK=off
  ```

- **Permanently uninstall**: run `./uninstall.sh`. It strips only the three
  hook entries this installer added (leaving any other hooks intact), with a
  backup.

The `.venv`, `.env`, and the scripts themselves stay in place. Delete the
directory if you want them gone.

## Configuration

`.env` knobs:

| Variable                      | Default                | Purpose                                              |
| ----------------------------- | ---------------------- | ---------------------------------------------------- |
| `TO_SLACK`                    | `on`                   | runtime on/off switch, one line in `.env`            |
| `SLACK_BOT_TOKEN`             | —                      | required (`xoxb-...`)                                |
| `SLACK_APP_TOKEN`             | —                      | required (`xapp-...`)                                |
| `SLACK_USER_ID`               | —                      | required; DM target and default approver             |
| `ALLOWED_SLACK_USER_IDS`      | `SLACK_USER_ID`        | comma-separated list of users allowed to approve     |
| `CLAUDE_SLACK_BRIDGE_PORT`    | `3737`                 | localhost port for hook ↔ bridge IPC                 |
| `CLAUDE_SLACK_IDLE_TIMEOUT`   | `300` (5 min)          | bridge exits after this long with 0 active sessions  |
| `CLAUDE_SLACK_APPROVAL_TTL`   | `300` (5 min)          | bridge-side TTL for stale approval records           |

Constants near the top of `hook.py` (edit the file to change):

| Constant                  | Default   | Purpose                                                    |
| ------------------------- | --------- | ---------------------------------------------------------- |
| `APPROVAL_WAIT_TIMEOUT`   | `60`      | seconds the hook waits on Slack before IDE fallback        |
| `APPROVAL_POLL_INTERVAL`  | `5`       | seconds between status polls to the bridge                 |
| `SPAWN_TIMEOUT`           | `15`      | seconds to wait for a freshly-spawned bridge to become ready |

## Security notes

- **No inbound network exposure.** Socket Mode means the host opens an
  outbound WebSocket to Slack; no public IP, no TLS cert, no signing secret
  needed.
- **`.env` holds two tokens** (`xoxb-` and `xapp-`). `install.sh` `chmod 600`s
  it and adds it to `.gitignore` automatically. Verify with
  `git check-ignore .env` before committing.
- **Approvals are DM-only.** Other Slack workspace members can't see them.
- **Approver allowlist.** Even if a notification leaked, only user IDs in
  `ALLOWED_SLACK_USER_IDS` (default: just you) can decide.
- **Localhost API has no write-side approval endpoint.** Approve/deny can
  only arrive via the authenticated Slack Socket Mode channel, never from
  other processes on the host.
- **Always-on hard denies.** Commands matching the hard-deny list are blocked
  in `hook.py` itself, before any Slack round-trip.
- **IDE fallback is the safety net.** If the bridge crashes, Slack rate-limits
  you, or you simply miss the notification, the prompt still surfaces in the
  IDE after 60 seconds — Claude never gets silently unblocked.

## Troubleshooting

Logs from the bridge are appended to `bridge.log` next to the scripts.

| Symptom                                                | Likely cause / fix                                                                  |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------- |
| `Slack承認サーバー(bridge)を起動できませんでした`       | `.venv` missing or `.env` invalid. Check `bridge.log`.                              |
| Slack DM never arrives, then IDE prompt fires at 60s   | Slack App not installed / scopes missing / `SLACK_USER_ID` wrong. Send yourself a test DM. |
| `port 3737 already bound`                              | Another bridge is already running. That's fine; the new one exits.                  |
| Buttons in Slack do nothing                            | Socket Mode disabled, or Interactivity off. Re-check the Slack App settings.        |
| Approve in Slack but Claude still asks via IDE         | You pressed Approve *after* the 60s window expired (the DM should show "⏱ switched to IDE"). Approve via IDE instead. |
| Approvals work on Slack web but not mobile             | Mobile app caching. Quit and relaunch the Slack mobile app.                         |

### Manual bridge control

```sh
# start manually (useful when debugging)
./.venv/bin/python ./slack_bridge.py

# kill any running bridge
pkill -f slack_bridge.py
```

## Files

| File                | Purpose                                                |
| ------------------- | ------------------------------------------------------ |
| `hook.py`           | `PreToolUse` / `SessionStart` / `SessionEnd` hook      |
| `slack_bridge.py`   | long-lived daemon: Socket Mode + localhost HTTP        |
| `install.sh`        | venv + deps + settings.json merge                      |
| `uninstall.sh`      | strip the three hook entries from settings.json        |
| `.env.example`      | token template                                         |
| `pyproject.toml`    | dependency metadata                                    |
| `bridge.log`        | (created at runtime) bridge stdout/stderr              |
