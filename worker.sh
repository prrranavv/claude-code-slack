#!/bin/bash
# Per-mention worker script. Invoked by socket_daemon.py via detach.py.
# Handles spinner + claude -p + reaction cleanup for a single Slack mention.
#
# Usage: worker.sh <channel> <ts> <user>

set -u

CH="$1"
TS="$2"
USER="$3"

# ─── Install dir & config ─────────────────────────────────────────────────────
INSTALL_DIR="${CLAUDE_SLACK_INSTALL_DIR:-${HOME}/.claude/claude-slack-bot}"

# Source config.env if present (sets SLACK_BOT_TOKEN, BOT_USER_ID, etc.)
if [ -f "${INSTALL_DIR}/config.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${INSTALL_DIR}/config.env"
  set +a
fi

# ─── Core config ──────────────────────────────────────────────────────────────
export BOT_USER_ID="${BOT_USER_ID:-}"
BOT_NAME="${BOT_NAME:-ClaudeBot}"
AUTHORIZED_USER_ID="${AUTHORIZED_USER_ID:-}"

# Find claude binary
if [ -n "${CLAUDE_BIN:-}" ]; then
  : # already set in env
elif command -v claude >/dev/null 2>&1; then
  CLAUDE_BIN="$(command -v claude)"
elif [ -x "/opt/homebrew/bin/claude" ]; then
  CLAUDE_BIN="/opt/homebrew/bin/claude"
elif [ -x "/usr/local/bin/claude" ]; then
  CLAUDE_BIN="/usr/local/bin/claude"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$TS] ERROR: claude binary not found" >> "${INSTALL_DIR}/watch.log"
  exit 1
fi

CLAUDE_WORKSPACE="${CLAUDE_WORKSPACE:-${HOME}}"
WATCHER_DIR="${INSTALL_DIR}"
LOG_FILE="${WATCHER_DIR}/watch.log"
PROMPT_FILE="${WATCHER_DIR}/job-prompt.md"
STATUS_DIR="/tmp/claude-slack-bot-status"
SESSION_DIR="${WATCHER_DIR}/sessions"
SLACK_SCRIPTS_DIR="${WATCHER_DIR}/slack-scripts"
SKILLS_DIR="${SKILLS_DIR:-}"    # optional; set in config.env to a skills directory
mkdir -p "$STATUS_DIR" "$SESSION_DIR"

SPINNER_INTERVAL=30
CYCLE_EMOJIS="eyes hourglass_flowing_sand brain writing_hand"
CLAUDE_TIMEOUT=${CLAUDE_TIMEOUT:-1800}
CONTEXT_CAP=${CONTEXT_CAP:-400000}
MODEL="${MODEL:-opus}"
FAST="${FAST:-}"
THREAD_TS="${THREAD_TS:-$TS}"
IS_DM="${IS_DM:-0}"
RESUME_SESSIONS="${RESUME_SESSIONS:-0}"
THREAD_KEY="${THREAD_TS//./_}"
SESSION_FILE="${SESSION_DIR}/${THREAD_KEY}.json"
LOCK_DIR="${SESSION_DIR}/${THREAD_KEY}.lock"

# ─── API routing (optional) ───────────────────────────────────────────────────
# If you're routing through Portkey, AWS Bedrock, or another proxy, set
# ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN in config.env.
# Leave them unset to use the Claude CLI's own stored credentials (claude login).
if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
  export ANTHROPIC_BASE_URL
fi
if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  export ANTHROPIC_AUTH_TOKEN
fi
if [ -n "${ANTHROPIC_CUSTOM_HEADERS:-}" ]; then
  export ANTHROPIC_CUSTOM_HEADERS
fi

export SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}"
export SLACK="${SLACK:-python3 ${SLACK_SCRIPTS_DIR}/slack-api.py}"
export SLACK_TEMPLATES="${SLACK_SCRIPTS_DIR}/templates"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export HEADLESS=1

# ─── Helpers ──────────────────────────────────────────────────────────────────
log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$TS] $*" >> "$LOG_FILE"
}

