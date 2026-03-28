# tests/test_storage_memory.py
"""Tests for InMemoryBackend — validates the StorageBackend contract."""

import pytest
from src.channels.models import Channel, ChannelMember, MemberRole
from src.storage.memory import InMemoryBackend


@pytest.fixture
def backend():
    return InMemoryBackend()


@pytest.fixture
def sample_channel():
    return Channel(channel_id="dev", name="dev-team")


class TestChannelStorage:
    async def test_save_and_get_channel(self, backend, sample_channel):
        await backend.save_channel(sample_channel)
        result = await backend.get_channel("dev")
        assert result is not None
        assert result.name == "dev-team"

    async def test_get_nonexistent_channel(self, backend):
        result = await backend.get_channel("nope")
        assert result is None

    async def test_list_channels(self, backend, sample_channel):
        await backend.save_channel(sample_channel)
        await backend.save_channel(Channel(channel_id="ops", name="ops-team"))
        channels = await backend.list_channels()
        assert len(channels) == 2

    async def test_delete_channel(self, backend, sample_channel):
        await backend.save_channel(sample_channel)
        assert await backend.delete_channel("dev") is True
        assert await backend.get_channel("dev") is None

    async def test_delete_nonexistent(self, backend):
        assert await backend.delete_channel("nope") is False


class TestMemberStorage:
    async def test_save_and_list_members(self, backend, sample_channel):
        await backend.save_channel(sample_channel)
        member = ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001")
        await backend.save_member("dev", member)
        ch = await backend.get_channel("dev")
        assert "rex" in ch.members

    async def test_remove_member(self, backend, sample_channel):
        await backend.save_channel(sample_channel)
        await backend.save_member("dev", ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
        assert await backend.remove_member("dev", "rex") is True
        ch = await backend.get_channel("dev")
        assert "rex" not in ch.members


class TestMessageStorage:
    async def test_save_and_get_messages(self, backend):
        from src.storage.base import StoredMessage
        msg = StoredMessage(
            message_id="msg-1",
            channel_id="dev",
            sender_id="apollo",
            text="Hello team",
            context_id="ctx-1",
        )
        await backend.save_message("dev", msg)
        messages = await backend.get_messages("dev", limit=10)
        assert len(messages) == 1
        assert messages[0].text == "Hello team"

    async def test_search_messages_substring(self, backend):
        from src.storage.base import StoredMessage
        await backend.save_message("dev", StoredMessage(
            message_id="1", channel_id="dev", sender_id="a", text="How to fix authentication?", context_id="c",
        ))
        await backend.save_message("dev", StoredMessage(
            message_id="2", channel_id="dev", sender_id="b", text="Deploy pipeline is broken", context_id="c",
        ))
        results = await backend.search_messages("dev", query="authentication")
        assert len(results) == 1
        assert "authentication" in results[0].text

    async def test_get_messages_pagination(self, backend):
        from src.storage.base import StoredMessage
        for i in range(20):
            await backend.save_message("dev", StoredMessage(
                message_id=f"msg-{i}", channel_id="dev", sender_id="a", text=f"Message {i}", context_id="c",
            ))
        page = await backend.get_messages("dev", limit=5, offset=10)
        assert len(page) == 5


class TestWebhookStorage:
    async def test_save_and_list_webhooks(self, backend):
        from src.storage.base import Webhook
        wh = Webhook(webhook_id="wh-1", url="http://example.com/hook", events=["message"])
        await backend.save_webhook("dev", wh)
        webhooks = await backend.list_webhooks("dev")
        assert len(webhooks) == 1

    async def test_delete_webhook(self, backend):
        from src.storage.base import Webhook
        await backend.save_webhook("dev", Webhook(webhook_id="wh-1", url="http://example.com", events=["message"]))
        assert await backend.delete_webhook("dev", "wh-1") is True
        assert len(await backend.list_webhooks("dev")) == 0
