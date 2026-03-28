# tests/test_registry.py
"""Tests for ChannelRegistry backed by StorageBackend."""

import pytest
from src.channels.models import ChannelMember, MemberRole
from src.channels.registry import ChannelRegistry
from src.storage.memory import InMemoryBackend


@pytest.fixture
def registry():
    return ChannelRegistry(InMemoryBackend())


class TestChannelRegistry:
    async def test_create_channel(self, registry):
        ch = await registry.create_channel(name="dev-team", channel_id="dev")
        assert ch.channel_id == "dev"
        assert ch.name == "dev-team"

    async def test_create_channel_auto_id(self, registry):
        ch = await registry.create_channel(name="ops-team")
        assert ch.channel_id  # auto-generated

    async def test_get_channel(self, registry):
        await registry.create_channel(name="dev", channel_id="dev")
        ch = await registry.get_channel("dev")
        assert ch is not None

    async def test_list_channels(self, registry):
        await registry.create_channel(name="dev", channel_id="dev")
        await registry.create_channel(name="ops", channel_id="ops")
        channels = await registry.list_channels()
        assert len(channels) == 2

    async def test_delete_channel(self, registry):
        await registry.create_channel(name="dev", channel_id="dev")
        assert await registry.delete_channel("dev") is True
        assert await registry.get_channel("dev") is None

    async def test_add_member(self, registry):
        await registry.create_channel(name="dev", channel_id="dev")
        member = ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001")
        await registry.add_member("dev", member)
        ch = await registry.get_channel("dev")
        assert "rex" in ch.members

    async def test_remove_member(self, registry):
        await registry.create_channel(name="dev", channel_id="dev")
        await registry.add_member("dev", ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
        assert await registry.remove_member("dev", "rex") is True

    async def test_find_channels_for_agent(self, registry):
        await registry.create_channel(name="dev", channel_id="dev")
        await registry.create_channel(name="ops", channel_id="ops")
        await registry.add_member("dev", ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
        await registry.add_member("ops", ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
        channels = await registry.find_channels_for_agent("rex")
        assert len(channels) == 2
