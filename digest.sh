#!/bin/bash
# Daily digest of claude-slack-bot activity.
# Appends a single-line summary to watch.log covering the last 24h.
#
# Invoked by launchd per com.claude-slack-bot-digest.plist.
# Safe to run manually: bash digest.sh

set -u

INSTALL_DIR="${CLAUDE_SLACK_INSTALL_DIR:-${HOME}/.claude/claude-slack-bot}"
LOG_FILE="${INSTALL_DIR}/watch.log"
SESSION_DIR="${INSTALL_DIR}/sessions"

CUTOFF=$(/bin/date -v-24H '+%Y-%m-%d %H:%M:%S')
RECENT=$(/usr/bin/awk -v cutoff="[$CUTOFF]" '$0 >= cutoff' "$LOG_FILE" 2>/dev/null)

count() {
  printf '%s' "$RECENT" | /usr/bin/grep -cE "$1" || true
}

count_reason() {
  printf '%s' "$RECENT" \
    | /usr/bin/grep "worker: FRESH" \
    | /usr/bin/awk -F'reason=' '{print $2}' \
    | /usr/bin/awk -F'[,)]' '{print $1}' \
    | /usr/bin/grep -cFx "$1" || true
}

MENTIONS=$(count "NEW MENTION")
RESUMES=$(count "worker: RESUME")
FRESH_TOTAL=$(count "worker: FRESH")
FR_FIRST=$(count_reason "first_turn")
FR_USER_CHG=$(count_reason "user_change")
FR_MODEL_CHG=$(count_reason "model_change")
FR_SKILL_DRIFT=$(count_reason "skill_drift")
FR_POISONED=$(count_reason "poisoned")
FR_CONTEXT_CAP=$(count_reason "context_cap")
FR_RESET=$(count_reason "reset")
FR_NOT_DM=$(count_reason "not_dm")
VISITOR_SAVES=$(count "session_not_saved")
MODEL_OVERRIDES=$(count "mid-thread model override ignored")
STALE_LOCKS=$(count "stale session lock")
FAILED=$(count "worker: failed")
CLARIFICATIONS=$(count "worker: clarification posted")
SESSION_FILES=$(/bin/ls -1 "$SESSION_DIR"/*.json 2>/dev/null | /usr/bin/wc -l | /usr/bin/tr -d ' ')

if [ "$((RESUMES + FR_FIRST))" -gt 0 ]; then
  RESUME_RATE=$(/usr/bin/awk "BEGIN {printf \"%.0f\", 100 * $RESUMES / ($RESUMES + $FR_FIRST)}")
else
  RESUME_RATE="n/a"
fi

{
  echo "[$(/bin/date '+%Y-%m-%d %H:%M:%S')] digest 24h: mentions=$MENTIONS, resume=$RESUMES ($RESUME_RATE% of eligible), fresh=$FRESH_TOTAL (first=$FR_FIRST drift=$FR_SKILL_DRIFT user_chg=$FR_USER_CHG ctx_cap=$FR_CONTEXT_CAP model_chg=$FR_MODEL_CHG poisoned=$FR_POISONED reset=$FR_RESET not_dm=$FR_NOT_DM), visitor_saves=$VISITOR_SAVES, model_locks=$MODEL_OVERRIDES, stale_locks=$STALE_LOCKS, failed=$FAILED, clarifications=$CLARIFICATIONS, live_sessions=$SESSION_FILES"
} >> "$LOG_FILE"
