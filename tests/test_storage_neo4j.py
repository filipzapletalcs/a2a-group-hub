# tests/test_storage_neo4j.py
"""Tests for Neo4jBackend — uses mocked Neo4j driver (no real Neo4j needed)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.channels.models import Channel, ChannelMember, MemberRole
from src.storage.base import StoredMessage, Webhook


# ---------------------------------------------------------------------------
# Helpers to build mock Neo4j records
# ---------------------------------------------------------------------------

def _make_record(data: dict) -> MagicMock:
    """Create a mock neo4j Record that supports .data() and bracket access."""
    rec = MagicMock()
    rec.data.return_value = data
    rec.__getitem__ = lambda self, key: data[key]
    rec.get = lambda key, default=None: data.get(key, default)
    return rec


class _AsyncIterResult:
    """Mock neo4j result that supports async iteration."""

    def __init__(self, records: list):
        self._records = records
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._records):
            raise StopAsyncIteration
        rec = self._records[self._idx]
        self._idx += 1
        return rec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _AsyncContextManager:
    """Helper that wraps a mock session as an async context manager."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def mock_driver():
    """Patch neo4j.AsyncGraphDatabase.driver and return the mock driver."""
    with patch("src.storage.neo4j_backend.AsyncGraphDatabase") as MockAGD:
        driver = MagicMock()
        session = AsyncMock()
        driver.session.return_value = _AsyncContextManager(session)
        MockAGD.driver.return_value = driver

        yield driver, session


@pytest.fixture
def backend(mock_driver):
    driver, _ = mock_driver
    from src.storage.neo4j_backend import Neo4jBackend
    b = Neo4jBackend(url="bolt://localhost:7687", user="neo4j", password="test")
    # Inject the mock driver
    b._driver = driver
    return b


# ---------------------------------------------------------------------------
# Channel tests
# ---------------------------------------------------------------------------

class TestChannelStorage:
    async def test_save_channel(self, backend, mock_driver):
        _, session = mock_driver
        session.run.return_value = AsyncMock()

        channel = Channel(channel_id="dev", name="dev-team", context_id="ctx-1")
        await backend.save_channel(channel)

        session.run.assert_called_once()
        cypher = session.run.call_args[0][0]
        assert "MERGE" in cypher
        assert "HubChannel" in cypher

    async def test_get_channel_found(self, backend, mock_driver):
        _, session = mock_driver

        # Mock result with channel data and members
        channel_record = _make_record({
            "c": {
                "channel_id": "dev",
                "name": "dev-team",
                "context_id": "ctx-1",
                "default_aggregation": "all",
                "agent_timeout": 60,
                "message_count": 0,
            },
            "members": [
                {
                    "agent": {"agent_id": "apollo", "name": "Apollo"},
                    "rel": {"role": "member", "joined_at": "2026-01-01T00:00:00Z", "messages_sent": 5, "last_active": "2026-01-02T00:00:00Z"},
                }
            ],
        })
        result = AsyncMock()
        result.single.return_value = channel_record
        session.run.return_value = result

        channel = await backend.get_channel("dev")

        assert channel is not None
        assert channel.channel_id == "dev"
        assert channel.name == "dev-team"
        assert "apollo" in channel.members

    async def test_get_channel_not_found(self, backend, mock_driver):
        _, session = mock_driver
        result = AsyncMock()
        result.single.return_value = None
        session.run.return_value = result

        channel = await backend.get_channel("nonexistent")
        assert channel is None

    async def test_list_channels(self, backend, mock_driver):
        _, session = mock_driver

        records = [
            _make_record({"c": {"channel_id": "dev", "name": "dev-team", "context_id": "ctx-1", "default_aggregation": "all", "agent_timeout": 60, "message_count": 0}}),
            _make_record({"c": {"channel_id": "ops", "name": "ops-team", "context_id": "ctx-2", "default_aggregation": "all", "agent_timeout": 60, "message_count": 0}}),
        ]
        session.run.return_value = _AsyncIterResult(records)

        channels = await backend.list_channels()
        assert len(channels) == 2
        assert channels[0].channel_id == "dev"
        assert channels[1].channel_id == "ops"

    async def test_delete_channel(self, backend, mock_driver):
        _, session = mock_driver

        result = AsyncMock()
        summary = MagicMock()
        summary.counters.nodes_deleted = 1
        result.consume.return_value = summary
        session.run.return_value = result

        deleted = await backend.delete_channel("dev")
        assert deleted is True

        cypher = session.run.call_args[0][0]
        assert "DELETE" in cypher or "DETACH DELETE" in cypher

    async def test_delete_channel_not_found(self, backend, mock_driver):
        _, session = mock_driver

        result = AsyncMock()
        summary = MagicMock()
        summary.counters.nodes_deleted = 0
        result.consume.return_value = summary
        session.run.return_value = result

        deleted = await backend.delete_channel("nonexistent")
        assert deleted is False


# ---------------------------------------------------------------------------
# Member tests
# ---------------------------------------------------------------------------

