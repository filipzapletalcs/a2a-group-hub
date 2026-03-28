# tests/test_coverage_gaps.py
"""Tests for coverage gaps identified by code review."""

import uuid

import pytest
from unittest.mock import AsyncMock

from a2a.types import (
    Artifact, Message, MessageSendParams, Part, Role, Task,
    TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent, TextPart,
)

from src.channels.models import Channel, ChannelMember, MemberRole
from src.channels.registry import ChannelRegistry
from src.hub.aggregator import Aggregator, AggregationStrategy
from src.hub.fanout import FanOutResult
from src.hub.handler import GroupChatHub
from src.observability.metrics import MAX_FANOUT_SAMPLES, MetricsCollector
from src.storage.memory import InMemoryBackend


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
async def hub_setup():
    storage = InMemoryBackend()
    registry = ChannelRegistry(storage)
    ch = await registry.create_channel(name="dev-team", channel_id="dev")
    await registry.add_member(
        "dev",
        ChannelMember(
            agent_id="apollo", name="Apollo",
            url="http://localhost:9001", role=MemberRole.owner,
        ),
    )
    await registry.add_member(
        "dev",
        ChannelMember(
            agent_id="rex", name="Rex", url="http://localhost:9002",
        ),
    )
    hub = GroupChatHub(registry=registry, storage=storage)
    return hub, registry, storage


def _make_params(text: str, channel_id: str = "dev", sender_id: str = "apollo",
                 context_id: str | None = None) -> MessageSendParams:
    return MessageSendParams(
        message=Message(
            role=Role.user,
            parts=[Part(root=TextPart(text=text))],
            message_id=str(uuid.uuid4()),
            metadata={"channel_id": channel_id, "sender_id": sender_id},
            context_id=context_id,
        ),
    )


# -- Test: on_message_send_stream (completely untested) ----------------------

class TestOnMessageSendStream:
    async def test_stream_yields_status_and_artifacts(self, hub_setup):
        hub, _, _ = hub_setup
        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", response_text="Got it"),
        ])
        params = _make_params("Hello team")
        events = []
        async for event in hub.on_message_send_stream(params):
            events.append(event)

        # Should yield: working status, artifact(s), completed status
        assert len(events) >= 3
        assert isinstance(events[0], TaskStatusUpdateEvent)
        assert events[0].status.state == TaskState.working
        assert isinstance(events[1], TaskArtifactUpdateEvent)
        assert "Rex" in events[1].artifact.name
        assert isinstance(events[-1], TaskStatusUpdateEvent)
        assert events[-1].status.state == TaskState.completed

    async def test_stream_unknown_channel_yields_error(self, hub_setup):
        hub, _, _ = hub_setup
        params = _make_params("Hello", channel_id="nonexistent")
        events = []
        async for event in hub.on_message_send_stream(params):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], Message)
        assert "error" in events[0].parts[0].root.text.lower() or "Error" in events[0].parts[0].root.text

    async def test_stream_observer_yields_permission_error(self, hub_setup):
        hub, registry, _ = hub_setup
        await registry.add_member(
            "dev",
            ChannelMember(
                agent_id="vigil", name="Vigil",
                url="http://localhost:9003", role=MemberRole.observer,
            ),
        )
        params = _make_params("Hello", sender_id="vigil")
        events = []
        async for event in hub.on_message_send_stream(params):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], Message)
        text = events[0].parts[0].root.text.lower()
        assert "permission" in text or "observer" in text

    async def test_stream_multiple_agents(self, hub_setup):
        hub, registry, _ = hub_setup
        await registry.add_member(
            "dev",
            ChannelMember(
                agent_id="pixel", name="Pixel", url="http://localhost:9004",
            ),
        )
        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", response_text="Response 1"),
            FanOutResult(agent_id="pixel", agent_name="Pixel", response_text="Response 2"),
        ])
        params = _make_params("Hello team")
        artifacts = []
        async for event in hub.on_message_send_stream(params):
            if isinstance(event, TaskArtifactUpdateEvent):
                artifacts.append(event)

        assert len(artifacts) == 2

    async def test_stream_with_error_result(self, hub_setup):
        hub, _, _ = hub_setup
        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", error="Connection refused"),
        ])
        params = _make_params("Hello")
        artifacts = []
        async for event in hub.on_message_send_stream(params):
            if isinstance(event, TaskArtifactUpdateEvent):
                artifacts.append(event)

        assert len(artifacts) == 1
        assert "Error" in artifacts[0].artifact.parts[0].root.text


