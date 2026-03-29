# tests/test_fanout.py
"""Tests for fan-out engine — mock A2A clients to test broadcast logic."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from a2a.types import Part, TextPart
from src.channels.models import Channel, ChannelMember, MemberRole
from src.hub.fanout import CircuitBreaker, FanOutEngine, FanOutResult


@pytest.fixture
def channel():
    ch = Channel(channel_id="dev", name="dev-team")
    ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
    ch.add_member(ChannelMember(agent_id="pixel", name="Pixel", url="http://localhost:9002"))
    ch.add_member(ChannelMember(agent_id="vigil", name="Vigil", url="http://localhost:9003", role=MemberRole.observer))
    return ch


class TestFanOutEngine:
    async def test_fan_out_excludes_sender(self, channel):
        engine = FanOutEngine()
        engine._send_to_agent = AsyncMock(return_value=FanOutResult(agent_id="test", agent_name="Test"))

        results = await engine.fan_out(channel, message_parts=[], sender_id="rex", context_id="ctx")

        # Should send to pixel (member) + vigil (observer) but not rex (sender)
        assert engine._send_to_agent.call_count == 2

    async def test_fan_out_returns_only_member_results(self, channel):
        """Observer results are fire-and-forget, not included in return."""
        engine = FanOutEngine()

        async def mock_send(member, **kwargs):
            return FanOutResult(agent_id=member.agent_id, agent_name=member.name, response_text=f"Reply from {member.name}")

        engine._send_to_agent = mock_send
        results = await engine.fan_out(channel, message_parts=[], sender_id="rex", context_id="ctx")

        # Only pixel's result returned (vigil is observer = fire-and-forget)
        assert len(results) == 1
        assert results[0].agent_id == "pixel"

    async def test_fan_out_handles_agent_failure(self, channel):
        engine = FanOutEngine()

        async def mock_send(member, **kwargs):
            if member.agent_id == "pixel":
                return FanOutResult(agent_id="pixel", agent_name="Pixel", error="Connection refused")
            return FanOutResult(agent_id=member.agent_id, agent_name=member.name, response_text="OK")

        engine._send_to_agent = mock_send
        results = await engine.fan_out(channel, message_parts=[], sender_id="rex", context_id="ctx")

        assert len(results) == 1
        assert results[0].error == "Connection refused"

    async def test_fan_out_empty_channel(self):
        ch = Channel(channel_id="empty", name="empty")
        engine = FanOutEngine()
        results = await engine.fan_out(ch, message_parts=[], sender_id=None, context_id="ctx")
        assert len(results) == 0

    async def test_observer_done_callback_logs_exception(self):
        """Observer fire-and-forget tasks should have a done callback that logs errors."""
        engine = FanOutEngine()

        async def failing_send(member, **kwargs):
            raise RuntimeError("Observer delivery exploded")

        engine._send_to_agent = failing_send

        ch = Channel(channel_id="dev", name="dev-team")
        ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
        ch.add_member(ChannelMember(
            agent_id="vigil", name="Vigil", url="http://localhost:9003", role=MemberRole.observer
        ))

        # Fan out from rex — vigil (observer) should get fire-and-forget
        # This should not raise, even though observer delivery fails
        results = await engine.fan_out(ch, message_parts=[], sender_id="rex", context_id="ctx")

        # Allow observer task to complete
        import asyncio
        await asyncio.sleep(0.05)

        # No member results since only vigil (observer) is a peer and not sendable
        assert len(results) == 0

    async def test_channel_context_prepended_to_message(self, channel):
        """Fan-out should prepend channel name and member list to message text."""
        engine = FanOutEngine()
        captured_parts: list = []

        original_send = engine._send_to_agent

        async def capture_send(member, *, message_parts, channel, context_id, metadata=None):
            """Intercept _send_to_agent to capture the enriched parts it builds."""
            # We can't easily capture what happens inside _send_to_agent since it
            # creates the Message internally. Instead, test the logic directly.
            pass

        # Test the enrichment logic directly by calling _send_to_agent with a
        # mock A2AClient and capturing the outbound message.
        original_text = "Hello team!"
        parts = [Part(root=TextPart(text=original_text))]

        with patch("src.hub.fanout.A2AClient") as MockClient:
            mock_instance = MagicMock()
            mock_response = MagicMock()
            mock_result = MagicMock()
            mock_result.result = MagicMock(spec=[])
            mock_result.result.parts = []
            mock_response.root = mock_result
            mock_instance.send_message = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            target = channel.members["pixel"]
            await engine._send_to_agent(
                target, message_parts=parts, channel=channel, context_id="ctx"
            )

            # Extract the message that was sent
            call_args = mock_instance.send_message.call_args
            request = call_args[0][0]
            sent_parts = request.params.message.parts
            sent_text = sent_parts[0].root.text

            # Verify context prefix is present
            assert sent_text.startswith("[Kanál: #dev-team |")
            assert "Členové:" in sent_text
            assert "Rex" in sent_text
            assert "Pixel" in sent_text
            assert "Vigil (observer)" in sent_text
            # Verify original text is preserved after prefix
            assert sent_text.endswith(original_text)

    async def test_channel_context_added_when_no_text_parts(self, channel):
        """When message has no text parts, context should be added as a new part."""
        engine = FanOutEngine()

        with patch("src.hub.fanout.A2AClient") as MockClient:
            mock_instance = MagicMock()
            mock_response = MagicMock()
            mock_result = MagicMock()
            mock_result.result = MagicMock(spec=[])
            mock_result.result.parts = []
            mock_response.root = mock_result
            mock_instance.send_message = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            target = channel.members["pixel"]
            await engine._send_to_agent(
                target, message_parts=[], channel=channel, context_id="ctx"
            )

            call_args = mock_instance.send_message.call_args
            request = call_args[0][0]
            sent_parts = request.params.message.parts

            assert len(sent_parts) == 1
            assert sent_parts[0].root.text.startswith("[Kanál: #dev-team |")


class TestSendToSingle:
    """Tests for send_to_single with enriched context."""

    async def test_send_to_single_includes_memory_context(self, channel):
        """send_to_single should include memory context in the message."""
        engine = FanOutEngine()

        with patch("src.hub.fanout.A2AClient") as MockClient:
            mock_instance = MagicMock()
            mock_response = MagicMock()
            mock_result = MagicMock()
            mock_result.result = MagicMock(spec=[])
            mock_result.result.parts = []
            mock_response.root = mock_result
            mock_instance.send_message = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            target = channel.members["pixel"]
            await engine.send_to_single(
                member=target,
                message_parts=[Part(root=TextPart(text="Fix the CSS"))],
                channel=channel,
                context_id="ctx",
                memory_context="[Relevantní historie]\n- rex: mentioned CSS issue yesterday",
            )

            call_args = mock_instance.send_message.call_args
            request = call_args[0][0]
            sent_text = request.params.message.parts[0].root.text

            assert "[Kanál: #dev-team |" in sent_text
            assert "Relevantní historie" in sent_text
            assert "rex: mentioned CSS issue" in sent_text
            assert "Fix the CSS" in sent_text

    async def test_send_to_single_includes_previous_responses(self, channel):
        """send_to_single should include previous responses in context."""
        engine = FanOutEngine()

        with patch("src.hub.fanout.A2AClient") as MockClient:
            mock_instance = MagicMock()
            mock_response = MagicMock()
            mock_result = MagicMock()
            mock_result.result = MagicMock(spec=[])
            mock_result.result.parts = []
            mock_response.root = mock_result
            mock_instance.send_message = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            target = channel.members["pixel"]
            prev = [
                FanOutResult(agent_id="apollo", agent_name="Apollo",
                             response_text="Delegate to pixel for CSS work"),
            ]
            await engine.send_to_single(
                member=target,
                message_parts=[Part(root=TextPart(text="Fix the CSS"))],
                channel=channel,
                context_id="ctx",
                previous_responses=prev,
            )

            call_args = mock_instance.send_message.call_args
            request = call_args[0][0]
            sent_text = request.params.message.parts[0].root.text

            assert "[Odpovědi v tomto kole]" in sent_text
            assert "Apollo: Delegate to pixel" in sent_text

    async def test_send_to_single_truncates_responses(self, channel):
        """Previous responses should be truncated to 500 chars."""
        engine = FanOutEngine()

        with patch("src.hub.fanout.A2AClient") as MockClient:
            mock_instance = MagicMock()
            mock_response = MagicMock()
            mock_result = MagicMock()
            mock_result.result = MagicMock(spec=[])
            mock_result.result.parts = []
            mock_response.root = mock_result
            mock_instance.send_message = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            target = channel.members["pixel"]
            long_text = "x" * 1000
            prev = [
                FanOutResult(agent_id="apollo", agent_name="Apollo",
                             response_text=long_text),
            ]
            await engine.send_to_single(
                member=target,
                message_parts=[Part(root=TextPart(text="Hello"))],
                channel=channel,
                context_id="ctx",
                previous_responses=prev,
            )

            call_args = mock_instance.send_message.call_args
            request = call_args[0][0]
            sent_text = request.params.message.parts[0].root.text

            # Should contain at most 500 x's from the truncated response
            apollo_line = [l for l in sent_text.split("\n") if "Apollo:" in l][0]
            # "- Apollo: " prefix + 500 x's
            assert len(apollo_line) <= len("- Apollo: ") + 500

    async def test_context_prefix_override_used_in_send_to_agent(self, channel):
        """_send_to_agent should use context_prefix_override when provided."""
        engine = FanOutEngine()

        with patch("src.hub.fanout.A2AClient") as MockClient:
            mock_instance = MagicMock()
            mock_response = MagicMock()
            mock_result = MagicMock()
            mock_result.result = MagicMock(spec=[])
            mock_result.result.parts = []
            mock_response.root = mock_result
            mock_instance.send_message = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            target = channel.members["pixel"]
            custom_prefix = "[Custom context prefix]\n"
            await engine._send_to_agent(
                target, message_parts=[Part(root=TextPart(text="Hello"))],
                channel=channel, context_id="ctx",
                context_prefix_override=custom_prefix,
            )

            call_args = mock_instance.send_message.call_args
            request = call_args[0][0]
            sent_text = request.params.message.parts[0].root.text

            assert sent_text.startswith("[Custom context prefix]")
            assert sent_text.endswith("Hello")


class TestFanOutCircuitBreaker:
    """Tests for circuit breaker integration in FanOutEngine."""

    async def test_fan_out_skips_circuit_broken_agent(self):
        """Agents with open circuit breaker should be skipped with error."""
        ch = Channel(channel_id="dev", name="dev-team")
        ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))
        ch.add_member(ChannelMember(agent_id="pixel", name="Pixel", url="http://localhost:9002"))

        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure("rex")  # Open circuit for rex

        engine = FanOutEngine(circuit_breaker=cb)
        engine._send_to_agent = AsyncMock(
            return_value=FanOutResult(agent_id="pixel", agent_name="Pixel", response_text="OK")
        )

        results = await engine.fan_out(ch, message_parts=[], sender_id=None, context_id="ctx")

        # rex skipped (circuit open), pixel sent normally
        assert len(results) == 2
        rex_result = next(r for r in results if r.agent_id == "rex")
        pixel_result = next(r for r in results if r.agent_id == "pixel")
        assert rex_result.error == "circuit_breaker_open"
        assert pixel_result.response_text == "OK"

        # _send_to_agent should only have been called for pixel
        engine._send_to_agent.assert_called_once()

    async def test_fan_out_records_ack_on_success(self):
        """Successful agent response should call record_success on circuit breaker."""
        ch = Channel(channel_id="dev", name="dev-team")
        ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))

        cb = CircuitBreaker()
        engine = FanOutEngine(circuit_breaker=cb)
        engine._send_to_agent = AsyncMock(
            return_value=FanOutResult(agent_id="rex", agent_name="Rex", response_text="OK")
        )

        await engine.fan_out(ch, message_parts=[], sender_id=None, context_id="ctx")

        # Agent should be in closed state (success recorded)
        assert cb.get_state("rex") == "closed"

    async def test_fan_out_records_nack_on_error(self):
        """Failed agent response should call record_failure on circuit breaker."""
        ch = Channel(channel_id="dev", name="dev-team")
        ch.add_member(ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001"))

        cb = CircuitBreaker(failure_threshold=3)
        engine = FanOutEngine(circuit_breaker=cb)
        engine._send_to_agent = AsyncMock(
            return_value=FanOutResult(agent_id="rex", agent_name="Rex", error="Connection refused")
        )

        await engine.fan_out(ch, message_parts=[], sender_id=None, context_id="ctx")

        # After one failure, should still be closed but have 1 failure recorded
        state = cb._agents.get("rex")
        assert state is not None
        assert state.failures == 1
        assert state.state == "closed"

    async def test_send_to_single_skips_circuit_broken_agent(self):
        """send_to_single should also respect circuit breaker."""
        ch = Channel(channel_id="dev", name="dev-team")
        member = ChannelMember(agent_id="rex", name="Rex", url="http://localhost:9001")
        ch.add_member(member)

        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure("rex")  # Open circuit

        engine = FanOutEngine(circuit_breaker=cb)
        engine._send_to_agent = AsyncMock()

        result = await engine.send_to_single(
            member=member, message_parts=[], channel=ch, context_id="ctx",
        )

        assert result.error == "circuit_breaker_open"
        engine._send_to_agent.assert_not_called()
