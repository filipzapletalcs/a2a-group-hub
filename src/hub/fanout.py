# src/hub/fanout.py
"""Fan-out engine — parallel A2A SendMessage to channel peers."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

import httpx
from a2a.client import A2AClient
from a2a.types import (
    Message, MessageSendConfiguration, MessageSendParams, Part,
    Role, SendMessageRequest, Task, TextPart,
)

from src.channels.models import Channel, ChannelMember, MemberRole
from src.hub.callback import CallbackStore

logger = logging.getLogger("a2a-hub.fanout")


# ---------------------------------------------------------------------------
# Async agent set — OpenClaw agents using the callback pattern (port 18800)
# ---------------------------------------------------------------------------

ASYNC_AGENTS: set[str] = {"nexus", "apollo", "rex", "sage", "vigil"}


# ---------------------------------------------------------------------------
# Circuit breaker — per-agent failure tracking with auto-recovery
# ---------------------------------------------------------------------------

@dataclass
class CircuitState:
    """Tracks failure state for a single agent."""
    failures: int = 0
    last_failure: float = 0.0
    state: str = "closed"  # closed, open, half-open


class CircuitBreaker:
    """Per-agent circuit breaker. Opens after consecutive failures, auto-recovers via half-open probe."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 30.0) -> None:
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._agents: dict[str, CircuitState] = {}

    def should_skip(self, agent_id: str) -> bool:
        """Return True if the agent's circuit is open and should be skipped."""
        state = self._agents.get(agent_id)
        if not state or state.state == "closed":
            return False
        if state.state == "open":
            if time.time() - state.last_failure > self._recovery_timeout:
                state.state = "half-open"
                return False  # Allow probe
            return True  # Still open, skip
        return False  # half-open: allow probe

    def record_success(self, agent_id: str) -> None:
        """Record a successful call — resets circuit to closed."""
        state = self._agents.get(agent_id)
        if state:
            state.failures = 0
            state.state = "closed"

    def record_failure(self, agent_id: str) -> None:
        """Record a failed call — may open the circuit."""
        state = self._agents.setdefault(agent_id, CircuitState())
        state.failures += 1
        state.last_failure = time.time()
        if state.failures >= self._threshold:
            state.state = "open"

    def get_state(self, agent_id: str) -> str:
        """Return circuit state for an agent (for metrics/logging)."""
        state = self._agents.get(agent_id)
        return state.state if state else "closed"

    def get_all_states(self) -> dict[str, str]:
        """Return all agent circuit states (for /health or metrics)."""
        return {aid: s.state for aid, s in self._agents.items()}


# ---------------------------------------------------------------------------
# Fan-out results
# ---------------------------------------------------------------------------

@dataclass
class FanOutResult:
    """Result from one agent in a fan-out broadcast."""
    agent_id: str
    agent_name: str
    response_text: str | None = None
    response: Message | Task | None = None
    error: str | None = None


