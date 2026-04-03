# src/hub/callback.py
"""Async callback handling for OpenClaw agents.

When the hub sends a message to an OpenClaw agent, the agent returns 202
immediately and later POSTs the response back to /api/callback. This module
provides the CallbackStore (pending correlation tracker) and the Starlette
request handler for that endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("a2a-hub.callback")


# ---------------------------------------------------------------------------
# Pending fan-out tracking
# ---------------------------------------------------------------------------

@dataclass
class PendingFanout:
    """Tracks a pending async fan-out waiting for callback."""
    correlation_id: str
    agent_id: str
    channel_id: str
    context_id: str
    future: asyncio.Future
    heartbeat_deadline: float  # loop.time() + timeout


class CallbackStore:
    """Stores pending fan-out correlations and resolves on callback."""

    def __init__(self, timeout: float = 120.0) -> None:
        self._pending: dict[str, PendingFanout] = {}
        self._timeout = timeout

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def register(
        self,
        correlation_id: str,
        agent_id: str,
        channel_id: str,
        context_id: str,
    ) -> asyncio.Future:
        """Register a pending correlation and return a Future to await."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[correlation_id] = PendingFanout(
            correlation_id=correlation_id,
            agent_id=agent_id,
            channel_id=channel_id,
            context_id=context_id,
            future=future,
            heartbeat_deadline=loop.time() + self._timeout,
        )
        logger.debug(
            "Registered pending callback %s for agent %s",
            correlation_id, agent_id,
        )
        return future

    def resolve(
        self,
        correlation_id: str,
        text: str,
        metadata: dict | None = None,
    ) -> bool:
        """Resolve a pending correlation with the agent's response.

        Returns True if resolved, False if correlation_id not found.
        """
        pending = self._pending.pop(correlation_id, None)
        if pending and not pending.future.done():
            pending.future.set_result({"text": text, "metadata": metadata})
            logger.info(
                "Resolved callback %s from agent %s (%d chars)",
                correlation_id, pending.agent_id, len(text),
            )
            return True
        if pending is None:
            logger.warning("Resolve for unknown correlation_id: %s", correlation_id)
        return False

    def heartbeat(self, correlation_id: str) -> bool:
        """Reset the heartbeat deadline for a pending correlation.

        Returns True if found and updated.
        """
        pending = self._pending.get(correlation_id)
        if pending:
            loop = asyncio.get_running_loop()
            pending.heartbeat_deadline = loop.time() + self._timeout
            logger.debug("Heartbeat for %s (agent %s)", correlation_id, pending.agent_id)
            return True
        return False

    def cleanup_expired(self) -> list[str]:
        """Find expired pending entries and cancel their futures.

        Returns list of expired correlation_ids for logging.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()
        expired: list[str] = []

        for cid, pending in list(self._pending.items()):
            if now > pending.heartbeat_deadline:
                expired.append(cid)
                self._pending.pop(cid, None)
                if not pending.future.done():
                    pending.future.set_exception(
                        TimeoutError(
                            f"Callback timeout for agent {pending.agent_id} "
                            f"(correlation {cid})"
                        )
                    )

        return expired


# ---------------------------------------------------------------------------
# Starlette request handler
# ---------------------------------------------------------------------------

async def handle_callback(request: Request) -> JSONResponse:
    """Handle POST /api/callback from OpenClaw agent plugins.

    Payload types:
      - heartbeat: { type: "heartbeat", correlation_id, agent_id }
      - response:  { type: "response", correlation_id, agent_id, text, metadata? }
      - proactive: { type: "proactive", agent_id, text, metadata: { channel_id } }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    msg_type = body.get("type")
    callback_store: CallbackStore = request.app.state.callback_store

    if msg_type == "heartbeat":
        ok = callback_store.heartbeat(body.get("correlation_id", ""))
        return JSONResponse({"ok": ok})

    elif msg_type == "response":
        ok = callback_store.resolve(
            correlation_id=body.get("correlation_id", ""),
            text=body.get("text", ""),
            metadata=body.get("metadata"),
        )
        return JSONResponse({"ok": ok})

    elif msg_type == "proactive":
        await handle_proactive_message(request.app, body)
        return JSONResponse({"ok": True})

    return JSONResponse({"error": "unknown type"}, status_code=400)


# ---------------------------------------------------------------------------
# Proactive message routing
# ---------------------------------------------------------------------------

async def handle_proactive_message(app, body: dict) -> None:
    """Route unsolicited agent output through the hub's storage.

    The Telegram bridge monitors hub messages; saving to storage is sufficient
    for the bridge to pick up and forward to Telegram.
    """
    agent_id = body.get("agent_id", "")
    text = body.get("text", "")
    channel_id = body.get("metadata", {}).get("channel_id", "")

    if not text or not channel_id:
        logger.warning(
            "Proactive message missing text or channel_id: agent=%s", agent_id,
        )
        return

    # Store message via hub's storage backend
    storage = app.state.storage
    from src.storage.base import StoredMessage

    await storage.save_message(
        channel_id,
        StoredMessage(
            message_id=str(uuid.uuid4()),
            channel_id=channel_id,
            sender_id=agent_id,
            text=text,
            context_id=channel_id,  # use channel_id as context for proactive
        ),
    )

    logger.info(
        "Proactive message from %s in #%s: %d chars",
        agent_id, channel_id, len(text),
    )


# ---------------------------------------------------------------------------
# Background cleanup task
# ---------------------------------------------------------------------------

async def run_callback_cleanup(
    callback_store: CallbackStore, interval: float = 10.0,
) -> None:
    """Background task to clean up expired pending callbacks."""
    while True:
        try:
            expired = callback_store.cleanup_expired()
            if expired:
                logger.warning(
                    "Expired %d pending callbacks: %s", len(expired), expired,
                )
        except Exception:
            logger.debug("Callback cleanup error", exc_info=True)
        await asyncio.sleep(interval)