slack_add_reaction() {
  curl -s -X POST "https://slack.com/api/reactions.add" \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    --data-urlencode "channel=$1" \
    --data-urlencode "timestamp=$2" \
    --data-urlencode "name=$3" >/dev/null 2>&1 || true
}
slack_remove_reaction() {
  curl -s -X POST "https://slack.com/api/reactions.remove" \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    --data-urlencode "channel=$1" \
    --data-urlencode "timestamp=$2" \
    --data-urlencode "name=$3" >/dev/null 2>&1 || true
}

STATUS_FILE="${STATUS_DIR}/${TS//./_}.status"
RUNNING_FILE="${STATUS_DIR}/${TS//./_}.running"
EMOJI_FILE="${STATUS_DIR}/${TS//./_}.emoji"
FIRED_MARKER="${STATUS_DIR}/${TS//./_}.fired"
WATCHER_DONE_MARKER="${STATUS_DIR}/${TS//./_}.wdone"
CONTEXT_FILE="${STATUS_DIR}/${TS//./_}.context"
rm -f "$STATUS_FILE" "$FIRED_MARKER" "$WATCHER_DONE_MARKER"

LOCK_HELD=0
trap 'rm -f "$RUNNING_FILE" "$EMOJI_FILE" "$FIRED_MARKER" "$WATCHER_DONE_MARKER" "$STATUS_FILE" "$CONTEXT_FILE"; [ "$LOCK_HELD" = "1" ] && rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT

# ─── Resume-decision helpers ──────────────────────────────────────────────────
skill_hash() {
  {
    if [ -n "${SKILLS_DIR:-}" ] && [ -d "$SKILLS_DIR" ]; then
      find "$SKILLS_DIR" -type f \( -name "*.md" -o -name "*.sh" -o -name "*.py" -o -name "*.json" \) \
        -exec shasum {} + 2>/dev/null
    fi
    shasum "$PROMPT_FILE" 2>/dev/null
    [ -f "${HOME}/.claude/CLAUDE.md" ] && shasum "${HOME}/.claude/CLAUDE.md" 2>/dev/null || true
  } | shasum | awk '{print $1}'
}

session_context_tokens() {
  local uuid="$1"
  local workspace_key="${CLAUDE_WORKSPACE//\//-}"
  local jsonl="${HOME}/.claude/projects/${workspace_key}/${uuid}.jsonl"
  python3 - "$jsonl" <<'PY' 2>/dev/null || echo 0
import json, os, sys
path = sys.argv[1]
if not os.path.exists(path):
    print(0); sys.exit(0)
try:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - 500_000))
        chunk = f.read().decode("utf-8", errors="ignore")
except Exception:
    print(0); sys.exit(0)
last = 0
for line in chunk.splitlines():
    try:
        d = json.loads(line)
        if d.get("type") == "assistant":
            u = d.get("message", {}).get("usage") or {}
            t = (u.get("input_tokens", 0)
                 + u.get("cache_read_input_tokens", 0)
                 + u.get("cache_creation_input_tokens", 0))
            if t:
                last = t
    except Exception:
        pass
print(last)
PY
}

json_get() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 1
  /usr/bin/sed -nE "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\1/p; s/.*\"${key}\"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p" "$file" | head -n1
}

acquire_lock() {
  local waited=0
  local queued=0
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    if [ "$queued" = "0" ]; then
      slack_remove_reaction "$CH" "$TS" "eyes"
      slack_add_reaction "$CH" "$TS" "zzz"
      queued=1
    fi
    sleep 1
    waited=$((waited + 1))
    if [ "$waited" -ge 1800 ]; then
      log "worker: stale session lock after ${waited}s, taking it"
      rm -rf "$LOCK_DIR"
      mkdir "$LOCK_DIR" 2>/dev/null || return 1
      break
    fi
  done
  if [ "$queued" = "1" ]; then
    slack_remove_reaction "$CH" "$TS" "zzz"
    slack_add_reaction "$CH" "$TS" "eyes"
  fi
}
release_lock() { rm -rf "$LOCK_DIR" 2>/dev/null; }

# ─── Resume vs fresh decision ─────────────────────────────────────────────────
RESUME_MODE="fresh"
RESUME_REASON="first_turn"
SESSION_UUID=""
TURN_COUNT=1
CONSECUTIVE_FAILURES=0
CURRENT_SKILL_HASH=""

