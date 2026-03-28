"""Webhook notifications for channel events."""
import asyncio
import logging
import httpx
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger("a2a-hub.webhooks")


@dataclass
class WebhookEvent:
    event: str  # "message", "member_join", "member_leave", "channel_created"
    channel_id: str
    timestamp: str
    data: dict


class WebhookDispatcher:
    """Fire-and-forget POST to registered webhook URLs."""

    def __init__(self, http_client: httpx.AsyncClient | None = None, metrics=None):
        self._http_client = http_client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = http_client is None
        self._metrics = metrics

    async def close(self):
        if self._owns_client:
            await self._http_client.aclose()

    async def dispatch(self, webhooks: list, event: WebhookEvent) -> None:
        """Send event to all webhooks that subscribe to this event type (fire-and-forget)."""
        for wh in webhooks:
            if event.event in wh.events:
                asyncio.create_task(self._deliver(wh.url, event))

    async def _deliver(self, url: str, event: WebhookEvent) -> None:
        """POST event payload to webhook URL. Logs errors, never raises."""
        try:
            payload = {
                "event": event.event,
                "channel_id": event.channel_id,
                "timestamp": event.timestamp,
                "data": event.data,
            }
            resp = await self._http_client.post(url, json=payload)
            logger.info(f"Webhook {url}: {resp.status_code}")
            if self._metrics:
                self._metrics.record_webhook_delivery(success=True)
        except Exception as e:
            logger.error(f"Webhook delivery to {url} failed: {e}")
            if self._metrics:
                self._metrics.record_webhook_delivery(success=False)
