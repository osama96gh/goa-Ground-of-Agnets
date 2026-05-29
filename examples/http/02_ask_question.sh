#!/usr/bin/env bash
# 02_ask_question.sh — append a question event addressed to the support agent.
#
# Mirrors `OutboundQuestion(payload=QuestionPayload(to=[support_id]), ...)`
# in the Python example (examples/chat-service/cli.py:52-55). The wire
# event has three fields: `event_type` (discriminator), `payload`
# (per-type — for questions it carries the `to` recipients), and
# `content` (text/data/attachments).
#
# Including the word "refund" triggers the support agent's sub-task
# branch in the demo (it spawns a private sub-task to payments before
# answering us). The customer never sees the sub-task — that's §5's
# visibility rule in action.

set -euo pipefail
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

MESSAGE="${1:-hi, can I get a refund for order #42?}"

echo "→ POST $BASE_URL/tasks/$TASK_ID/events"
echo "  message: \"$MESSAGE\""
response="$(
  curl -sS -X POST "$BASE_URL/tasks/$TASK_ID/events" \
    -H "Authorization: Bearer $CHAT_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg msg "$MESSAGE" --arg to "$SUPPORT_ID" '{
      event_type: "question",
      payload: { to: [$to] },
      content: { text: $msg }
    }')"
)"

EVENT_ID="$(echo "$response" | jq -r '.event.id')"
echo "  question event $EVENT_ID posted; support agent now has a pending question."
echo "  next: bash 03_stream_answer.sh"
