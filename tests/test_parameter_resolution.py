"""Unit tests for {{parameter}} resolution in replay/replay_engine.py.

Pure string/pydantic-model logic -- no Playwright, no LLM.
"""

from __future__ import annotations

import pytest

from agent.artifact import Condition, ConditionKind, Locator, LocatorKind, NameMatch
from replay.replay_engine import ParameterError, resolve_condition, resolve_locator, resolve_placeholders


def test_single_placeholder_resolves_to_provided_value():
    resolved = resolve_placeholders("{{target_name}}", {"target_name": "Doe Jason"})

    assert resolved == "Doe Jason"


def test_placeholder_embedded_in_surrounding_text_resolves():
    resolved = resolve_placeholders("Row for {{target_name}} is visible", {"target_name": "Doe Jason"})

    assert resolved == "Row for Doe Jason is visible"


def test_none_text_resolves_to_none():
    assert resolve_placeholders(None, {"target_name": "Doe Jason"}) is None


def test_text_with_no_placeholders_is_unchanged():
    assert resolve_placeholders("Due", {"target_name": "Doe Jason"}) == "Due"


def test_multiple_different_placeholders_in_one_string_resolve_independently():
    resolved = resolve_placeholders("{{first_name}} {{last_name}}", {"first_name": "Jason", "last_name": "Doe"})

    assert resolved == "Jason Doe"


def test_multiple_different_placeholders_across_fields_resolve_independently():
    params = {"person_name": "Doe Jason", "delete_link_name": "delete", "due_column": "Due"}

    contains_text = resolve_placeholders("{{person_name}}", params)
    locator = resolve_locator(
        Locator(kind=LocatorKind.ROLE, role="link", name="{{delete_link_name}}", scope="row"),
        params,
    )
    condition = resolve_condition(
        Condition(
            kind=ConditionKind.TEXT_EQUALS,
            description="cell matches",
            locator=Locator(kind=LocatorKind.ROLE, role="cell", name="{{due_column}}", scope="row"),
            expected_value="{{due_column}}",
        ),
        params,
    )

    assert contains_text == "Doe Jason"
    assert locator.name == "delete"
    assert condition.locator.name == "Due"
    assert condition.expected_value == "Due"


def test_resolve_condition_resolves_contains_text_expected_value_and_url_pattern_independently():
    params = {"person_name": "Doe Jason", "status": "Active", "path": "/tables"}
    condition = Condition(
        kind=ConditionKind.ROW_VISIBLE,
        description="row visible",
        contains_text="{{person_name}}",
        expected_value="{{status}}",
        url_pattern="{{path}}",
    )

    resolved = resolve_condition(condition, params)

    assert resolved.contains_text == "Doe Jason"
    assert resolved.expected_value == "Active"
    assert resolved.url_pattern == "/tables"
    # Resolution must not mutate the original condition.
    assert condition.contains_text == "{{person_name}}"


def test_resolve_locator_returns_none_for_none_input():
    assert resolve_locator(None, {"person_name": "Doe Jason"}) is None


def test_resolve_locator_leaves_locator_without_placeholder_name_unchanged():
    locator = Locator(kind=LocatorKind.ROLE, role="link", name="edit", name_match=NameMatch.EXACT, scope="row")

    resolved = resolve_locator(locator, {"person_name": "Doe Jason"})

    assert resolved.name == "edit"
    assert resolved.role == "link"
    assert resolved.scope == "row"


def test_missing_parameter_raises_clear_error_instead_of_silent_passthrough():
    with pytest.raises(ParameterError) as excinfo:
        resolve_placeholders("{{target_name}}", {})

    assert "target_name" in str(excinfo.value)


def test_missing_parameter_in_condition_raises_via_resolve_condition():
    condition = Condition(
        kind=ConditionKind.ROW_VISIBLE,
        description="row visible",
        contains_text="{{missing_param}}",
    )

    with pytest.raises(ParameterError) as excinfo:
        resolve_condition(condition, {"other_param": "value"})

    assert "missing_param" in str(excinfo.value)


def test_missing_parameter_in_locator_name_raises_via_resolve_locator():
    locator = Locator(kind=LocatorKind.ROLE, role="link", name="{{missing_param}}", scope="row")

    with pytest.raises(ParameterError) as excinfo:
        resolve_locator(locator, {})

    assert "missing_param" in str(excinfo.value)
