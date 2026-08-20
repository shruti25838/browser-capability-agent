"""Tests for discovery's step/wall-clock budget enforcement (agent/discovery_agent.py).

No live browser, no LLM: DiscoveryAgent is constructed with a dummy client
object (so the real Gemini client is never built and no GEMINI_API_KEY is
needed) purely to exercise _check_time_budget, which is pure arithmetic
against a monotonic clock and never touches Playwright or the network.
"""

from __future__ import annotations

import time

import pytest

from agent.discovery_agent import BudgetExceededError, DiscoveryAgent, DiscoveryRun, DiscoveryStatus
from agent.guardrails import GuardrailChecker


def _agent(max_seconds=None) -> DiscoveryAgent:
    guardrail = GuardrailChecker(allowed_domains=["example.com"], allowed_actions=["find_row", "click", "type", "extract", "finish"])
    return DiscoveryAgent(
        goal="test goal",
        target_url="https://example.com",
        guardrail=guardrail,
        client=object(),  # never touched by _check_time_budget -- avoids needing a real API key
        max_turns=20,
        max_seconds=max_seconds,
    )


def test_max_turns_defaults_to_25_when_not_overridden():
    guardrail = GuardrailChecker(allowed_domains=["example.com"], allowed_actions=["find_row"])
    agent = DiscoveryAgent(goal="g", target_url="https://example.com", guardrail=guardrail, client=object())
    assert agent.max_turns == 25


def test_max_seconds_defaults_to_300_when_not_overridden():
    guardrail = GuardrailChecker(allowed_domains=["example.com"], allowed_actions=["find_row"])
    agent = DiscoveryAgent(goal="g", target_url="https://example.com", guardrail=guardrail, client=object())
    assert agent.max_seconds == 300.0


def test_check_time_budget_does_not_raise_when_max_seconds_is_none():
    agent = _agent(max_seconds=None)

    # start_time far in the past -- would trip any real budget, but None means disabled.
    agent._check_time_budget(start_time=time.monotonic() - 10_000)


def test_check_time_budget_does_not_raise_when_within_budget():
    agent = _agent(max_seconds=60.0)

    agent._check_time_budget(start_time=time.monotonic())  # ~0s elapsed


def test_check_time_budget_raises_when_budget_exceeded():
    agent = _agent(max_seconds=0.01)

    with pytest.raises(BudgetExceededError, match="max_seconds"):
        agent._check_time_budget(start_time=time.monotonic() - 10)


def test_budget_exceeded_error_is_a_discovery_error_subclass():
    # So existing `except DiscoveryError` call sites still catch it if a more specific
    # handler isn't present -- but run() catches BudgetExceededError first (see below).
    from agent.discovery_agent import DiscoveryError

    assert issubclass(BudgetExceededError, DiscoveryError)


# -- DiscoveryRun.status derivation ------------------------------------------


def test_discovery_run_status_defaults_to_success_when_success_true():
    run = DiscoveryRun(goal="g", target_url="https://example.com", success=True, reason="done")
    assert run.status == DiscoveryStatus.SUCCESS


def test_discovery_run_status_defaults_to_failed_when_success_false():
    run = DiscoveryRun(goal="g", target_url="https://example.com", success=False, reason="broke")
    assert run.status == DiscoveryStatus.FAILED


def test_discovery_run_status_budget_exceeded_must_be_set_explicitly():
    run = DiscoveryRun(
        goal="g",
        target_url="https://example.com",
        success=False,
        reason="Exceeded max_turns (20) without the agent calling finish",
        status=DiscoveryStatus.BUDGET_EXCEEDED,
    )
    assert run.status == DiscoveryStatus.BUDGET_EXCEEDED
    assert run.success is False
