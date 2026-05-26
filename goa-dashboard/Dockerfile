# syntax=docker/dockerfile:1.7
# Web image — Caddy serving the dashboard SPA AND reverse-proxying the
# hub at the same origin. Used by docker-compose.yml as the `web` service.
# One container is the entire public face of the deployment: TLS, dashboard,
# /admin/* + /stream + /tasks + /participants proxy.
#
# Stage 1 (builder): node:20-alpine builds the Vite SPA into dist/.
# Stage 2 (runtime): caddy:2-alpine serves dist/ + reverse-proxies the hub.
#
# Build context is `./goa-dashboard` (set in docker-compose.yml). The runtime
# Caddyfile is bind-mounted from the repo root in Compose — kept editable
# without rebuilding the image.

# ─── Stage 1: build the SPA ──────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /build

# Cache deps separately from sources.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Important: production builds get an EMPTY VITE_GOA_ADMIN_TOKEN via the
# logic in vite.config.ts (`mode === "development" ? devToken : ""`). The
# token is never inlined into a prod bundle. The dashboard prompts the
# user for it at first load. See goa-dashboard/src/components/AdminTokenGate.tsx.
RUN npm run build


# ─── Stage 2: serve via Caddy ────────────────────────────────────────
FROM caddy:2-alpine AS runtime

# Static assets live at /srv/dashboard. Caddyfile (bind-mounted from
# repo root by Compose) references this path via `root * /srv/dashboard`.
COPY --from=builder /build/dist /srv/dashboard

# Caddy listens on 80 + 443 by default; the Compose service maps the
# host ports. No CMD override — the base image's entrypoint runs Caddy
# against /etc/caddy/Caddyfile, which Compose bind-mounts in.
EXPOSE 80 443
