hub:      uv run --package goa uvicorn goa.main:app --host 127.0.0.1 --port 8000 --log-level info
# `setup` runs once then has to *stay alive* — honcho kills the whole
# formation when any process exits, so a clean rc=0 from a one-shot
# register would terminate the demo seconds after start (the "already
# registered" path completes in ~100ms). `exec tail -f /dev/null` after
# the `&&` replaces bash with a blocker; SIGTERM still propagates
# cleanly. If register exits non-zero (e.g. hub unreachable), `&&`
# short-circuits and the formation tears down — which is what we want.
setup:    bash -c 'until nc -z 127.0.0.1 8000 2>/dev/null; do sleep 0.2; done; uv run --package goa-sdk python scripts/register_agents.py && exec tail -f /dev/null'
payments: bash -c 'until nc -z 127.0.0.1 8000 2>/dev/null; do sleep 0.2; done; until [ -f examples/payments-agent/.env ]; do sleep 0.2; done; PYTHONUNBUFFERED=1 exec uv run --package goa-sdk python examples/payments-agent/main.py'
support:  bash -c 'until nc -z 127.0.0.1 8000 2>/dev/null; do sleep 0.2; done; until [ -f examples/support-agent/.env ]; do sleep 0.2; done; PYTHONUNBUFFERED=1 exec uv run --package goa-sdk python examples/support-agent/main.py'
chat:     bash -c 'until nc -z 127.0.0.1 8000 2>/dev/null; do sleep 0.2; done; until [ -f examples/chat-service/.env ]; do sleep 0.2; done; PYTHONUNBUFFERED=1 exec uv run --package goa-sdk python examples/chat-service/main.py'
dash:     bash -c 'cd goa-dashboard && npm run dev'
