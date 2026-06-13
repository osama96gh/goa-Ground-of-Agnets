#!/usr/bin/env bash
# Self-healing env files for first-run developers.
#
# Goa uses two separate, self-contained env files:
#
#   .env.local   →  `make demo`   (native hub on the host)
#   .env.docker  →  `make up`     (containerized hub via docker compose)
#
# Neither file overlays the other — each is complete on its own. A few
# values are duplicated across both (secrets, blob creds); we keep them
# in sync on first bootstrap by generating a single pepper and writing
# it to both. After that, rotating one file's pepper is a deliberate
# act and does not propagate.
#
# Behavior:
#   1. If .env.local is missing, copy from .env.local.example.
#   2. If .env.docker is missing, copy from .env.docker.example.
#   3. Collect every file whose GOA_SERVER_PEPPER is unset, empty, or
#      still the `dev-pepper` placeholder, then write a single random
#      pepper to all of them. This keeps the two files agreeing on
#      first run (so agents registered via `make demo` keep working
#      under `make up` and vice versa) without overwriting peppers a
#      user has already customized.
#   4. GOA_ADMIN_TOKEN is intentionally left as-is. Devs type it into
#      the dashboard; rotate before exposing publicly.
#
# Idempotent: re-running with both files present and real peppers is a no-op.

set -euo pipefail

cd "$(dirname "$0")/.."

LOCAL_FILE=".env.local"
LOCAL_EXAMPLE=".env.local.example"
DOCKER_FILE=".env.docker"
DOCKER_EXAMPLE=".env.docker.example"

created_local=0
created_docker=0

if [ ! -f "$LOCAL_FILE" ]; then
  if [ ! -f "$LOCAL_EXAMPLE" ]; then
    echo "error: $LOCAL_EXAMPLE is missing — cannot bootstrap" >&2
    exit 1
  fi
  cp "$LOCAL_EXAMPLE" "$LOCAL_FILE"
  created_local=1
fi

if [ ! -f "$DOCKER_FILE" ]; then
  if [ ! -f "$DOCKER_EXAMPLE" ]; then
    echo "error: $DOCKER_EXAMPLE is missing — cannot bootstrap" >&2
    exit 1
  fi
  cp "$DOCKER_EXAMPLE" "$DOCKER_FILE"
  created_docker=1
fi

# Extract current pepper value from a file (handles missing key, empty
# value, quoted value). Prints empty string when absent.
read_pepper() {
  local file="$1"
  awk -F= '/^[[:space:]]*GOA_SERVER_PEPPER[[:space:]]*=/ {
    sub(/^[[:space:]]*GOA_SERVER_PEPPER[[:space:]]*=[[:space:]]*/, "", $0);
    gsub(/^["'"'"']|["'"'"']$/, "", $0);
    print; exit
  }' "$file"
}

# Replace (or append) GOA_SERVER_PEPPER in a file with the given value.
write_pepper() {
  local file="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  if grep -q '^[[:space:]]*GOA_SERVER_PEPPER[[:space:]]*=' "$file"; then
    awk -v new="$value" '
      /^[[:space:]]*GOA_SERVER_PEPPER[[:space:]]*=/ { print "GOA_SERVER_PEPPER=" new; next }
      { print }
    ' "$file" > "$tmp"
  else
    cat "$file" > "$tmp"
    printf '\nGOA_SERVER_PEPPER=%s\n' "$value" >> "$tmp"
  fi
  mv "$tmp" "$file"
}

# Collect files needing a fresh pepper.
needs_pepper=()
for f in "$LOCAL_FILE" "$DOCKER_FILE"; do
  current="$(read_pepper "$f")"
  if [ -z "$current" ] || [ "$current" = "dev-pepper" ]; then
    needs_pepper+=("$f")
  fi
done

randomized=0
if [ "${#needs_pepper[@]}" -gt 0 ]; then
  new_pepper="$(openssl rand -hex 32)"
  for f in "${needs_pepper[@]}"; do
    write_pepper "$f" "$new_pepper"
  done
  randomized=1
fi

if [ "$created_local" = "1" ] || [ "$created_docker" = "1" ] || [ "$randomized" = "1" ]; then
  echo "──────────────────────────────────────────────────────────────"
  if [ "$created_local" = "1" ]; then
    echo "  Created $LOCAL_FILE from $LOCAL_EXAMPLE."
  fi
  if [ "$created_docker" = "1" ]; then
    echo "  Created $DOCKER_FILE from $DOCKER_EXAMPLE."
  fi
  if [ "$randomized" = "1" ]; then
    if [ "${#needs_pepper[@]}" -gt 1 ]; then
      echo "  Generated a random GOA_SERVER_PEPPER (written to both files)."
    else
      echo "  Generated a random GOA_SERVER_PEPPER in ${needs_pepper[0]}."
    fi
  fi
  echo "  GOA_ADMIN_TOKEN left as-is — rotate before exposing publicly."
  echo "──────────────────────────────────────────────────────────────"
fi
