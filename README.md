# claude-code-slack

A macOS background daemon that bridges Slack and Claude Code. @mention your bot in any Slack channel or DM and it spawns a headless `claude -p` session on your local machine — with access to your filesystem, tools, and any skills you've configured.

```
You (Slack)  →  @mybot what's broken in src/auth?
Bot          →  [reads your code, replies in-thread with findings]
```

**This runs on your Mac.** Claude has full access to whatever directory you point it at. Treat this like giving Slack a terminal into your local dev environment.

---

## How it works

```
launchd (KeepAlive — stays up 24/7)
    │
    ▼
socket_daemon.py  ──►  WebSocket to Slack (Socket Mode)
    │                  receives app_mention + DM events in real-time
    │
    │  per event: ack immediately (<3s Slack requirement)
    ▼
[1] reactions.add :eyes:   (atomic claim — prevents duplicate processing)
[2] python3 detach.py bash worker.sh CHANNEL TS USER
                │
                └──► os.setsid()  (new session, survives daemon restart)

worker.sh (independent process per mention)
    ├─► spinner (cycles :eyes: → :hourglass: → :brain: → :writing_hand: every 30s)
    ├─► spawns `claude -p` with job-prompt.md (30-min timeout)
    │        └──► Claude reads thread, uses tools, posts reply, writes status file
    └─► final reaction: ✅ success | 💬 clarification | ❌ failed
```

