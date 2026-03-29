# tests/test_circuit_breaker.py
"""Tests for CircuitBreaker — per-agent failure tracking with state transitions."""

import time
import pytest

from src.hub.fanout import CircuitBreaker, CircuitState


class TestCircuitBreaker:

    def test_closed_allows_requests(self):
        """New circuit breaker should allow all requests (closed state)."""
        cb = CircuitBreaker()
        assert cb.should_skip("agent-a") is False
        assert cb.get_state("agent-a") == "closed"

    def test_opens_after_threshold_failures(self):
        """Circuit should open after 3 consecutive failures."""
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure("agent-a")
        cb.record_failure("agent-a")
        assert cb.should_skip("agent-a") is False  # Only 2 failures
        cb.record_failure("agent-a")
        assert cb.should_skip("agent-a") is True  # 3 failures = open
        assert cb.get_state("agent-a") == "open"

    def test_half_open_after_timeout(self):
        """After recovery timeout expires, circuit should enter half-open (allow probe)."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)
        # Trip the breaker
        for _ in range(3):
            cb.record_failure("agent-a")
        assert cb.should_skip("agent-a") is True

        # Simulate timeout expiry by backdating last_failure
        state = cb._agents["agent-a"]
        state.last_failure = time.time() - 31.0

        # Should now allow a probe (half-open)
        assert cb.should_skip("agent-a") is False
        assert cb.get_state("agent-a") == "half-open"

    def test_success_resets_to_closed(self):
        """Successful probe during half-open should reset to closed."""
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure("agent-a")
        assert cb.get_state("agent-a") == "open"

        # Simulate half-open
        cb._agents["agent-a"].last_failure = time.time() - 31.0
        cb.should_skip("agent-a")  # Triggers half-open transition
        assert cb.get_state("agent-a") == "half-open"

        # Success resets to closed
        cb.record_success("agent-a")
        assert cb.get_state("agent-a") == "closed"
        assert cb._agents["agent-a"].failures == 0
        assert cb.should_skip("agent-a") is False

    def test_failure_during_half_open_reopens(self):
        """Failure during half-open probe should reopen the circuit."""
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure("agent-a")

        # Enter half-open
        cb._agents["agent-a"].last_failure = time.time() - 31.0
        cb.should_skip("agent-a")  # half-open
        assert cb.get_state("agent-a") == "half-open"

        # Probe fails — should reopen
        cb.record_failure("agent-a")
        assert cb.get_state("agent-a") == "open"
        assert cb.should_skip("agent-a") is True

    def test_independent_per_agent(self):
        """Failures for one agent should not affect another."""
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure("agent-a")
        assert cb.should_skip("agent-a") is True
        assert cb.should_skip("agent-b") is False

    def test_get_all_states(self):
        """get_all_states should return states for all tracked agents."""
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure("agent-a")
        for _ in range(3):
            cb.record_failure("agent-b")

        states = cb.get_all_states()
        assert states["agent-a"] == "closed"  # Only 1 failure, still closed
        assert states["agent-b"] == "open"

    def test_record_success_on_unknown_agent_is_noop(self):
        """Recording success for an unknown agent should not crash."""
        cb = CircuitBreaker()
        cb.record_success("unknown-agent")  # Should not raise
        assert cb.get_state("unknown-agent") == "closed"