if [ "$RESUME_SESSIONS" = "dm" ] || [ "$RESUME_SESSIONS" = "1" ]; then
  if [ "$RESUME_SESSIONS" = "dm" ] && [ "$IS_DM" != "1" ]; then
    RESUME_REASON="not_dm"
  else
    acquire_lock
    LOCK_HELD=1

    if [ -f "$SESSION_FILE" ]; then
      PEEK_USER=$(json_get "$SESSION_FILE" first_user_id)
      PEEK_MODEL=$(json_get "$SESSION_FILE" model)
      if [ "$PEEK_USER" = "$USER" ] && [ -n "$PEEK_MODEL" ] && [ "$PEEK_MODEL" != "$MODEL" ]; then
        log "worker: mid-thread model override ignored (requested=$MODEL, keeping=$PEEK_MODEL)"
        MODEL="$PEEK_MODEL"
      fi
    fi

    if [ -n "${MENTION_TEXT:-}" ] && echo "$MENTION_TEXT" | grep -qiE '(^|[[:space:]])!reset([[:space:]]|$)'; then
      rm -f "$SESSION_FILE"
      RESUME_REASON="reset"
    elif [ -f "$SESSION_FILE" ]; then
      STORED_UUID=$(json_get "$SESSION_FILE" session_uuid)
      STORED_USER=$(json_get "$SESSION_FILE" first_user_id)
      STORED_MODEL=$(json_get "$SESSION_FILE" model)
      STORED_HASH=$(json_get "$SESSION_FILE" skill_hash)
      STORED_TURNS=$(json_get "$SESSION_FILE" turn_count)
      STORED_FAILS=$(json_get "$SESSION_FILE" consecutive_failures)
      CURRENT_SKILL_HASH=$(skill_hash)

      if [ -z "$STORED_UUID" ]; then
        RESUME_REASON="corrupt_session_file"
      elif [ "$STORED_USER" != "$USER" ]; then
        RESUME_REASON="user_change"
      elif [ "$STORED_MODEL" != "$MODEL" ]; then
        RESUME_REASON="model_change"
      elif [ "$STORED_HASH" != "$CURRENT_SKILL_HASH" ]; then
        RESUME_REASON="skill_drift"
      elif [ "${STORED_FAILS:-0}" -ge 2 ] 2>/dev/null; then
        RESUME_REASON="poisoned"
      else
        CONTEXT_TOKENS=$(session_context_tokens "$STORED_UUID")
        if [ -n "$CONTEXT_TOKENS" ] && [ "$CONTEXT_TOKENS" -ge "$CONTEXT_CAP" ] 2>/dev/null; then
          RESUME_REASON="context_cap"
          log "worker: context_cap hit (tokens=$CONTEXT_TOKENS >= $CONTEXT_CAP)"
        else
          RESUME_MODE="resume"
          SESSION_UUID="$STORED_UUID"
          TURN_COUNT=$(( ${STORED_TURNS:-1} + 1 ))
          CONSECUTIVE_FAILURES="${STORED_FAILS:-0}"
        fi
      fi
    fi
  fi
fi

# ─── Load pre-fetched thread context sidecar ──────────────────────────────────
CONTEXT_BLOCK=""
if [ "${CONTEXT_MODE:-}" != "shadow" ] && [ -s "$CONTEXT_FILE" ]; then
  CONTEXT_BLOCK=$(cat "$CONTEXT_FILE")
fi

