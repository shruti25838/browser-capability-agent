"""Regression tests for the VALUE_EXTRACTED generalization fix in agent/artifact_writer.py.

The bug: a run's own extracted literal (e.g. the "$100.00" observed for one
specific row during discovery) could get hardcoded into a postcondition or
the overall success_condition, making the resulting artifact only ever
succeed for that one run's incidental data instead of generalizing to any
input parameter. The fix detects when an asserted literal is exactly one of
this run's own extracted output values and rewrites that condition to
kind=VALUE_EXTRACTED instead.

Pure logic against ArtifactWriter.build() -- no live browser or LLM. Building
DiscoveryRun/ExecutedStep/ParameterCandidate is safe to import: those are
plain dataclasses, and importing agent.discovery_agent only does local
JSON-schema-to-Gemini-schema conversion at module load, no network calls.
"""

from __future__ import annotations

from agent.artifact import ConditionKind
from agent.artifact_writer import ArtifactWriter
from agent.discovery_agent import DiscoveryRun, ExecutedStep, ParameterCandidate


def _row_visible(contains_text: str = "Doe Jason") -> dict:
    return {"kind": "row_visible", "description": "row visible", "contains_text": contains_text}


def _find_row_step() -> ExecutedStep:
    return ExecutedStep(
        order=0,
        type="find_row",
        description="Find the row containing 'Doe Jason'",
        precondition=_row_visible(),
        postcondition=_row_visible(),
        contains_text="Doe Jason",
    )


def _run_with_extracted_literal_in_expected_value() -> DiscoveryRun:
    extract_step = ExecutedStep(
        order=1,
        type="extract",
        description="Extract due_amount",
        precondition=_row_visible(),
        postcondition={
            "kind": "text_equals",
            "description": "Extracted due amount from Due column",
            "expected_value": "$100.00",
        },
        locator={"kind": "table_cell_by_column", "column_header": "Due", "scope": "row"},
        output_field="due_amount",
        output_type="string",
    )
    return DiscoveryRun(
        goal="find the row for Jason Doe and extract his Due amount",
        target_url="https://the-internet.herokuapp.com/tables",
        success=True,
        reason="done",
        steps=[_find_row_step(), extract_step],
        outputs={"due_amount": "$100.00"},
        parameter_candidates=[
            ParameterCandidate(name="person_name", value="Doe Jason", type="string", description="Row to select")
        ],
        success_condition={
            "kind": "text_equals",
            "description": "Due amount extracted for Jason Doe is $100.00",
            "expected_value": "$100.00",
        },
    )


def test_extracted_literal_in_expected_value_becomes_value_extracted_not_hardcoded():
    run = _run_with_extracted_literal_in_expected_value()

    artifact = ArtifactWriter().build(run)

    extract_postcondition = artifact.steps[1].postcondition
    assert extract_postcondition.kind == ConditionKind.VALUE_EXTRACTED
    assert extract_postcondition.output_field == "due_amount"
    assert extract_postcondition.expected_type == "string"
    # The incidental literal from this one run must not survive anywhere on the condition.
    assert extract_postcondition.expected_value is None
    assert extract_postcondition.contains_text is None


def test_success_condition_extracted_literal_also_generalizes():
    run = _run_with_extracted_literal_in_expected_value()

    artifact = ArtifactWriter().build(run)

    assert artifact.success_condition.kind == ConditionKind.VALUE_EXTRACTED
    assert artifact.success_condition.output_field == "due_amount"
    assert artifact.success_condition.expected_value is None


def test_extracted_literal_in_contains_text_also_generalizes():
    # Some discovery turns put the literal in contains_text instead of expected_value --
    # ArtifactWriter must check both fields, not just expected_value.
    extract_step = ExecutedStep(
        order=1,
        type="extract",
        description="Extract due_amount",
        precondition=_row_visible(),
        postcondition={
            "kind": "text_contains",
            "description": "Extracted due amount from Due column",
            "contains_text": "$100.00",
        },
        locator={"kind": "table_cell_by_column", "column_header": "Due", "scope": "row"},
        output_field="due_amount",
        output_type="string",
    )
    run = DiscoveryRun(
        goal="find the row for Jason Doe and extract his Due amount",
        target_url="https://the-internet.herokuapp.com/tables",
        success=True,
        reason="done",
        steps=[_find_row_step(), extract_step],
        outputs={"due_amount": "$100.00"},
        parameter_candidates=[
            ParameterCandidate(name="person_name", value="Doe Jason", type="string", description="Row to select")
        ],
        success_condition=_row_visible(),
    )

    artifact = ArtifactWriter().build(run)

    extract_postcondition = artifact.steps[1].postcondition
    assert extract_postcondition.kind == ConditionKind.VALUE_EXTRACTED
    assert extract_postcondition.output_field == "due_amount"
    assert extract_postcondition.contains_text is None


def test_genuine_fixed_constant_is_not_generalized():
    # expected_value here ("Active") does not match anything in run.outputs, so it's a
    # real constant and must survive untouched -- confirms the fix doesn't over-generalize.
    status_check_step = ExecutedStep(
        order=1,
        type="click",
        description="Confirm status before clicking edit",
        precondition={
            "kind": "text_equals",
            "description": "Status cell reads Active",
            "role": "cell",
            "name": "Active",
            "expected_value": "Active",
        },
        postcondition={
            "kind": "text_equals",
            "description": "Status cell reads Active",
            "role": "cell",
            "name": "Active",
            "expected_value": "Active",
        },
    )
    run = DiscoveryRun(
        goal="test",
        target_url="https://the-internet.herokuapp.com/tables",
        success=True,
        reason="done",
        steps=[_find_row_step(), status_check_step],
        outputs={"due_amount": "$100.00"},  # unrelated output -- must not match "Active"
        parameter_candidates=[
            ParameterCandidate(name="person_name", value="Doe Jason", type="string", description="Row to select")
        ],
        success_condition=_row_visible(),
    )

    artifact = ArtifactWriter().build(run)

    condition = artifact.steps[1].postcondition
    assert condition.kind == ConditionKind.TEXT_EQUALS
    assert condition.expected_value == "Active"
    assert condition.output_field is None


