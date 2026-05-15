#!/bin/bash
# Nightly GC for claude-slack-bot state. Deletes:
#   1. Session metadata files (sessions/*.json) older than SESSION_AGE_DAYS.
#   2. Claude Code project JSONL transcripts not referenced by any live session
#      AND older than JSONL_AGE_DAYS.
#
# Invoked by launchd per com.claude-slack-bot-gc.plist.
# Safe to run manually: bash gc.sh

set -u

INSTALL_DIR="${CLAUDE_SLACK_INSTALL_DIR:-${HOME}/.claude/claude-slack-bot}"
SESSION_DIR="${INSTALL_DIR}/sessions"
LOG_FILE="${INSTALL_DIR}/watch.log"
CLAUDE_WORKSPACE="${CLAUDE_WORKSPACE:-${HOME}}"

# Derive the project key the same way Claude Code does: replace / with -
WORKSPACE_KEY="${CLAUDE_WORKSPACE//\//-}"
JSONL_DIR="${HOME}/.claude/projects/${WORKSPACE_KEY}"

SESSION_AGE_DAYS="${SESSION_AGE_DAYS:-7}"
JSONL_AGE_DAYS="${JSONL_AGE_DAYS:-7}"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] gc: $*" >> "$LOG_FILE"
}

log "=== gc start ==="

# 1. Prune old session files
SESS_DELETED=0
if [ -d "$SESSION_DIR" ]; then
  while IFS= read -r f; do
    rm -f "$f" && SESS_DELETED=$((SESS_DELETED + 1))
  done < <(/usr/bin/find "$SESSION_DIR" -type f -name '*.json' -mtime "+${SESSION_AGE_DAYS}")
fi
log "sessions pruned: $SESS_DELETED (age > ${SESSION_AGE_DAYS}d)"

# 2. Collect live session UUIDs
LIVE_UUIDS_FILE=$(mktemp)
trap 'rm -f "$LIVE_UUIDS_FILE"' EXIT
if [ -d "$SESSION_DIR" ]; then
  /usr/bin/grep -hE '"session_uuid"' "$SESSION_DIR"/*.json 2>/dev/null \
    | /usr/bin/sed -nE 's/.*"session_uuid"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' \
    | /usr/bin/sort -u \
    > "$LIVE_UUIDS_FILE" || true
fi
LIVE_COUNT=$(/usr/bin/wc -l < "$LIVE_UUIDS_FILE" | /usr/bin/tr -d ' ')
log "live session uuids: $LIVE_COUNT"

# 3. Prune orphaned JSONLs
JSONL_DELETED=0
JSONL_BYTES_FREED=0
if [ -d "$JSONL_DIR" ]; then
  while IFS= read -r f; do
    UUID=$(/usr/bin/basename "$f" .jsonl)
    if /usr/bin/grep -qxF "$UUID" "$LIVE_UUIDS_FILE"; then
      continue
    fi
    SZ=$(/usr/bin/stat -f "%z" "$f" 2>/dev/null || echo 0)
    rm -f "$f" && JSONL_DELETED=$((JSONL_DELETED + 1)) && JSONL_BYTES_FREED=$((JSONL_BYTES_FREED + SZ))
  done < <(/usr/bin/find "$JSONL_DIR" -maxdepth 1 -type f -name '*.jsonl' -mtime "+${JSONL_AGE_DAYS}")
fi
MB_FREED=$(( JSONL_BYTES_FREED / 1024 / 1024 ))
log "orphan jsonls pruned: $JSONL_DELETED (age > ${JSONL_AGE_DAYS}d, freed ~${MB_FREED}MB)"

log "=== gc done ==="
