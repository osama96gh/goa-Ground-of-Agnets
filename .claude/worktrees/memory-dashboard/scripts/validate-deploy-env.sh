#!/usr/bin/env bash
# Advisory pre-flight check for `make up`.
#
# Reads .env.docker (the compose-runtime file) and prints warnings for
# common mistakes, but never blocks the stack from starting. Compose
# itself will fail loudly on actually-missing required vars (see the
# `:?` syntax in docker-compose.yml), so this script is purely about
# catching the soft footguns earlier and with a friendlier message:
#
#   - Required vars left empty
#   - Placeholder secrets that obviously haven't been rotated
#     (`dev-pepper`, `dev-admin-token`, `changeme`, …)
#   - Supabase transaction-mode pooler port 6543 (unsupported)
#
# Rotate secrets before exposing the hub on a public domain. This script
# trusts you to do that — it just nudges.

set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE=".env.docker"

if [ ! -f "$ENV_FILE" ]; then
  echo "error: $ENV_FILE missing — run \`make bootstrap-env\` first" >&2
  exit 1
fi

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

warnings=()

# ─── Required vars ──────────────────────────────────────────
for var in GOA_DOMAIN GOA_SERVER_PEPPER GOA_ADMIN_TOKEN GOA_DATABASE_URL; do
  if [ -z "$(get "$var")" ]; then
    warnings+=("$var is empty or unset in $ENV_FILE")
  fi
done

# ─── Placeholder detection ──────────────────────────────────
PEPPER="$(get GOA_SERVER_PEPPER)"
case "$PEPPER" in
  dev-pepper|changeme|change-me|placeholder|secret|password)
    warnings+=("GOA_SERVER_PEPPER looks like a placeholder ($PEPPER) — rotate before exposing publicly: openssl rand -hex 32")
    ;;
esac

ADMIN_TOKEN="$(get GOA_ADMIN_TOKEN)"
case "$ADMIN_TOKEN" in
  dev-admin-token|dev|changeme|change-me|admin|password|secret)
    warnings+=("GOA_ADMIN_TOKEN looks like a placeholder ($ADMIN_TOKEN) — rotate before exposing publicly: openssl rand -hex 32")
    ;;
esac

# Catch the Supabase port-6543 footgun explicitly — better to flag now
# than have asyncpg explode at runtime with a less-obvious error.
DB_URL="$(get GOA_DATABASE_URL)"
if echo "$DB_URL" | grep -qE ':6543/'; then
  warnings+=("GOA_DATABASE_URL uses port 6543 — Supabase transaction-mode pooler is unsupported. Use the session pooler (port 5432) or direct-connection URL. See DEPLOY.md.")
fi

if [ ${#warnings[@]} -gt 0 ]; then
  echo "──────────────────────────────────────────────────────────────" >&2
  echo "  ⚠ $ENV_FILE warnings (starting anyway):" >&2
  echo "──────────────────────────────────────────────────────────────" >&2
  for w in "${warnings[@]}"; do
    echo "  • $w" >&2
  done
  echo "──────────────────────────────────────────────────────────────" >&2
else
  echo "✓ $ENV_FILE looks good"
fi

exit 0