class TestMemberStorage:
    async def test_save_member_with_role(self, backend, mock_driver):
        _, session = mock_driver
        session.run.return_value = AsyncMock()

        member = ChannelMember(
            agent_id="apollo", name="Apollo",
            url="http://apollo:9002", role=MemberRole.member,
        )
        await backend.save_member("dev", member)

        session.run.assert_called_once()
        cypher = session.run.call_args[0][0]
        assert "MERGE" in cypher
        assert "Agent" in cypher
        assert "HAS_MEMBER" in cypher
        params = session.run.call_args[0][1] if len(session.run.call_args[0]) > 1 else session.run.call_args[1]
        assert params["role"] == "member"

    async def test_save_member_owner_role(self, backend, mock_driver):
        _, session = mock_driver
        session.run.return_value = AsyncMock()

        member = ChannelMember(
            agent_id="nexus", name="Nexus",
            url="http://nexus:9001", role=MemberRole.owner,
        )
        await backend.save_member("leaders", member)

        params = session.run.call_args[0][1] if len(session.run.call_args[0]) > 1 else session.run.call_args[1]
        assert params["role"] == "owner"

    async def test_remove_member(self, backend, mock_driver):
        _, session = mock_driver

        result = AsyncMock()
        summary = MagicMock()
        summary.counters.relationships_deleted = 1
        result.consume.return_value = summary
        session.run.return_value = result

        removed = await backend.remove_member("dev", "apollo")
        assert removed is True

        cypher = session.run.call_args[0][0]
        assert "DELETE" in cypher
        assert "HAS_MEMBER" in cypher

    async def test_remove_member_not_found(self, backend, mock_driver):
        _, session = mock_driver

        result = AsyncMock()
        summary = MagicMock()
        summary.counters.relationships_deleted = 0
        result.consume.return_value = summary
        session.run.return_value = result

        removed = await backend.remove_member("dev", "nonexistent")
        assert removed is False

    async def test_update_member_activity(self, backend, mock_driver):
        _, session = mock_driver
        session.run.return_value = AsyncMock()

        await backend.update_member_activity("dev", "apollo")

        session.run.assert_called_once()
        cypher = session.run.call_args[0][0]
        assert "messages_sent" in cypher
        assert "last_active" in cypher


# ---------------------------------------------------------------------------
# Webhook tests
# ---------------------------------------------------------------------------

class TestWebhookStorage:
    async def test_save_webhook(self, backend, mock_driver):
        _, session = mock_driver
        session.run.return_value = AsyncMock()

        webhook = Webhook(webhook_id="wh-1", url="http://example.com/hook", events=["message"])
        await backend.save_webhook("dev", webhook)

        session.run.assert_called_once()
        cypher = session.run.call_args[0][0]
        assert "HubWebhook" in cypher
        assert "HAS_WEBHOOK" in cypher

    async def test_list_webhooks(self, backend, mock_driver):
        _, session = mock_driver

        records = [
            _make_record({"w": {"webhook_id": "wh-1", "url": "http://example.com/hook", "events": ["message"]}}),
            _make_record({"w": {"webhook_id": "wh-2", "url": "http://example.com/hook2", "events": ["message", "join"]}}),
        ]
        session.run.return_value = _AsyncIterResult(records)

        webhooks = await backend.list_webhooks("dev")
        assert len(webhooks) == 2
        assert webhooks[0].webhook_id == "wh-1"
        assert webhooks[1].url == "http://example.com/hook2"

    async def test_delete_webhook(self, backend, mock_driver):
        _, session = mock_driver

        result = AsyncMock()
        summary = MagicMock()
        summary.counters.nodes_deleted = 1
        result.consume.return_value = summary
        session.run.return_value = result

        deleted = await backend.delete_webhook("dev", "wh-1")
        assert deleted is True

    async def test_delete_webhook_not_found(self, backend, mock_driver):
        _, session = mock_driver

        result = AsyncMock()
        summary = MagicMock()
        summary.counters.nodes_deleted = 0
        result.consume.return_value = summary
        session.run.return_value = result

        deleted = await backend.delete_webhook("dev", "wh-nonexistent")
        assert deleted is False


# ---------------------------------------------------------------------------
# Message stubs (handled by Qdrant)
# ---------------------------------------------------------------------------

class TestMessageStubs:
    async def test_save_message_raises(self, backend):
        msg = StoredMessage(
            message_id="msg-1", channel_id="dev", sender_id="a",
            text="Hello", context_id="ctx",
        )
        with pytest.raises(NotImplementedError):
            await backend.save_message("dev", msg)

    async def test_get_messages_raises(self, backend):
        with pytest.raises(NotImplementedError):
            await backend.get_messages("dev")

    async def test_search_messages_raises(self, backend):
        with pytest.raises(NotImplementedError):
            await backend.search_messages("dev", "query")

    async def test_search_all_messages_raises(self, backend):
        with pytest.raises(NotImplementedError):
            await backend.search_all_messages("query")