class FanOutEngine:

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        callback_store: CallbackStore | None = None,
    ) -> None:
        self._http_client = http_client or httpx.AsyncClient(timeout=120.0)
        self._owns_client = http_client is None
        self._circuit_breaker = circuit_breaker or CircuitBreaker()
        self._callback_store = callback_store  # None = sync-only mode

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Expose circuit breaker for handler/metrics access."""
        return self._circuit_breaker

    async def close(self) -> None:
        if self._owns_client:
            await self._http_client.aclose()

    @staticmethod
    def _observer_done_callback(task: asyncio.Task) -> None:
        """Log exceptions from observer fire-and-forget tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Observer task {task.get_name()} failed: {exc}")

    async def fan_out(
        self,
        channel: Channel,
        message_parts: list[Part],
        sender_id: str | None,
        context_id: str,
        message_metadata: dict | None = None,
    ) -> list[FanOutResult]:
        """Broadcast to all peers. Observers get fire-and-forget. Returns only member results."""

        sendable = channel.get_sendable_peers(exclude_agent_id=sender_id)
        observers = [o for o in channel.get_observers() if o.agent_id != sender_id]

        logger.info(
            f"Fan-out in #{channel.name}: {len(sendable)} members, {len(observers)} observers"
        )

        # Fire-and-forget to observers (tracked with error logging callback)
        for obs in observers:
            if obs.agent_id in ASYNC_AGENTS:
                task = asyncio.create_task(
                    self._send_to_agent_async(obs, message_parts=message_parts, channel=channel,
                                               context_id=context_id, metadata=message_metadata),
                    name=f"observer-{obs.agent_id}",
                )
            else:
                task = asyncio.create_task(
                    self._send_to_agent(obs, message_parts=message_parts, channel=channel,
                                         context_id=context_id, metadata=message_metadata),
                    name=f"observer-{obs.agent_id}",
                )
            task.add_done_callback(self._observer_done_callback)

        if not sendable:
            return []

        # Sequential send with small delay to avoid overwhelming the LLM proxy
        # (parallel fan-out can trigger rate limits when all agents call the same proxy)
        fan_out_delay = float(os.environ.get("FANOUT_DELAY_SECONDS", "5.0"))
        results: list[FanOutResult] = []
        for i, member in enumerate(sendable):
            # Circuit breaker check — skip agents with open circuits
            if self._circuit_breaker.should_skip(member.agent_id):
                logger.warning("Circuit breaker OPEN for %s -- skipping", member.name)
                results.append(FanOutResult(
                    agent_id=member.agent_id,
                    agent_name=member.name,
                    error="circuit_breaker_open",
                ))
                continue

            # Dispatch: async callback for OpenClaw agents, sync for lightweight
            if member.agent_id in ASYNC_AGENTS:
                result = await self._send_to_agent_async(
                    member, message_parts=message_parts, channel=channel,
                    context_id=context_id, metadata=message_metadata,
                )
            else:
                result = await self._send_to_agent(
                    member, message_parts=message_parts, channel=channel,
                    context_id=context_id, metadata=message_metadata,
                )

            # ACK/NACK tracking
            if result.error:
                self._circuit_breaker.record_failure(member.agent_id)
                logger.info(
                    "NACK from %s: %s (circuit: %s)",
                    member.name, result.error,
                    self._circuit_breaker.get_state(member.agent_id),
                )
            else:
                self._circuit_breaker.record_success(member.agent_id)
                logger.info(
                    "ACK from %s (%d chars)",
                    member.name, len(result.response_text or ""),
                )

            results.append(result)
            if i < len(sendable) - 1 and fan_out_delay > 0:
                # Skip delay for async agents (202 is instant)
                if member.agent_id not in ASYNC_AGENTS:
                    await asyncio.sleep(fan_out_delay)
                else:
                    await asyncio.sleep(0.1)  # minimal delay for async
        return results

    async def send_to_single(
        self,
        member: ChannelMember,
        message_parts: list[Part],
        channel: Channel,
        context_id: str,
        message_metadata: dict | None = None,
        memory_context: str = "",
        previous_responses: list[FanOutResult] | None = None,
    ) -> FanOutResult:
        """Send to a single agent with enriched context including previous responses."""
        # Circuit breaker check
        if self._circuit_breaker.should_skip(member.agent_id):
            logger.warning("Circuit breaker OPEN for %s -- skipping", member.name)
            return FanOutResult(
                agent_id=member.agent_id,
                agent_name=member.name,
                error="circuit_breaker_open",
            )

        # Build channel context prefix
        member_list = []
        for m in channel.members.values():
            role_tag = f" ({m.role.value})" if m.role.value != "member" else ""
            member_list.append(f"{m.name}{role_tag}")
        context_prefix = f"[Kanal: #{channel.name} | Clenove: {', '.join(member_list)}]\n"

        # Add memory context (from Qdrant recall)
        if memory_context:
            context_prefix += f"\n{memory_context}\n"

        # Add accumulated responses from this round
        if previous_responses:
            responses_text = "\n".join(
                f"- {r.agent_name}: {r.response_text[:500]}"
                for r in previous_responses if r.response_text
            )
            if responses_text:
                context_prefix += f"\n[Odpovědi v tomto kole]\n{responses_text}\n"

        # Dispatch: async callback for OpenClaw agents, sync for lightweight
        if member.agent_id in ASYNC_AGENTS:
            result = await self._send_to_agent_async(
                member, message_parts=message_parts, channel=channel,
                context_id=context_id, metadata=message_metadata,
                context_prefix_override=context_prefix,
            )
        else:
            result = await self._send_to_agent(
                member, message_parts=message_parts, channel=channel,
                context_id=context_id, metadata=message_metadata,
                context_prefix_override=context_prefix,
            )

        # ACK/NACK tracking
        if result.error:
            self._circuit_breaker.record_failure(member.agent_id)
        else:
            self._circuit_breaker.record_success(member.agent_id)

        return result

    async def _send_to_agent_async(
        self,
        member: ChannelMember,
        message_parts: list[Part],
        channel: Channel,
        context_id: str,
        metadata: dict | None = None,
        context_prefix_override: str | None = None,
    ) -> FanOutResult:
        """Send to OpenClaw agent via async callback pattern: POST message, get 202, await callback."""
        if self._callback_store is None:
            logger.warning("No callback store -- falling back to sync for %s", member.name)
            return await self._send_to_agent(
                member, message_parts, channel, context_id, metadata, context_prefix_override,
            )

        try:
            # Build context prefix
            if context_prefix_override is not None:
                context_prefix = context_prefix_override
            else:
                member_list = []
                for m in channel.members.values():
                    role_tag = f" ({m.role.value})" if m.role.value != "member" else ""
                    member_list.append(f"{m.name}{role_tag}")
                context_prefix = f"[Kanal: #{channel.name} | Clenove: {', '.join(member_list)}]\n"

            # Build enriched text from message parts
            enriched_text = context_prefix
            for part in message_parts:
                if hasattr(part, 'root') and hasattr(part.root, 'text'):
                    enriched_text += part.root.text
                    break

            # Build A2A-style JSON body (the plugin expects JSONRPC format)
            body = {
                "jsonrpc": "2.0",
                "method": "message/send",
                "id": str(uuid.uuid4()),
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"type": "text", "text": enriched_text}],
                        "message_id": str(uuid.uuid4()),
                        "context_id": context_id,
                        "metadata": {
                            **(metadata or {}),
                            "hub_channel_id": channel.channel_id,
                            "hub_channel_name": channel.name,
                        },
                    },
                    "configuration": {
                        "blocking": False,
                        "accepted_output_modes": ["text"],
                    },
                },
            }

            # POST to plugin — expect 202 with correlation_id
            response = await self._http_client.post(
                member.url, json=body, timeout=10.0,
            )

            if response.status_code not in (200, 202):
                return FanOutResult(
                    agent_id=member.agent_id, agent_name=member.name,
                    error=f"Plugin returned {response.status_code}",
                )

            resp_data = response.json()
            correlation_id = resp_data.get("result", {}).get("correlation_id", "") or resp_data.get("correlation_id", "")
            if not correlation_id:
                return FanOutResult(
                    agent_id=member.agent_id, agent_name=member.name,
                    error="Plugin did not return correlation_id",
                )

            # Register correlation and await callback
            future = self._callback_store.register(
                correlation_id=correlation_id,
                agent_id=member.agent_id,
                channel_id=channel.channel_id,
                context_id=context_id,
            )

            # Wait for callback (slightly more than store timeout for clean expiry)
            result_data = await asyncio.wait_for(future, timeout=130.0)
            text = result_data.get("text", "")
            return FanOutResult(
                agent_id=member.agent_id, agent_name=member.name,
                response_text=text,
            )

        except asyncio.TimeoutError:
            logger.error("Async callback timeout for %s (%s)", member.name, member.url)
            return FanOutResult(agent_id=member.agent_id, agent_name=member.name, error="callback_timeout")
        except Exception as e:
            logger.error("Async fan-out to %s (%s) failed: %s", member.name, member.url, e)
            return FanOutResult(agent_id=member.agent_id, agent_name=member.name, error=str(e))

    async def _send_to_agent(
        self,
        member: ChannelMember,
        message_parts: list[Part],
        channel: Channel,
        context_id: str,
        metadata: dict | None = None,
        context_prefix_override: str | None = None,
    ) -> FanOutResult:
        """Send a message to a single agent via A2A SendMessage."""
        try:
            client = A2AClient(httpx_client=self._http_client, url=member.url)

            # Use override or build default channel context prefix
            if context_prefix_override is not None:
                context_prefix = context_prefix_override
            else:
                member_list = []
                for m in channel.members.values():
                    role_tag = f" ({m.role.value})" if m.role.value != "member" else ""
                    member_list.append(f"{m.name}{role_tag}")
                context_prefix = f"[Kanal: #{channel.name} | Clenove: {', '.join(member_list)}]\n"

            # Prepend channel context to message text
            enriched_parts = []
            for part in message_parts:
                if hasattr(part, 'root') and hasattr(part.root, 'text'):
                    enriched_parts.append(Part(root=TextPart(text=context_prefix + part.root.text)))
                else:
                    enriched_parts.append(part)

            # If no text parts found, add context as new part
            if not enriched_parts:
                enriched_parts = [Part(root=TextPart(text=context_prefix))]

            outbound = Message(
                role=Role.user,
                parts=enriched_parts,
                message_id=str(uuid.uuid4()),
                context_id=context_id,
                metadata={
                    **(metadata or {}),
                    "hub_channel_id": channel.channel_id,
                    "hub_channel_name": channel.name,
                },
            )

            request = SendMessageRequest(
                id=str(uuid.uuid4()),
                params=MessageSendParams(
                    message=outbound,
                    configuration=MessageSendConfiguration(
                        blocking=True,
                        accepted_output_modes=["text"],
                    ),
                ),
            )

            response = await client.send_message(
                request,
                http_kwargs={"headers": member.auth_headers} if member.auth_token else {},
            )

            result = response.root
            if hasattr(result, "result"):
                inner = result.result
                # Extract text from response
                text = self._extract_text(inner)
                return FanOutResult(
                    agent_id=member.agent_id,
                    agent_name=member.name,
                    response_text=text,
                    response=inner,
                )
            elif hasattr(result, "error"):
                return FanOutResult(
                    agent_id=member.agent_id,
                    agent_name=member.name,
                    error=str(result.error),
                )
            return FanOutResult(agent_id=member.agent_id, agent_name=member.name, error="Unknown response format")

        except Exception as e:
            logger.error(f"Fan-out to {member.name} ({member.url}) failed: {e}")
            return FanOutResult(agent_id=member.agent_id, agent_name=member.name, error=str(e))

    @staticmethod
    def _extract_text(response: Message | Task) -> str:
        """Extract text content from an A2A response."""
        if isinstance(response, Message):
            return "".join(
                p.root.text for p in response.parts
                if hasattr(p, "root") and hasattr(p.root, "text")
            )
        if isinstance(response, Task):
            texts = []
            if response.artifacts:
                for art in response.artifacts:
                    for p in art.parts:
                        if hasattr(p, "root") and hasattr(p.root, "text"):
                            texts.append(p.root.text)
            return "\n".join(texts)
        return ""
