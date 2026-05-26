#!/usr/bin/env bash
# Pre-flight check before `make deploy` runs `docker compose up`.
#
# Refuses to start the stack if .env.deploy has:
#   - Missing required vars (GOA_DOMAIN, GOA_SERVER_PEPPER, GOA_ADMIN_TOKEN,
#     GOA_DATABASE_URL)
#   - Placeholder secrets that obviously haven't been rotated
#     (`dev-pepper`, `changeme`, `dev-admin-token`, empty strings)
#
# Compose itself would also fail on missing-required vars (see the `:?`
# syntax in docker-compose.yml), but this script catches the
# placeholder-leakage case earlier and with a clearer message.

set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE=".env.deploy"

if [ ! -f "$ENV_FILE" ]; then
  echo "error: $ENV_FILE missing" >&2
  exit 1
fi

# Source the file in a subshell to grab the values without leaking them
# into this script's env (and back to the make process).
get() {
  local key="$1"
  awk -F= -v k="$key" '
    /^[[:space:]]*#/ { next }
    $0 ~ "^[[:space:]]*"k"[[:space:]]*=" {
      sub(/^[[:space:]]*[^=]+=[[:space:]]*/, "", $0)
      gsub(/^["'"'"']|["'"'"']$/, "", $0)
      print
      exit
    }
  ' "$ENV_FILE"
}

errors=()

# ─── Required vars ──────────────────────────────────────────
for var in GOA_DOMAIN GOA_SERVER_PEPPER GOA_ADMIN_TOKEN GOA_DATABASE_URL; do
  if [ -z "$(get "$var")" ]; then
    errors+=("$var is empty or unset in $ENV_FILE")
  fi
done

# ─── Placeholder rejection ──────────────────────────────────
PEPPER="$(get GOA_SERVER_PEPPER)"
case "$PEPPER" in
  dev-pepper|changeme|change-me|placeholder|""|secret|password)
    errors+=("GOA_SERVER_PEPPER looks like a placeholder ($PEPPER) — generate with: openssl rand -hex 32")
    ;;
esac

ADMIN_TOKEN="$(get GOA_ADMIN_TOKEN)"
case "$ADMIN_TOKEN" in
  dev-admin-token|dev|changeme|change-me|admin|password|""|secret)
    errors+=("GOA_ADMIN_TOKEN looks like a placeholder ($ADMIN_TOKEN) — generate with: openssl rand -hex 32")
    ;;
esac

# Catch the Supabase port-6543 footgun explicitly — better to fail loudly
# now than have asyncpg explode at runtime with a less-obvious error.
DB_URL="$(get GOA_DATABASE_URL)"
if echo "$DB_URL" | grep -qE ':6543/'; then
  errors+=("GOA_DATABASE_URL uses port 6543 — Supabase transaction-mode pooler is unsupported. Use the session pooler (port 5432) or direct-connection URL. See DEPLOY.md.")
fi

if [ ${#errors[@]} -gt 0 ]; then
  echo "──────────────────────────────────────────────────────────────" >&2
  echo "  Refusing to deploy: $ENV_FILE has issues" >&2
  echo "──────────────────────────────────────────────────────────────" >&2
  for err in "${errors[@]}"; do
    echo "  • $err" >&2
  done
  echo "──────────────────────────────────────────────────────────────" >&2
  exit 1
fi

echo "✓ .env.deploy looks safe"
