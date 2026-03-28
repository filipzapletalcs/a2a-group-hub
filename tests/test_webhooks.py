# tests/test_webhooks.py
"""Tests for webhook dispatcher."""

import asyncio
import pytest
import httpx
from unittest.mock import AsyncMock, patch

from src.notifications.webhooks import WebhookDispatcher, WebhookEvent
from src.storage.base import Webhook


@pytest.fixture
def dispatcher():
    client = httpx.AsyncClient(timeout=10.0)
    d = WebhookDispatcher(http_client=client)
    yield d
    # cleanup handled by test


def _make_event(event_type: str = "message") -> WebhookEvent:
    return WebhookEvent(
        event=event_type,
        channel_id="dev-team",
        timestamp="2026-03-28T12:00:00Z",
        data={"sender": "rex", "text": "hello"},
    )


class TestWebhookEvent:
    def test_event_fields(self):
        ev = _make_event()
        assert ev.event == "message"
        assert ev.channel_id == "dev-team"
        assert ev.data["sender"] == "rex"


class TestWebhookDispatcher:
    @pytest.mark.asyncio
    async def test_dispatch_calls_deliver_for_matching_events(self):
        dispatcher = WebhookDispatcher()
        wh = Webhook(webhook_id="wh1", url="http://example.com/hook", events=["message"])
        event = _make_event("message")

        with patch.object(dispatcher, "_deliver", new_callable=AsyncMock) as mock_deliver:
            await dispatcher.dispatch([wh], event)
            # Let the created task run
            await asyncio.sleep(0.05)
            mock_deliver.assert_called_once_with("http://example.com/hook", event)
        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_dispatch_ignores_non_matching_events(self):
        dispatcher = WebhookDispatcher()
        wh = Webhook(webhook_id="wh1", url="http://example.com/hook", events=["member_join"])
        event = _make_event("message")

        with patch.object(dispatcher, "_deliver", new_callable=AsyncMock) as mock_deliver:
            await dispatcher.dispatch([wh], event)
            await asyncio.sleep(0.05)
            mock_deliver.assert_not_called()
        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_dispatch_multiple_webhooks(self):
        dispatcher = WebhookDispatcher()
        wh1 = Webhook(webhook_id="wh1", url="http://a.com/hook", events=["message"])
        wh2 = Webhook(webhook_id="wh2", url="http://b.com/hook", events=["message", "member_join"])
        wh3 = Webhook(webhook_id="wh3", url="http://c.com/hook", events=["member_leave"])
        event = _make_event("message")

        with patch.object(dispatcher, "_deliver", new_callable=AsyncMock) as mock_deliver:
            await dispatcher.dispatch([wh1, wh2, wh3], event)
            await asyncio.sleep(0.05)
            assert mock_deliver.call_count == 2
        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_deliver_posts_correct_payload(self, dispatcher):
        event = _make_event()
        mock_response = httpx.Response(200)

        with patch.object(dispatcher._http_client, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
            await dispatcher._deliver("http://example.com/hook", event)
            mock_post.assert_called_once_with(
                "http://example.com/hook",
                json={
                    "event": "message",
                    "channel_id": "dev-team",
                    "timestamp": "2026-03-28T12:00:00Z",
                    "data": {"sender": "rex", "text": "hello"},
                },
            )
        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_deliver_handles_failure_gracefully(self, dispatcher):
        """Delivery errors are logged, never raised."""
        event = _make_event()

        with patch.object(
            dispatcher._http_client, "post", new_callable=AsyncMock, side_effect=httpx.ConnectError("refused")
        ):
            # Should NOT raise
            await dispatcher._deliver("http://down.example.com/hook", event)
        await dispatcher.close()

    @pytest.mark.asyncio
    async def test_close_without_owning_client(self):
        """close() should not close an externally-provided client."""
        external_client = httpx.AsyncClient()
        dispatcher = WebhookDispatcher(http_client=external_client)
        await dispatcher.close()
        # External client should still be open
        assert not external_client.is_closed
        await external_client.aclose()

    @pytest.mark.asyncio
    async def test_close_owns_client(self):
        """close() should close the internally-created client."""
        dispatcher = WebhookDispatcher()
        assert not dispatcher._http_client.is_closed
        await dispatcher.close()
        assert dispatcher._http_client.is_closed
