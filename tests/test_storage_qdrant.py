# tests/test_storage_qdrant.py
"""Tests for QdrantBackend — uses mocked QdrantClient and Voyage AI (no real services needed)."""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from src.storage.qdrant import QdrantBackend
from src.storage.base import StoredMessage


@pytest.fixture
def mock_qdrant_client():
    """Mock qdrant_client to avoid needing a real Qdrant instance."""
    with patch("src.storage.qdrant.QdrantClient") as MockClient:
        client = MagicMock()
        client.get_collections.return_value = MagicMock(collections=[])
        MockClient.return_value = client
        yield client


@pytest.fixture
def mock_voyage():
    """Create a mock Voyage AI client with embed returning 1024-dim vectors."""
    voyage_client = MagicMock()
    embed_result = MagicMock()
    embed_result.embeddings = [[0.0] * 1024]
    voyage_client.embed.return_value = embed_result
    return voyage_client


@pytest.fixture
def backend(mock_qdrant_client, mock_voyage):
    """Create QdrantBackend with mocked Qdrant and Voyage clients."""
    b = QdrantBackend(url="http://localhost:6333", collection="test_hub_messages")
    b._voyage = mock_voyage
    return b


class TestQdrantMessageStorage:
    async def test_save_message(self, backend, mock_qdrant_client):
        msg = StoredMessage(
            message_id="msg-1", channel_id="dev", sender_id="apollo",
            text="Hello team", context_id="ctx-1",
        )
        await backend.save_message("dev", msg)
        mock_qdrant_client.upsert.assert_called_once()

    async def test_save_message_skips_when_no_embedding(self, mock_qdrant_client):
        """When Voyage client is unavailable, save_message skips upsert gracefully."""
        b = QdrantBackend(url="http://localhost:6333", collection="test_hub_messages")
        b._voyage = None
        msg = StoredMessage(
            message_id="msg-skip", channel_id="dev", sender_id="apollo",
            text="No embedding", context_id="ctx-1",
        )
        await b.save_message("dev", msg)
        mock_qdrant_client.upsert.assert_not_called()

    async def test_get_messages_by_channel(self, backend, mock_qdrant_client):
        mock_qdrant_client.scroll.return_value = ([], None)
        messages = await backend.get_messages("dev", limit=10)
        assert isinstance(messages, list)
        assert len(messages) == 0

    async def test_search_messages_semantic(self, backend, mock_qdrant_client):
        mock_qdrant_client.query_points.return_value = MagicMock(points=[])
        results = await backend.search_messages("dev", query="authentication")
        assert isinstance(results, list)
        assert len(results) == 0

    async def test_search_all_messages(self, backend, mock_qdrant_client):
        mock_qdrant_client.query_points.return_value = MagicMock(points=[])
        results = await backend.search_all_messages(query="how did we solve auth?")
        assert isinstance(results, list)
        assert len(results) == 0

    async def test_search_returns_empty_when_no_voyage(self, mock_qdrant_client):
        """When Voyage client is unavailable, search returns empty list."""
        b = QdrantBackend(url="http://localhost:6333", collection="test_hub_messages")
        b._voyage = None
        results = await b.search_messages("dev", query="test")
        assert results == []
        results = await b.search_all_messages(query="test")
        assert results == []

    async def test_save_message_with_project_and_tags(self, backend, mock_qdrant_client):
        msg = StoredMessage(
            message_id="msg-2", channel_id="dev", sender_id="rex",
            text="Use FastAPI", context_id="ctx-1",
            project_id="openclaw", tags=["architecture", "api"],
        )
        await backend.save_message("dev", msg)
        call_args = mock_qdrant_client.upsert.call_args
        points = call_args.kwargs.get("points") or call_args[1].get("points")
        point = points[0]
        assert point.payload["project_id"] == "openclaw"
        assert point.payload["tags"] == ["architecture", "api"]

    async def test_save_message_with_reply_to(self, backend, mock_qdrant_client):
        msg = StoredMessage(
            message_id="msg-3", channel_id="dev", sender_id="nova",
            text="I agree with Rex", context_id="ctx-1",
            reply_to_message_id="msg-2",
        )
        await backend.save_message("dev", msg)
        call_args = mock_qdrant_client.upsert.call_args
        points = call_args.kwargs.get("points") or call_args[1].get("points")
        point = points[0]
        assert point.payload["reply_to_message_id"] == "msg-2"

    async def test_get_messages_returns_stored_messages(self, backend, mock_qdrant_client):
        """Verify scroll results are converted to StoredMessage objects."""
        mock_point = MagicMock()
        mock_point.payload = {
            "message_id": "msg-1",
            "channel_id": "dev",
            "sender_id": "apollo",
            "text": "Hello team",
            "context_id": "ctx-1",
            "timestamp": "2026-03-28T10:00:00+00:00",
            "metadata": {},
            "project_id": None,
            "tags": [],
            "reply_to_message_id": None,
        }
        mock_qdrant_client.scroll.return_value = ([mock_point], None)
        messages = await backend.get_messages("dev", limit=10)
        assert len(messages) == 1
        assert isinstance(messages[0], StoredMessage)
        assert messages[0].message_id == "msg-1"
        assert messages[0].text == "Hello team"

    async def test_search_messages_returns_stored_messages(self, backend, mock_qdrant_client):
        """Verify query_points results are converted to StoredMessage objects."""
        mock_point = MagicMock()
        mock_point.payload = {
            "message_id": "msg-1",
            "channel_id": "dev",
            "sender_id": "apollo",
            "text": "Authentication fix",
            "context_id": "ctx-1",
            "timestamp": "2026-03-28T10:00:00+00:00",
            "metadata": {},
            "project_id": "openclaw",
            "tags": ["auth"],
            "reply_to_message_id": None,
        }
        mock_qdrant_client.query_points.return_value = MagicMock(points=[mock_point])
        results = await backend.search_messages("dev", query="auth")
        assert len(results) == 1
        assert results[0].project_id == "openclaw"
        assert results[0].tags == ["auth"]

    async def test_channel_methods_raise_not_implemented(self, backend):
        with pytest.raises(NotImplementedError):
            await backend.save_channel(None)
        with pytest.raises(NotImplementedError):
            await backend.get_channel("dev")
        with pytest.raises(NotImplementedError):
            await backend.list_channels()
        with pytest.raises(NotImplementedError):
            await backend.delete_channel("dev")

    async def test_member_methods_raise_not_implemented(self, backend):
        with pytest.raises(NotImplementedError):
            await backend.save_member("dev", None)
        with pytest.raises(NotImplementedError):
            await backend.remove_member("dev", "agent-1")

    async def test_webhook_methods_raise_not_implemented(self, backend):
        with pytest.raises(NotImplementedError):
            await backend.save_webhook("dev", None)
        with pytest.raises(NotImplementedError):
            await backend.list_webhooks("dev")
        with pytest.raises(NotImplementedError):
            await backend.delete_webhook("dev", "wh-1")


