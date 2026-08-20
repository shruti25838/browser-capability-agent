"""Tests for the ReplayEngine business_outcome / hard_failure taxonomy.

This is the most important file in the suite: it pins down the core design
decision agreed on with the assignment author --

  * A condition check that raises an exception is ALWAYS a hard_failure,
    regardless of step type.
  * A condition check that cleanly evaluates to False (no exception) is a
    business_outcome when a step declares outcome_mapping and one of its
    rules matches current page state (classified under that rule's
    outcome_name), or -- for backward compatibility with artifacts that
    declare no outcome_mapping -- for a find_row step's ROW_VISIBLE/
    ROW_ABSENT check (precondition or postcondition), reported as
    outcome_name "not_found".
  * Every other clean-false condition, on any other step type with no
    matching outcome_mapping rule (or the wrong condition kind even on a
    find_row step), is a hard_failure.
  * A condition that only passes after a retry is visible in the result
    (recovered_via_retry / retry_count, per-step and rolled up to the top
    level) -- not silently absorbed.
  * Control ownership (ReplayEngine.owner / result["owner"]) flips to
    "human" for exactly the window EscalationHandler is active, and back
    to "automation" the instant it returns.

No Playwright, no LLM, no network: Perceiver is replaced with a tiny fake
that returns pre-programmed FakeLocator results, and GuardrailChecker is
the real (Playwright-free) implementation configured to always allow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest

from agent.artifact import (
    Artifact,
    Condition,
    ConditionKind,
    Locator,
    LocatorKind,
    Metadata,
    OutcomeRule,
    Parameter,
    Step,
    StepType,
)
from agent.guardrails import GuardrailChecker
from replay.replay_engine import ConditionCheck, ControlOwner, EscalationResolution, ReplayEngine


class FakeLocator:
    """Minimal stand-in for a Playwright Locator: only the surface
    ReplayEngine's condition checks / target resolution actually calls."""

    def __init__(self, count: int = 0, visible: bool = False):
        self._count = count
        self._visible = visible
        self.click_calls = 0

    def count(self) -> int:
        return self._count

    @property
    def first(self):
        return self

    def is_visible(self, timeout=None) -> bool:
        return self._visible

    def get_by_role(self, role, name=None, exact=None):
        return self

    def locator(self, css_selector):
        return self

    def nth(self, idx):
        return self

    def click(self, timeout=None) -> None:
        # If the guardrail is doing its job, this must never be called for a
        # blocked target -- tests assert click_calls stays 0.
        self.click_calls += 1


class FakePerceiver:
    """Double for agent.perceiver.Perceiver, swapped onto a real ReplayEngine
    instance after construction so no Playwright Page is ever touched."""

    def __init__(self, find_row_result=None, root: Optional[FakeLocator] = None):
        # find_row_result: a FakeLocator, an Exception instance to raise, or a
        # callable(call_number) -> FakeLocator | Exception for multi-attempt scenarios.
        self.find_row_result = find_row_result
        self.find_row_calls = 0
        self.root = root if root is not None else FakeLocator(count=0, visible=False)

    def find_row(self, contains_text: str):
        self.find_row_calls += 1
        result = self.find_row_result(self.find_row_calls) if callable(self.find_row_result) else self.find_row_result
        if isinstance(result, Exception):
            raise result
        return result

    def resolve_column_index(self, column_header: str) -> int:
        raise NotImplementedError("not exercised by these tests")


class StubPage:
    def __init__(self, url: str = "https://the-internet.herokuapp.com/tables"):
        self.url = url


def _make_engine(find_row_result=None, root: Optional[FakeLocator] = None) -> ReplayEngine:
    guardrail = GuardrailChecker(
        allowed_domains=["the-internet.herokuapp.com"],
        allowed_actions=["find_row", "click", "type", "extract"],
    )
    engine = ReplayEngine(page=StubPage(), guardrail=guardrail, retry_wait_s=0.0)
    engine.perceiver = FakePerceiver(find_row_result=find_row_result, root=root)
    return engine


def _row_visible_condition() -> Condition:
    return Condition(kind=ConditionKind.ROW_VISIBLE, description="row visible", contains_text="{{target_name}}")


def _find_row_step(condition: Condition) -> Step:
    return Step(
        order=0,
        type=StepType.FIND_ROW,
        description="find row",
        precondition=condition,
        postcondition=condition,
        contains_text="{{target_name}}",
    )


def _element_visible_condition() -> Condition:
    return Condition(
        kind=ConditionKind.ELEMENT_VISIBLE,
        description="delete link is visible",
        locator=Locator(kind=LocatorKind.ROLE, role="link", name="delete", scope="page"),
    )


def _click_step(condition: Condition) -> Step:
    return Step(
        order=0,
        type=StepType.CLICK,
        description="click delete link",
        precondition=condition,
        postcondition=condition,
        locator=condition.locator,
    )


