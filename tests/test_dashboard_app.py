"""Tests for dashboard/app.py's Flask route -- rendering only, no browser.

Uses Flask's own test client, never a real HTTP server or Playwright.
"""

from __future__ import annotations

import json

from dashboard.app import create_app


def test_dashboard_renders_empty_state_without_crashing(tmp_path):
    app = create_app(str(tmp_path / "does_not_exist.jsonl"))
    client = app.test_client()

    resp = client.get("/")

    assert resp.status_code == 200
    assert b"No runs yet" in resp.data


def test_dashboard_renders_capability_cards_and_recent_runs_table(tmp_path):
    log_path = tmp_path / "run_log.jsonl"
    records = [
        {
            "timestamp": "2026-08-26T20:00:00+00:00",
            "capability": "sign_on_to_meridian_core",
            "params": {"member_number": "100234"},
            "status": "success",
            "outcome_name": None,
            "recovered_via_retry": False,
            "retry_count": 0,
            "evidence": None,
        },
        {
            "timestamp": "2026-08-26T20:05:00+00:00",
            "capability": "sign_on_to_meridian_core",
            "params": {"member_number": "999999"},
            "status": "business_outcome",
            "outcome_name": "not_found",
            "recovered_via_retry": True,
            "retry_count": 1,
            "evidence": None,
        },
    ]
    log_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    app = create_app(str(log_path))
    client = app.test_client()

    resp = client.get("/")
    body = resp.data.decode("utf-8")

    assert resp.status_code == 200
    assert "sign_on_to_meridian_core" in body
    assert "not_found" in body
    # Both status badges show up somewhere in the recent-runs table.
    assert "status-success" in body
    assert "status-outcome" in body


def test_dashboard_evidence_route_404s_for_missing_file(tmp_path):
    app = create_app(str(tmp_path / "run_log.jsonl"))
    client = app.test_client()

    resp = client.get("/evidence", query_string={"path": str(tmp_path / "nope.png")})

    assert resp.status_code == 404


def test_dashboard_evidence_route_403s_outside_evidence_root(tmp_path):
    outside = tmp_path.parent / "outside_evidence_root.png"
    outside.write_bytes(b"fake png bytes")
    try:
        app = create_app(str(tmp_path / "run_log.jsonl"), evidence_root=str(tmp_path))
        client = app.test_client()

        resp = client.get("/evidence", query_string={"path": str(outside)})

        assert resp.status_code == 403
    finally:
        outside.unlink()
