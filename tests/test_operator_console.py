"""Tests for operator_console/console.py -- the web operator console that
replaces EscalationHandler's terminal input() with an HTTP-based decision
channel, without moving browser control anywhere.

No live browser and no LLM anywhere in this file. Two tiers:

  * Flask test-client tests (no real socket) for the server's own routes --
    publish, poll, decide, dashboard rendering, error cases. This is what
    "the server correctly serializes/exposes an intervention request" means
    here: hit the real routes, check the real JSON/HTML they produce.
  * Real background-thread server tests (a real localhost socket, bound to
    an OS-assigned free port -- no external network) for the actual
    publish -> human decides -> handler resumes/aborts round trip, and for
    the can't-reach-server degrade-to-hard_failure case. Page/browser is a
    StubPage, exactly like tests/test_escalation_handler.py.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from agent.artifact import Artifact, Condition, ConditionKind, Metadata, Parameter, Step, StepType
from replay.replay_engine import ConditionCheck, EscalationContext
from operator_console.console import (
    OperatorConsoleClient,
    OperatorConsoleServer,
    WebConsoleEscalationHandler,
    create_app,
)


class StubPage:
    """Same minimal double as tests/test_escalation_handler.py -- just enough
    surface for EscalationHandler._take_screenshot()."""

    def __init__(self):
        self.screenshot_calls = 0

    def screenshot(self, path: str) -> None:
        self.screenshot_calls += 1
        with open(path, "wb") as f:
            f.write(b"fake-png-bytes")


def _make_context(recheck=None, observed: str = "count=0 visible=False", goal: str = "test goal") -> EscalationContext:
    condition = Condition(kind=ConditionKind.ELEMENT_VISIBLE, description="link visible")
    step = Step(order=1, type=StepType.CLICK, description="click a link", precondition=condition, postcondition=condition)
    artifact = Artifact(
        metadata=Metadata(goal=goal, target_url="https://example.com", created_at=datetime.now(timezone.utc)),
        parameters=[Parameter(name="p", type="string", description="d", example_value="v")],
        outputs=[],
        steps=[step],
        success_condition=condition,
    )
    return EscalationContext(
        artifact=artifact,
        step=step,
        step_index=1,
        condition_type="precondition",
        condition=condition,
        expected="link visible",
        observed=observed,
        error="condition not met after retries",
        page=StubPage(),
        outputs={},
        recheck=recheck or (lambda: ConditionCheck(passed=False, observed="still not visible")),
    )


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- server route tests (Flask test client -- no real socket) ---------------


def _sample_record() -> dict:
    return {
        "goal": "attempt login with wrong password",
        "target_url": "https://the-internet.herokuapp.com/login",
        "step": 2,
        "step_description": "click Login button",
        "condition_type": "postcondition",
        "expected": "flash message displayed",
        "observed": "url='.../secure'",
        "error": "condition not met after retries",
        "screenshot": "evidence/escalation_step2_123.png",
    }


def test_publish_correctly_serializes_and_exposes_the_intervention():
    app = create_app(evidence_dir="evidence")
    client = app.test_client()

    resp = client.post("/api/interventions", json=_sample_record())
    assert resp.status_code == 201
    intervention_id = resp.get_json()["id"]
    assert intervention_id

    current = client.get("/api/interventions/current").get_json()
    pending = current["pending"]
    assert pending["id"] == intervention_id
    assert pending["decision"] is None
    assert pending["goal"] == "attempt login with wrong password"
    assert pending["step_description"] == "click Login button"
    assert pending["observed"] == "url='.../secure'"


def test_no_pending_intervention_reports_null_not_an_error():
    app = create_app(evidence_dir="evidence")
    client = app.test_client()

    current = client.get("/api/interventions/current").get_json()

    assert current == {"pending": None}


def test_dashboard_renders_pending_intervention_details_and_both_buttons():
    app = create_app(evidence_dir="evidence")
    client = app.test_client()
    client.post("/api/interventions", json=_sample_record())

    page = client.get("/").get_data(as_text=True)

    assert "attempt login with wrong password" in page
    assert "click Login button" in page
    assert "/secure" in page
    assert "Resume" in page
    assert "Abort" in page


def test_dashboard_shows_waiting_state_when_nothing_is_pending():
    app = create_app(evidence_dir="evidence")
    client = app.test_client()

    page = client.get("/").get_data(as_text=True)

    assert "No intervention is currently pending" in page


def test_posted_json_decision_is_visible_via_the_poll_endpoint():
    app = create_app(evidence_dir="evidence")
    client = app.test_client()
    intervention_id = client.post("/api/interventions", json=_sample_record()).get_json()["id"]

    before = client.get(f"/api/interventions/{intervention_id}/decision").get_json()
    assert before["decision"] is None

    resp = client.post(f"/interventions/{intervention_id}/decision", json={"decision": "resume"})
    assert resp.status_code == 200
    assert resp.get_json()["decision"] == "resume"

    after = client.get(f"/api/interventions/{intervention_id}/decision").get_json()
    assert after["decision"] == "resume"
    # A decided intervention is no longer the "current" (pending) one.
    assert client.get("/api/interventions/current").get_json()["pending"] is None


def test_decision_via_html_form_post_is_recorded_and_redirects_to_dashboard():
    # Simulates a human clicking the "Abort" button in an actual browser tab.
    app = create_app(evidence_dir="evidence")
    client = app.test_client()
    intervention_id = client.post("/api/interventions", json=_sample_record()).get_json()["id"]

    resp = client.post(f"/interventions/{intervention_id}/decision", data={"decision": "abort"})

    assert resp.status_code in (302, 303)
    assert client.get(f"/api/interventions/{intervention_id}/decision").get_json()["decision"] == "abort"


def test_invalid_decision_value_is_rejected():
    app = create_app(evidence_dir="evidence")
    client = app.test_client()
    intervention_id = client.post("/api/interventions", json=_sample_record()).get_json()["id"]

    resp = client.post(f"/interventions/{intervention_id}/decision", json={"decision": "maybe"})

    assert resp.status_code == 400
    assert client.get(f"/api/interventions/{intervention_id}/decision").get_json()["decision"] is None


def test_decision_on_unknown_intervention_id_is_404():
    app = create_app(evidence_dir="evidence")
    client = app.test_client()

    resp = client.post("/interventions/does-not-exist/decision", json={"decision": "resume"})

    assert resp.status_code == 404


def test_screenshot_route_serves_the_file_the_intervention_points_at(tmp_path):
    shot = tmp_path / "escalation_step1_999.png"
    shot.write_bytes(b"fake-png-bytes")
    app = create_app(evidence_dir=str(tmp_path))
    client = app.test_client()
    record = _sample_record()
    record["screenshot"] = str(shot)
    intervention_id = client.post("/api/interventions", json=record).get_json()["id"]

    resp = client.get(f"/interventions/{intervention_id}/screenshot")

    assert resp.status_code == 200
    assert resp.data == b"fake-png-bytes"


def test_screenshot_route_refuses_a_path_outside_evidence_dir(tmp_path):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    secret = outside_dir / "secret.png"
    secret.write_bytes(b"not evidence")
    app = create_app(evidence_dir=str(evidence_dir))
    client = app.test_client()
    record = _sample_record()
    record["screenshot"] = str(secret)
    intervention_id = client.post("/api/interventions", json=record).get_json()["id"]

    resp = client.get(f"/interventions/{intervention_id}/screenshot")

    assert resp.status_code == 403


# -- real background-thread server: client publish/poll round trip ----------


def test_client_publish_and_wait_for_decision_round_trip_over_real_http():
    server = OperatorConsoleServer(evidence_dir="evidence", port=0)
    server.start()
    try:
        client = OperatorConsoleClient(server.base_url, request_timeout=2.0)
        intervention_id = client.publish(_sample_record())

        def decide_after_a_beat():
            time.sleep(0.15)
            resp = requests.post(
                f"{server.base_url}/interventions/{intervention_id}/decision",
                json={"decision": "resume"},
                timeout=2.0,
            )
            assert resp.status_code == 200  # JSON post -> JSON ack, no redirect

        t = threading.Thread(target=decide_after_a_beat)
        t.start()
        decision = client.wait_for_decision(intervention_id, poll_interval=0.05, timeout=5.0)
        t.join(timeout=5)

        assert decision == "resume"
    finally:
        server.shutdown()


def test_wait_for_decision_times_out_if_nobody_decides():
    server = OperatorConsoleServer(evidence_dir="evidence", port=0)
    server.start()
    try:
        client = OperatorConsoleClient(server.base_url, request_timeout=2.0)
        intervention_id = client.publish(_sample_record())

        try:
            client.wait_for_decision(intervention_id, poll_interval=0.05, timeout=0.3)
            assert False, "expected OperatorConsoleUnavailable on timeout"
        except Exception as e:
            assert "no decision received" in str(e)
    finally:
        server.shutdown()


# -- WebConsoleEscalationHandler end-to-end (mocked Page, real local server) -


def _find_pending_intervention_id(base_url: str, deadline_s: float = 5.0) -> str:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        current = requests.get(f"{base_url}/api/interventions/current", timeout=2.0).json()
        if current["pending"]:
            return current["pending"]["id"]
        time.sleep(0.02)
    raise AssertionError("no intervention was published to the console in time")


def test_web_console_handler_resume_end_to_end_mirrors_terminal_handler_logging(tmp_path):
    server = OperatorConsoleServer(evidence_dir=str(tmp_path), port=0)
    server.start()
    try:
        handler = WebConsoleEscalationHandler(
            console_url=server.base_url, evidence_dir=str(tmp_path), poll_interval=0.05
        )
        context = _make_context(recheck=lambda: ConditionCheck(passed=True, observed="now visible"))

        outcome = {}

        def run_handler():
            outcome["resolution"] = handler.handle(context)

        t = threading.Thread(target=run_handler)
        t.start()

        intervention_id = _find_pending_intervention_id(server.base_url)
        # Simulate the human clicking "Resume" in the actual browser tab. allow_redirects=False
        # so we can see the raw 302-back-to-dashboard a real browser would follow, rather than
        # requests silently following it for us.
        resp = requests.post(
            f"{server.base_url}/interventions/{intervention_id}/decision",
            data={"decision": "resume"},
            timeout=2.0,
            allow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        t.join(timeout=5)
        resolution = outcome["resolution"]

        assert resolution.resumed is True
        assert context.page.screenshot_calls == 1

        lines = (tmp_path / "intervention_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
        human_actions = [json.loads(line)["human_action"] for line in lines]
        # Identical shape to the terminal handler's successful-resume log
        # (tests/test_escalation_handler.py::test_successful_resume_flow_still_works...).
        assert human_actions == ["escalation_triggered", "attempted_resume"]
        assert all(json.loads(line)["owner"] == "human" for line in lines)
    finally:
        server.shutdown()


def test_web_console_handler_abort_end_to_end_mirrors_terminal_handler_logging(tmp_path):
    server = OperatorConsoleServer(evidence_dir=str(tmp_path), port=0)
    server.start()
    try:
        handler = WebConsoleEscalationHandler(
            console_url=server.base_url, evidence_dir=str(tmp_path), poll_interval=0.05
        )
        context = _make_context()

        outcome = {}

        def run_handler():
            outcome["resolution"] = handler.handle(context)

        t = threading.Thread(target=run_handler)
        t.start()

        intervention_id = _find_pending_intervention_id(server.base_url)
        requests.post(
            f"{server.base_url}/interventions/{intervention_id}/decision", data={"decision": "abort"}, timeout=2.0
        )

        t.join(timeout=5)
        resolution = outcome["resolution"]

        assert resolution.resumed is False
        assert resolution.note == "human aborted"

        lines = (tmp_path / "intervention_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
        human_actions = [json.loads(line)["human_action"] for line in lines]
        assert human_actions == ["escalation_triggered", "aborted"]
    finally:
        server.shutdown()


# -- can't-reach-server degrades to hard_failure, not a hang or a crash -----


def test_web_console_handler_falls_back_gracefully_when_console_is_unreachable(tmp_path):
    unreachable_port = _find_free_port()  # bound, released, guaranteed nothing listens there
    handler = WebConsoleEscalationHandler(
        console_url=f"http://127.0.0.1:{unreachable_port}",
        evidence_dir=str(tmp_path),
        client=OperatorConsoleClient(f"http://127.0.0.1:{unreachable_port}", request_timeout=1.0),
    )
    context = _make_context()

    resolution = handler.handle(context)

    assert resolution.resumed is False
    assert "operator console" in resolution.note.lower()

    lines = (tmp_path / "intervention_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    human_actions = [json.loads(line)["human_action"] for line in lines]
    # Same two-entry shape as the terminal EOFError path
    # (test_escalation_handler.py::test_screenshot_always_has_a_matching_log_entry...),
    # just with a channel-specific human_action instead of "no_interactive_terminal".
    assert human_actions == ["escalation_triggered", "operator_console_unreachable"]
    assert context.page.screenshot_calls == 1


def test_web_console_handler_unreachable_console_never_hangs(tmp_path):
    # The regression this guards: a network call with no timeout could block forever
    # instead of degrading like a terminal EOFError does. Bound how long this test itself
    # is allowed to take, in addition to asserting the graceful outcome.
    unreachable_port = _find_free_port()
    handler = WebConsoleEscalationHandler(
        console_url=f"http://127.0.0.1:{unreachable_port}",
        evidence_dir=str(tmp_path),
        client=OperatorConsoleClient(f"http://127.0.0.1:{unreachable_port}", request_timeout=1.0),
    )
    context = _make_context()

    start = time.monotonic()
    resolution = handler.handle(context)
    elapsed = time.monotonic() - start

    assert resolution.resumed is False
    assert elapsed < 10.0
