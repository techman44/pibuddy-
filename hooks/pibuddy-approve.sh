#!/usr/bin/env bash
# PiBuddy blocking PreToolUse approval hook for Claude Code.
#
# Sends the pending tool call to the Pi and waits for you to tap
# Approve or Deny on the touchscreen. If nobody answers in time (or the
# Pi is unreachable) the hook emits nothing and exits 0, so Claude Code
# falls back to its normal terminal permission prompt.
#
# Configuration (baked in by install-hooks.py, or via environment):
#   PIBUDDY_URL              e.g. http://pibuddy.local:8765
#   PIBUDDY_TOKEN            shared secret, if configured
#   PIBUDDY_APPROVAL_WAIT    seconds to wait for a tap (default 45)

PIBUDDY_URL="${PIBUDDY_URL:-http://pibuddy.local:8765}"
WAIT="${PIBUDDY_APPROVAL_WAIT:-45}"

payload=$(cat)

response=$(curl --silent --max-time "$((WAIT + 5))" \
  --header "Content-Type: application/json" \
  --header "X-PiBuddy-Token: ${PIBUDDY_TOKEN:-}" \
  --data "$payload" \
  "$PIBUDDY_URL/api/approval?wait=$WAIT" 2>/dev/null)

decision=$(printf '%s' "$response" | sed -n 's/.*"decision"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p')

case "$decision" in
  allow)
    printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"Approved on PiBuddy"}}'
    ;;
  deny)
    printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Denied on PiBuddy"}}'
    ;;
  *)
    # Timeout / unreachable: stay silent and let the terminal prompt handle it.
    ;;
esac

exit 0
