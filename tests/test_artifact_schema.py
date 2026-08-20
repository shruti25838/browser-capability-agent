"""Round-trip and per-condition-kind serialization tests for agent/artifact.py.

Pure pydantic model tests -- no Playwright, no LLM, no filesystem beyond a
tmp_path fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent.artifact import (
    Artifact,
    Condition,
    ConditionKind,
    Locator,
    LocatorKind,
    Metadata,
    NameMatch,
    OutcomeRule,
    OutputField,
    Parameter,
    Step,
    StepType,
)


def _sample_artifact() -> Artifact:
    row_visible = Condition(
        kind=ConditionKind.ROW_VISIBLE,
        description="Row for {{person_name}} is visible",
        contains_text="{{person_name}}",
    )
    value_extracted = Condition(
        kind=ConditionKind.VALUE_EXTRACTED,
        description="due_amount was extracted as a non-empty string value",
        output_field="due_amount",
        expected_type="string",
    )
    text_equals = Condition(
        kind=ConditionKind.TEXT_EQUALS,
        description="Status cell reads 'Active'",
        locator=Locator(kind=LocatorKind.ROLE, role="cell", name="Active", name_match=NameMatch.EXACT, scope="row"),
        expected_value="Active",
    )

    return Artifact(
        metadata=Metadata(
            goal="find the row for a person and extract their due amount",
            target_url="https://the-internet.herokuapp.com/tables",
            created_at=datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc),
        ),
        parameters=[
            Parameter(name="person_name", type="string", description="Name of the person", example_value="Doe Jason")
        ],
        outputs=[OutputField(name="due_amount", type="string", description="Amount due for the row")],
        steps=[
            Step(
                order=0,
                type=StepType.FIND_ROW,
                description="Find the row containing {{person_name}}",
                precondition=row_visible,
                postcondition=row_visible,
                contains_text="{{person_name}}",
            ),
            Step(
                order=1,
                type=StepType.EXTRACT,
                description="Extract due_amount from the Due column",
                precondition=row_visible,
                postcondition=value_extracted,
                locator=Locator(kind=LocatorKind.TABLE_CELL_BY_COLUMN, column_header="Due", scope="row"),
                output_field="due_amount",
            ),
            Step(
                order=2,
                type=StepType.CLICK,
                description="Confirm the status cell reads Active before clicking edit",
                precondition=text_equals,
                postcondition=text_equals,
                locator=Locator(kind=LocatorKind.ROLE, role="link", name="edit", scope="row"),
                outcome_mapping=[
                    OutcomeRule(
                        outcome_name="permission_denied",
                        condition=Condition(
                            kind=ConditionKind.ELEMENT_VISIBLE,
                            description="a 'permission denied' message is visible",
                            locator=Locator(kind=LocatorKind.ROLE, role="text", name="Permission Denied"),
                        ),
                    )
                ],
            ),
        ],
        success_condition=row_visible,
    )


def test_round_trip_via_file(tmp_path):
    artifact = _sample_artifact()
    path = tmp_path / "artifact.json"

    artifact.to_json_file(str(path))
    reloaded = Artifact.from_json_file(str(path))

    assert reloaded == artifact
    assert reloaded.model_dump() == artifact.model_dump()


def test_round_trip_via_json_string():
    artifact = _sample_artifact()

    reloaded = Artifact.model_validate_json(artifact.model_dump_json())

    assert reloaded == artifact
    assert reloaded.metadata.goal == artifact.metadata.goal
    assert reloaded.metadata.target_url == artifact.metadata.target_url
    assert [s.order for s in reloaded.steps] == [s.order for s in artifact.steps]


def test_value_extracted_condition_round_trips():
    condition = Condition(
        kind=ConditionKind.VALUE_EXTRACTED,
        description="due_amount was extracted as a non-empty string value",
        output_field="due_amount",
        expected_type="string",
    )

    reloaded = Condition.model_validate_json(condition.model_dump_json())

    assert reloaded == condition
    assert reloaded.kind == ConditionKind.VALUE_EXTRACTED
    assert reloaded.output_field == "due_amount"
    assert reloaded.expected_type == "string"
    # value_extracted has no locator/contains_text/expected_value of its own
    assert reloaded.locator is None
    assert reloaded.expected_value is None


def test_text_equals_condition_round_trips():
    condition = Condition(
        kind=ConditionKind.TEXT_EQUALS,
        description="Status cell reads 'Active'",
        locator=Locator(kind=LocatorKind.ROLE, role="cell", name="Active", name_match=NameMatch.EXACT, scope="row"),
        expected_value="Active",
    )

    reloaded = Condition.model_validate_json(condition.model_dump_json())

    assert reloaded == condition
    assert reloaded.kind == ConditionKind.TEXT_EQUALS
    assert reloaded.expected_value == "Active"
    assert reloaded.locator is not None
    assert reloaded.locator.kind == LocatorKind.ROLE
    assert reloaded.locator.role == "cell"
    assert reloaded.locator.name == "Active"
    assert reloaded.locator.scope == "row"


def test_outcome_mapping_round_trips_and_defaults_to_empty():
    rule = OutcomeRule(
        outcome_name="permission_denied",
        condition=Condition(kind=ConditionKind.ELEMENT_VISIBLE, description="denied message visible"),
    )
    step_with_mapping = Step(
        order=0,
        type=StepType.CLICK,
        description="click",
        precondition=Condition(kind=ConditionKind.ELEMENT_VISIBLE, description="d"),
        postcondition=Condition(kind=ConditionKind.ELEMENT_VISIBLE, description="d"),
        outcome_mapping=[rule],
    )
    step_without_mapping = Step(
        order=1,
        type=StepType.CLICK,
        description="click",
        precondition=Condition(kind=ConditionKind.ELEMENT_VISIBLE, description="d"),
        postcondition=Condition(kind=ConditionKind.ELEMENT_VISIBLE, description="d"),
    )

    reloaded = Step.model_validate_json(step_with_mapping.model_dump_json())
    assert reloaded == step_with_mapping
    assert len(reloaded.outcome_mapping) == 1
    assert reloaded.outcome_mapping[0].outcome_name == "permission_denied"
    assert reloaded.outcome_mapping[0].condition.kind == ConditionKind.ELEMENT_VISIBLE

    # Additive: a step declaring nothing defaults to an empty list, not None/missing.
    assert step_without_mapping.outcome_mapping == []


def test_full_artifact_json_contains_both_condition_kinds(tmp_path):
    artifact = _sample_artifact()
    path = tmp_path / "artifact.json"
    artifact.to_json_file(str(path))

    raw = path.read_text(encoding="utf-8")
    assert '"value_extracted"' in raw
    assert '"text_equals"' in raw

    reloaded = Artifact.from_json_file(str(path))
    kinds = {step.postcondition.kind for step in reloaded.steps}
    assert ConditionKind.VALUE_EXTRACTED in kinds
    assert ConditionKind.TEXT_EQUALS in kinds
