#!/usr/bin/env bash
# 04_close.sh — close the task. Initiator-only, idempotent.
#
# Closing releases the `(initiator, external_ref)` slot so the same
# external_ref can be `upsert`-ed to a fresh task later (§8). Subsequent
# event appends to a closed task return 409 invalid_state.

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

echo "→ POST $BASE_URL/tasks/$TASK_ID/close"
response="$(
  curl -sS -X POST "$BASE_URL/tasks/$TASK_ID/close" \
    -H "Authorization: Bearer $CHAT_API_KEY"
)"
status="$(echo "$response" | jq -r '.task.status')"
echo "  task $TASK_ID is now: $status"