def test_success_condition_genuine_constant_is_not_generalized():
    run = _run_with_extracted_literal_in_expected_value()
    run.success_condition = {
        "kind": "row_visible",
        "description": "Row for Jason Doe is visible in the table",
        "contains_text": "Doe Jason",
    }

    artifact = ArtifactWriter().build(run)

    assert artifact.success_condition.kind == ConditionKind.ROW_VISIBLE
    assert artifact.success_condition.contains_text == "{{person_name}}"
    assert artifact.success_condition.output_field is None


# -- outcome_mapping (named business outcomes beyond find_row) --------------


def test_outcome_mapping_populates_from_discovery_run_and_reuses_generalization():
    click_step = ExecutedStep(
        order=1,
        type="click",
        description="click submit",
        precondition=_row_visible(),
        postcondition=_row_visible(),
        outcome_mapping=[
            {
                "outcome_name": "permission_denied",
                "condition": {
                    "kind": "row_visible",
                    "description": "Row for Doe Jason still shows a permission error",
                    "contains_text": "Doe Jason",
                },
            }
        ],
    )
    run = DiscoveryRun(
        goal="test",
        target_url="https://the-internet.herokuapp.com/tables",
        success=True,
        reason="done",
        steps=[_find_row_step(), click_step],
        outputs={},
        parameter_candidates=[
            ParameterCandidate(name="person_name", value="Doe Jason", type="string", description="Row to select")
        ],
        success_condition=_row_visible(),
    )

    artifact = ArtifactWriter().build(run)

    click_step_out = artifact.steps[1]
    assert len(click_step_out.outcome_mapping) == 1
    rule = click_step_out.outcome_mapping[0]
    assert rule.outcome_name == "permission_denied"
    # The literal "Doe Jason" inside the rule's own condition must be generalized the same
    # way precondition/postcondition literals are -- this reuses _condition_from_dict via
    # the same `condition()` closure, not a separate, unmaintained substitution path.
    assert rule.condition.contains_text == "{{person_name}}"


# -- output_value backstop (generalization net without a declared `outputs`) --


def _run_with_extracted_literal_but_no_declared_outputs() -> DiscoveryRun:
    # The model declared nothing in `outputs` at finish time (run.outputs={}) -- only the
    # extract step's own `output_value`, captured directly from execution, is available.
    extract_step = ExecutedStep(
        order=1,
        type="extract",
        description="Extract error_message",
        precondition=_row_visible(),
        postcondition={
            "kind": "text_equals",
            "description": "Extracted error message",
            "expected_value": "Your password is invalid!",
        },
        output_field="error_message",
        output_type="string",
        output_value="Your password is invalid!",
    )
    return DiscoveryRun(
        goal="attempt login with wrong password",
        target_url="https://the-internet.herokuapp.com/login",
        success=True,
        reason="done",
        steps=[_find_row_step(), extract_step],
        outputs={},
        parameter_candidates=[],
        success_condition={
            "kind": "text_equals",
            "description": "Error message is displayed",
            "expected_value": "Your password is invalid!",
        },
    )


def test_output_value_backstop_generalizes_postcondition_when_run_declares_no_outputs():
    run = _run_with_extracted_literal_but_no_declared_outputs()

    artifact = ArtifactWriter().build(run)

    extract_postcondition = artifact.steps[1].postcondition
    assert extract_postcondition.kind == ConditionKind.VALUE_EXTRACTED
    assert extract_postcondition.output_field == "error_message"
    assert extract_postcondition.expected_value is None


def test_output_value_backstop_generalizes_success_condition_when_run_declares_no_outputs():
    run = _run_with_extracted_literal_but_no_declared_outputs()

    artifact = ArtifactWriter().build(run)

    assert artifact.success_condition.kind == ConditionKind.VALUE_EXTRACTED
    assert artifact.success_condition.output_field == "error_message"
    assert artifact.success_condition.expected_value is None


def test_unrelated_output_value_on_a_non_extract_step_does_not_cause_generalization():
    # output_value is only ever set on extract steps (see agent/discovery_agent.py) --
    # confirm a step with no output_field/output_value (e.g. find_row) contributes nothing
    # to the backstop map and a genuine constant elsewhere still survives untouched.
    run = _run_with_extracted_literal_but_no_declared_outputs()
    assert run.steps[0].output_value is None  # the find_row step

    artifact = ArtifactWriter().build(run)

    assert artifact.steps[0].postcondition.kind == ConditionKind.ROW_VISIBLE


def test_step_with_no_outcome_mapping_in_discovery_run_gets_empty_list():
    run = DiscoveryRun(
        goal="test",
        target_url="https://the-internet.herokuapp.com/tables",
        success=True,
        reason="done",
        steps=[_find_row_step()],
        outputs={},
        parameter_candidates=[
            ParameterCandidate(name="person_name", value="Doe Jason", type="string", description="Row to select")
        ],
        success_condition=_row_visible(),
    )

    artifact = ArtifactWriter().build(run)

    assert artifact.steps[0].outcome_mapping == []
