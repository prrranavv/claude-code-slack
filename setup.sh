#!/bin/bash
# claude-code-slack setup script
# Installs the bot to INSTALL_DIR, creates venv, generates plists, loads launchd services.
#
# Usage: bash setup.sh [--install-dir /path/to/dir]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-${HOME}/.claude/claude-slack-bot}"
LAUNCHAGENTS_DIR="${HOME}/Library/LaunchAgents"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║       claude-code-slack  setup           ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ─── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "Install directory: $INSTALL_DIR"
echo ""

# ─── Preflight ────────────────────────────────────────────────────────────────
if ! command -v claude >/dev/null 2>&1 && \
   ! [ -x "/opt/homebrew/bin/claude" ] && \
   ! [ -x "/usr/local/bin/claude" ]; then
  echo "ERROR: Claude Code CLI not found. Install it first:"
  echo "  https://claude.ai/download"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found."
  exit 1
fi

# ─── Create install dir & copy files ──────────────────────────────────────────
mkdir -p "$INSTALL_DIR/sessions" "$INSTALL_DIR/slack-scripts/templates"

for f in socket_daemon.py worker.sh detach.py job-prompt.md digest.sh gc.sh requirements.txt; do
  cp "${SCRIPT_DIR}/${f}" "${INSTALL_DIR}/${f}"
done

cp "${SCRIPT_DIR}/slack-scripts/slack-api.py" "${INSTALL_DIR}/slack-scripts/slack-api.py"
cp "${SCRIPT_DIR}/slack-scripts/templates/"*.json "${INSTALL_DIR}/slack-scripts/templates/"

chmod +x "${INSTALL_DIR}/detach.py" "${INSTALL_DIR}/worker.sh" \
         "${INSTALL_DIR}/digest.sh" "${INSTALL_DIR}/gc.sh"

echo "✓ Files copied to $INSTALL_DIR"

# ─── Config ───────────────────────────────────────────────────────────────────
CONFIG_FILE="${INSTALL_DIR}/config.env"

if [ -f "$CONFIG_FILE" ]; then
  echo ""
  echo "config.env already exists at $CONFIG_FILE"
  echo "Skipping — edit it directly if you need to change values."
else
  echo ""
  echo "Let's configure the bot. You'll need:"
  echo "  • A Slack Bot Token (xoxb-...) — from api.slack.com → OAuth & Permissions"
  echo "  • A Slack App Token (xapp-...) — from api.slack.com → Basic Information → App-Level Tokens"
  echo "  • Your bot's Slack user ID"
  echo "  • Your own Slack user ID"
  echo ""

  read -rp "SLACK_BOT_TOKEN (xoxb-...): " BOT_TOKEN
  read -rp "SLACK_APP_TOKEN (xapp-...): " APP_TOKEN
  read -rp "BOT_USER_ID (U...): " BOT_USER_ID
  read -rp "AUTHORIZED_USER_ID (your Slack user ID, U...): " AUTH_USER_ID
  read -rp "AUTHORIZED_USER_NAME (your name, for 'Stopped by X' messages): " AUTH_USER_NAME
  read -rp "CLAUDE_WORKSPACE (project dir for Claude to work in, default=$HOME): " WORKSPACE
  WORKSPACE="${WORKSPACE:-$HOME}"

  cat > "$CONFIG_FILE" <<EOF
SLACK_BOT_TOKEN=${BOT_TOKEN}
SLACK_APP_TOKEN=${APP_TOKEN}
BOT_USER_ID=${BOT_USER_ID}
AUTHORIZED_USER_ID=${AUTH_USER_ID}
AUTHORIZED_USER_NAME=${AUTH_USER_NAME}
CLAUDE_WORKSPACE=${WORKSPACE}
# RESUME_SESSIONS=1
# MAX_PARALLEL=10
# BOT_NAME=ClaudeBot
# FORWARD_CHANNEL=C
EOF

  chmod 600 "$CONFIG_FILE"
  echo "✓ config.env written (mode 600)"
fi

# ─── Python venv ──────────────────────────────────────────────────────────────
echo ""
echo "Setting up Python venv..."
python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install -q --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"
echo "✓ venv created at ${INSTALL_DIR}/.venv"

# ─── Generate plists ──────────────────────────────────────────────────────────
echo ""
echo "Generating launchd plists..."
mkdir -p "$LAUNCHAGENTS_DIR"

for template in "${SCRIPT_DIR}/launchagents/"*.plist.template; do
  basename_no_template="$(basename "$template" .template)"
  out="${LAUNCHAGENTS_DIR}/${basename_no_template}"
  sed -e "s|%%INSTALL_DIR%%|${INSTALL_DIR}|g" \
      -e "s|%%HOME%%|${HOME}|g" \
      "$template" > "$out"
  echo "  → $out"
done

# ─── Load launchd services ────────────────────────────────────────────────────
echo ""
echo "Loading launchd services..."

for plist in "${LAUNCHAGENTS_DIR}/com.claude-slack-bot.plist" \
             "${LAUNCHAGENTS_DIR}/com.claude-slack-bot-digest.plist" \
             "${LAUNCHAGENTS_DIR}/com.claude-slack-bot-gc.plist"; do
  label="$(basename "$plist" .plist)"
  # Unload first if already loaded (ignore errors)
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "  ✓ loaded $label"
done

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Setup complete! The bot is now running.             ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "Live log:"
echo "  tail -f ${INSTALL_DIR}/watch.log"
echo ""
echo "Stop the bot:"
echo "  launchctl unload ~/Library/LaunchAgents/com.claude-slack-bot.plist"
echo ""
echo "Restart after config changes:"
echo "  launchctl unload ~/Library/LaunchAgents/com.claude-slack-bot.plist"
echo "  launchctl load   ~/Library/LaunchAgents/com.claude-slack-bot.plist"
echo ""
echo "Invite the bot to a Slack channel: /invite @YourBotName"
echo "Then @mention it to test: @YourBotName hello"
