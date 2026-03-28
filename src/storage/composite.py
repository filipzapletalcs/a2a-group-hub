# src/storage/composite.py
"""Composite storage backend — Neo4j for structure, Qdrant for content."""

from __future__ import annotations

from src.channels.models import Channel, ChannelMember
from src.storage.base import StorageBackend, StoredMessage, Webhook


class CompositeBackend(StorageBackend):
    """Delegates channels/members/webhooks to Neo4j, messages to Qdrant."""

    def __init__(self, neo4j_backend: StorageBackend, qdrant_backend: StorageBackend):
        self._neo4j = neo4j_backend
        self._qdrant = qdrant_backend

    # -- Channels -> Neo4j --

    async def save_channel(self, channel: Channel) -> None:
        return await self._neo4j.save_channel(channel)

    async def get_channel(self, channel_id: str) -> Channel | None:
        return await self._neo4j.get_channel(channel_id)

    async def list_channels(self) -> list[Channel]:
        return await self._neo4j.list_channels()

    async def delete_channel(self, channel_id: str) -> bool:
        return await self._neo4j.delete_channel(channel_id)

    # -- Members -> Neo4j --

    async def save_member(self, channel_id: str, member: ChannelMember) -> None:
        return await self._neo4j.save_member(channel_id, member)

    async def remove_member(self, channel_id: str, agent_id: str) -> bool:
        return await self._neo4j.remove_member(channel_id, agent_id)

    # -- Messages -> Qdrant --

    async def save_message(self, channel_id: str, message: StoredMessage) -> None:
        return await self._qdrant.save_message(channel_id, message)

    async def get_messages(self, channel_id: str, limit: int = 50, offset: int = 0) -> list[StoredMessage]:
        return await self._qdrant.get_messages(channel_id, limit=limit, offset=offset)

    async def search_messages(self, channel_id: str, query: str, limit: int = 10) -> list[StoredMessage]:
        return await self._qdrant.search_messages(channel_id, query, limit=limit)

    async def search_all_messages(self, query: str, limit: int = 10) -> list[StoredMessage]:
        return await self._qdrant.search_all_messages(query, limit=limit)

    # -- Webhooks -> Neo4j --

    async def save_webhook(self, channel_id: str, webhook: Webhook) -> None:
        return await self._neo4j.save_webhook(channel_id, webhook)

    async def list_webhooks(self, channel_id: str) -> list[Webhook]:
        return await self._neo4j.list_webhooks(channel_id)

    async def delete_webhook(self, channel_id: str, webhook_id: str) -> bool:
        return await self._neo4j.delete_webhook(channel_id, webhook_id)
