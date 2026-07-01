#!/usr/bin/env bash
# PiBuddy fire-and-forget event hook for Claude Code.
#
# Claude Code pipes the hook payload JSON to stdin; we forward it to the
# PiBuddy server in the background with a hard timeout, so a powered-off
# Pi can never slow a session down.
#
# Configuration (baked in by install-hooks.py, or via environment):
#   PIBUDDY_URL    e.g. http://pibuddy.local:8765
#   PIBUDDY_TOKEN  shared secret, if the server has one configured

PIBUDDY_URL="${PIBUDDY_URL:-http://pibuddy.local:8765}"

payload=$(cat)

curl --silent --output /dev/null --max-time 2 \
  --header "Content-Type: application/json" \
  --header "X-PiBuddy-Token: ${PIBUDDY_TOKEN:-}" \
  --data "$payload" \
  "$PIBUDDY_URL/api/event" >/dev/null 2>&1 &

exit 0