# ─── Build prompt ─────────────────────────────────────────────────────────────
if [ "$RESUME_MODE" = "resume" ]; then
  log "worker: RESUME (session=$SESSION_UUID, turn=$TURN_COUNT, dm=$IS_DM)"
  JOB_PROMPT=$(cat <<EOF
You are continuing a prior session in this Slack thread. A new mention just arrived.

- **Channel:** $CH
- **New message ts:** $TS
- **Posted by user:** $USER
- **Thread ts:** $THREAD_TS
- **Status file:** $STATUS_FILE — write \`success\`, \`failed\`, or \`clarification\` when done.
- **Turn number:** $TURN_COUNT

## Thread context

$CONTEXT_BLOCK

Before acting:
1. **New messages since your last turn** are in the "Thread context" section above. Image attachments are pre-downloaded to \`/tmp/slackimg_<F...>.<ext>\` — use \`Read\` on those paths. Do NOT call \`\$SLACK replies\`, \`\$SLACK history\`, or \`\$SLACK files\` at turn start unless the section is empty (pre-fetch failed — fallback to calling \`\$SLACK replies --ts $THREAD_TS\`).
2. Authorization re-check: this turn was sent by \`$USER\`. If that is NOT \`$AUTHORIZED_USER_ID\`$([ -n "${EXTRA_WRITE_USER_IDS:-}" ] && echo " or one of \`${EXTRA_WRITE_USER_IDS}\`"), refuse any write/destructive operations.
3. Your prior context and tool results are still in memory — use them. Don't re-derive what you already know.
4. Act ONLY on the new mention at $TS.

All formatting rules from turn 1 remain in force. Write status to \`$STATUS_FILE\` when done.
EOF
)
else
  if [ "$RESUME_MODE" = "fresh" ] && { [ "$RESUME_SESSIONS" = "dm" ] || [ "$RESUME_SESSIONS" = "1" ]; }; then
    log "worker: FRESH (reason=$RESUME_REASON, dm=$IS_DM)"
  fi
  # Build extra authorized users block for the prompt
  EXTRA_WRITE_USER_IDS="${EXTRA_WRITE_USER_IDS:-}"
  if [ -n "$EXTRA_WRITE_USER_IDS" ]; then
    EXTRA_AUTH_BLOCK="- Additional write-authorized users: \`${EXTRA_WRITE_USER_IDS}\`"
  else
    EXTRA_AUTH_BLOCK=""
  fi

  JOB_PROMPT=$(CH="$CH" TS="$TS" SLACK_USER="$USER" STATUS_FILE="$STATUS_FILE" \
               PROMPT_FILE="$PROMPT_FILE" CONTEXT_BLOCK="$CONTEXT_BLOCK" \
               AUTHORIZED_USER_ID="$AUTHORIZED_USER_ID" BOT_USER_ID="$BOT_USER_ID" \
               BOT_NAME="$BOT_NAME" EXTRA_AUTH_BLOCK="$EXTRA_AUTH_BLOCK" \
               EXTRA_WRITE_USER_IDS="$EXTRA_WRITE_USER_IDS" \
               python3 -c '
import os, sys
with open(os.environ["PROMPT_FILE"]) as f:
    p = f.read()
p = p.replace("{{TURN_MODE_BLOCK}}", "")
p = p.replace("{{CHANNEL}}", os.environ.get("CH", ""))
p = p.replace("{{TS}}", os.environ.get("TS", ""))
p = p.replace("{{USER}}", os.environ.get("SLACK_USER", ""))
p = p.replace("{{STATUS_FILE}}", os.environ.get("STATUS_FILE", ""))
p = p.replace("{{THREAD_CONTEXT}}", os.environ.get("CONTEXT_BLOCK", ""))
p = p.replace("{{AUTHORIZED_USER_ID}}", os.environ.get("AUTHORIZED_USER_ID", ""))
p = p.replace("{{EXTRA_AUTH_BLOCK}}", os.environ.get("EXTRA_AUTH_BLOCK", ""))
p = p.replace("{{EXTRA_WRITE_USER_IDS}}", os.environ.get("EXTRA_WRITE_USER_IDS", ""))
p = p.replace("{{BOT_USER_ID}}", os.environ.get("BOT_USER_ID", ""))
p = p.replace("{{BOT_NAME}}", os.environ.get("BOT_NAME", "ClaudeBot"))
sys.stdout.write(p)
')
fi

# ─── Spinner ──────────────────────────────────────────────────────────────────
echo "eyes" > "$EMOJI_FILE"
(
  IDX=0
  PREV="eyes"
  EMOJIS=($CYCLE_EMOJIS)
  while true; do
    sleep "$SPINNER_INTERVAL"
    IDX=$(( (IDX + 1) % ${#EMOJIS[@]} ))
    NEXT="${EMOJIS[$IDX]}"
    [ "$NEXT" = "$PREV" ] && continue
    slack_remove_reaction "$CH" "$TS" "$PREV"
    slack_add_reaction "$CH" "$TS" "$NEXT"
    echo "$NEXT" > "$EMOJI_FILE"
    PREV="$NEXT"
  done
) &
SPINNER_PID=$!

fire_final_emoji() {
  ( set -C; : > "$FIRED_MARKER" ) 2>/dev/null || return 0
  kill "$SPINNER_PID" 2>/dev/null
  wait "$SPINNER_PID" 2>/dev/null
  local status="unknown"
  if [ -f "$STATUS_FILE" ]; then
    status=$(cat "$STATUS_FILE" | tr -d '[:space:]')
  fi
  local emoji msg
  case "$status" in
    clarification)  emoji="speech_balloon";   msg="worker: clarification posted" ;;
    success)        emoji="white_check_mark"; msg="worker: completed" ;;
    failed|failure) emoji="x";                msg="worker: failed" ;;
    *)
      if [ "${CLAUDE_EXIT:-0}" = "0" ]; then
        emoji="white_check_mark"
      else
        emoji="x"
      fi
      msg="worker: claude exited ${CLAUDE_EXIT:-?} (status=$status)"
      ;;
  esac
  local current
  current=$(cat "$EMOJI_FILE" 2>/dev/null | tr -d '[:space:]')
  [ -z "$current" ] && current="eyes"
  slack_remove_reaction "$CH" "$TS" "$current" &
  slack_add_reaction "$CH" "$TS" "$emoji" &
  wait
  rm -f "$EMOJI_FILE"
  log "$msg"
}

# ─── Background watcher ───────────────────────────────────────────────────────
(
  ticks=0
  while [ ! -f "$STATUS_FILE" ] && [ ! -f "$WATCHER_DONE_MARKER" ] && [ "$ticks" -lt 14400 ]; do
    sleep 0.25
    ticks=$((ticks + 1))
  done
  [ -f "$STATUS_FILE" ] && fire_final_emoji
) &
WATCHER_PID=$!

# ─── Spawn Claude ─────────────────────────────────────────────────────────────
cd "$CLAUDE_WORKSPACE"

FEATURE_ON=0
if [ "$RESUME_SESSIONS" = "1" ] || { [ "$RESUME_SESSIONS" = "dm" ] && [ "$IS_DM" = "1" ]; }; then
  FEATURE_ON=1
fi

FAST_FLAG=()
FAST_LOG=""
if [ "$FAST" = "1" ]; then
  FAST_FLAG=(--settings '{"alwaysThinkingEnabled":false}')
  FAST_LOG=", thinking=off"
fi

CLAUDE_STDERR="${STATUS_DIR}/${TS//./_}.claude-stderr"

spawn_claude() {
  if [ "$RESUME_MODE" = "resume" ]; then
    log "worker: spawning claude (model=$MODEL${FAST_LOG}, resume=$SESSION_UUID)"
    "$CLAUDE_BIN" -p "$JOB_PROMPT" \
      --resume "$SESSION_UUID" \
      --model "$MODEL" \
      ${FAST_FLAG[@]+"${FAST_FLAG[@]}"} \
      --dangerously-skip-permissions \
      >> "$LOG_FILE" 2> "$CLAUDE_STDERR" &
  elif [ "$FEATURE_ON" = "1" ]; then
    SESSION_UUID=$(/usr/bin/uuidgen | tr '[:upper:]' '[:lower:]')
    log "worker: spawning claude (model=$MODEL${FAST_LOG}, session=$SESSION_UUID)"
    "$CLAUDE_BIN" -p "$JOB_PROMPT" \
      --session-id "$SESSION_UUID" \
      --model "$MODEL" \
      ${FAST_FLAG[@]+"${FAST_FLAG[@]}"} \
      --dangerously-skip-permissions \
      >> "$LOG_FILE" 2> "$CLAUDE_STDERR" &
  else
    log "worker: spawning claude (model=$MODEL${FAST_LOG})"
    "$CLAUDE_BIN" -p "$JOB_PROMPT" \
      --model "$MODEL" \
      ${FAST_FLAG[@]+"${FAST_FLAG[@]}"} \
      --dangerously-skip-permissions \
      >> "$LOG_FILE" 2> "$CLAUDE_STDERR" &
  fi
  CLAUDE_PID=$!

  (
    sleep "$CLAUDE_TIMEOUT"
    kill -TERM "$CLAUDE_PID" 2>/dev/null
    sleep 15
    kill -KILL "$CLAUDE_PID" 2>/dev/null
    pkill -P "$CLAUDE_PID" 2>/dev/null
  ) &
  KILLER_PID=$!

  SPAWN_START=$(date +%s)
  wait "$CLAUDE_PID" 2>/dev/null
  CLAUDE_EXIT=$?
  SPAWN_DURATION=$(( $(date +%s) - SPAWN_START ))
  kill "$KILLER_PID" 2>/dev/null
  wait "$KILLER_PID" 2>/dev/null
  pkill -P "$CLAUDE_PID" 2>/dev/null

  if [ "$CLAUDE_EXIT" = "143" ] || [ "$CLAUDE_EXIT" = "137" ]; then
    log "worker: claude timed out (SIGTERM/SIGKILL)"
  fi
}

spawn_claude

# Retry on instant-exit (<5s, non-resume): transient Claude CLI startup failure
if [ "$CLAUDE_EXIT" != "0" ] && [ "$SPAWN_DURATION" -lt 5 ] && [ "$RESUME_MODE" != "resume" ]; then
  STDERR_PREVIEW=$(head -c 400 "$CLAUDE_STDERR" 2>/dev/null | tr '\n' ' ')
  log "worker: claude instant-exit (exit=$CLAUDE_EXIT, ${SPAWN_DURATION}s, stderr=${STDERR_PREVIEW:-<empty>}) — retrying once"
  cp "$CLAUDE_STDERR" "${CLAUDE_STDERR}.attempt1" 2>/dev/null
  sleep 1
  spawn_claude
  if [ "$CLAUDE_EXIT" = "0" ]; then
    log "worker: retry succeeded"
  else
    STDERR_PREVIEW=$(head -c 400 "$CLAUDE_STDERR" 2>/dev/null | tr '\n' ' ')
    log "worker: retry also failed (exit=$CLAUDE_EXIT, ${SPAWN_DURATION}s, stderr=${STDERR_PREVIEW:-<empty>})"
  fi
fi

if [ -s "$CLAUDE_STDERR" ] && [ "$CLAUDE_EXIT" != "0" ]; then
  log "worker: claude stderr (first 1KB): $(head -c 1024 "$CLAUDE_STDERR" | tr '\n' ' ')"
fi
rm -f "$CLAUDE_STDERR"

# ─── Cleanup ──────────────────────────────────────────────────────────────────
touch "$WATCHER_DONE_MARKER"
wait "$WATCHER_PID" 2>/dev/null
fire_final_emoji

STATUS="unknown"
if [ -f "$STATUS_FILE" ]; then
  STATUS=$(cat "$STATUS_FILE" | tr -d '[:space:]')
fi
log "worker: claude teardown (exit=$CLAUDE_EXIT)"

# ─── Persist session state ────────────────────────────────────────────────────
if [ "$FEATURE_ON" = "1" ]; then
  if [ "$RESUME_MODE" = "fresh" ] && [ "$RESUME_REASON" = "user_change" ]; then
    log "worker: session_not_saved (reason=preserved_for_owner, visitor=$USER)"
  else
    {
      if [ -z "$CURRENT_SKILL_HASH" ]; then
        CURRENT_SKILL_HASH=$(skill_hash)
      fi
      if [ "$CLAUDE_EXIT" = "0" ] && { [ "$STATUS" = "success" ] || [ "$STATUS" = "clarification" ]; }; then
        NEW_FAILS=0
      else
        NEW_FAILS=$(( CONSECUTIVE_FAILURES + 1 ))
      fi
      FIRST_USER="$USER"
      if [ "$RESUME_MODE" = "resume" ]; then
        EXISTING=$(json_get "$SESSION_FILE" first_user_id)
        [ -n "$EXISTING" ] && FIRST_USER="$EXISTING"
      fi
      NOW=$(date +%s)
      cat > "$SESSION_FILE" <<EOF
{
  "session_uuid": "$SESSION_UUID",
  "channel": "$CH",
  "thread_ts": "$THREAD_TS",
  "first_user_id": "$FIRST_USER",
  "model": "$MODEL",
  "turn_count": $TURN_COUNT,
  "consecutive_failures": $NEW_FAILS,
  "skill_hash": "$CURRENT_SKILL_HASH",
  "last_turn_ts": "$TS",
  "updated_at": $NOW
}
EOF
      log "worker: session_saved (uuid=$SESSION_UUID, turn=$TURN_COUNT, fails=$NEW_FAILS)"
    }
  fi
fi
