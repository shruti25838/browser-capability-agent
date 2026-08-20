"""Tests for agent/audit_log.py -- hash-chained tamper evidence for the
append-only intervention_log.jsonl. Pure file I/O + hashing, no browser,
no network.
"""

from __future__ import annotations

import json

from agent.audit_log import GENESIS_HASH, append_entry, verify_chain


def test_first_entry_chains_to_the_genesis_hash(tmp_path):
    log_path = str(tmp_path / "log.jsonl")

    append_entry(log_path, {"event": "first"})

    lines = (tmp_path / "log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[0])
    assert entry["prev_hash"] == GENESIS_HASH


def test_valid_chain_of_multiple_entries_verifies_successfully(tmp_path):
    log_path = str(tmp_path / "log.jsonl")
    for i in range(5):
        append_entry(log_path, {"event": f"entry-{i}"})

    result = verify_chain(log_path)

    assert result.valid is True
    assert result.entry_count == 5
    assert result.broken_at_index is None


def test_missing_log_file_verifies_as_valid_with_zero_entries(tmp_path):
    log_path = str(tmp_path / "does_not_exist.jsonl")

    result = verify_chain(log_path)

    assert result.valid is True
    assert result.entry_count == 0


def test_each_entry_prev_hash_is_the_real_sha256_of_the_line_before_it(tmp_path):
    import hashlib

    log_path = str(tmp_path / "log.jsonl")
    append_entry(log_path, {"event": "first"})
    append_entry(log_path, {"event": "second"})

    lines = (tmp_path / "log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    second_entry = json.loads(lines[1])
    assert second_entry["prev_hash"] == hashlib.sha256(lines[0].encode("utf-8")).hexdigest()


def test_corrupting_a_line_by_hand_is_detected_at_the_right_index(tmp_path):
    log_path = tmp_path / "log.jsonl"
    for i in range(5):
        append_entry(str(log_path), {"event": f"entry-{i}"})

    lines = log_path.read_text(encoding="utf-8").splitlines()
    corrupted = json.loads(lines[2])
    corrupted["event"] = "tampered"
    lines[2] = json.dumps(corrupted)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_chain(str(log_path))

    assert result.valid is False
    # Corrupting entry 2 changes its hash, so entry 3 is the first whose stored
    # prev_hash no longer matches the (now different) actual hash of entry 2.
    assert result.broken_at_index == 3


def test_deleting_a_line_by_hand_is_detected(tmp_path):
    log_path = tmp_path / "log.jsonl"
    for i in range(4):
        append_entry(str(log_path), {"event": f"entry-{i}"})

    lines = log_path.read_text(encoding="utf-8").splitlines()
    del lines[1]  # remove the second entry entirely
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_chain(str(log_path))

    assert result.valid is False
    assert result.broken_at_index == 1


def test_invalid_json_line_is_reported_as_a_break(tmp_path):
    log_path = tmp_path / "log.jsonl"
    append_entry(str(log_path), {"event": "first"})
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("this is not json\n")

    result = verify_chain(str(log_path))

    assert result.valid is False
    assert result.broken_at_index == 1
    assert "not valid JSON" in result.reason


def test_appending_after_a_valid_chain_extends_it_correctly(tmp_path):
    log_path = str(tmp_path / "log.jsonl")
    append_entry(log_path, {"event": "first"})
    append_entry(log_path, {"event": "second"})
    append_entry(log_path, {"event": "third"})

    result = verify_chain(log_path)

    assert result.valid is True
    assert result.entry_count == 3
