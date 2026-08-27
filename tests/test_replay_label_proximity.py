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