**Key design choices:**
- **Socket Mode push** — events arrive in real time, no polling
- **`os.setsid()`** — workers survive daemon restarts (in-flight sessions don't die when launchd reloads)
- **Atomic `reactions.add` claim** — idempotent across restarts and Slack redeliveries
- **Thread-continuous sessions** — follow-up replies in a thread resume the prior Claude session via `--resume` (optional, off by default)

---

## Requirements

- **macOS** (uses launchd)
- **[Claude Code CLI](https://claude.ai/download)** installed and authenticated (`claude login`)
- **Python 3.9+**
- A Slack workspace where you can create apps

---

## Slack app setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**

2. **Enable Socket Mode:** App Settings → Socket Mode → Enable → create an App-Level Token with `connections:write` scope → save the `xapp-...` token

3. **Bot Token Scopes** (OAuth & Permissions → Scopes → Bot Token Scopes):
   ```
   app_mentions:read
   assistant:write
   channels:history
   channels:read
   chat:write
   files:read
   groups:history
   groups:read
   im:history
   im:read
   im:write
   mpim:history
   mpim:read
   reactions:read
   reactions:write
   search:read
   users:read
   ```

4. **Event Subscriptions** → Enable → Subscribe to bot events:
   ```
   app_mention
   message.im
   reaction_added
   ```
   Also enable: `assistant_thread_started`, `assistant_thread_context_changed`

5. **App Home** → Show Tabs → Messages Tab → enable "Allow users to send Slash commands and messages from the messages tab"

6. **Install to workspace** → copy the `xoxb-...` Bot User OAuth Token

7. Find your **Bot User ID**: install the app, send it a DM, click the sender → "Copy member ID"

8. Find **your Slack user ID**: Slack → your profile → "..." → "Copy member ID"

---

## Installation

```bash
git clone https://github.com/prrranavv/claude-code-slack
cd claude-code-slack
bash setup.sh
```

The setup script will:
- Prompt for your tokens and user IDs
- Copy files to `~/.claude/claude-slack-bot/`
- Create a Python venv and install dependencies
- Generate and load three launchd services

To install to a custom directory:
```bash
INSTALL_DIR=/path/to/dir bash setup.sh
```

---

## Configuration

All config lives in `~/.claude/claude-slack-bot/config.env` (created by setup.sh). Never commit this file — it contains your Slack tokens.

| Variable | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | ✓ | Bot OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | ✓ | App-level token (`xapp-...`) |
| `BOT_USER_ID` | ✓ | Your bot's Slack user ID |
| `AUTHORIZED_USER_ID` | ✓ | Your Slack user ID — the only person who can run write/destructive operations |
| `AUTHORIZED_USER_NAME` | ✓ | Your name (shown in "Stopped by X." messages) |
| `CLAUDE_WORKSPACE` | | Directory where Claude sessions run. Default: `$HOME` |
| `BOT_NAME` | | Bot display name in logs/prompts. Default: `ClaudeBot` |
| `FORWARD_CHANNEL` | | Slack channel ID to forward new thread starts to. Default: disabled |
| `RESUME_SESSIONS` | | `0` = fresh each time, `dm` = resume in DMs, `1` = resume everywhere. Default: `0` |
| `MAX_PARALLEL` | | Max concurrent Claude sessions. Default: `10` |
| `CLAUDE_TIMEOUT` | | Session timeout in seconds. Default: `1800` |
| `PREFETCH_CONTEXT` | | Pre-fetch thread context before spawning worker: `off`/`dm`/`1`. Default: `off` |

After changing config.env, reload the daemon:
```bash
launchctl unload ~/Library/LaunchAgents/com.claude-slack-bot.plist
launchctl load   ~/Library/LaunchAgents/com.claude-slack-bot.plist
```

---

## Using the bot

Invite it to a channel: `/invite @YourBotName`

Then @mention it:
```
@YourBotName what files handle authentication in this repo?
@YourBotName summarize the last 10 commits
@YourBotName !haiku quick question: what's 2+2?
```

### Per-mention flags

| Flag | Effect |
|---|---|
| `!opus` / `!sonnet` / `!haiku` | Use a specific model for this session |
| `!fast` | Disable extended thinking for this turn |
| `!reset` | Clear the thread's session memory and start fresh |
| `!delete` | Delete all bot messages in this thread (owner only) |

### Built-in commands (no Claude spawned)

| Command | Effect |
|---|---|
| `@bot status` or `@bot ?` | Show active sessions + thread memory for this thread |
| `@bot stop` | Kill all active workers in this thread (owner only) |
| 🛑 reaction on any message | Kill all active workers in that thread (owner only) |

### Reaction state machine

| Reaction | Meaning |
|---|---|
| 👀 `:eyes:` | Claimed, starting |
| 💤 `:zzz:` | Queued (waiting on another turn in same thread) |
| ⏳ `:hourglass_flowing_sand:` | Working |
| 🧠 `:brain:` | Working |
| ✍️ `:writing_hand:` | Working |
| ✅ `:white_check_mark:` | Done |
| 💬 `:speech_balloon:` | Waiting for clarification |
| ❌ `:x:` | Failed |
| 🛑 `:octagonal_sign:` | Stopped by owner |

---

## Thread-continuous sessions

When `RESUME_SESSIONS=1`, follow-up replies in a Slack thread resume the prior Claude session via `claude --resume <uuid>`. Claude remembers your prior tool calls, reasoning, and outputs — faster followups and coherent multi-turn work.

Session files live at `~/.claude/claude-slack-bot/sessions/`. The nightly GC prunes files older than 7 days.

Session rotation happens automatically when:
- Context exceeds 400k tokens
- Skills directory changes (hash drift)
- 2+ consecutive failures
- You send `!reset`

---

## Customizing Claude's behavior

Edit `~/.claude/claude-slack-bot/job-prompt.md` to change what Claude does with mentions. The template supports:
- `{{CHANNEL}}`, `{{TS}}`, `{{USER}}` — Slack context
- `{{THREAD_CONTEXT}}` — pre-fetched thread (filled by daemon when `PREFETCH_CONTEXT` is on)
- `{{AUTHORIZED_USER_ID}}`, `{{BOT_USER_ID}}`, `{{BOT_NAME}}` — config values
- `{{STATUS_FILE}}` — path to write `success`/`failed`/`clarification`

No daemon restart needed — the next mention picks up the new prompt automatically.

---

## Operations

```bash
# Live log
tail -f ~/.claude/claude-slack-bot/watch.log

# Daemon status
launchctl list | grep claude-slack-bot

# Restart daemon (picks up socket_daemon.py changes)
launchctl kickstart -k gui/$(id -u)/com.claude-slack-bot

# Reload after config.env changes (re-reads env vars)
launchctl unload ~/Library/LaunchAgents/com.claude-slack-bot.plist
launchctl load   ~/Library/LaunchAgents/com.claude-slack-bot.plist

# Kill all running workers (blunt, does not stop daemon)
pkill -f "worker.sh"

# Hard reset
launchctl unload ~/Library/LaunchAgents/com.claude-slack-bot.plist
pkill -9 -f "socket_daemon.py" 2>/dev/null || true
pkill -9 -f "worker.sh" 2>/dev/null || true
rm -rf /tmp/claude-slack-bot-status && mkdir /tmp/claude-slack-bot-status
launchctl load ~/Library/LaunchAgents/com.claude-slack-bot.plist
```

---

## Debugging

**Bot silent after a mention:**
1. `launchctl list | grep claude-slack-bot` — expect a non-zero PID
2. `tail -20 ~/.claude/claude-slack-bot/watch.log` — look for `NEW MENTION` or errors
3. If nothing logged: verify the bot is a member of the channel and Socket Mode is enabled
4. Check tokens: `grep SLACK_ ~/.claude/claude-slack-bot/config.env`

**Daemon won't start:**
- Check `stderr.log` for the crash reason
- Common causes: missing tokens in config.env, `.venv` path wrong, `slack_sdk` not installed
- Reinstall: `~/.claude/claude-slack-bot/.venv/bin/pip install -r ~/.claude/claude-slack-bot/requirements.txt`

**Worker spawns but Claude exits immediately:**
- Test interactively: `claude -p "hello"` in terminal. If that fails → auth issue, not bot code
- Check `watch.log` for `claude stderr` lines

**Mac was asleep → missed mentions:**
- Expected. Socket Mode buffers events for short sleeps (seconds to a few minutes). Long sleeps drop events silently. No fix without a cloud relay.

---

## Authorization model

Only `AUTHORIZED_USER_ID` can trigger write/destructive operations. Everyone else who mentions the bot gets read-only access — they can ask questions, read files, summarize threads, but cannot modify files, run git operations, or touch external systems.

Enforcement is in `job-prompt.md`'s Authorization section. The authorized user's identity comes from Slack's event payload — it cannot be spoofed from message content.

**What this doesn't protect against:** Pranav's Slack account being compromised, or anyone with shell access to the Mac.

---

## File map

| Path | Purpose |
|---|---|
| `~/.claude/claude-slack-bot/socket_daemon.py` | Long-running WebSocket daemon |
| `~/.claude/claude-slack-bot/worker.sh` | Per-mention worker (spinner + claude + cleanup) |
| `~/.claude/claude-slack-bot/detach.py` | Process detachment (`os.setsid()`) |
| `~/.claude/claude-slack-bot/job-prompt.md` | Claude prompt template — edit to change bot behavior |
| `~/.claude/claude-slack-bot/config.env` | Your secrets and config (gitignored) |
| `~/.claude/claude-slack-bot/slack-scripts/slack-api.py` | Slack API wrapper used by Claude |
| `~/.claude/claude-slack-bot/slack-scripts/templates/` | Block Kit reply templates |
| `~/.claude/claude-slack-bot/sessions/` | Per-thread session state (resume feature) |
| `~/.claude/claude-slack-bot/watch.log` | Activity log |
| `~/Library/LaunchAgents/com.claude-slack-bot.plist` | Main daemon launchd config |
| `~/Library/LaunchAgents/com.claude-slack-bot-gc.plist` | Nightly GC (04:07) |
| `~/Library/LaunchAgents/com.claude-slack-bot-digest.plist` | Daily digest (09:03) |

---

## Limitations

- **Mac-only** — no responses while the laptop is asleep
- **Local credentials** — Claude runs as your user with `--dangerously-skip-permissions`; it has whatever access you have
- **In-memory backlog** — deferred mentions (when at MAX_PARALLEL cap) are lost on daemon restart
- **Single Mac** — no redundancy; if the laptop's offline, the bot is offline
