"""Replay-time dispatch for the LABEL_PROXIMITY locator kind.

Real (headless) Playwright page, not a mock: the thing under test is that
ReplayEngine._resolve_target actually routes a label_proximity locator into
Perceiver.resolve_input_by_label and gets back the right element -- the same
gap that made discovery pass through a legacy login form but crash the
artifact write (kind rejected by the schema) before this fix.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from agent.artifact import Locator, LocatorKind
from agent.guardrails import GuardrailChecker
from replay.replay_engine import ReplayEngine

LEGACY_LOGIN_HTML = """
<html>
<body>
<table>
  <tr><td>Operator ID:</td><td><input type="text" id="f1"></td></tr>
  <tr><td>Password:</td><td><input type="password" id="f2"></td></tr>
</table>
</body>
</html>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    pg.set_content(LEGACY_LOGIN_HTML)
    yield pg
    pg.close()


@pytest.fixture
def engine(page):
    guardrail = GuardrailChecker(allowed_domains=["example.com"], allowed_actions=["type"])
    return ReplayEngine(page, guardrail)


def test_resolve_target_dispatches_label_proximity_to_perceiver(engine):
    locator = Locator(kind=LocatorKind.LABEL_PROXIMITY, role="textbox", name="Operator ID:")

    target = engine._resolve_target(locator, engine.perceiver.root)

    assert target.count() == 1
    assert target.get_attribute("id") == "f1"


def test_resolve_target_label_proximity_distinguishes_labels_in_same_form(engine):
    operator_id = engine._resolve_target(
        Locator(kind=LocatorKind.LABEL_PROXIMITY, role="textbox", name="Operator ID:"), engine.perceiver.root
    )
    password = engine._resolve_target(
        Locator(kind=LocatorKind.LABEL_PROXIMITY, role="textbox", name="Password:"), engine.perceiver.root
    )

    assert operator_id.get_attribute("id") == "f1"
    assert password.get_attribute("id") == "f2"


def test_resolve_target_role_locator_falls_back_to_label_proximity_when_unlabeled(engine):
    # Regression for the MERIDIAN replay-at-login bug: some recorded artifacts have their
    # precondition/postcondition locators saved as ROLE+name for a field that has no
    # accessible name at all (only the step's own action locator was correctly saved as
    # LABEL_PROXIMITY). A plain get_by_role(name=...) lookup for such a field resolves to
    # zero elements -- confirm _resolve_target now falls back to label-proximity resolution
    # instead of failing the precondition/postcondition before the working action ever runs.
    locator = Locator(kind=LocatorKind.ROLE, role="textbox", name="Operator ID:")

    # Sanity check this fixture actually exercises the gap: no accessible name means the
    # underlying role+name lookup alone would find nothing.
    assert engine.perceiver.root.get_by_role("textbox", name="Operator ID:").count() == 0

    target = engine._resolve_target(locator, engine.perceiver.root)

    assert target.count() == 1
    assert target.get_attribute("id") == "f1"


def test_resolve_target_role_locator_stays_empty_when_no_label_match(engine):
    # If the fallback also finds nothing, the original empty role locator must be returned
    # so the caller still sees count=0 and reports honestly, rather than raising.
    locator = Locator(kind=LocatorKind.ROLE, role="textbox", name="Nonexistent Field:")

    target = engine._resolve_target(locator, engine.perceiver.root)

    assert target.count() == 0


def test_perform_type_action_fills_label_proximity_target(engine, page):
    from agent.artifact import Condition, ConditionKind, Step, StepType

    step = Step(
        order=0,
        type=StepType.TYPE,
        description="Type into textbox 'Operator ID:'",
        precondition=Condition(kind=ConditionKind.ELEMENT_VISIBLE, description="d"),
        postcondition=Condition(kind=ConditionKind.ELEMENT_VISIBLE, description="d"),
        locator=Locator(kind=LocatorKind.LABEL_PROXIMITY, role="textbox", name="Operator ID:"),
        input_text="OP42",
    )

    engine._perform_action(step, None, "OP42", step.locator, None)

    assert page.locator("#f1").input_value() == "OP42"
