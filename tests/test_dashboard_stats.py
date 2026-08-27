"""Tests for dashboard/stats.py -- pure aggregation, no I/O, no browser."""

from __future__ import annotations

from dashboard.stats import aggregate_stats


def _run(capability="cap_a", status="success", recovered=False, retry_count=0, outcome_name=None):
    return {
        "capability": capability,
        "status": status,
        "outcome_name": outcome_name,
        "recovered_via_retry": recovered,
        "retry_count": retry_count,
    }


def test_aggregate_stats_on_empty_runs_returns_empty_dict():
    assert aggregate_stats([]) == {}


def test_aggregate_stats_groups_by_capability():
    runs = [_run("cap_a"), _run("cap_b"), _run("cap_a")]

    stats = aggregate_stats(runs)

    assert set(stats.keys()) == {"cap_a", "cap_b"}
    assert stats["cap_a"].total == 2
    assert stats["cap_b"].total == 1


def test_aggregate_stats_counts_each_status_bucket_and_rates():
    runs = [
        _run(status="success"),
        _run(status="success"),
        _run(status="business_outcome", outcome_name="not_found"),
        _run(status="hard_failure"),
    ]

    s = aggregate_stats(runs)["cap_a"]

    assert s.total == 4
    assert s.success == 2
    assert s.business_outcome == 1
    assert s.hard_failure == 1
    assert s.success_rate == 0.5
    assert s.business_outcome_rate == 0.25
    assert s.hard_failure_rate == 0.25


def test_aggregate_stats_avg_and_median_retry_count():
    runs = [_run(retry_count=0), _run(retry_count=2), _run(retry_count=4)]

    s = aggregate_stats(runs)["cap_a"]

    assert s.avg_retry_count == 2.0
    assert s.median_retry_count == 2.0


def test_aggregate_stats_median_retry_count_with_even_number_of_runs():
    runs = [_run(retry_count=0), _run(retry_count=1), _run(retry_count=2), _run(retry_count=3)]

    s = aggregate_stats(runs)["cap_a"]

    assert s.median_retry_count == 1.5


def test_aggregate_stats_counts_recovered_via_retry():
    runs = [_run(recovered=True), _run(recovered=True), _run(recovered=False)]

    s = aggregate_stats(runs)["cap_a"]

    assert s.recovered_via_retry == 2


def test_aggregate_stats_on_capability_with_no_runs_has_zero_rates_not_a_crash():
    # A CapabilityStats that's never populated (total=0) must not divide by zero.
    from dashboard.stats import CapabilityStats

    s = CapabilityStats(capability="unused")

    assert s.success_rate == 0.0
    assert s.avg_retry_count == 0.0
    assert s.median_retry_count == 0.0


def test_aggregate_stats_unknown_status_counts_toward_total_but_no_bucket():
    runs = [_run(status="something_new")]

    s = aggregate_stats(runs)["cap_a"]

    assert s.total == 1
    assert s.success == 0
    assert s.business_outcome == 0
    assert s.hard_failure == 0
