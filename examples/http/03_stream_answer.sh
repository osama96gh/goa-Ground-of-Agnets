#!/usr/bin/env bash
# 03_stream_answer.sh — subscribe to /stream, print the support agent's answer.
#
# Mirrors `async with client.stream() as frames: ... AnswerEvent ...` in
# the Python example (examples/chat-service/cli.py:44-69). The SSE
# subscription delivers every event addressed to us; we filter for an
# `AnswerEvent` in our task and exit on the first match.
#
# SSE frame shape from the hub: each frame is two lines plus a blank:
#   event: event
#   id: <monotonic stream id>
#   data: {"task_id": "...", "event": {"event_type": "answer", ...}, "task": {...}}
#
# `event: ping` frames arrive every ~20s as keepalives; we ignore them.
# We bound the wait with a background sleeper that kills curl — portable
# across macOS (no GNU `timeout`) and Linux.

set -uo pipefail
cd "$(dirname "$0")"

if [ ! -f .env.http ]; then
  echo "error: .env.http missing — run 00_register.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1091
. ./.env.http

if [ -z "${TASK_ID:-}" ]; then
  echo "error: TASK_ID missing from .env.http — run 01_create_task.sh first." >&2
  exit 1
fi

WAIT_SECONDS="${WAIT_SECONDS:-30}"

echo "→ GET $BASE_URL/stream  (waiting up to ${WAIT_SECONDS}s for an answer in task $TASK_ID)"

# Stream into a FIFO so we can read line-by-line and tear curl down on
# match. The background sleeper bounds the wait if no answer arrives.
fifo="$(mktemp -u)"
mkfifo "$fifo"

curl -sN \
  -H "Authorization: Bearer $CHAT_API_KEY" \
  "$BASE_URL/stream" > "$fifo" 2>/dev/null &
curl_pid=$!
disown "$curl_pid" 2>/dev/null || true   # suppress "Terminated" job message

( sleep "$WAIT_SECONDS"; kill "$curl_pid" 2>/dev/null || true ) &
sleeper_pid=$!
disown "$sleeper_pid" 2>/dev/null || true

cleanup() {
  kill "$curl_pid" 2>/dev/null || true
  kill "$sleeper_pid" 2>/dev/null || true
  rm -f "$fifo"
}
trap cleanup EXIT

got_answer=0
while IFS= read -r line; do
  case "$line" in
    "data: "*)
      payload="${line#data: }"
      event_type="$(echo "$payload" | jq -r '.event.event_type // empty' 2>/dev/null || true)"
      task_id="$(echo "$payload" | jq -r '.task_id // empty' 2>/dev/null || true)"
      if [ "$event_type" = "answer" ] && [ "$task_id" = "$TASK_ID" ]; then
        text="$(echo "$payload" | jq -r '.event.content.text // empty')"
        echo "  ← answer: $text"
        got_answer=1
        break
      fi
      ;;
  esac
done < "$fifo"

if [ "$got_answer" = "0" ]; then
  echo "  no answer within ${WAIT_SECONDS}s — is the support-agent running? (\`make demo\`)" >&2
  exit 1
fi
