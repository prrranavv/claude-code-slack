#!/bin/bash
# GMAIL_CMD for quote-present on the team@ client; see quote_gmail_shim.ts
export PATH="$HOME/.bun/bin:$PATH"
exec bun "$(dirname "$0")/quote_gmail_shim.ts" "$@"
