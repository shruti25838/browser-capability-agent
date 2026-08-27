"""Replay-time dispatch for TABLE_CELL_BY_COLUMN with column_index.

Regression for the MERIDIAN balance bug: a share's Balance/Type/Status must
be extracted by position within the row already found by share_id content,
never by matching the literal value being read. Real (headless) Playwright
page, not a mock, to exercise the same find_row -> row-scoped nth(cell) path
replay actually uses.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from agent.artifact import Locator, LocatorKind
from agent.guardrails import GuardrailChecker
from replay.replay_engine import ReplayEngine

SHARES_TABLE_HTML = """
<html>
<body>
<table>
  <tr><td>Share ID</td><td>Type</td><td>Balance</td><td>Status</td></tr>
  <tr><td>100234-S0001</td><td>Regular Shares</td><td>$1,500.00</td><td>HOLD</td></tr>
  <tr><td>100234-S0002</td><td>Christmas Club</td><td>$50.00</td><td>ACTIVE</td></tr>
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
    pg.set_content(SHARES_TABLE_HTML)
    yield pg
    pg.close()


@pytest.fixture
def engine(page):
    guardrail = GuardrailChecker(allowed_domains=["example.com"], allowed_actions=["extract", "find_row"])
    return ReplayEngine(page, guardrail)


def test_resolve_target_uses_column_index_directly_within_found_row(engine):
    row = engine.perceiver.find_row("100234-S0001")
    locator = Locator(kind=LocatorKind.TABLE_CELL_BY_COLUMN, column_index=2, scope="row")

    target = engine._resolve_target(locator, row)

    assert target.inner_text() == "$1,500.00"


def test_column_index_generalizes_across_different_rows(engine):
    # The whole point of the fix: the same locator (column_index=2, "Balance") must
    # resolve correctly for a different row, not just the one recorded during discovery.
    row = engine.perceiver.find_row("100234-S0002")
    locator = Locator(kind=LocatorKind.TABLE_CELL_BY_COLUMN, column_index=2, scope="row")

    target = engine._resolve_target(locator, row)

    assert target.inner_text() == "$50.00"


def test_column_index_reads_type_and_status_from_same_row(engine):
    row = engine.perceiver.find_row("100234-S0001")

    type_cell = engine._resolve_target(Locator(kind=LocatorKind.TABLE_CELL_BY_COLUMN, column_index=1, scope="row"), row)
    status_cell = engine._resolve_target(Locator(kind=LocatorKind.TABLE_CELL_BY_COLUMN, column_index=3, scope="row"), row)

    assert type_cell.inner_text() == "Regular Shares"
    assert status_cell.inner_text() == "HOLD"


def test_column_header_lookup_still_works_when_column_index_absent(engine):
    # Backward compatibility: existing artifacts that use column_header (no column_index
    # at all) must still resolve via the header-text path, unaffected by this addition.
    row = engine.perceiver.find_row("100234-S0001")
    locator = Locator(kind=LocatorKind.TABLE_CELL_BY_COLUMN, column_header="Balance", scope="row")

    target = engine._resolve_target(locator, row)

    assert target.inner_text() == "$1,500.00"
