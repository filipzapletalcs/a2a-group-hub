# Architecture

## Overview

A2A Group Chat Hub is a broadcasting relay that adds group chat capabilities to the [A2A (Agent-to-Agent) protocol](https://github.com/a2aproject/A2A). A2A is strictly point-to-point; this hub sits in the middle, fans out messages to all channel members, and aggregates their responses.

## System Architecture

```
Agent A ──SendMessage──> Hub ──fan-out──> Agent B
                          │                Agent C
                          │                Agent D (observer)
                          │
                          v
                    Storage Layer
                  ┌───────┴───────┐
                  │               │
                Qdrant          Neo4j
           (messages +      (channels +
            vectors)         members)
```

### Core Components

| Component | Module | Responsibility |
|-----------|--------|---------------|
| Hub Handler | `src/hub/handler.py` | Receives A2A SendMessage, resolves channel, enforces permissions, orchestrates fan-out and aggregation |
| Fan-out Engine | `src/hub/fanout.py` | Parallel SendMessage dispatch to all peers via httpx |
| Aggregator | `src/hub/aggregator.py` | 5 strategies for combining responses: all, first, consensus, voting, best_of_n |
| Channel Models | `src/channels/models.py` | Channel, ChannelMember, MemberRole (owner/member/observer) |
| Permissions | `src/channels/permissions.py` | Role-based send/manage checks |
| Registry | `src/channels/registry.py` | Channel CRUD backed by storage |
| Storage | `src/storage/` | Pluggable backends: InMemoryBackend (default), Qdrant, Neo4j, Composite |
| Webhooks | `src/notifications/webhooks.py` | Fire-and-forget POST on channel events |
| Metrics | `src/observability/metrics.py` | Prometheus-compatible counters and gauges |

### Message Flow

1. Agent sends `SendMessage` to Hub with `channel_id` in metadata
2. Hub resolves channel via Registry, checks sender permissions
3. Message is persisted to storage
4. Fan-out engine dispatches to all peers (excluding sender); observers receive fire-and-forget
5. Aggregator combines responses using the channel's configured strategy
6. Aggregated result is persisted and returned to the sender

## Role-Based Access

| Role | Send | Receive | Manage Members | Delete Channel |
|------|------|---------|----------------|----------------|
| owner | yes | yes | yes | yes |
| member | yes | yes | no | no |
| observer | no | yes (fire-and-forget) | no | no |

## Aggregation Strategies

| Strategy | Waits For | Returns | Needs LLM | Use Case |
|----------|-----------|---------|-----------|----------|
| all | All members | N artifacts | No | Brainstorming, team review |
| first | First respondent | 1 artifact | No | Quick answer, fallback |
| consensus | All, then compare | 1 + outliers | Yes | Fact-checking |
| voting | All (structured) | Vote result | No | Decisions, approvals |
| best_of_n | All, then judge | 1 best | Yes | Quality single answer |

`consensus` and `best_of_n` require `ANTHROPIC_API_KEY` on the hub. Without it, they fall back to `all`.

## Storage Architecture

**Split responsibility:**
- **Qdrant:** Message content with FastEmbed vectors for semantic search
- **Neo4j:** Channel graph, member relationships, webhook registrations

**In-memory fallback:** `STORAGE_BACKEND=memory` uses `InMemoryBackend` for zero-dependency testing.

## Agents

Agents are standalone A2A servers built on the `a2a-sdk`:

- **DemoEchoExecutor** (`agents/demo_agent.py`) — echoes input, no LLM required
- **LLMAgentExecutor** (`agents/llm_agent.py`) — Claude API with per-context conversation history (capped at 20 messages), echo fallback when no API key
- **Pre-built roles** (`agents/agents_config.py`) — researcher (temp 0.7), engineer (temp 0.5), critic (temp 0.8)
- **CLI launcher** (`agents/run_agent.py`) — `python -m agents.run_agent --role researcher --port 9001`

## Deployment Modes

1. **Full stack:** `docker compose up` — Hub + Qdrant + Neo4j + 3 agents
2. **Hub only:** `docker compose up hub qdrant neo4j` — agents run elsewhere
3. **Zero deps:** `STORAGE_BACKEND=memory python -m uvicorn src.hub.server:create_app --factory`
