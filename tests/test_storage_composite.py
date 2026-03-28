# tests/test_storage_composite.py
"""Tests for CompositeBackend — verifies correct delegation to Neo4j and Qdrant."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from src.storage.composite import CompositeBackend
from src.storage.base import StoredMessage, Webhook
from src.channels.models import Channel, ChannelMember, MemberRole


@pytest.fixture
def mock_neo4j():
    backend = AsyncMock()
    return backend


@pytest.fixture
def mock_qdrant():
    backend = AsyncMock()
    return backend


@pytest.fixture
def composite(mock_neo4j, mock_qdrant):
    return CompositeBackend(neo4j_backend=mock_neo4j, qdrant_backend=mock_qdrant)


class TestChannelDelegation:
    """Channel operations should delegate to Neo4j."""

    async def test_save_channel(self, composite, mock_neo4j):
        ch = Channel(channel_id="dev", name="dev-team")
        await composite.save_channel(ch)
        mock_neo4j.save_channel.assert_awaited_once_with(ch)

    async def test_get_channel(self, composite, mock_neo4j):
        mock_neo4j.get_channel.return_value = Channel(channel_id="dev", name="dev-team")
        result = await composite.get_channel("dev")
        mock_neo4j.get_channel.assert_awaited_once_with("dev")
        assert result.channel_id == "dev"

    async def test_list_channels(self, composite, mock_neo4j):
        mock_neo4j.list_channels.return_value = [Channel(channel_id="dev", name="dev-team")]
        result = await composite.list_channels()
        mock_neo4j.list_channels.assert_awaited_once()
        assert len(result) == 1

    async def test_delete_channel(self, composite, mock_neo4j):
        mock_neo4j.delete_channel.return_value = True
        result = await composite.delete_channel("dev")
        mock_neo4j.delete_channel.assert_awaited_once_with("dev")
        assert result is True


class TestMemberDelegation:
    """Member operations should delegate to Neo4j."""

    async def test_save_member(self, composite, mock_neo4j):
        member = ChannelMember(agent_id="apollo", name="Apollo", url="http://localhost:9002")
        await composite.save_member("dev", member)
        mock_neo4j.save_member.assert_awaited_once_with("dev", member)

    async def test_remove_member(self, composite, mock_neo4j):
        mock_neo4j.remove_member.return_value = True
        result = await composite.remove_member("dev", "apollo")
        mock_neo4j.remove_member.assert_awaited_once_with("dev", "apollo")
        assert result is True


class TestMessageDelegation:
    """Message operations should delegate to Qdrant."""

    async def test_save_message(self, composite, mock_qdrant):
        msg = StoredMessage(
            message_id="msg-1", channel_id="dev", sender_id="apollo",
            text="Hello", context_id="ctx-1",
        )
        await composite.save_message("dev", msg)
        mock_qdrant.save_message.assert_awaited_once_with("dev", msg)

    async def test_get_messages(self, composite, mock_qdrant):
        mock_qdrant.get_messages.return_value = []
        result = await composite.get_messages("dev", limit=10, offset=0)
        mock_qdrant.get_messages.assert_awaited_once_with("dev", limit=10, offset=0)
        assert result == []

    async def test_search_messages(self, composite, mock_qdrant):
        mock_qdrant.search_messages.return_value = []
        result = await composite.search_messages("dev", "auth", limit=5)
        mock_qdrant.search_messages.assert_awaited_once_with("dev", "auth", limit=5)
        assert result == []

    async def test_search_all_messages(self, composite, mock_qdrant):
        mock_qdrant.search_all_messages.return_value = []
        result = await composite.search_all_messages("auth", limit=5)
        mock_qdrant.search_all_messages.assert_awaited_once_with("auth", limit=5)
        assert result == []


class TestWebhookDelegation:
    """Webhook operations should delegate to Neo4j."""

    async def test_save_webhook(self, composite, mock_neo4j):
        wh = Webhook(webhook_id="wh-1", url="http://example.com/hook")
        await composite.save_webhook("dev", wh)
        mock_neo4j.save_webhook.assert_awaited_once_with("dev", wh)

    async def test_list_webhooks(self, composite, mock_neo4j):
        mock_neo4j.list_webhooks.return_value = []
        result = await composite.list_webhooks("dev")
        mock_neo4j.list_webhooks.assert_awaited_once_with("dev")
        assert result == []

    async def test_delete_webhook(self, composite, mock_neo4j):
        mock_neo4j.delete_webhook.return_value = True
        result = await composite.delete_webhook("dev", "wh-1")
        mock_neo4j.delete_webhook.assert_awaited_once_with("dev", "wh-1")
        assert result is True


class TestNoCrossContamination:
    """Verify that Neo4j never gets message calls, Qdrant never gets channel calls."""

    async def test_save_message_does_not_touch_neo4j(self, composite, mock_neo4j):
        msg = StoredMessage(
            message_id="msg-1", channel_id="dev", sender_id="a",
            text="test", context_id="c",
        )
        await composite.save_message("dev", msg)
        mock_neo4j.save_message.assert_not_awaited()

    async def test_save_channel_does_not_touch_qdrant(self, composite, mock_qdrant):
        ch = Channel(channel_id="dev", name="dev-team")
        await composite.save_channel(ch)
        mock_qdrant.save_channel.assert_not_awaited()
