# tests/test_hub_handler.py
"""Tests for GroupChatHub handler — the A2A RequestHandler."""

import uuid
from dataclasses import dataclass
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
from src.hub.fanout import FanOutEngine, FanOutResult
from src.storage.base import StoredMessage
from src.storage.memory import InMemoryBackend


# -- Mock router for two-phase fan-out tests --------------------------------

@dataclass
class MockRoutingResult:
    lead_id: str
    delegates: list[str]
    strategy: str = "delegated"


class MockRouter:
    """Minimal mock matching HierarchicalRouter interface."""

    def __init__(self, lead_id: str, delegates: list[str] | None = None, strategy: str = "delegated"):
        self._lead_id = lead_id
        self._delegates = delegates or []
        self._strategy = strategy

    async def route(self, channel_id: str, text: str) -> MockRoutingResult:
        return MockRoutingResult(
            lead_id=self._lead_id,
            delegates=self._delegates,
            strategy=self._strategy,
        )

    def parse_mentions(self, text: str) -> list[str]:
        """Extract @agent_id mentions from text."""
        import re
        return re.findall(r"@(\w+)", text)


@pytest.fixture
async def hub_setup():
    storage = InMemoryBackend()
    registry = ChannelRegistry(storage)
    ch = await registry.create_channel(name="dev-team", channel_id="dev")
    await registry.add_member("dev", ChannelMember(agent_id="apollo", name="Apollo", url="http://localhost:9001", role=MemberRole.owner))
    await registry.add_member("dev", ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9002"))
    hub = GroupChatHub(registry=registry, storage=storage)
    return hub, registry, storage


@pytest.fixture
async def hub_setup_with_router():
    """Hub with a mock router and 3 members (lead=apollo, members=rex,pixel)."""
    storage = InMemoryBackend()
    registry = ChannelRegistry(storage)
    await registry.create_channel(name="dev-team", channel_id="dev")
    await registry.add_member("dev", ChannelMember(agent_id="apollo", name="Apollo", url="http://localhost:9001", role=MemberRole.owner))
    await registry.add_member("dev", ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9002"))
    await registry.add_member("dev", ChannelMember(agent_id="pixel", name="Pixel", url="http://localhost:9003"))
    router = MockRouter(lead_id="apollo", delegates=["rex", "pixel"])
    hub = GroupChatHub(registry=registry, storage=storage, router=router)
    return hub, registry, storage, router


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


class TestTwoPhaseFanout:
    """Tests for two-phase fan-out delegation (lead first, then delegates)."""

    async def test_two_phase_sends_to_lead_first(self, hub_setup_with_router):
        """With router, only lead gets the message initially via send_to_single."""
        hub, _, _, _ = hub_setup_with_router
        send_order = []

        async def mock_send_to_single(member, message_parts, channel, context_id,
                                       message_metadata=None, memory_context="",
                                       previous_responses=None):
            send_order.append(member.agent_id)
            return FanOutResult(
                agent_id=member.agent_id,
                agent_name=member.name,
                response_text=f"Response from {member.name}",
            )

        hub._fanout_engine.send_to_single = mock_send_to_single

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Fix the CSS"))],
                message_id="msg-1",
                metadata={"channel_id": "dev"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)
        # Lead (apollo) should be first in send order
        assert send_order[0] == "apollo"

    async def test_lead_mentions_trigger_delegation(self, hub_setup_with_router):
        """Lead says '@pixel' -> pixel gets message with lead's response in context."""
        hub, _, _, _ = hub_setup_with_router
        send_order = []
        captured_previous = {}

        async def mock_send_to_single(member, message_parts, channel, context_id,
                                       message_metadata=None, memory_context="",
                                       previous_responses=None):
            send_order.append(member.agent_id)
            captured_previous[member.agent_id] = previous_responses or []
            if member.agent_id == "apollo":
                return FanOutResult(
                    agent_id="apollo", agent_name="Apollo",
                    response_text="This is for @pixel, please fix the CSS hero section",
                )
            return FanOutResult(
                agent_id=member.agent_id, agent_name=member.name,
                response_text=f"Done: {member.name}",
            )

        hub._fanout_engine.send_to_single = mock_send_to_single

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Fix the CSS"))],
                message_id="msg-1",
                metadata={"channel_id": "dev"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)
        # Apollo first, then pixel (from @mention)
        assert send_order == ["apollo", "pixel"]

    async def test_keyword_fallback_when_no_mentions(self, hub_setup_with_router):
        """No @mentions in lead response -> keyword-based delegates from routing."""
        hub, _, _, _ = hub_setup_with_router
        send_order = []

        async def mock_send_to_single(member, message_parts, channel, context_id,
                                       message_metadata=None, memory_context="",
                                       previous_responses=None):
            send_order.append(member.agent_id)
            if member.agent_id == "apollo":
                # No @mentions in response
                return FanOutResult(
                    agent_id="apollo", agent_name="Apollo",
                    response_text="I'll handle coordination, team should look at this",
                )
            return FanOutResult(
                agent_id=member.agent_id, agent_name=member.name,
                response_text="Acknowledged",
            )

        hub._fanout_engine.send_to_single = mock_send_to_single

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Fix the CSS"))],
                message_id="msg-1",
                metadata={"channel_id": "dev"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)
        # Apollo first, then rex and pixel (keyword fallback delegates)
        assert send_order[0] == "apollo"
        assert "rex" in send_order
        assert "pixel" in send_order

    async def test_delegates_receive_lead_response_in_context(self, hub_setup_with_router):
        """Delegates should receive previous_responses containing lead's response."""
        hub, _, _, _ = hub_setup_with_router
        captured_previous = {}

        async def mock_send_to_single(member, message_parts, channel, context_id,
                                       message_metadata=None, memory_context="",
                                       previous_responses=None):
            captured_previous[member.agent_id] = list(previous_responses or [])
            if member.agent_id == "apollo":
                return FanOutResult(
                    agent_id="apollo", agent_name="Apollo",
                    response_text="Delegate to @rex for code review",
                )
            return FanOutResult(
                agent_id=member.agent_id, agent_name=member.name,
                response_text="Reviewed",
            )

        hub._fanout_engine.send_to_single = mock_send_to_single

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Review the PR"))],
                message_id="msg-1",
                metadata={"channel_id": "dev"},
            ),
        )
        await hub.on_message_send(params)

        # Apollo got no previous responses (first in chain)
        assert captured_previous["apollo"] == []
        # Rex got Apollo's response as previous
        assert len(captured_previous["rex"]) == 1
        assert captured_previous["rex"][0].agent_id == "apollo"
        assert "code review" in captured_previous["rex"][0].response_text

    async def test_broadcast_fallback_without_router(self, hub_setup):
        """Without router, hub falls back to broadcast (original behavior)."""
        hub, _, _ = hub_setup

        fan_out_called = False

        async def mock_fan_out(channel, message_parts, sender_id, context_id, message_metadata=None):
            nonlocal fan_out_called
            fan_out_called = True
            return [FanOutResult(agent_id="rex", agent_name="Rex", response_text="Got it")]

        hub._fanout_engine.fan_out = mock_fan_out

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
        assert fan_out_called

    async def test_broadcast_fallback_unknown_lead(self, hub_setup):
        """Router returns a lead that's not in the channel -> broadcast fallback."""
        hub, _, _ = hub_setup
        hub._router = MockRouter(lead_id="nonexistent-agent", delegates=[])

        fan_out_called = False

        async def mock_fan_out(channel, message_parts, sender_id, context_id, message_metadata=None):
            nonlocal fan_out_called
            fan_out_called = True
            return [FanOutResult(agent_id="rex", agent_name="Rex", response_text="Got it")]

        hub._fanout_engine.fan_out = mock_fan_out

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
        assert fan_out_called

    async def test_broadcast_fallback_strategy(self, hub_setup):
        """Router returns broadcast_fallback strategy -> broadcast."""
        hub, _, _ = hub_setup
        hub._router = MockRouter(lead_id="apollo", strategy="broadcast_fallback")

        fan_out_called = False

        async def mock_fan_out(channel, message_parts, sender_id, context_id, message_metadata=None):
            nonlocal fan_out_called
            fan_out_called = True
            return [FanOutResult(agent_id="rex", agent_name="Rex", response_text="Got it")]

        hub._fanout_engine.fan_out = mock_fan_out

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
        assert fan_out_called

    async def test_two_phase_stream(self, hub_setup_with_router):
        """on_message_send_stream also uses two-phase fan-out."""
        hub, _, _, _ = hub_setup_with_router
        send_order = []

        async def mock_send_to_single(member, message_parts, channel, context_id,
                                       message_metadata=None, memory_context="",
                                       previous_responses=None):
            send_order.append(member.agent_id)
            if member.agent_id == "apollo":
                return FanOutResult(
                    agent_id="apollo", agent_name="Apollo",
                    response_text="Delegating to @pixel",
                )
            return FanOutResult(
                agent_id=member.agent_id, agent_name=member.name,
                response_text="Done",
            )

        hub._fanout_engine.send_to_single = mock_send_to_single

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Fix CSS"))],
                message_id="msg-1",
                metadata={"channel_id": "dev"},
            ),
        )
        events = []
        async for event in hub.on_message_send_stream(params):
            events.append(event)

        assert send_order[0] == "apollo"
        assert "pixel" in send_order
        # Should have: working status, 2 artifact events (apollo + pixel), completed status
        status_events = [e for e in events if isinstance(e, TaskStatusUpdateEvent)]
        artifact_events = [e for e in events if isinstance(e, TaskArtifactUpdateEvent)]
        assert len(status_events) == 2  # working + completed
        assert len(artifact_events) == 2  # apollo + pixel

    async def test_lead_failure_falls_back_to_broadcast(self, hub_setup_with_router):
        """If lead's send_to_single raises, fall back to broadcast."""
        hub, _, _, _ = hub_setup_with_router

        call_count = {"send_single": 0, "fan_out": 0}

        async def mock_send_to_single(member, message_parts, channel, context_id,
                                       message_metadata=None, memory_context="",
                                       previous_responses=None):
            call_count["send_single"] += 1
            raise ConnectionError("Lead unreachable")

        async def mock_fan_out(channel, message_parts, sender_id, context_id, message_metadata=None):
            call_count["fan_out"] += 1
            return [FanOutResult(agent_id="rex", agent_name="Rex", response_text="Fallback")]

        hub._fanout_engine.send_to_single = mock_send_to_single
        hub._fanout_engine.fan_out = mock_fan_out

        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello"))],
                message_id="msg-1",
                metadata={"channel_id": "dev"},
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)
        assert call_count["send_single"] == 1  # tried lead
        assert call_count["fan_out"] == 1  # fell back to broadcast
