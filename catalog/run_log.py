"""Run-log: minimal, append-only JSONL record of every capability invocation.

One JSON line per run, written as a side effect of catalog/service.py's
invoke path -- never a new source of truth. Every field here is read
straight off the existing ReplayEngine.run() result contract (status,
outcome_name, recovered_via_retry, steps[].retry_count) and the existing
redaction rules (agent/redaction.redact_mapping and redact_sensitive_keys),
not reinvented.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from agent.redaction import redact_mapping, redact_sensitive_keys

DEFAULT_RUN_LOG_PATH = os.path.join("runs", "run_log.jsonl")


def build_run_record(capability: str, params: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Shape one invoke's (params, result) into the run-log's record schema.

    retry_count is summed across result["steps"] (per-step retry info
    ReplayEngine already records) rather than tracked separately -- there is
    exactly one place that counts retries, and this is not it.
    """
    retry_count = sum(step.get("retry_count", 0) for step in result.get("steps", []))
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "capability": capability,
        "params": redact_mapping(redact_sensitive_keys(params)),
        "status": result.get("status"),
        "outcome_name": result.get("outcome_name"),
        "recovered_via_retry": bool(result.get("recovered_via_retry", False)),
        "retry_count": retry_count,
        "evidence": result.get("screenshot"),
    }


def append_run(log_path: str, capability: str, params: dict[str, Any], result: dict[str, Any]) -> None:
    """Append one run record as a JSON line, creating runs/ on first use."""
    record = build_run_record(capability, params, result)
    parent = os.path.dirname(log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def load_runs(log_path: str) -> list[dict[str, Any]]:
    """Read every run record from the log.

    Missing file -> empty list, not an error -- the dashboard must render a
    graceful empty state before any run has ever happened. A malformed line
    is skipped, not fatal, mirroring _load_artifacts' one-bad-file-must-not-
    break-the-whole-view convention in catalog/service.py.
    """
    if not os.path.isfile(log_path):
        return []
    runs: list[dict[str, Any]] = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return runs
