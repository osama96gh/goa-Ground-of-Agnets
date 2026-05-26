ifneq (,$(wildcard .env))
include .env
export
endif

GOA_SERVER_PEPPER ?= dev-pepper
PORT ?= 8000

.PHONY: help install bootstrap-env goa setup demo demo-clean example-chat-cli test test-core test-sdk dashboard-install dashboard-build deploy deploy-down deploy-logs deploy-update clean

help:           ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:        ## Install workspace dev deps (uv sync)
	uv sync --all-packages

bootstrap-env:  ## Create .env from .env.example and randomize the dev pepper (idempotent).
	@bash scripts/bootstrap-env.sh

goa:            ## Run Goa hub on :$(PORT) with info logging
	GOA_SERVER_PEPPER=$(GOA_SERVER_PEPPER) \
	  uv run --package goa uvicorn goa.main:app \
	  --host 127.0.0.1 --port $(PORT) --log-level info

setup:          ## Register the demo agents and write per-example .env files (idempotent). Requires the hub to be running.
	uv run --package goa-sdk python scripts/register_agents.py

demo: bootstrap-env dashboard-install   ## Run hub + agent registration + 3 example agents + dashboard via honcho. One command, one terminal.
	uv run honcho start

demo-clean:     ## Wipe SQLite DB + per-example .env files so the next `make demo` re-registers cleanly.
	@if [ "$${GOA_DATABASE_URL#sqlite:}" != "$$GOA_DATABASE_URL" ]; then \
	    f="$${GOA_DATABASE_URL#sqlite:}"; \
	    rm -f "$$f" "$$f-wal" "$$f-shm"; \
	    echo "wiped $$f (+ -wal/-shm)"; \
	else \
	    echo "GOA_DATABASE_URL is not sqlite — nothing to wipe"; \
	fi
	@rm -f examples/payments-agent/.env examples/support-agent/.env examples/chat-service/.env
	@echo "removed examples/*/.env — the next \`make demo\` will re-register"

example-chat-cli:    ## Drive the chat-service one-shot CLI (legacy form)
	uv run --package goa-sdk python examples/chat-service/cli.py

test: test-core test-sdk     ## Run full test suite (core unit + sdk)

test-core:      ## Run goa-core tests
	cd goa-core && uv run pytest -v

test-sdk:       ## Run goa-sdk tests
	cd goa-sdk && uv run pytest -v

dashboard-install:   ## Install dashboard npm deps
	cd goa-dashboard && npm install

dashboard-build:     ## Build dashboard for production
	cd goa-dashboard && npm run build

# ─── Deployment targets ─────────────────────────────────────────────
# The DEPLOY_COMPOSE_PROFILE selector: by default, bring up the bundled
# Postgres container; set EXTERNAL_DB=1 to skip it (you've pointed
# GOA_DATABASE_URL at an external host). See DEPLOY.md.
DEPLOY_COMPOSE_PROFILE := --profile bundled-db
ifeq ($(EXTERNAL_DB),1)
DEPLOY_COMPOSE_PROFILE :=
endif
DEPLOY_COMPOSE := docker compose --env-file .env.deploy $(DEPLOY_COMPOSE_PROFILE)

deploy:         ## Deploy via docker compose (refuses to start if secrets look like placeholders). EXTERNAL_DB=1 skips the bundled Postgres.
	@if [ ! -f .env.deploy ]; then \
	    echo "error: .env.deploy is missing. Run: cp .env.deploy.example .env.deploy && \$$EDITOR .env.deploy" >&2; \
	    exit 1; \
	fi
	@bash scripts/validate-deploy-env.sh
	$(DEPLOY_COMPOSE) up -d --build

deploy-down:    ## Stop the deployed stack (data volumes preserved).
	$(DEPLOY_COMPOSE) down

deploy-logs:    ## Tail logs from all deployed services (Ctrl-C to exit).
	$(DEPLOY_COMPOSE) logs -f --tail=200

deploy-update:  ## Pull/rebuild images and recreate only changed containers.
	$(DEPLOY_COMPOSE) pull
	$(DEPLOY_COMPOSE) up -d --build

clean:          ## Remove pytest caches and __pycache__ dirs
	rm -rf .pytest_cache goa-core/.pytest_cache goa-sdk/.pytest_cache
	find . -type d -name __pycache__ -not -path '*/node_modules/*' -prune -exec rm -rf {} +