def _artifact_with_single_step(step: Step) -> Artifact:
    return Artifact(
        metadata=Metadata(
            goal="test",
            target_url="https://the-internet.herokuapp.com/tables",
            created_at=datetime.now(timezone.utc),
        ),
        parameters=[Parameter(name="target_name", type="string", description="d", example_value="Doe Jason")],
        outputs=[],
        steps=[step],
        success_condition=step.postcondition,
    )


# -- end-to-end through ReplayEngine.run() -----------------------------------


def test_click_on_blocked_target_is_refused_by_guardrail_before_playwright_click():
    # The element_visible precondition passes (the fake root reports the "delete" link
    # exists and is visible), so execution reaches the guardrail check next -- which must
    # refuse the click via the real DEFAULT_ACTION_RISK_TIERS ("click:delete" -> blocked)
    # strictly before FakeLocator.click() is ever invoked.
    condition = _element_visible_condition()
    root = FakeLocator(count=1, visible=True)
    artifact = _artifact_with_single_step(_click_step(condition))
    engine = _make_engine(root=root)

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert result["status"] == "hard_failure"
    assert "blocked" in result["error"]
    assert root.click_calls == 0


def test_find_row_condition_cleanly_false_after_retries_is_business_outcome():
    condition = _row_visible_condition()
    artifact = _artifact_with_single_step(_find_row_step(condition))
    engine = _make_engine(find_row_result=FakeLocator(count=0))

    result = engine.run(artifact, {"target_name": "Nobody Fake"})

    assert result["status"] == "business_outcome"
    assert result["step"] == 0
    # initial check + 2 retries = 3 attempts, then stop (no action attempted).
    assert engine.perceiver.find_row_calls == 3


def test_non_find_row_condition_cleanly_false_after_retries_is_hard_failure():
    condition = _element_visible_condition()
    artifact = _artifact_with_single_step(_click_step(condition))
    engine = _make_engine(root=FakeLocator(count=0, visible=False))

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert result["status"] == "hard_failure"
    assert result["step"] == 0
    assert result["error"]


def test_condition_exception_is_always_hard_failure_even_for_find_row():
    condition = _row_visible_condition()
    artifact = _artifact_with_single_step(_find_row_step(condition))
    engine = _make_engine(find_row_result=ValueError("row-lookup exploded"))

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert result["status"] == "hard_failure"
    assert result["step"] == 0
    assert "row-lookup exploded" in result["error"]
    assert engine.perceiver.find_row_calls == 3


def test_find_row_precondition_and_postcondition_both_get_the_business_outcome_exemption():
    # Precondition fails cleanly -> business_outcome without ever reaching the action.
    condition = _row_visible_condition()
    artifact = _artifact_with_single_step(_find_row_step(condition))
    engine = _make_engine(find_row_result=FakeLocator(count=0))

    result = engine.run(artifact, {"target_name": "Nobody Fake"})

    assert result["status"] == "business_outcome"


def test_find_row_multiple_matches_is_hard_failure_not_success_or_business_outcome():
    # An ambiguous "which row?" is a real break -- not a legitimate not-found answer,
    # and definitely not a silent success against whichever row Playwright picks first.
    condition = _row_visible_condition()
    artifact = _artifact_with_single_step(_find_row_step(condition))
    engine = _make_engine(find_row_result=FakeLocator(count=2))

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert result["status"] == "hard_failure"
    assert result["step"] == 0
    assert "2" in result["error"]


def test_row_visible_check_flags_ambiguous_match_as_an_error_not_a_clean_false():
    engine = _make_engine(find_row_result=FakeLocator(count=3))
    condition = _row_visible_condition()

    check = engine._check_condition(condition, current_row=None)

    assert check.passed is False
    assert check.error is not None
    assert "3" in check.error


def test_row_absent_is_unaffected_by_the_ambiguity_check():
    # ROW_ABSENT's answer ("is it gone or not") doesn't depend on identifying a specific
    # row, so multiple matches should behave exactly like the pre-fix single-match case:
    # a clean False (row(s) still there), not an error.
    condition = Condition(kind=ConditionKind.ROW_ABSENT, description="row gone", contains_text="{{target_name}}")
    engine = _make_engine(find_row_result=FakeLocator(count=2))

    check = engine._check_condition(condition, current_row=None)

    assert check.passed is False
    assert check.error is None


# -- direct unit tests of the retry mechanism --------------------------------


def test_check_with_retries_retries_until_condition_passes():
    condition = _row_visible_condition()
    engine = _make_engine()
    calls = {"n": 0}

    def flaky(contains_text):
        calls["n"] += 1
        return FakeLocator(count=0) if calls["n"] < 3 else FakeLocator(count=1)

    engine.perceiver.find_row = flaky

    check = engine._check_with_retries(condition, current_row=None)

    assert check.passed is True
    assert calls["n"] == 3


