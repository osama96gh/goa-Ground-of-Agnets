hub:      uv run --package goa uvicorn goa.main:app --host 127.0.0.1 --port 8000 --log-level info
setup:    bash -c 'until nc -z 127.0.0.1 8000 2>/dev/null; do sleep 0.2; done; exec uv run --package goa-sdk python scripts/register_agents.py'
payments: bash -c 'until nc -z 127.0.0.1 8000 2>/dev/null; do sleep 0.2; done; until [ -f examples/payments-agent/.env ]; do sleep 0.2; done; PYTHONUNBUFFERED=1 exec uv run --package goa-sdk python examples/payments-agent/main.py'
support:  bash -c 'until nc -z 127.0.0.1 8000 2>/dev/null; do sleep 0.2; done; until [ -f examples/support-agent/.env ]; do sleep 0.2; done; PYTHONUNBUFFERED=1 exec uv run --package goa-sdk python examples/support-agent/main.py'
chat:     bash -c 'until nc -z 127.0.0.1 8000 2>/dev/null; do sleep 0.2; done; until [ -f examples/chat-service/.env ]; do sleep 0.2; done; PYTHONUNBUFFERED=1 exec uv run --package goa-sdk python examples/chat-service/main.py'
dash:     bash -c 'cd goa-dashboard && npm run dev'
