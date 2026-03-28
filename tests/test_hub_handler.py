# tests/test_hub_handler.py
"""Tests for GroupChatHub handler — the A2A RequestHandler."""

import uuid

import pytest
from unittest.mock import AsyncMock
from a2a.types import (
    Message, MessageSendParams, Part, Role, Task, TaskArtifactUpdateEvent,
    TaskIdParams, TaskQueryParams, TaskState, TaskStatusUpdateEvent, TextPart,
)

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


class TestOnMessageSendStream:
    async def test_yields_working_artifacts_completed(self, hub_setup):
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
        events = []
        async for event in hub.on_message_send_stream(params):
            events.append(event)

        assert len(events) >= 3
        assert isinstance(events[0], TaskStatusUpdateEvent)
        assert events[0].status.state == TaskState.working
        assert isinstance(events[1], TaskArtifactUpdateEvent)
        assert isinstance(events[-1], TaskStatusUpdateEvent)
        assert events[-1].status.state == TaskState.completed

    async def test_stream_error_channel(self, hub_setup):
        hub, _, _ = hub_setup
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello"))],
                message_id="msg-1",
                metadata={"channel_id": "nonexistent"},
            ),
        )
        events = []
        async for event in hub.on_message_send_stream(params):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], Message)

    async def test_stream_permission_denied(self, hub_setup):
        hub, registry, _ = hub_setup
        await registry.add_member("dev", ChannelMember(
            agent_id="vigil", name="Vigil", url="http://localhost:9003", role=MemberRole.observer,
        ))
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello"))],
                message_id="msg-1",
                metadata={"channel_id": "dev", "sender_id": "vigil"},
            ),
        )
        events = []
        async for event in hub.on_message_send_stream(params):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], Message)
        assert "permission" in events[0].parts[0].root.text.lower() or "observer" in events[0].parts[0].root.text.lower()


class TestOnGetTask:
    async def test_get_existing_task(self, hub_setup):
        hub, _, _ = hub_setup
        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", response_text="Response"),
        ])
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello"))],
                message_id="msg-1",
                metadata={"channel_id": "dev", "sender_id": "apollo"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)

        retrieved = await hub.on_get_task(TaskQueryParams(id=result.id))
        assert retrieved is not None
        assert retrieved.id == result.id

    async def test_get_nonexistent_task(self, hub_setup):
        hub, _, _ = hub_setup
        retrieved = await hub.on_get_task(TaskQueryParams(id="nonexistent"))
        assert retrieved is None


class TestOnCancelTask:
    async def test_cancel_existing_task(self, hub_setup):
        hub, _, _ = hub_setup
        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", response_text="Response"),
        ])
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello"))],
                message_id="msg-1",
                metadata={"channel_id": "dev", "sender_id": "apollo"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)

        cancelled = await hub.on_cancel_task(TaskIdParams(id=result.id))
        assert cancelled is not None
        assert cancelled.status.state == TaskState.canceled

    async def test_cancel_nonexistent_task(self, hub_setup):
        hub, _, _ = hub_setup
        cancelled = await hub.on_cancel_task(TaskIdParams(id="nonexistent"))
        assert cancelled is None


class TestStrategyFromMetadata:
    async def test_aggregation_strategy_from_message_metadata(self, hub_setup):
        hub, _, _ = hub_setup
        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", response_text="Response 1"),
        ])
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello"))],
                message_id="msg-1",
                metadata={"channel_id": "dev", "sender_id": "apollo", "aggregation": "first"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)
        assert result.metadata["strategy"] == "first"

    async def test_invalid_strategy_falls_back_to_all(self, hub_setup):
        hub, _, _ = hub_setup
        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", response_text="Response"),
        ])
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello"))],
                message_id="msg-1",
                metadata={"channel_id": "dev", "sender_id": "apollo", "aggregation": "bogus"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)
        assert result.metadata["strategy"] == "all"


class TestContextIdResolution:
    async def test_resolve_via_context_id(self, hub_setup):
        hub, registry, _ = hub_setup
        ch = await registry.get_channel("dev")
        context_id = ch.context_id

        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", response_text="Found via context"),
        ])
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello via context"))],
                message_id="msg-1",
                metadata={"sender_id": "apollo"},
                context_id=context_id,
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)

    async def test_context_id_not_matching(self, hub_setup):
        hub, _, _ = hub_setup
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello"))],
                message_id="msg-1",
                metadata={"sender_id": "apollo"},
                context_id="nonexistent-context-id",
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Message)
        assert "error" in result.parts[0].root.text.lower() or "Error" in result.parts[0].root.text
