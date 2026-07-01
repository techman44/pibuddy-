#!/usr/bin/env bash
# PiBuddy blocking PreToolUse approval hook for Claude Code.
#
# Sends the pending tool call to the Pi — enriched with Claude's last
# message from the session transcript, so the screen shows *why* it's
# asking — and waits for you to tap Approve / Deny / Terminal on the
# touchscreen (or the phone remote). If nobody answers in time, the Pi
# is unreachable, or you tapped "Terminal…", the hook emits nothing and
# exits 0, so Claude Code falls back to its normal terminal permission
# prompt with the full option list.
#
# Configuration (baked in by install-hooks.py, or via environment):
#   PIBUDDY_URL              e.g. http://pibuddy.local:8765
#   PIBUDDY_TOKEN            shared secret, if configured
#   PIBUDDY_APPROVAL_WAIT    seconds to wait for a tap (default 45)

PIBUDDY_URL="${PIBUDDY_URL:-http://pibuddy.local:8765}"
WAIT="${PIBUDDY_APPROVAL_WAIT:-45}"

payload=$(cat)

# Enrich the payload with Claude's most recent message (best-effort;
# transcripts live on this machine, not on the Pi).
if command -v python3 >/dev/null 2>&1; then
  enriched=$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    payload = json.load(sys.stdin)
    path = payload.get("transcript_path")
    context = ""
    if path:
        with open(path, "rb") as f:
            lines = f.readlines()[-80:]
        for raw in reversed(lines):
            try:
                entry = json.loads(raw)
            except Exception:
                continue
            if entry.get("type") != "assistant":
                continue
            content = (entry.get("message") or {}).get("content") or []
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = " ".join(t for t in texts if t).strip()
            if text:
                context = text[-500:]
                break
    if context:
        payload["pibuddy_context"] = context
    print(json.dumps(payload))
except Exception:
    pass  # print nothing; the shell keeps the original payload
' 2>/dev/null)
  [ -n "$enriched" ] && payload="$enriched"
fi

response=$(printf '%s' "$payload" | curl --silent --max-time "$((WAIT + 5))" \
  --header "Content-Type: application/json" \
  --header "X-PiBuddy-Token: ${PIBUDDY_TOKEN:-}" \
  --data @- \
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
    # "pass", timeout, or unreachable: stay silent so the terminal
    # prompt (with its full options) handles it.
    ;;
esac

exit 0
