# tests/test_bootstrap.py
"""Tests for OpenClaw channel bootstrap."""

from __future__ import annotations

import pytest

from src.bootstrap import AGENT_PORTS, OPENCLAW_CHANNELS, bootstrap_channels
from src.channels.models import MemberRole
from src.channels.registry import ChannelRegistry
from src.storage.memory import InMemoryBackend


@pytest.fixture
def registry() -> ChannelRegistry:
    return ChannelRegistry(InMemoryBackend())


async def test_bootstrap_creates_all_channels(registry: ChannelRegistry) -> None:
    await bootstrap_channels(registry)
    channels = await registry.list_channels()
    assert len(channels) == 7
    channel_ids = {ch.channel_id for ch in channels}
    assert channel_ids == {
        "leaders", "dev-team", "testing-team", "ops-team",
        "pm-team", "knowledge-team", "filip-nexus",
    }


async def test_bootstrap_assigns_correct_roles(registry: ChannelRegistry) -> None:
    await bootstrap_channels(registry)

    # Check leaders channel
    leaders = await registry.get_channel("leaders")
    assert leaders is not None
    assert leaders.members["nexus"].role == MemberRole.owner
    assert leaders.members["apollo"].role == MemberRole.member
    assert leaders.members["vigil"].role == MemberRole.observer

    # Check dev-team
    dev = await registry.get_channel("dev-team")
    assert dev is not None
    assert dev.members["apollo"].role == MemberRole.owner
    assert dev.members["rex"].role == MemberRole.member
    assert dev.members["vigil"].role == MemberRole.observer


async def test_bootstrap_is_idempotent(registry: ChannelRegistry) -> None:
    await bootstrap_channels(registry)
    channels_1 = await registry.list_channels()

    # Run again
    await bootstrap_channels(registry)
    channels_2 = await registry.list_channels()

    assert len(channels_1) == len(channels_2)
    for ch in channels_2:
        original = next(c for c in channels_1 if c.channel_id == ch.channel_id)
        assert set(ch.members.keys()) == set(original.members.keys())


async def test_bootstrap_adds_missing_members(registry: ChannelRegistry) -> None:
    """If a channel exists but is missing members, bootstrap adds them."""
    # Create channel with only the owner
    await registry.create_channel(name="#dev-team", channel_id="dev-team")
    from src.channels.models import ChannelMember
    owner = ChannelMember(agent_id="apollo", name="Apollo", url="http://apollo:9002/", role=MemberRole.owner)
    await registry.add_member("dev-team", owner)

    await bootstrap_channels(registry)

    dev = await registry.get_channel("dev-team")
    assert dev is not None
    # Should now have apollo + rex, pixel, nova, swift, hawk + vigil = 7
    assert len(dev.members) == 7
    assert "rex" in dev.members
    assert "vigil" in dev.members


async def test_agent_urls_use_docker_service_names(registry: ChannelRegistry) -> None:
    await bootstrap_channels(registry)

    dev = await registry.get_channel("dev-team")
    assert dev is not None
    # swift uses swift-agent as service name
    assert dev.members["swift"].url == "http://swift-agent:9006/"
    # rex uses rex directly
    assert dev.members["rex"].url == "http://rex:9003/"

    pm = await registry.get_channel("pm-team")
    assert pm is not None
    # echo uses echo-agent as service name
    assert pm.members["echo"].url == "http://echo-agent:9019/"


async def test_filip_nexus_channel_has_only_owner(registry: ChannelRegistry) -> None:
    await bootstrap_channels(registry)
    ch = await registry.get_channel("filip-nexus")
    assert ch is not None
    assert len(ch.members) == 1
    assert ch.members["nexus"].role == MemberRole.owner


async def test_all_agents_have_port_assignments() -> None:
    """Every agent referenced in channels must have a port."""
    all_agents = set()
    for chan_def in OPENCLAW_CHANNELS:
        all_agents.add(chan_def["owner"])
        all_agents.update(chan_def["members"])
        all_agents.update(chan_def["observers"])
    for agent_id in all_agents:
        assert agent_id in AGENT_PORTS, f"{agent_id} missing from AGENT_PORTS"