# -- Test: consensus/best_of_n fallback behavior ----------------------------

class TestConsensusAndBestOfNFallback:
    def test_consensus_falls_back_to_all(self):
        results = [
            FanOutResult(agent_id="a", agent_name="A", response_text="answer 1"),
            FanOutResult(agent_id="b", agent_name="B", response_text="answer 2"),
        ]
        agg = Aggregator()
        task = agg.aggregate(
            results, strategy=AggregationStrategy.consensus,
            task_id="t1", context_id="c1", channel_id="dev", channel_name="dev",
        )
        assert task.metadata["strategy"] == "all"
        assert task.metadata["requested_strategy"] == "consensus"
        assert len(task.artifacts) == 2

    def test_best_of_n_falls_back_to_all(self):
        results = [
            FanOutResult(agent_id="a", agent_name="A", response_text="answer 1"),
            FanOutResult(agent_id="b", agent_name="B", response_text="answer 2"),
            FanOutResult(agent_id="c", agent_name="C", response_text="answer 3"),
        ]
        agg = Aggregator()
        task = agg.aggregate(
            results, strategy=AggregationStrategy.best_of_n,
            task_id="t1", context_id="c1", channel_id="dev", channel_name="dev",
        )
        assert task.metadata["strategy"] == "all"
        assert task.metadata["requested_strategy"] == "best_of_n"
        assert len(task.artifacts) == 3

    def test_consensus_with_errors_includes_error_artifacts(self):
        results = [
            FanOutResult(agent_id="a", agent_name="A", response_text="answer"),
            FanOutResult(agent_id="b", agent_name="B", error="timeout"),
        ]
        agg = Aggregator()
        task = agg.aggregate(
            results, strategy=AggregationStrategy.consensus,
            task_id="t1", context_id="c1", channel_id="dev", channel_name="dev",
        )
        assert len(task.artifacts) == 2
        assert task.metadata["success_count"] == 1
        assert task.metadata["error_count"] == 1


# -- Test: voting with tie votes and edge cases -----------------------------

class TestVotingEdgeCases:
    def test_tie_vote_picks_one(self):
        """With a tie, most_common returns deterministic order (insertion)."""
        results = [
            FanOutResult(agent_id="a", agent_name="A", response_text="approve"),
            FanOutResult(agent_id="b", agent_name="B", response_text="reject"),
        ]
        agg = Aggregator()
        task = agg.aggregate(
            results, strategy=AggregationStrategy.voting,
            task_id="t1", context_id="c1", channel_id="dev", channel_name="dev",
        )
        text = task.artifacts[0].parts[0].root.text
        # Should contain a result and breakdown
        assert "1/2 votes" in text
        assert "approve" in text.lower() or "reject" in text.lower()

    def test_empty_results_list(self):
        agg = Aggregator()
        task = agg.aggregate(
            [], strategy=AggregationStrategy.voting,
            task_id="t1", context_id="c1", channel_id="dev", channel_name="dev",
        )
        text = task.artifacts[0].parts[0].root.text
        assert "no valid votes" in text.lower()

    def test_all_errors_no_valid_votes(self):
        results = [
            FanOutResult(agent_id="a", agent_name="A", error="timeout"),
            FanOutResult(agent_id="b", agent_name="B", error="refused"),
        ]
        agg = Aggregator()
        task = agg.aggregate(
            results, strategy=AggregationStrategy.voting,
            task_id="t1", context_id="c1", channel_id="dev", channel_name="dev",
        )
        text = task.artifacts[0].parts[0].root.text
        assert "no valid votes" in text.lower()

    def test_voting_case_insensitive(self):
        """Votes are normalized to lowercase."""
        results = [
            FanOutResult(agent_id="a", agent_name="A", response_text="Approve"),
            FanOutResult(agent_id="b", agent_name="B", response_text="approve"),
            FanOutResult(agent_id="c", agent_name="C", response_text="APPROVE"),
        ]
        agg = Aggregator()
        task = agg.aggregate(
            results, strategy=AggregationStrategy.voting,
            task_id="t1", context_id="c1", channel_id="dev", channel_name="dev",
        )
        text = task.artifacts[0].parts[0].root.text
        assert "3/3 votes" in text

    def test_first_strategy_empty_results(self):
        agg = Aggregator()
        task = agg.aggregate(
            [], strategy=AggregationStrategy.first,
            task_id="t1", context_id="c1", channel_id="dev", channel_name="dev",
        )
        assert task.artifacts == []

    def test_first_strategy_all_errors(self):
        results = [
            FanOutResult(agent_id="a", agent_name="A", error="timeout"),
            FanOutResult(agent_id="b", agent_name="B", error="refused"),
        ]
        agg = Aggregator()
        task = agg.aggregate(
            results, strategy=AggregationStrategy.first,
            task_id="t1", context_id="c1", channel_id="dev", channel_name="dev",
        )
        assert len(task.artifacts) == 1
        assert "timeout" in task.artifacts[0].parts[0].root.text

    def test_all_strategy_empty_results(self):
        agg = Aggregator()
        task = agg.aggregate(
            [], strategy=AggregationStrategy.all,
            task_id="t1", context_id="c1", channel_id="dev", channel_name="dev",
        )
        assert task.artifacts == []


