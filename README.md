# A2A Group Chat Hub

Broadcasting hub that adds group chat to the [A2A (Agent-to-Agent) protocol](https://github.com/a2aproject/A2A).

A2A is point-to-point by design. This hub sits in the middle and fans out messages to all members of a channel, then aggregates their responses using configurable strategies.

## Quickstart

### Zero-dependency mode (in-memory storage)

```bash
# Clone and install
git clone https://github.com/filipzapletal/a2a-group-hub.git
cd a2a-group-hub
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Start the hub
python -m uvicorn src.hub.server:create_app --factory --host 0.0.0.0 --port 8000

# In another terminal, start an echo agent
python -m agents.run_agent --role echo --port 9001
```

### Docker (full stack with Qdrant + Neo4j)

```bash
# Optional: set your API key for LLM agents
export ANTHROPIC_API_KEY=sk-ant-...

# Start everything
docker compose up

# Hub at localhost:8000, agents at 9001-9003
```

### Create a channel and add members

```bash
# Create channel
curl -X POST http://localhost:8000/api/channels \
  -H "Content-Type: application/json" \
  -d '{"name": "dev-team", "channel_id": "dev"}'

# Add agents
curl -X POST http://localhost:8000/api/channels/dev/members \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "researcher", "name": "Researcher", "url": "http://localhost:9001", "role": "member"}'

curl -X POST http://localhost:8000/api/channels/dev/members \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "engineer", "name": "Engineer", "url": "http://localhost:9002", "role": "member"}'
```

## Architecture

```
Agent A ──SendMessage──> Hub ──fan-out──> Agent B, C, D
                          │
                          v
                    Storage Layer
                  (Qdrant + Neo4j)
```

The hub is a stateless relay with:
- **Role-based channels** — owner, member, observer roles with permission enforcement
- **5 aggregation strategies** — all, first, consensus, voting, best_of_n
- **Pluggable storage** — in-memory (default), Qdrant + Neo4j for production
- **Webhook notifications** — fire-and-forget POST on channel events
- **Prometheus metrics** — counters and gauges at `/api/metrics`

See [docs/architecture.md](docs/architecture.md) for detailed design.

## API

### REST Management

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/channels` | List channels |
| POST | `/api/channels` | Create channel |
| GET | `/api/channels/{id}` | Channel detail |
| PATCH | `/api/channels/{id}` | Update channel |
| DELETE | `/api/channels/{id}` | Delete channel |
| POST | `/api/channels/{id}/members` | Add member |
| PATCH | `/api/channels/{id}/members/{agent_id}` | Change role |
| DELETE | `/api/channels/{id}/members/{agent_id}` | Remove member |
| GET | `/api/status` | Hub status |
| GET | `/api/metrics` | Prometheus metrics |

See [docs/api-reference.md](docs/api-reference.md) for full details.

### A2A Protocol

Send messages to channels via standard A2A `SendMessage` with `channel_id` in metadata:

```json
{
  "message": {
    "parts": [{"kind": "text", "text": "Hello team!"}],
    "metadata": {"channel_id": "dev"}
  }
}
```

## Agents

Pre-built A2A agent servers:

| Role | Temperature | Description |
|------|-------------|-------------|
| echo | n/a | Simple echo for testing (no LLM) |
| researcher | 0.7 | Research and information synthesis |
| engineer | 0.5 | Code writing and review |
| critic | 0.8 | Critical analysis and edge cases |

```bash
# Run any role
python -m agents.run_agent --role researcher --port 9001

# Without ANTHROPIC_API_KEY, LLM agents fall back to echo
```

## Deployment Modes

1. **Full stack:** `docker compose up` — Hub + Qdrant + Neo4j + 3 agents
2. **Hub only:** `docker compose up hub qdrant neo4j` — bring your own agents
3. **Zero deps:** `STORAGE_BACKEND=memory python -m uvicorn src.hub.server:create_app --factory`

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|----------|---------|-------------|
| `STORAGE_BACKEND` | `memory` | `memory` or `composite` |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant endpoint |
| `NEO4J_URL` | `bolt://localhost:7687` | Neo4j endpoint |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | `password` | Neo4j password |
| `ANTHROPIC_API_KEY` | — | Required for LLM agents and consensus/best_of_n strategies |
| `HUB_HOST` | `0.0.0.0` | Hub bind address |
| `HUB_PORT` | `8000` | Hub port |

## Development

```bash
pip install -e ".[dev]"
python -m pytest -v
```

## License

MIT
