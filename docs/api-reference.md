# API Reference

## REST Management API

All endpoints are served by the hub at `http://localhost:8000`.

### Channels

#### List Channels

```
GET /api/channels
```

Returns an array of all channels.

**Response:** `200 OK`

```json
[
  {
    "channel_id": "dev",
    "name": "dev-team",
    "context_id": "uuid",
    "created_at": "2026-03-28T12:00:00+00:00",
    "message_count": 0,
    "default_aggregation": "all",
    "agent_timeout": 60,
    "members": {},
    "metadata": {}
  }
]
```

#### Create Channel

```
POST /api/channels
Content-Type: application/json

{
  "name": "dev-team",
  "channel_id": "dev",
  "default_aggregation": "all",
  "agent_timeout": 60
}
```

`channel_id` is optional (auto-generated UUID if omitted). `default_aggregation` defaults to `"all"`. `agent_timeout` defaults to `60` seconds.

**Response:** `201 Created`

#### Get Channel

```
GET /api/channels/{channel_id}
```

**Response:** `200 OK` or `404 Not Found`

#### Update Channel

```
PATCH /api/channels/{channel_id}
Content-Type: application/json

{
  "name": "new-name",
  "default_aggregation": "first",
  "agent_timeout": 30
}
```

All fields are optional — only provided fields are updated.

**Response:** `200 OK`

#### Delete Channel

```
DELETE /api/channels/{channel_id}
```

**Response:** `200 OK` with `{"deleted": true}` or `404 Not Found`

### Members

#### Add Member

```
POST /api/channels/{channel_id}/members
Content-Type: application/json

{
  "agent_id": "rex",
  "name": "Rex",
  "url": "http://localhost:9001",
  "role": "member",
  "auth_token": "optional-bearer-token"
}
```

`role` must be one of: `owner`, `member`, `observer`. Defaults to `member`.

**Response:** `201 Created`

#### Update Member

```
PATCH /api/channels/{channel_id}/members/{agent_id}
Content-Type: application/json

{
  "role": "owner"
}
```

**Response:** `200 OK`

#### Remove Member

```
DELETE /api/channels/{channel_id}/members/{agent_id}
```

**Response:** `200 OK` with `{"removed": true}` or `404 Not Found`

### Status

#### Hub Status

```
GET /api/status
```

**Response:** `200 OK`

```json
{
  "status": "running",
  "channels": 3,
  "total_members": 12,
  "timestamp": "2026-03-28T12:00:00+00:00"
}
```

#### Metrics

```
GET /api/metrics
```

Returns Prometheus-compatible text metrics.

## A2A Protocol

The hub accepts standard A2A `SendMessage` requests. Channel routing is specified via metadata:

```json
{
  "message": {
    "message_id": "uuid",
    "role": "user",
    "parts": [{"kind": "text", "text": "Hello team!"}],
    "metadata": {
      "channel_id": "dev"
    }
  }
}
```

The hub fans out the message to all channel peers and returns an aggregated response as a Task with artifacts from each responding agent.

### Aggregation

Set the aggregation strategy per-channel via `default_aggregation` or per-message via metadata:

```json
{
  "message": {
    "parts": [{"kind": "text", "text": "Review this code"}],
    "metadata": {
      "channel_id": "dev",
      "aggregation": "first"
    }
  }
}
```

Available strategies: `all`, `first`, `consensus`, `voting`, `best_of_n`.

## Agent CLI

Run agents as standalone A2A servers:

```bash
# Echo agent (no LLM)
python -m agents.run_agent --role echo --port 9001

# LLM agent with Claude
python -m agents.run_agent --role researcher --port 9001

# Custom settings
python -m agents.run_agent --role engineer --port 9002 --name "my-engineer"
```

Available roles: `echo`, `researcher`, `engineer`, `critic`.

Without `ANTHROPIC_API_KEY`, LLM agents automatically fall back to echo behavior.
