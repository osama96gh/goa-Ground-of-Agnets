# Goa is configured by two self-contained env files:
#
#   .env.local   →  native targets   (make demo / goa / setup)
#   .env.docker  →  compose targets  (make up / down / logs / update)
#
# Neither overlays the other. The block below loads .env.local into
# Make's environment for native targets, but skips it for compose
# targets — exporting .env.local would override the values Compose
# reads via --env-file (process env wins over --env-file in Compose's
# substitution precedence).
COMPOSE_GOALS := up down logs update
ifeq ($(filter $(COMPOSE_GOALS),$(MAKECMDGOALS)),)
ifneq (,$(wildcard .env.local))
include .env.local
export
endif
endif

# Fallbacks for running native targets without a .env.local file.
GOA_SERVER_PEPPER ?= dev-pepper
PORT              ?= 8000

# docker compose, pinned to the deploy-runtime env file.
COMPOSE := docker compose --env-file .env.docker

# `make up EXTERNAL_DB=1` stops the bundled Postgres (use when you've
# pointed GOA_DATABASE_URL at Supabase / Neon / RDS in .env.docker).
SCALE :=
ifeq ($(EXTERNAL_DB),1)
SCALE := --scale postgres=0
endif

.PHONY: help install bootstrap-env goa setup demo demo-db-up demo-db-down demo-clean example-chat-cli openapi-export openapi-check test test-core test-sdk dashboard-install dashboard-build up down logs update clean

help:                ## Show this help.
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Setup ───────────────────────────────────────────────────────────

install:             ## Install Python workspace dev deps (uv sync).
	uv sync --all-packages

bootstrap-env:       ## Create .env.local + .env.docker from templates and randomize the dev pepper (idempotent).
	@bash scripts/bootstrap-env.sh

# ─── Native dev (make demo) ──────────────────────────────────────────

demo: bootstrap-env demo-db-up dashboard-install   ## One-command local dev: hub + agent registration + 3 example agents + dashboard via honcho.
	uv run honcho -e .env.local start

goa:                 ## Run the bare hub on :8000 (no agents, no dashboard) — for hacking on goa-core. Override port with PORT=<n>.
	GOA_SERVER_PEPPER=$(GOA_SERVER_PEPPER) \
	  uv run --package goa uvicorn goa.main:app \
	  --host 127.0.0.1 --port $(PORT) --log-level info

setup:               ## Register the demo agents and write per-example .env files (idempotent). Requires the hub to be running.
	uv run --package goa-sdk python scripts/register_agents.py

demo-db-up:          ## Start bundled Postgres + MinIO containers for `make demo`. No-op for sqlite/in-memory/external endpoints.
	@bash scripts/demo-db.sh up

demo-db-down:        ## Stop the bundled Postgres + MinIO containers (data volumes preserved).
	@bash scripts/demo-db.sh down

demo-clean:          ## Wipe demo state (SQLite file OR Postgres + MinIO data volumes) + per-example .env files so the next `make demo` re-registers cleanly.
	@if [ "$${GOA_DATABASE_URL#sqlite:}" != "$$GOA_DATABASE_URL" ]; then \
	    f="$${GOA_DATABASE_URL#sqlite:}"; \
	    rm -f "$$f" "$$f-wal" "$$f-shm"; \
	    echo "wiped $$f (+ -wal/-shm)"; \
	else \
	    bash scripts/demo-db.sh wipe; \
	fi
	@rm -f examples/payments-agent/.env examples/support-agent/.env examples/chat-service/.env
	@echo "removed examples/*/.env — the next \`make demo\` will re-register"

example-chat-cli:    ## Drive the chat-service one-shot CLI (alt to the interactive demo).
	uv run --package goa-sdk python examples/chat-service/cli.py

# ─── API contract ────────────────────────────────────────────────────

openapi-export:      ## Regenerate the committed openapi.json from goa-core (the source-of-truth spec).
	uv run --package goa python scripts/export_openapi.py

openapi-check:       ## Fail if openapi.json drifts from current code (run after model/route changes).
	@uv run --package goa python scripts/export_openapi.py --check

# ─── Tests ───────────────────────────────────────────────────────────

test: test-core test-sdk     ## Run full test suite (goa-core + goa-sdk).

test-core:           ## Run goa-core tests.
	cd goa-core && uv run pytest -v

test-sdk:            ## Run goa-sdk tests.
	cd goa-sdk && uv run pytest -v

# ─── Dashboard ───────────────────────────────────────────────────────

dashboard-install:   ## Install dashboard npm deps.
	cd goa-dashboard && npm install

dashboard-build:     ## Build dashboard for production (output: goa-dashboard/dist/).
	cd goa-dashboard && npm run build

# ─── Stack lifecycle (make up / down / logs / update) ────────────────

up: bootstrap-env    ## Start the full stack (web + hub + postgres + minio). Warns on placeholder secrets. EXTERNAL_DB=1 stops bundled Postgres.
	@bash scripts/validate-deploy-env.sh
	@$(COMPOSE) up -d --build --force-recreate $(SCALE)

down:                ## Stop the stack (data volumes preserved).
	@$(COMPOSE) down

logs:                ## Tail logs from all services (Ctrl-C to exit).
	@$(COMPOSE) logs -f --tail=200

update:              ## Pull/rebuild images and recreate all containers.
	@$(COMPOSE) pull
	@$(COMPOSE) up -d --build --force-recreate $(SCALE)

# ─── Housekeeping ────────────────────────────────────────────────────

clean:               ## Remove pytest caches and __pycache__ dirs.
	rm -rf .pytest_cache goa-core/.pytest_cache goa-sdk/.pytest_cache
	find . -type d -name __pycache__ -not -path '*/node_modules/*' -prune -exec rm -rf {} +
