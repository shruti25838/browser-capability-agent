"""Regression test for _check_text reading form-field values.

Real (headless) Playwright page, not a mock: the thing under test is that
Playwright's own input_value()/inner_text() behave differently for a form
field vs. a plain text element, which a mock would just assert against
itself. Root cause: target.first.inner_text() reads an <input>'s rendered
text, not its `value` property, so a text_contains/text_equals postcondition
on a filled input always observed an empty string and failed -- even for
MERIDIAN's login/search fields, which are exactly such inputs.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from agent.artifact import Condition, ConditionKind, Locator, LocatorKind
from agent.guardrails import GuardrailChecker
from replay.replay_engine import ReplayEngine

HTML = """
<html>
<body>
  <input type="text" id="operator_id" value="teller1">
  <div id="message">Welcome teller1</div>
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
    pg.set_content(HTML)
    yield pg
    pg.close()


@pytest.fixture
def engine(page):
    guardrail = GuardrailChecker(allowed_domains=["example.com"], allowed_actions=["type"])
    return ReplayEngine(page, guardrail)


def test_check_text_reads_input_value_for_a_filled_form_field(engine):
    condition = Condition(
        kind=ConditionKind.TEXT_CONTAINS,
        description="Operator ID input contains teller1",
        locator=Locator(kind=LocatorKind.CSS, css_selector="#operator_id"),
        expected_value="teller1",
    )

    check = engine._check_condition(condition, current_row=None)

    assert check.passed is True
    assert check.observed == "text='teller1'"


def test_check_text_still_falls_back_to_inner_text_for_a_non_input_element(engine):
    condition = Condition(
        kind=ConditionKind.TEXT_CONTAINS,
        description="Welcome message shown",
        locator=Locator(kind=LocatorKind.CSS, css_selector="#message"),
        expected_value="Welcome teller1",
    )

    check = engine._check_condition(condition, current_row=None)

    assert check.passed is True
    assert check.observed == "text='Welcome teller1'"


# -- page-text fallback when the locator points near, but not at, the match ---

RESULTS_HTML = """
<html>
<body>
  <h1 id="heading">MEMBER INQUIRY / SELECTION</h1>
  <table>
    <tr><td>100234</td><td>Jason Doe</td></tr>
  </table>
</body>
</html>
"""


@pytest.fixture
def results_page(browser):
    pg = browser.new_page()
    pg.set_content(RESULTS_HTML)
    yield pg
    pg.close()


@pytest.fixture
def results_engine(results_page):
    guardrail = GuardrailChecker(allowed_domains=["example.com"], allowed_actions=["type"])
    return ReplayEngine(results_page, guardrail)


def test_check_text_falls_back_to_page_text_when_locator_element_lacks_expected_text(results_engine):
    # Regression: discovery recorded this postcondition's locator against the page heading,
    # not the results table -- the expected member number is present elsewhere on the page.
    # The single-element read must miss, but the page-text fallback must still find it.
    condition = Condition(
        kind=ConditionKind.TEXT_CONTAINS,
        description="Page shows results containing 100234",
        locator=Locator(kind=LocatorKind.CSS, css_selector="#heading"),
        expected_value="100234",
    )

    check = results_engine._check_condition(condition, current_row=None)

    assert check.passed is True
    assert check.observed.startswith("page_text")


def test_check_text_page_text_fallback_still_fails_when_expected_text_is_nowhere_on_page(results_engine):
    condition = Condition(
        kind=ConditionKind.TEXT_CONTAINS,
        description="Page shows results containing a number that isn't there",
        locator=Locator(kind=LocatorKind.CSS, css_selector="#heading"),
        expected_value="999999",
    )

    check = results_engine._check_condition(condition, current_row=None)

    assert check.passed is False
    assert check.observed == "text='MEMBER INQUIRY / SELECTION'"
