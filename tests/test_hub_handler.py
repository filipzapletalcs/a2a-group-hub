# tests/test_hub_handler.py
"""Tests for GroupChatHub handler — the A2A RequestHandler."""

import uuid
from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, patch
from a2a.types import (
    Message, MessageSendParams, Part, Role, Task, TaskArtifactUpdateEvent,
    TaskIdParams, TaskQueryParams, TaskState, TaskStatusUpdateEvent, TextPart,
)

from src.channels.models import Channel, ChannelMember, MemberRole
from src.channels.registry import ChannelRegistry
from src.hub.handler import GroupChatHub
from src.hub.fanout import FanOutResult
from src.storage.base import StoredMessage
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


class TestMemoryRecall:
    async def test_recall_memory_dedup_by_message_id(self, hub_setup):
        """Two messages with same text but different IDs — only the incoming one is filtered."""
        hub, _, storage = hub_setup
        ch = await hub.registry.get_channel("dev")

        # Seed two messages with identical text but different message_ids
        await storage.save_message("dev", StoredMessage(
            message_id="msg-A", channel_id="dev", sender_id="rex",
            text="OK", context_id=ch.context_id,
        ))
        await storage.save_message("dev", StoredMessage(
            message_id="msg-B", channel_id="dev", sender_id="nova",
            text="OK", context_id=ch.context_id,
        ))

        # Recall with message_id="msg-A" should exclude msg-A but keep msg-B
        result = await hub._recall_memory(ch, "OK", "msg-A")
        assert "nova" in result
        # msg-A (rex) should be filtered out by dedup
        # But both have text "OK" — old text-based dedup would remove both

    async def test_recall_memory_no_results(self, hub_setup):
        """When no past messages match, return empty string."""
        hub, _, _ = hub_setup
        ch = await hub.registry.get_channel("dev")
        result = await hub._recall_memory(ch, "completely unique query", "msg-1")
        assert result == ""

    async def test_recall_memory_method_directly(self, hub_setup):
        """Unit test _recall_memory returns formatted context with timestamps."""
        hub, _, storage = hub_setup
        ch = await hub.registry.get_channel("dev")

        await storage.save_message("dev", StoredMessage(
            message_id="msg-100", channel_id="dev", sender_id="pixel",
            text="The deploy is ready", context_id=ch.context_id,
            timestamp=datetime(2026, 3, 28, 10, 30, tzinfo=timezone.utc),
        ))

        result = await hub._recall_memory(ch, "deploy", "msg-other")
        assert "pixel" in result
        assert "28.03 10:30" in result
        assert "deploy" in result.lower()
        assert result.startswith("[Relevantní historie")

    async def test_memory_recall_in_on_message_send(self, hub_setup):
        """on_message_send passes memory_context in metadata to fan-out."""
        hub, _, storage = hub_setup
        ch = await hub.registry.get_channel("dev")

        # Seed a past message (text must contain the incoming query as substring for InMemory search)
        await storage.save_message("dev", StoredMessage(
            message_id="past-1", channel_id="dev", sender_id="rex",
            text="testing the deploy pipeline", context_id=ch.context_id,
        ))

        captured_metadata = {}

        async def mock_fan_out(channel, message_parts, sender_id, context_id, message_metadata=None):
            captured_metadata.update(message_metadata or {})
            return [FanOutResult(agent_id="rex", agent_name="Rex", response_text="Got it")]

        hub._fanout_engine.fan_out = mock_fan_out

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="testing"))],
                message_id="msg-new",
                metadata={"channel_id": "dev", "sender_id": "apollo"},
            ),
        )
        await hub.on_message_send(params)
        assert "memory_context" in captured_metadata
        assert "rex" in captured_metadata["memory_context"]

    async def test_memory_recall_stream_path(self, hub_setup):
        """on_message_send_stream also calls _recall_memory and passes context."""
        hub, _, storage = hub_setup
        ch = await hub.registry.get_channel("dev")

        await storage.save_message("dev", StoredMessage(
            message_id="past-2", channel_id="dev", sender_id="nova",
            text="architecture overview and design", context_id=ch.context_id,
        ))

        captured_metadata = {}

        async def mock_fan_out(channel, message_parts, sender_id, context_id, message_metadata=None):
            captured_metadata.update(message_metadata or {})
            return [FanOutResult(agent_id="rex", agent_name="Rex", response_text="Ack")]

        hub._fanout_engine.fan_out = mock_fan_out

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="architecture"))],
                message_id="msg-stream",
                metadata={"channel_id": "dev", "sender_id": "apollo"},
            ),
        )
        events = []
        async for event in hub.on_message_send_stream(params):
            events.append(event)

        assert "memory_context" in captured_metadata
        assert "nova" in captured_metadata["memory_context"]
