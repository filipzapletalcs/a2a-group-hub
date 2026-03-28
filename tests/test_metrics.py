# tests/test_metrics.py
"""Tests for Prometheus-compatible metrics collector."""

import time
import pytest

from src.observability.metrics import MetricsCollector


class TestMetricsCollector:
    def test_initial_state(self):
        m = MetricsCollector()
        assert m.channels_total == 0
        assert m.agents_total == 0
        assert m.messages_total == 0
        assert m.fanout_durations == []
        assert m.agent_errors == {}
        assert m.strategy_usage == {}
        assert m.webhook_success == 0
        assert m.webhook_failure == 0

    def test_record_message_increments(self):
        m = MetricsCollector()
        m.record_message()
        m.record_message()
        m.record_message()
        assert m.messages_total == 3

    def test_record_fanout_duration(self):
        m = MetricsCollector()
        m.record_fanout_duration(0.1)
        m.record_fanout_duration(0.5)
        assert m.fanout_durations == [0.1, 0.5]

    def test_record_agent_error(self):
        m = MetricsCollector()
        m.record_agent_error("rex")
        m.record_agent_error("rex")
        m.record_agent_error("pixel")
        assert m.agent_errors == {"rex": 2, "pixel": 1}

    def test_record_strategy_usage(self):
        m = MetricsCollector()
        m.record_strategy_usage("all")
        m.record_strategy_usage("all")
        m.record_strategy_usage("first")
        assert m.strategy_usage == {"all": 2, "first": 1}

    def test_record_webhook_delivery_success(self):
        m = MetricsCollector()
        m.record_webhook_delivery(success=True)
        m.record_webhook_delivery(success=True)
        assert m.webhook_success == 2
        assert m.webhook_failure == 0

    def test_record_webhook_delivery_failure(self):
        m = MetricsCollector()
        m.record_webhook_delivery(success=False)
        assert m.webhook_success == 0
        assert m.webhook_failure == 1

    def test_update_counts(self):
        m = MetricsCollector()
        m.update_counts(channels=5, agents=12)
        assert m.channels_total == 5
        assert m.agents_total == 12


class TestPercentile:
    def test_empty_list_returns_zero(self):
        m = MetricsCollector()
        assert m._percentile([], 50) == 0.0

    def test_single_value(self):
        m = MetricsCollector()
        assert m._percentile([0.5], 50) == 0.5
        assert m._percentile([0.5], 99) == 0.5

    def test_known_percentiles(self):
        m = MetricsCollector()
        values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        p50 = m._percentile(values, 50)
        p95 = m._percentile(values, 95)
        p99 = m._percentile(values, 99)
        assert p50 == 0.6  # index 5
        assert p95 == 1.0  # index 9
        assert p99 == 1.0  # index 9 (clamped)

    def test_unsorted_input(self):
        m = MetricsCollector()
        values = [0.9, 0.1, 0.5, 0.3, 0.7]
        p50 = m._percentile(values, 50)
        # sorted: [0.1, 0.3, 0.5, 0.7, 0.9], index 2 -> 0.5
        assert p50 == 0.5


class TestPrometheusOutput:
    def test_basic_output_format(self):
        m = MetricsCollector()
        m.update_counts(channels=3, agents=7)
        m.record_message()
        output = m.to_prometheus()
        assert "a2a_hub_channels_total 3" in output
        assert "a2a_hub_agents_total 7" in output
        assert "a2a_hub_messages_total 1" in output

    def test_fanout_quantiles_in_output(self):
        m = MetricsCollector()
        m.record_fanout_duration(0.123)
        output = m.to_prometheus()
        assert 'a2a_hub_fanout_duration_seconds{quantile="0.5"}' in output
        assert 'a2a_hub_fanout_duration_seconds{quantile="0.95"}' in output
        assert 'a2a_hub_fanout_duration_seconds{quantile="0.99"}' in output

    def test_agent_errors_in_output(self):
        m = MetricsCollector()
        m.record_agent_error("rex")
        m.record_agent_error("rex")
        output = m.to_prometheus()
        assert 'a2a_hub_agent_errors_total{agent="rex"} 2' in output

    def test_strategy_usage_in_output(self):
        m = MetricsCollector()
        m.record_strategy_usage("consensus")
        output = m.to_prometheus()
        assert 'a2a_hub_aggregation_strategy_usage{strategy="consensus"} 1' in output

    def test_webhook_deliveries_in_output(self):
        m = MetricsCollector()
        m.record_webhook_delivery(success=True)
        m.record_webhook_delivery(success=False)
        output = m.to_prometheus()
        assert 'a2a_hub_webhook_deliveries_total{status="success"} 1' in output
        assert 'a2a_hub_webhook_deliveries_total{status="failure"} 1' in output

    def test_uptime_in_output(self):
        m = MetricsCollector()
        output = m.to_prometheus()
        assert "a2a_hub_uptime_seconds" in output

    def test_output_ends_with_newline(self):
        m = MetricsCollector()
        output = m.to_prometheus()
        assert output.endswith("\n")

    def test_empty_errors_and_strategies(self):
        """No agent_errors or strategy lines when none recorded."""
        m = MetricsCollector()
        output = m.to_prometheus()
        assert "a2a_hub_agent_errors_total" not in output
        assert "a2a_hub_aggregation_strategy_usage" not in output
