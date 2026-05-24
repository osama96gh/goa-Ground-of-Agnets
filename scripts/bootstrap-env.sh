#!/usr/bin/env bash
# Self-healing .env for first-run developers.
#
# Behavior:
#   1. If .env is missing, copy from .env.example.
#   2. If GOA_SERVER_PEPPER is unset or still the placeholder `dev-pepper`,
#      replace it with a random 32-byte hex value. The pepper is used only
#      server-side for hashing API keys, so devs never need to type or read
#      it — randomizing is pure win.
#   3. GOA_ADMIN_TOKEN is intentionally left alone. It's the value the dev
#      types into the dashboard (or curls /admin/* with), so it stays as a
#      short memorable string. Rotate before deploying.
#
# Idempotent: re-running on a populated .env with a real pepper is a no-op.

set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE=".env"
ENV_EXAMPLE=".env.example"

created=0
randomized_pepper=0

if [ ! -f "$ENV_FILE" ]; then
  if [ ! -f "$ENV_EXAMPLE" ]; then
    echo "error: $ENV_EXAMPLE is missing — cannot bootstrap" >&2
    exit 1
  fi
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  created=1
fi

# Extract current pepper value (handles missing key, empty value, quoted value).
current_pepper="$(awk -F= '/^[[:space:]]*GOA_SERVER_PEPPER[[:space:]]*=/ {
  sub(/^[[:space:]]*GOA_SERVER_PEPPER[[:space:]]*=[[:space:]]*/, "", $0);
  gsub(/^["'"'"']|["'"'"']$/, "", $0);
  print; exit
}' "$ENV_FILE")"

if [ -z "${current_pepper:-}" ] || [ "$current_pepper" = "dev-pepper" ]; then
  new_pepper="$(openssl rand -hex 32)"
  # Use a temp file + mv for portability (BSD sed vs GNU sed -i differ).
  tmp="$(mktemp)"
  if grep -q '^[[:space:]]*GOA_SERVER_PEPPER[[:space:]]*=' "$ENV_FILE"; then
    awk -v new="$new_pepper" '
      /^[[:space:]]*GOA_SERVER_PEPPER[[:space:]]*=/ { print "GOA_SERVER_PEPPER=" new; next }
      { print }
    ' "$ENV_FILE" > "$tmp"
  else
    cat "$ENV_FILE" > "$tmp"
    printf '\nGOA_SERVER_PEPPER=%s\n' "$new_pepper" >> "$tmp"
  fi
  mv "$tmp" "$ENV_FILE"
  randomized_pepper=1
fi

if [ "$created" = "1" ] || [ "$randomized_pepper" = "1" ]; then
  echo "──────────────────────────────────────────────────────────────"
  if [ "$created" = "1" ]; then
    echo "  Created .env from .env.example."
  fi
  if [ "$randomized_pepper" = "1" ]; then
    echo "  Generated a random GOA_SERVER_PEPPER for local dev."
  fi
  echo "  GOA_ADMIN_TOKEN left as-is — rotate before deploying."
  echo "──────────────────────────────────────────────────────────────"
fi
