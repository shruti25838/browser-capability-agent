"""Pure aggregation of run-log records into per-capability eval stats.

No I/O here -- takes whatever catalog.run_log.load_runs() already returned
and reduces it. Kept separate from run_log.py so the aggregation logic is
testable with plain lists of dicts, no file/JSONL concerns at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CapabilityStats:
    capability: str
    total: int = 0
    success: int = 0
    business_outcome: int = 0
    hard_failure: int = 0
    recovered_via_retry: int = 0
    retry_counts: list[int] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 0.0

    @property
    def business_outcome_rate(self) -> float:
        return self.business_outcome / self.total if self.total else 0.0

    @property
    def hard_failure_rate(self) -> float:
        return self.hard_failure / self.total if self.total else 0.0

    @property
    def avg_retry_count(self) -> float:
        return sum(self.retry_counts) / len(self.retry_counts) if self.retry_counts else 0.0

    @property
    def median_retry_count(self) -> float:
        if not self.retry_counts:
            return 0.0
        ordered = sorted(self.retry_counts)
        n = len(ordered)
        mid = n // 2
        if n % 2:
            return float(ordered[mid])
        return (ordered[mid - 1] + ordered[mid]) / 2.0


def aggregate_stats(runs: list[dict[str, Any]]) -> dict[str, CapabilityStats]:
    """Group run-log records by capability and reduce each group to CapabilityStats.

    Unknown/unexpected status values are counted in `total` but not in any
    of the three named buckets -- silently misclassifying a status this
    codebase doesn't produce would be worse than an undercount that's
    visible in the numbers (total != success+business_outcome+hard_failure).
    """
    stats: dict[str, CapabilityStats] = {}
    for run in runs:
        name = run.get("capability") or "unknown"
        s = stats.setdefault(name, CapabilityStats(capability=name))
        s.total += 1
        status = run.get("status")
        if status == "success":
            s.success += 1
        elif status == "business_outcome":
            s.business_outcome += 1
        elif status == "hard_failure":
            s.hard_failure += 1
        if run.get("recovered_via_retry"):
            s.recovered_via_retry += 1
        s.retry_counts.append(run.get("retry_count") or 0)
    return stats