def test_check_with_retries_gives_up_after_max_retries():
    condition = _row_visible_condition()
    engine = _make_engine(find_row_result=FakeLocator(count=0))

    check = engine._check_with_retries(condition, current_row=None)

    assert check.passed is False
    assert engine.perceiver.find_row_calls == 3  # 1 initial + 2 retries, no more


# -- direct unit tests of the classification matrix --------------------------


@pytest.mark.parametrize(
    "step_type, condition_kind, has_error, expected_status",
    [
        (StepType.FIND_ROW, ConditionKind.ROW_VISIBLE, False, "business_outcome"),
        (StepType.FIND_ROW, ConditionKind.ROW_ABSENT, False, "business_outcome"),
        (StepType.FIND_ROW, ConditionKind.ROW_VISIBLE, True, "hard_failure"),  # error overrides the exemption
        (StepType.FIND_ROW, ConditionKind.ELEMENT_VISIBLE, False, "hard_failure"),  # exempt kind, wrong condition
        (StepType.CLICK, ConditionKind.ROW_VISIBLE, False, "hard_failure"),  # exempt kind, wrong step type
        (StepType.EXTRACT, ConditionKind.ELEMENT_VISIBLE, False, "hard_failure"),
        (StepType.TYPE, ConditionKind.ROW_ABSENT, True, "hard_failure"),
    ],
)
def test_classify_failure_matrix(step_type, condition_kind, has_error, expected_status):
    engine = _make_engine()
    condition = Condition(kind=condition_kind, description="d")
    step = Step(order=0, type=step_type, description="d", precondition=condition, postcondition=condition)
    check = ConditionCheck(passed=False, observed="observed", error="boom" if has_error else None)

    outcome = engine._classify_failure(step, 0, "precondition", condition, check, current_row=None, params={})

    assert outcome["status"] == expected_status
    if expected_status == "hard_failure":
        assert outcome["result"]["step"] == 0
        assert outcome["result"]["error"]


# -- generalized business_outcome via outcome_mapping ------------------------


def test_declared_outcome_mapping_classifies_as_business_outcome_with_outcome_name():
    # Without outcome_mapping, a CLICK step whose precondition kind is ROW_VISIBLE and
    # cleanly fails is hard_failure (see test_classify_failure_matrix's
    # click-row_visible-False-hard_failure case). Declaring an outcome_mapping rule
    # that matches current page state overrides that default.
    precondition = Condition(kind=ConditionKind.ROW_VISIBLE, description="row visible", contains_text="{{target_name}}")
    outcome_rule = OutcomeRule(
        outcome_name="permission_denied",
        condition=Condition(
            kind=ConditionKind.ELEMENT_VISIBLE,
            description="a 'permission denied' message is visible",
            locator=Locator(kind=LocatorKind.ROLE, role="text", name="Permission Denied", scope="page"),
        ),
    )
    step = Step(
        order=0,
        type=StepType.CLICK,
        description="click something",
        precondition=precondition,
        postcondition=precondition,
        outcome_mapping=[outcome_rule],
    )
    artifact = _artifact_with_single_step(step)
    engine = _make_engine(find_row_result=FakeLocator(count=0), root=FakeLocator(count=1, visible=True))

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert result["status"] == "business_outcome"
    assert result["outcome_name"] == "permission_denied"
    assert result["step"] == 0


def test_outcome_mapping_rule_that_does_not_match_falls_through_to_default_classification():
    # The declared rule's condition does NOT hold (root reports not visible), so with no
    # matching rule, classification falls through to the existing default: CLICK + ROW_VISIBLE
    # with no match is hard_failure, exactly as it was before outcome_mapping existed.
    precondition = Condition(kind=ConditionKind.ROW_VISIBLE, description="row visible", contains_text="{{target_name}}")
    outcome_rule = OutcomeRule(
        outcome_name="permission_denied",
        condition=Condition(
            kind=ConditionKind.ELEMENT_VISIBLE,
            description="a 'permission denied' message is visible",
            locator=Locator(kind=LocatorKind.ROLE, role="text", name="Permission Denied", scope="page"),
        ),
    )
    step = Step(
        order=0,
        type=StepType.CLICK,
        description="click something",
        precondition=precondition,
        postcondition=precondition,
        outcome_mapping=[outcome_rule],
    )
    artifact = _artifact_with_single_step(step)
    engine = _make_engine(find_row_result=FakeLocator(count=0), root=FakeLocator(count=0, visible=False))

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert result["status"] == "hard_failure"


