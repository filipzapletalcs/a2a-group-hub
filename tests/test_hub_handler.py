# tests/test_hub_handler.py
"""Tests for GroupChatHub handler — the A2A RequestHandler."""

import pytest
from unittest.mock import AsyncMock
from a2a.types import Message, MessageSendParams, Part, Role, TextPart, Task

from src.channels.models import Channel, ChannelMember, MemberRole
from src.channels.registry import ChannelRegistry
from src.hub.handler import GroupChatHub
from src.hub.fanout import FanOutResult
from src.storage.memory import InMemoryBackend


@pytest.fixture
async def hub_setup():
    storage = InMemoryBackend()
    registry = ChannelRegistry(storage)
    ch = await registry.create_channel(name="dev-team", channel_id="dev")
    await registry.add_member("dev", ChannelMember(agent_id="apollo", name="Apollo", url="http://localhost:9001", role=MemberRole.owner))
    await registry.add_member("dev", ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9002"))
    hub = GroupChatHub(registry=registry, storage=storage)
    return hub, registry, storage


class TestOnMessageSend:
    async def test_resolves_channel_from_metadata(self, hub_setup):
        hub, _, _ = hub_setup
        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", response_text="Got it"),
        ])
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello team"))],
                message_id="msg-1",
                metadata={"channel_id": "dev", "sender_id": "apollo"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)
        assert len(result.artifacts) >= 1

    async def test_unknown_channel_returns_error(self, hub_setup):
        hub, _, _ = hub_setup
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello"))],
                message_id="msg-1",
                metadata={"channel_id": "nonexistent"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Message)
        assert "error" in result.parts[0].root.text.lower() or "Error" in result.parts[0].root.text

    async def test_observer_cannot_send(self, hub_setup):
        hub, registry, _ = hub_setup
        await registry.add_member("dev", ChannelMember(agent_id="vigil", name="Vigil", url="http://localhost:9003", role=MemberRole.observer))
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello"))],
                message_id="msg-1",
                metadata={"channel_id": "dev", "sender_id": "vigil"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Message)
        assert "observer" in result.parts[0].root.text.lower() or "permission" in result.parts[0].root.text.lower()

    async def test_message_persisted_to_storage(self, hub_setup):
        hub, _, storage = hub_setup
        hub._fanout_engine.fan_out = AsyncMock(return_value=[])
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Persisted message"))],
                message_id="msg-1",
                metadata={"channel_id": "dev", "sender_id": "apollo"},
            ),
        )
        await hub.on_message_send(params)
        messages = await storage.get_messages("dev")
        assert len(messages) >= 1
