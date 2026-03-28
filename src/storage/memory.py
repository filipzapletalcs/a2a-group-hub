# src/storage/memory.py
"""In-memory storage backend. Zero dependencies, data lost on restart."""

from __future__ import annotations

from src.channels.models import Channel, ChannelMember
from src.storage.base import StorageBackend, StoredMessage, Webhook


class InMemoryBackend(StorageBackend):

    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}
        self._messages: dict[str, list[StoredMessage]] = {}
        self._webhooks: dict[str, list[Webhook]] = {}

    async def save_channel(self, channel: Channel) -> None:
        self._channels[channel.channel_id] = channel

    async def get_channel(self, channel_id: str) -> Channel | None:
        return self._channels.get(channel_id)

    async def list_channels(self) -> list[Channel]:
        return list(self._channels.values())

    async def delete_channel(self, channel_id: str) -> bool:
        if channel_id in self._channels:
            del self._channels[channel_id]
            self._messages.pop(channel_id, None)
            self._webhooks.pop(channel_id, None)
            return True
        return False

    async def save_member(self, channel_id: str, member: ChannelMember) -> None:
        ch = self._channels.get(channel_id)
        if ch:
            ch.add_member(member)

    async def remove_member(self, channel_id: str, agent_id: str) -> bool:
        ch = self._channels.get(channel_id)
        if ch:
            return ch.remove_member(agent_id) is not None
        return False

    async def save_message(self, channel_id: str, message: StoredMessage) -> None:
        self._messages.setdefault(channel_id, []).append(message)

    async def get_messages(self, channel_id: str, limit: int = 50, offset: int = 0) -> list[StoredMessage]:
        msgs = self._messages.get(channel_id, [])
        return msgs[offset:offset + limit]

    async def search_messages(self, channel_id: str, query: str, limit: int = 10) -> list[StoredMessage]:
        msgs = self._messages.get(channel_id, [])
        query_lower = query.lower()
        return [m for m in msgs if query_lower in m.text.lower()][:limit]

    async def save_webhook(self, channel_id: str, webhook: Webhook) -> None:
        self._webhooks.setdefault(channel_id, []).append(webhook)

    async def list_webhooks(self, channel_id: str) -> list[Webhook]:
        return self._webhooks.get(channel_id, [])

    async def delete_webhook(self, channel_id: str, webhook_id: str) -> bool:
        hooks = self._webhooks.get(channel_id, [])
        for i, wh in enumerate(hooks):
            if wh.webhook_id == webhook_id:
                hooks.pop(i)
                return True
        return False
