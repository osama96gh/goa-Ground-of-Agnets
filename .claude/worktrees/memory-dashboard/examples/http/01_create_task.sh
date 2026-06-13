#!/usr/bin/env bash
# 01_create_task.sh — find-or-create the customer thread's task.
#
# Mirrors `client.upsert_task(external_ref=thread, ...)` in the Python
# example (cf. examples/chat-service/cli.py:50-57 — the SDK calls
# `upsert_task` then `append_event` as one sugar method
# `upsert_and_send`; here we do the two HTTP calls separately so the
# wire shape is obvious).
#
# The `(initiator, external_ref)` pair is the idempotency key: re-running
# this script returns the existing task without creating a duplicate.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env.http ]; then
  echo "error: .env.http missing — run 00_register.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1091
. ./.env.http

EXTERNAL_REF="curl-demo-thread"

echo "→ POST $BASE_URL/tasks/upsert"
response="$(
  curl -sS -X POST "$BASE_URL/tasks/upsert" \
    -H "Authorization: Bearer $CHAT_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg ref "$EXTERNAL_REF" '{
      external_ref: $ref,
      on_create: {
        subject: ("curl-driven thread " + $ref)
      }
    }')"
)"

TASK_ID="$(echo "$response" | jq -r '.task.id')"
created="$(echo "$response" | jq -r '.created')"
if [ "$created" = "true" ]; then
  echo "  created task $TASK_ID"
else
  echo "  resumed existing task $TASK_ID"
fi

# Append TASK_ID to .env.http (replace prior line if any).
grep -v '^TASK_ID=' .env.http > .env.http.tmp || true
echo "TASK_ID=$TASK_ID" >> .env.http.tmp
mv .env.http.tmp .env.http
echo "→ wrote TASK_ID to .env.http"