class TestQdrantCollectionInit:
    def test_creates_collection_if_not_exists(self, mock_qdrant_client):
        """Verify _ensure_collection creates the collection when missing."""
        QdrantBackend(url="http://localhost:6333", collection="new_collection")
        mock_qdrant_client.create_collection.assert_called_once()

    def test_skips_creation_if_collection_exists(self, mock_qdrant_client):
        """Verify _ensure_collection skips creation when collection exists."""
        existing = MagicMock()
        existing.name = "existing_collection"
        mock_qdrant_client.get_collections.return_value = MagicMock(collections=[existing])
        QdrantBackend(url="http://localhost:6333", collection="existing_collection")
        mock_qdrant_client.create_collection.assert_not_called()

    def test_qdrant_client_created_with_timeout(self):
        """Verify QdrantClient is created with timeout=5."""
        with patch("src.storage.qdrant.QdrantClient") as MockClient:
            client = MagicMock()
            client.get_collections.return_value = MagicMock(collections=[])
            MockClient.return_value = client
            QdrantBackend(url="http://localhost:6333")
            MockClient.assert_called_once_with(url="http://localhost:6333", timeout=5)

    def test_vector_size_is_1024(self):
        """Verify VECTOR_SIZE matches voyage-3-lite dimensions."""
        assert QdrantBackend.VECTOR_SIZE == 1024

    def test_embedding_model_is_voyage_3_lite(self):
        """Verify EMBEDDING_MODEL is voyage-3-lite."""
        assert QdrantBackend.EMBEDDING_MODEL == "voyage-3-lite"


class TestTimezoneHandling:
    """Tests for Bug #10: timezone-naive datetime handling in _point_to_message."""

    @pytest.fixture
    def tz_backend(self, mock_qdrant_client):
        b = QdrantBackend(url="http://localhost:6333", collection="test_hub_messages")
        b._voyage = None
        return b

    def _make_point(self, timestamp_str):
        point = MagicMock()
        point.payload = {
            "message_id": "msg-tz",
            "channel_id": "dev",
            "sender_id": "apollo",
            "text": "tz test",
            "context_id": "ctx-1",
            "timestamp": timestamp_str,
            "metadata": {},
            "project_id": None,
            "tags": [],
            "reply_to_message_id": None,
        }
        return point

    def test_timezone_aware_timestamp_preserved(self, tz_backend):
        point = self._make_point("2026-03-28T10:00:00+00:00")
        msg = tz_backend._point_to_message(point)
        assert msg.timestamp.tzinfo is not None
        assert msg.timestamp == datetime(2026, 3, 28, 10, 0, 0, tzinfo=timezone.utc)

    def test_timezone_naive_gets_utc(self, tz_backend):
        """Timezone-naive timestamps should be assumed UTC."""
        point = self._make_point("2026-03-28T10:00:00")
        msg = tz_backend._point_to_message(point)
        assert msg.timestamp.tzinfo is not None
        assert msg.timestamp.tzinfo == timezone.utc

    def test_empty_timestamp_defaults_to_now_utc(self, tz_backend):
        point = self._make_point("")
        msg = tz_backend._point_to_message(point)
        assert msg.timestamp.tzinfo is not None

    def test_invalid_timestamp_defaults_to_now_utc(self, tz_backend):
        point = self._make_point("not-a-date")
        msg = tz_backend._point_to_message(point)
        assert msg.timestamp.tzinfo is not None

    def test_non_utc_timezone_preserved(self, tz_backend):
        """A timestamp with a non-UTC timezone should keep its original tzinfo."""
        point = self._make_point("2026-03-28T10:00:00+02:00")
        msg = tz_backend._point_to_message(point)
        assert msg.timestamp.tzinfo is not None
        assert msg.timestamp.utcoffset().total_seconds() == 7200
