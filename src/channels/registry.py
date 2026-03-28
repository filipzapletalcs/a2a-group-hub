# src/channels/registry.py
"""Channel registry backed by a StorageBackend."""

from __future__ import annotations

import uuid

from src.channels.models import Channel, ChannelMember
from src.storage.base import StorageBackend


class ChannelRegistry:

    def __init__(self, storage: StorageBackend) -> None:
        self.storage = storage

    async def create_channel(
        self,
        name: str,
        channel_id: str | None = None,
        default_aggregation: str = "all",
        agent_timeout: int = 60,
    ) -> Channel:
        cid = channel_id or str(uuid.uuid4())[:8]
        channel = Channel(
            channel_id=cid,
            name=name,
            default_aggregation=default_aggregation,
            agent_timeout=agent_timeout,
        )
        await self.storage.save_channel(channel)
        return channel

    async def get_channel(self, channel_id: str) -> Channel | None:
        return await self.storage.get_channel(channel_id)

    async def list_channels(self) -> list[Channel]:
        return await self.storage.list_channels()

    async def delete_channel(self, channel_id: str) -> bool:
        return await self.storage.delete_channel(channel_id)

    async def add_member(self, channel_id: str, member: ChannelMember) -> None:
        await self.storage.save_member(channel_id, member)

    async def remove_member(self, channel_id: str, agent_id: str) -> bool:
        return await self.storage.remove_member(channel_id, agent_id)

    async def find_channels_for_agent(self, agent_id: str) -> list[Channel]:
        all_channels = await self.storage.list_channels()
        return [ch for ch in all_channels if agent_id in ch.members]
