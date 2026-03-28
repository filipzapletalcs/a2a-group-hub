# src/telegram/bridge.py
"""Bidirectional Telegram <-> A2A Hub bridge.

Hub -> Telegram: After fan-out, agent responses are forwarded to Telegram topics.
Telegram -> Hub: Filip's messages in Telegram topics are sent as A2A SendMessage.
Errors -> system-log topic (ID 9).

Uses polling (not webhooks) since the server has no public URL.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import httpx

from src.telegram.config import TelegramConfig
from src.telegram.formatter import format_agent_message, format_system_message

logger = logging.getLogger("a2a-hub.telegram")

# System-log topic for errors and diagnostics
SYSTEM_LOG_TOPIC_ID = 9


class TelegramBridge:
    """Bidirectional Telegram <-> A2A Hub bridge."""

    def __init__(
        self,
        config: TelegramConfig,
        hub_base_url: str = "http://localhost:8000",
    ) -> None:
        self._config = config
        self._hub_base_url = hub_base_url.rstrip("/")
        self._bot = None
        self._http_client: httpx.AsyncClient | None = None
        self._polling_task: asyncio.Task | None = None
        self._running = False

    @property
    def _reverse_topic_map(self) -> dict[int, str]:
        """Reverse map: topic_id -> channel_id."""
        return {v: k for k, v in self._config.channel_topic_map.items()}

    def _channel_for_topic(self, topic_id: int) -> str | None:
        return self._reverse_topic_map.get(topic_id)

    async def start(self) -> None:
        """Initialize bot, HTTP client, flush old messages, and start polling."""
        try:
            from telegram import Bot
            self._bot = Bot(token=self._config.bot_token)
        except ImportError:
            logger.error("python-telegram-bot not installed — pip install python-telegram-bot")
            return

        self._http_client = httpx.AsyncClient(timeout=120.0)
        self._running = True

        # Flush old Telegram messages before we start processing
        await self._flush_old_updates()

        # Start polling for NEW Telegram messages only
        self._polling_task = asyncio.create_task(self._poll_telegram())

        logger.info(
            "Telegram bridge started (polling mode): chat_id=%s, channels=%s",
            self._config.chat_id,
            list(self._config.channel_topic_map.keys()),
        )

    async def stop(self) -> None:
        """Stop polling and clean up."""
        self._running = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        if self._http_client:
            await self._http_client.aclose()

    # ── Flush old updates on startup ─────────────────────────

    async def _flush_old_updates(self) -> None:
        """Consume all pending Telegram updates without processing them.
        This prevents the bridge from reacting to old messages after a restart."""
        url = f"https://api.telegram.org/bot{self._config.bot_token}/getUpdates"
        flushed = 0
        offset = 0

        while True:
            resp = await self._http_client.get(url, params={"offset": offset, "timeout": 0})
            data = resp.json()
            updates = data.get("result", [])
            if not updates:
                break
            flushed += len(updates)
            offset = updates[-1]["update_id"] + 1

        self._poll_offset = offset
        logger.info("Flushed %d old Telegram updates", flushed)

    # ── Telegram polling ─────────────────────────────────────

    async def _poll_telegram(self) -> None:
        """Long-poll Telegram Bot API for new messages."""
        offset = self._poll_offset
        logger.info("Telegram polling started (offset=%d)", offset)

        while self._running:
            try:
                url = f"https://api.telegram.org/bot{self._config.bot_token}/getUpdates"
                params = {"offset": offset, "timeout": 30, "allowed_updates": ["message"]}

                resp = await self._http_client.get(url, params=params, timeout=45.0)
                data = resp.json()

                if not data.get("ok"):
                    logger.error("Telegram API error: %s", data)
                    await asyncio.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if message:
                        await self._handle_telegram_message(message)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Telegram polling error")
                await asyncio.sleep(5)

    async def _handle_telegram_message(self, message: dict) -> None:
        """Process incoming Telegram message — forward to hub if in a mapped topic."""
        # Only our supergroup
        chat_id = message.get("chat", {}).get("id")
        if chat_id != self._config.chat_id:
            return

        # Ignore messages from bots (including our own System bot)
        from_user = message.get("from", {})
        if from_user.get("is_bot", False):
            return

        # Must be in a topic thread
        topic_id = message.get("message_thread_id")
        if topic_id is None:
            return

        # Must be a mapped channel topic (not announcements, system-log, etc.)
        channel_id = self._channel_for_topic(topic_id)
        if channel_id is None:
            return

        text = message.get("text", "")
        if not text:
            return

        user_name = from_user.get("first_name", "Unknown")
        logger.info("Telegram -> Hub: [%s] in #%s: %s", user_name, channel_id, text[:80])

        await self._send_to_hub(channel_id, user_name, text)

    # ── Telegram -> Hub ──────────────────────────────────────

    async def _send_to_hub(self, channel_id: str, sender_name: str, text: str) -> None:
        """Send message to hub via A2A SendMessage (JSON-RPC)."""
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": text}],
                    "metadata": {
                        "channel_id": channel_id,
                        "sender_name": sender_name,
                        "source": "telegram",
                    },
                },
            },
        }

        try:
            resp = await self._http_client.post(
                f"{self._hub_base_url}/",
                json=payload,
                timeout=120.0,
            )
            result = resp.json()

            artifacts = result.get("result", {}).get("artifacts", [])
            if artifacts:
                await self._send_responses_to_telegram(channel_id, artifacts)

        except Exception:
            logger.exception("Failed to send to hub: #%s", channel_id)

    # ── Hub -> Telegram ──────────────────────────────────────

    async def _send_responses_to_telegram(self, channel_id: str, artifacts: list[dict]) -> None:
        """Send agent responses to the correct Telegram topic.
        Errors go to system-log topic, real responses go to the channel topic."""
        topic_id = self._config.channel_topic_map.get(channel_id)
        if topic_id is None:
            return

        for artifact in artifacts:
            name = artifact.get("name", "Agent")
            parts = artifact.get("parts", [])
            text = ""
            for part in parts:
                if part.get("kind") == "text":
                    text += part.get("text", "")

            if not text:
                continue

            agent_name = name.replace("Response from ", "")

            # Detect errors — send to system-log, not to channel topic
            is_error = (
                text.startswith("[Error from")
                or text.startswith("[error]")
                or "Rate limited" in text
                or "error**:" in text
                or "clewd" in text.lower()[:50]
            )

            if is_error:
                error_msg = format_system_message(f"[{agent_name}] {text[:500]}")
                await self._send_telegram_message(error_msg, SYSTEM_LOG_TOPIC_ID)
                continue

            # Real response — send to channel topic
            formatted = format_agent_message(agent_name, text)
            if len(formatted) > 4096:
                formatted = formatted[:4093] + "..."

            await self._send_telegram_message(formatted, topic_id)

    async def _send_telegram_message(self, text: str, topic_id: int | None = None) -> None:
        """Send a message to the Telegram supergroup."""
        if self._bot is None:
            return

        try:
            kwargs = {
                "chat_id": self._config.chat_id,
                "text": text,
            }
            if topic_id is not None:
                kwargs["message_thread_id"] = topic_id

            await self._bot.send_message(**kwargs)
        except Exception:
            logger.exception("Failed to send Telegram message to topic %s", topic_id)

    # ── Public API for hub integration ────────────────────────

    async def notify_channel_message(self, channel_id: str, sender_name: str, text: str) -> None:
        """Called by hub after agent responds — forwards to Telegram."""
        topic_id = self._config.channel_topic_map.get(channel_id)
        if topic_id is None:
            return

        formatted = format_agent_message(sender_name, text)
        if len(formatted) > 4096:
            formatted = formatted[:4093] + "..."

        await self._send_telegram_message(formatted, topic_id)
