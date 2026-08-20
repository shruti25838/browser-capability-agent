"""Tamper-evident hash chaining for append-only JSONL logs (intervention_log.jsonl).

Each entry stores prev_hash: the SHA-256 of the previous entry's exact line
content, so a hand-edit or deletion of any historical line is detectable by
re-walking the file and recomputing the chain, rather than trusting the
filesystem alone. The first entry in a fresh log chains to a fixed genesis
value rather than a null/empty prev_hash, so "no prior entry" is itself an
explicit, checkable value.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

GENESIS_HASH = "0" * 64


def _hash_line(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _read_lines(log_path: str) -> list[str]:
    try:
        with open(log_path, encoding="utf-8") as f:
            return [line for line in f.read().splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def append_entry(log_path: str, record: dict) -> None:
    """Append `record` as one JSON line, with a prev_hash linking it to the
    previous entry (or GENESIS_HASH if the log is empty or doesn't exist yet).
    """
    prev_hash = _last_line_hash(log_path)
    line = json.dumps({**record, "prev_hash": prev_hash})
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _last_line_hash(log_path: str) -> str:
    lines = _read_lines(log_path)
    if not lines:
        return GENESIS_HASH
    return _hash_line(lines[-1])


@dataclass
class ChainVerification:
    valid: bool
    entry_count: int
    broken_at_index: Optional[int] = None
    reason: Optional[str] = None


def verify_chain(log_path: str) -> ChainVerification:
    """Walk the whole log and confirm each entry's prev_hash matches the actual
    hash of the line immediately before it. Returns where the chain breaks
    (by entry index) if it's been tampered with -- a line deleted, edited by
    hand, or replaced with something that isn't valid JSON.
    """
    lines = _read_lines(log_path)
    expected_prev = GENESIS_HASH

    for i, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return ChainVerification(
                valid=False, entry_count=len(lines), broken_at_index=i, reason=f"entry {i} is not valid JSON"
            )
        actual_prev = entry.get("prev_hash")
        if actual_prev != expected_prev:
            return ChainVerification(
                valid=False,
                entry_count=len(lines),
                broken_at_index=i,
                reason=f"entry {i} has prev_hash {actual_prev!r}, expected {expected_prev!r} "
                f"(the entry before it was edited, deleted, or reordered)",
            )
        expected_prev = _hash_line(line)

    return ChainVerification(valid=True, entry_count=len(lines))