def test_step_with_no_outcome_mapping_falls_back_to_existing_default_behavior():
    # outcome_mapping defaults to [] -- confirms this is purely additive: a step declaring
    # nothing behaves exactly as it did before this feature existed.
    condition = _element_visible_condition()
    step = _click_step(condition)
    assert step.outcome_mapping == []
    artifact = _artifact_with_single_step(step)
    engine = _make_engine(root=FakeLocator(count=0, visible=False))

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert result["status"] == "hard_failure"


def test_find_row_still_gets_its_built_in_not_found_exemption_with_no_declared_mapping():
    condition = _row_visible_condition()
    artifact = _artifact_with_single_step(_find_row_step(condition))
    engine = _make_engine(find_row_result=FakeLocator(count=0))

    result = engine.run(artifact, {"target_name": "Nobody Fake"})

    assert result["status"] == "business_outcome"
    assert result["outcome_name"] == "not_found"


# -- recovery visibility (recovered_via_retry / retry_count) -----------------


def test_recovered_via_retry_is_true_when_a_check_passes_only_on_the_second_attempt():
    condition = _row_visible_condition()
    artifact = _artifact_with_single_step(_find_row_step(condition))
    calls = {"n": 0}

    def flaky(contains_text):
        calls["n"] += 1
        return FakeLocator(count=0) if calls["n"] < 2 else FakeLocator(count=1)

    engine = _make_engine(find_row_result=flaky)

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert result["status"] == "success"
    assert result["recovered_via_retry"] is True
    step_0_info = next(info for info in result["steps"] if info["step"] == 0)
    assert step_0_info["recovered_via_retry"] is True
    assert step_0_info["retry_count"] >= 1


def test_recovered_via_retry_is_false_when_every_check_passes_on_the_first_try():
    condition = _row_visible_condition()
    artifact = _artifact_with_single_step(_find_row_step(condition))
    engine = _make_engine(find_row_result=FakeLocator(count=1))

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert result["status"] == "success"
    assert result["recovered_via_retry"] is False
    assert all(info["recovered_via_retry"] is False and info["retry_count"] == 0 for info in result["steps"])


# -- control ownership --------------------------------------------------------


def _neutral_click_step() -> Step:
    # Uses a target name NOT in DEFAULT_ACTION_RISK_TIERS (unlike _click_step's "delete"),
    # so a successful escalation resume can actually proceed to the click/postcondition/
    # success_condition checks without tripping the guardrail block -- these tests are
    # about ownership state, not guardrails.
    condition = Condition(
        kind=ConditionKind.ELEMENT_VISIBLE,
        description="details link is visible",
        locator=Locator(kind=LocatorKind.ROLE, role="link", name="details", scope="page"),
    )
    return Step(
        order=0,
        type=StepType.CLICK,
        description="click details",
        precondition=condition,
        postcondition=condition,
        locator=condition.locator,
    )


def test_owner_flips_to_human_during_escalation_and_back_to_automation_after_abort():
    step = _neutral_click_step()
    artifact = _artifact_with_single_step(step)
    engine = _make_engine(root=FakeLocator(count=0, visible=False))

    owner_snapshots = []

    class RecordingHandler:
        def handle(self, context):
            owner_snapshots.append(engine.owner)
            return EscalationResolution(resumed=False, note="test abort")

    engine.escalation_handler = RecordingHandler()
    assert engine.owner == ControlOwner.AUTOMATION

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert owner_snapshots == [ControlOwner.HUMAN]
    assert engine.owner == ControlOwner.AUTOMATION
    assert result["owner"] == ControlOwner.AUTOMATION
    assert result["escalated_to_human"] is True


def test_owner_returns_to_automation_and_escalated_flag_stays_true_after_a_successful_resume():
    step = _neutral_click_step()
    artifact = _artifact_with_single_step(step)
    engine = _make_engine(root=FakeLocator(count=0, visible=False))

    class ResumingHandler:
        def __init__(self, root_locator):
            self.root_locator = root_locator

        def handle(self, context):
            # Simulate a human fixing the live page mid-escalation.
            self.root_locator._visible = True
            self.root_locator._count = 1
            return EscalationResolution(resumed=True, note="fixed by human")

    engine.escalation_handler = ResumingHandler(engine.perceiver.root)

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert result["status"] == "success"
    assert result["escalated_to_human"] is True
    assert result["owner"] == ControlOwner.AUTOMATION
    assert engine.perceiver.root.click_calls == 1


def test_run_that_never_escalates_reports_escalated_to_human_false():
    condition = _row_visible_condition()
    artifact = _artifact_with_single_step(_find_row_step(condition))
    engine = _make_engine(find_row_result=FakeLocator(count=1))

    result = engine.run(artifact, {"target_name": "Doe Jason"})

    assert result["status"] == "success"
    assert result["escalated_to_human"] is False
    assert result["owner"] == ControlOwner.AUTOMATION