# -- Test: context_id-based channel resolution (Strategy 2) -----------------

class TestContextIdResolution:
    async def test_resolve_channel_via_context_id(self, hub_setup):
        hub, registry, _ = hub_setup
        ch = await registry.get_channel("dev")
        context_id = ch.context_id

        hub._fanout_engine.fan_out = AsyncMock(return_value=[
            FanOutResult(agent_id="rex", agent_name="Rex", response_text="Found via context"),
        ])

        # No channel_id in metadata, but context_id on the message
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello via context"))],
                message_id=str(uuid.uuid4()),
                metadata={"sender_id": "apollo"},
                context_id=context_id,
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Task)

    async def test_context_id_not_matching_returns_error(self, hub_setup):
        hub, _, _ = hub_setup
        params = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text="Hello"))],
                message_id=str(uuid.uuid4()),
                metadata={"sender_id": "apollo"},
                context_id="nonexistent-context-id",
            ),
        )
        result = await hub.on_message_send(params)
        assert isinstance(result, Message)
        assert "error" in result.parts[0].root.text.lower() or "Error" in result.parts[0].root.text


# -- Test: metrics memory leak fix + Prometheus format ----------------------

class TestMetricsMemoryLeak:
    def test_fanout_durations_capped(self):
        m = MetricsCollector()
        for i in range(MAX_FANOUT_SAMPLES + 500):
            m.record_fanout_duration(float(i))
        assert len(m.fanout_durations) == MAX_FANOUT_SAMPLES

    def test_capped_durations_keep_latest(self):
        m = MetricsCollector()
        for i in range(MAX_FANOUT_SAMPLES + 100):
            m.record_fanout_duration(float(i))
        # Should keep the last MAX_FANOUT_SAMPLES values
        assert m.fanout_durations[0] == 100.0
        assert m.fanout_durations[-1] == float(MAX_FANOUT_SAMPLES + 99)

    def test_under_cap_not_truncated(self):
        m = MetricsCollector()
        for i in range(10):
            m.record_fanout_duration(float(i))
        assert len(m.fanout_durations) == 10


class TestPrometheusTypeAndHelp:
    def test_type_lines_present(self):
        m = MetricsCollector()
        m.update_counts(channels=1, agents=2)
        m.record_message()
        output = m.to_prometheus()
        assert "# TYPE a2a_hub_channels_total gauge" in output
        assert "# TYPE a2a_hub_agents_total gauge" in output
        assert "# TYPE a2a_hub_messages_total counter" in output
        assert "# TYPE a2a_hub_fanout_duration_seconds summary" in output
        assert "# TYPE a2a_hub_webhook_deliveries_total counter" in output
        assert "# TYPE a2a_hub_uptime_seconds gauge" in output

    def test_help_lines_present(self):
        m = MetricsCollector()
        output = m.to_prometheus()
        assert "# HELP a2a_hub_channels_total" in output
        assert "# HELP a2a_hub_messages_total" in output
        assert "# HELP a2a_hub_fanout_duration_seconds" in output
        assert "# HELP a2a_hub_uptime_seconds" in output

    def test_agent_errors_type_only_when_present(self):
        m = MetricsCollector()
        output = m.to_prometheus()
        assert "# TYPE a2a_hub_agent_errors_total" not in output

        m.record_agent_error("rex")
        output = m.to_prometheus()
        assert "# TYPE a2a_hub_agent_errors_total counter" in output

    def test_strategy_usage_type_only_when_present(self):
        m = MetricsCollector()
        output = m.to_prometheus()
        assert "# TYPE a2a_hub_aggregation_strategy_usage" not in output

        m.record_strategy_usage("all")
        output = m.to_prometheus()
        assert "# TYPE a2a_hub_aggregation_strategy_usage counter" in output
