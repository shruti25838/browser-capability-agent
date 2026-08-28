"""Tests for catalog/run_log.py -- pure record shaping + file I/O, no browser."""

from __future__ import annotations

import json

from catalog.run_log import append_run, build_run_record, load_runs


def _result(
    status: str = "success",
    outcome_name=None,
    recovered_via_retry: bool = False,
    steps=None,
    screenshot=None,
) -> dict:
    result = {"status": status, "outcome_name": outcome_name, "recovered_via_retry": recovered_via_retry}
    if steps is not None:
        result["steps"] = steps
    if screenshot is not None:
        result["screenshot"] = screenshot
    return result


def test_build_run_record_sums_retry_count_across_steps():
    result = _result(
        steps=[
            {"step": 0, "recovered_via_retry": False, "retry_count": 0},
            {"step": 1, "recovered_via_retry": True, "retry_count": 2},
        ]
    )

    record = build_run_record("some_capability", {"person_name": "Doe Jason"}, result)

    assert record["capability"] == "some_capability"
    assert record["status"] == "success"
    assert record["retry_count"] == 2
    assert "timestamp" in record


def test_build_run_record_redacts_params_but_not_structural_fields():
    result = _result(status="hard_failure")

    record = build_run_record("cap", {"password": "wrongpassword", "ssn": "123-45-6789"}, result)

    assert record["params"]["ssn"] == "[REDACTED]"
    assert record["params"]["password"] == "[REDACTED]"


def test_build_run_record_redacts_a_plaintext_password_field():
    # Regression: "password" the literal string ("password", "teller1", etc.) matches
    # none of redact_text's value-shape heuristics, so it must be caught by field name
    # instead -- this is the exact bug reported from the live dashboard.
    result = _result(status="hard_failure")

    record = build_run_record(
        "sign_on_to_meridian_core_as_operator_tel_1787804230",
        {"member_number": "100234", "operator_id": "teller1", "password": "password"},
        result,
    )

    assert record["params"]["password"] == "[REDACTED]"
    assert record["params"]["operator_id"] == "teller1"
    assert record["params"]["member_number"] == "100234"


def test_build_run_record_picks_up_outcome_name_and_evidence():
    result = _result(status="business_outcome", outcome_name="not_found", screenshot="evidence/shot.png")

    record = build_run_record("cap", {}, result)

    assert record["outcome_name"] == "not_found"
    assert record["evidence"] == "evidence/shot.png"


def test_build_run_record_defaults_missing_fields_gracefully():
    record = build_run_record("cap", {}, {"status": "success"})

    assert record["outcome_name"] is None
    assert record["recovered_via_retry"] is False
    assert record["retry_count"] == 0
    assert record["evidence"] is None


def test_append_run_then_load_runs_round_trips(tmp_path):
    log_path = str(tmp_path / "run_log.jsonl")

    append_run(log_path, "cap_a", {"x": "1"}, _result(status="success"))
    append_run(log_path, "cap_b", {"y": "2"}, _result(status="hard_failure"))

    runs = load_runs(log_path)

    assert [r["capability"] for r in runs] == ["cap_a", "cap_b"]
    assert runs[0]["status"] == "success"
    assert runs[1]["status"] == "hard_failure"


def test_append_run_creates_parent_directory(tmp_path):
    log_path = str(tmp_path / "nested" / "dir" / "run_log.jsonl")

    append_run(log_path, "cap", {}, _result())

    assert load_runs(log_path) == load_runs(log_path)  # doesn't raise
    assert len(load_runs(log_path)) == 1


def test_load_runs_missing_file_returns_empty_list(tmp_path):
    assert load_runs(str(tmp_path / "does_not_exist.jsonl")) == []


def test_load_runs_skips_a_malformed_line_without_raising(tmp_path):
    log_path = tmp_path / "run_log.jsonl"
    log_path.write_text(
        json.dumps({"capability": "good", "status": "success"}) + "\n" + "{not valid json\n",
        encoding="utf-8",
    )

    runs = load_runs(str(log_path))

    assert len(runs) == 1
    assert runs[0]["capability"] == "good"
