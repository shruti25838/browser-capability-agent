"""Tests for catalog/service.py -- the agent-facing capability catalog.

No live browser, no LLM. Every test points create_app() at an isolated
tmp_path directory of synthetic artifacts (never the real artifacts/), and
the invoke tests replace the replay/browser seam (get_replay_runner) via
FastAPI's dependency_overrides -- exactly the seam catalog/service.py
carves out for this purpose -- so ReplayEngine/Playwright are never
actually exercised here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from agent.artifact import (
    Artifact,
    Condition,
    ConditionKind,
    Metadata,
    OutputField,
    Parameter,
    Step,
    StepType,
)
from catalog.run_log import load_runs
from catalog.service import create_app, get_replay_runner


def _login_artifact() -> Artifact:
    condition = Condition(kind=ConditionKind.ELEMENT_VISIBLE, description="username box visible")
    step = Step(
        order=0,
        type=StepType.TYPE,
        description="type username",
        precondition=condition,
        postcondition=condition,
        input_text="{{username}}",
    )
    return Artifact(
        metadata=Metadata(
            goal="attempt to log in with username tomsmith and password wrongpassword",
            target_url="https://the-internet.herokuapp.com/login",
            created_at=datetime.now(timezone.utc),
        ),
        parameters=[
            Parameter(name="username", type="string", description="Username for login", example_value="tomsmith"),
            Parameter(name="password", type="string", description="Password for login", example_value="wrongpassword"),
        ],
        outputs=[OutputField(name="error_message", type="string", description="Flash error message text")],
        steps=[step],
        success_condition=condition,
    )


def _lookup_artifact() -> Artifact:
    condition = Condition(kind=ConditionKind.ROW_VISIBLE, description="row visible", contains_text="{{person_name}}")
    step = Step(
        order=0,
        type=StepType.FIND_ROW,
        description="find row",
        precondition=condition,
        postcondition=condition,
        contains_text="{{person_name}}",
    )
    return Artifact(
        metadata=Metadata(
            goal="find the row for Jason Doe and extract his Due amount",
            target_url="https://the-internet.herokuapp.com/tables",
            created_at=datetime.now(timezone.utc),
        ),
        parameters=[
            Parameter(name="person_name", type="string", description="Row to select", example_value="Doe Jason")
        ],
        outputs=[OutputField(name="due_amount", type="string", description="Amount due for the row")],
        steps=[step],
        success_condition=condition,
    )


def _write_artifacts(tmp_path) -> dict[str, str]:
    """Writes two synthetic artifacts and returns {capability_name: goal}."""
    login_path = tmp_path / "attempt_to_log_in_with_username_tomsmith_1787678625.json"
    lookup_path = tmp_path / "find_the_row_for_jason_doe_and_extract_h_1787179354.json"
    _login_artifact().to_json_file(str(login_path))
    _lookup_artifact().to_json_file(str(lookup_path))
    return {
        "attempt_to_log_in_with_username_tomsmith_1787678625": _login_artifact().metadata.goal,
        "find_the_row_for_jason_doe_and_extract_h_1787179354": _lookup_artifact().metadata.goal,
    }


# -- GET /capabilities --------------------------------------------------------


def test_list_capabilities_returns_expected_entries_with_correct_schemas(tmp_path):
    expected = _write_artifacts(tmp_path)
    app = create_app(str(tmp_path))
    client = TestClient(app)

    resp = client.get("/capabilities")

    assert resp.status_code == 200
    body = resp.json()
    names = {c["name"] for c in body["capabilities"]}
    assert names == set(expected.keys())

    login = next(c for c in body["capabilities"] if c["name"] == "attempt_to_log_in_with_username_tomsmith_1787678625")
    assert login["description"] == expected["attempt_to_log_in_with_username_tomsmith_1787678625"]
    assert login["target_url"] == "https://the-internet.herokuapp.com/login"
    assert login["parameters"]["type"] == "object"
    assert login["parameters"]["properties"]["username"]["type"] == "string"
    assert login["parameters"]["properties"]["password"]["type"] == "string"
    assert set(login["parameters"]["required"]) == {"username", "password"}
    assert login["outputs"]["properties"]["error_message"]["type"] == "string"


def test_list_capabilities_never_exposes_the_raw_step_list():
    # The whole point of the catalog: an agent can decide what to call from the contract
    # alone, without reading steps/locators/preconditions.
    import json as _json

    app = create_app("/nonexistent-dir-for-this-test")
    client = TestClient(app)
    resp = client.get("/capabilities")
    raw = _json.dumps(resp.json())

    assert "steps" not in raw
    assert "precondition" not in raw
    assert "locator" not in raw


def test_list_capabilities_is_empty_but_not_an_error_when_dir_has_no_artifacts(tmp_path):
    app = create_app(str(tmp_path))
    client = TestClient(app)

    resp = client.get("/capabilities")

    assert resp.status_code == 200
    assert resp.json() == {"capabilities": []}


def test_a_malformed_artifact_file_is_skipped_not_fatal_to_the_catalog(tmp_path):
    (tmp_path / "good_one_123.json").write_text(_login_artifact().model_dump_json(), encoding="utf-8")
    (tmp_path / "corrupt_456.json").write_text("{not valid json", encoding="utf-8")
    app = create_app(str(tmp_path))
    client = TestClient(app)

    resp = client.get("/capabilities")

    assert resp.status_code == 200
    names = {c["name"] for c in resp.json()["capabilities"]}
    assert names == {"good_one_123"}


# -- GET /capabilities/{name} -------------------------------------------------


def test_get_single_capability_returns_the_full_contract(tmp_path):
    _write_artifacts(tmp_path)
    app = create_app(str(tmp_path))
    client = TestClient(app)

    resp = client.get("/capabilities/find_the_row_for_jason_doe_and_extract_h_1787179354")

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "find_the_row_for_jason_doe_and_extract_h_1787179354"
    assert body["parameters"]["properties"]["person_name"]["type"] == "string"
    assert body["parameters"]["required"] == ["person_name"]
    assert body["outputs"]["properties"]["due_amount"]["type"] == "string"


def test_get_unknown_capability_is_404(tmp_path):
    app = create_app(str(tmp_path))
    client = TestClient(app)

    resp = client.get("/capabilities/does-not-exist")

    assert resp.status_code == 404


# -- POST /capabilities/{name}/invoke -----------------------------------------


def test_invoke_with_valid_params_returns_the_replay_engines_result(tmp_path):
    _write_artifacts(tmp_path)
    app = create_app(str(tmp_path), str(tmp_path / "run_log.jsonl"))
    captured = {}

    def fake_runner(artifact, params):
        captured["artifact_goal"] = artifact.metadata.goal
        captured["params"] = params
        return {"status": "success", "outputs": {"error_message": "Your password is invalid!"}}

    app.dependency_overrides[get_replay_runner] = lambda: fake_runner
    client = TestClient(app)

    resp = client.post(
        "/capabilities/attempt_to_log_in_with_username_tomsmith_1787678625/invoke",
        json={"username": "tomsmith", "password": "wrongpassword"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "outputs": {"error_message": "Your password is invalid!"}}
    # Proves the real plumbing (artifact lookup, params pass-through) ran, not just the mock.
    assert captured["artifact_goal"] == _login_artifact().metadata.goal
    assert captured["params"] == {"username": "tomsmith", "password": "wrongpassword"}


def test_invoke_business_outcome_and_hard_failure_pass_through_unmodified(tmp_path):
    _write_artifacts(tmp_path)
    app = create_app(str(tmp_path), str(tmp_path / "run_log.jsonl"))

    for canned in (
        {"status": "business_outcome", "outcome_name": "not_found", "reason": "no such row"},
        {"status": "hard_failure", "step": 0, "expected": "x", "observed": "y", "error": "boom"},
    ):
        app.dependency_overrides[get_replay_runner] = lambda canned=canned: (lambda a, p: canned)
        client = TestClient(app)

        resp = client.post(
            "/capabilities/find_the_row_for_jason_doe_and_extract_h_1787179354/invoke",
            json={"person_name": "Doe Jason"},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == canned["status"]


def test_invoke_redacts_incidental_sensitive_looking_text_but_not_outputs(tmp_path):
    _write_artifacts(tmp_path)
    app = create_app(str(tmp_path), str(tmp_path / "run_log.jsonl"))
    app.dependency_overrides[get_replay_runner] = lambda: (
        lambda a, p: {
            "status": "hard_failure",
            "observed": "page showed account SSN 123-45-6789",
            "outputs": {"due_amount": "123456789012345"},
        }
    )
    client = TestClient(app)

    resp = client.post(
        "/capabilities/find_the_row_for_jason_doe_and_extract_h_1787179354/invoke",
        json={"person_name": "Doe Jason"},
    )

    body = resp.json()
    assert "123-45-6789" not in body["observed"]
    assert "[REDACTED]" in body["observed"]
    # outputs is protected -- never redacted, even though it looks like a long digit run.
    assert body["outputs"]["due_amount"] == "123456789012345"


def test_invoke_with_missing_required_param_returns_a_clean_error_not_a_crash(tmp_path):
    _write_artifacts(tmp_path)
    app = create_app(str(tmp_path))

    def never_call(artifact, params):
        raise AssertionError("replay_runner must not run when a required parameter is missing")

    app.dependency_overrides[get_replay_runner] = lambda: never_call
    client = TestClient(app)

    resp = client.post(
        "/capabilities/attempt_to_log_in_with_username_tomsmith_1787678625/invoke",
        json={"username": "tomsmith"},  # password missing
    )

    assert resp.status_code == 422
    assert "password" in resp.json()["detail"]


def test_invoke_with_no_body_at_all_is_a_clean_error_for_a_capability_with_required_params(tmp_path):
    _write_artifacts(tmp_path)
    app = create_app(str(tmp_path))
    app.dependency_overrides[get_replay_runner] = lambda: (lambda a, p: (_ for _ in ()).throw(AssertionError()))
    client = TestClient(app)

    resp = client.post("/capabilities/attempt_to_log_in_with_username_tomsmith_1787678625/invoke")

    assert resp.status_code == 422


def test_invoke_on_unknown_capability_is_404_before_touching_replay(tmp_path):
    app = create_app(str(tmp_path))

    def never_call(artifact, params):
        raise AssertionError("replay_runner must not run for an unknown capability")

    app.dependency_overrides[get_replay_runner] = lambda: never_call
    client = TestClient(app)

    resp = client.post("/capabilities/does-not-exist/invoke", json={})

    assert resp.status_code == 404


# -- run-log side effect -------------------------------------------------------


def test_invoke_appends_a_run_log_record_without_changing_the_response(tmp_path):
    _write_artifacts(tmp_path)
    run_log_path = str(tmp_path / "run_log.jsonl")
    app = create_app(str(tmp_path), run_log_path)
    app.dependency_overrides[get_replay_runner] = lambda: (
        lambda a, p: {
            "status": "success",
            "outputs": {"error_message": "Your password is invalid!"},
            "recovered_via_retry": True,
            "steps": [{"step": 0, "recovered_via_retry": True, "retry_count": 2}],
        }
    )
    client = TestClient(app)

    resp = client.post(
        "/capabilities/attempt_to_log_in_with_username_tomsmith_1787678625/invoke",
        json={"username": "tomsmith", "password": "wrongpassword"},
    )

    # The response is exactly what it would have been with no run-log at all.
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    runs = load_runs(run_log_path)
    assert len(runs) == 1
    assert runs[0]["capability"] == "attempt_to_log_in_with_username_tomsmith_1787678625"
    assert runs[0]["status"] == "success"
    assert runs[0]["recovered_via_retry"] is True
    assert runs[0]["retry_count"] == 2
    # Params land in the log redacted the same way any other free text is.
    assert runs[0]["params"]["username"] == "tomsmith"


def test_invoke_response_unaffected_when_run_log_write_fails(tmp_path):
    # A directory in place of the log file makes the write raise OSError/IsADirectoryError
    # -- confirms the try/except in catalog/service.py swallows it rather than 500ing.
    _write_artifacts(tmp_path)
    bad_run_log_path = str(tmp_path / "run_log_is_a_dir.jsonl")
    (tmp_path / "run_log_is_a_dir.jsonl").mkdir()
    app = create_app(str(tmp_path), bad_run_log_path)
    app.dependency_overrides[get_replay_runner] = lambda: (lambda a, p: {"status": "success", "outputs": {}})
    client = TestClient(app)

    resp = client.post(
        "/capabilities/attempt_to_log_in_with_username_tomsmith_1787678625/invoke",
        json={"username": "tomsmith", "password": "wrongpassword"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_default_replay_runner_dependency_is_the_real_run_replay_function():
    # Confirms the production wiring: with no override, the dependency really is the
    # function that reuses ReplayEngine/GuardrailChecker -- not swapped out anywhere else.
    from catalog.service import _run_replay

    assert get_replay_runner() is _run_replay
