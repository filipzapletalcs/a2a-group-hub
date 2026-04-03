# src/bootstrap.py
"""Idempotent channel bootstrap for OpenClaw agent topology.

On hub startup (BOOTSTRAP_CHANNELS=true), creates the team channels
and registers all 13 active agents with correct roles. Safe to run on every
restart — only adds missing channels/members, never deletes.

Phase 6 consolidation: 11 lightweight agents dropped, 13 remain.
OpenClaw agents use internal port 18800 (gateway).
Lightweight Python agents use their assigned port directly.
"""

from __future__ import annotations

import logging

from src.channels.models import ChannelMember, MemberRole
from src.channels.registry import ChannelRegistry

logger = logging.getLogger("a2a-hub.bootstrap")

# Agent port assignments (must match docker-compose)
# OpenClaw agents listen on 18800 internally (gateway plugin)
# Lightweight Python agents listen on their assigned port
AGENT_PORTS: dict[str, int] = {
    # OpenClaw runtime (internal port 18800)
    "nexus": 18800,
    "apollo": 18800,
    "rex": 18800,
    "sage": 18800,
    "archi": 18800,
    "vigil": 18800,
    "iris": 18800,
    "atlas": 18800,
    "scout": 18800,
    # Lightweight Python agents (direct port)
    "pixel": 9004,
    "nova": 9005,
    "swift": 9006,
    "hawk": 9007,
}

# Docker service names that differ from agent_id
_SERVICE_NAMES: dict[str, str] = {
    "swift": "swift-agent",
}


def _agent_url(agent_id: str) -> str:
    """Build the A2A endpoint URL for an agent on the Docker network."""
    service = _SERVICE_NAMES.get(agent_id, agent_id)
    port = AGENT_PORTS[agent_id]
    return f"http://{service}:{port}/"


# Channel definitions after Phase 6 consolidation (13 agents, 3 channels)
# Dropped: testing-team, ops-team, pm-team (owners/members removed)
OPENCLAW_CHANNELS: list[dict] = [
    {
        "channel_id": "leaders",
        "name": "#leaders",
        "owner": "nexus",
        "members": ["apollo", "sage"],
        "observers": ["vigil"],
    },
    {
        "channel_id": "dev-team",
        "name": "#dev-team",
        "owner": "apollo",
        "members": ["rex", "pixel", "nova", "swift", "hawk"],
        "observers": ["vigil"],
    },
    {
        "channel_id": "knowledge-team",
        "name": "#knowledge-team",
        "owner": "sage",
        "members": ["scout", "archi"],
        "observers": [],
    },
    {
        "channel_id": "filip-nexus",
        "name": "#filip-nexus",
        "owner": "nexus",
        "members": [],
        "observers": [],
    },
]


def _build_member(agent_id: str, role: MemberRole) -> ChannelMember:
    return ChannelMember(
        agent_id=agent_id,
        name=agent_id.capitalize(),
        url=_agent_url(agent_id),
        role=role,
    )


async def bootstrap_channels(registry: ChannelRegistry) -> None:
    """Create OpenClaw channels and register agents. Idempotent."""
    existing = {ch.channel_id: ch for ch in await registry.list_channels()}

    for chan_def in OPENCLAW_CHANNELS:
        cid = chan_def["channel_id"]
        channel = existing.get(cid)

        if channel is None:
            channel = await registry.create_channel(
                name=chan_def["name"],
                channel_id=cid,
                default_aggregation="all",
                agent_timeout=120,
            )
            logger.info("Created channel %s", cid)
        else:
            logger.debug("Channel %s already exists, checking members", cid)

        # Collect desired members with roles
        desired: list[tuple[str, MemberRole]] = []
        desired.append((chan_def["owner"], MemberRole.owner))
        for agent_id in chan_def["members"]:
            desired.append((agent_id, MemberRole.member))
        for agent_id in chan_def["observers"]:
            desired.append((agent_id, MemberRole.observer))

        # Add missing members
        for agent_id, role in desired:
            if agent_id not in channel.members:
                member = _build_member(agent_id, role)
                await registry.add_member(cid, member)
                logger.info("Added %s to %s as %s", agent_id, cid, role.value)
            else:
                logger.debug("%s already in %s", agent_id, cid)

    logger.info(
        "Bootstrap complete: %d channels configured",
        len(OPENCLAW_CHANNELS),
    )
