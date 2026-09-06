"""Regression test for find_row's exact-match option.

Real (headless) Playwright page, not a mock: the thing under test is
Playwright's own text/XPath matching semantics, not something a mock could
usefully stand in for. Root cause #1: MERIDIAN share ids are hierarchical
("100234-S0001" is a substring-prefix of "100234-S0001-5", "100234-S0001-6",
etc.), so the default substring `has_text` filter matches every row sharing
that prefix. Root cause #2: legacy nested-table markup (a single-cell wrapper
table per row, used for pixel borders) makes the accessibility "row" role
match both an outer wrapper row and the inner leaf row for what is visually
one row -- an earlier fix using has=get_by_text(exact=True) still matched 2
rows for MERIDIAN's actual table because of this nesting.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from agent.perceiver import Perceiver

SHARES_TABLE_HTML = """
<html>
<body>
<table>
  <tr><td>Share ID</td><td>Type</td><td>Balance</td><td>Status</td></tr>
  <tr><td>100234-S0001</td><td>Regular Shares</td><td>$1,500.00</td><td>HOLD</td></tr>
  <tr><td>100234-S0001-5</td><td>Sub-share</td><td>$10.00</td><td>ACTIVE</td></tr>
  <tr><td>100234-S0001-6</td><td>Sub-share</td><td>$20.00</td><td>ACTIVE</td></tr>
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
def perceiver(page):
    return Perceiver(page)


def test_find_row_default_substring_match_collides_with_suffixed_rows(perceiver):
    # Confirms the fixture actually exercises the collision: the default (exact=False)
    # substring behavior matches the header/label cell plus all three data rows sharing
    # the "100234-S0001" prefix.
    rows = perceiver.find_row("100234-S0001")

    assert rows.count() == 3


def test_find_row_exact_match_selects_only_the_matching_row(perceiver):
    rows = perceiver.find_row("100234-S0001", exact=True)

    assert rows.count() == 1
    assert rows.inner_text().startswith("100234-S0001\t")


def test_find_row_exact_match_excludes_suffixed_rows_individually(perceiver):
    assert perceiver.find_row("100234-S0001-5", exact=True).count() == 1
    assert perceiver.find_row("100234-S0001-6", exact=True).count() == 1
    # Still true that a plain substring search for the base id also matches the -5/-6 rows.
    assert perceiver.find_row("100234-S0001").count() == 3


# -- nested-table markup: outer wrapper row + inner leaf row for one visual row -----

NESTED_ROW_HTML = """
<html>
<body>
<table>
  <tr><td>Share ID</td><td>Type</td><td>Balance</td><td>Status</td></tr>
  <tr>
    <td colspan="4">
      <table>
        <tr><td>100234-S0001</td><td>Regular Shares</td><td>$1,500.00</td><td>HOLD</td></tr>
      </table>
    </td>
  </tr>
  <tr><td>100234-S0001-5</td><td>Sub-share</td><td>$10.00</td><td>ACTIVE</td></tr>
  <tr><td>100234-S0001-6</td><td>Sub-share</td><td>$20.00</td><td>ACTIVE</td></tr>
</table>
</body>
</html>
"""


@pytest.fixture
def nested_row_page(browser):
    pg = browser.new_page()
    pg.set_content(NESTED_ROW_HTML)
    yield pg
    pg.close()


@pytest.fixture
def nested_row_perceiver(nested_row_page):
    return Perceiver(nested_row_page)


def test_find_row_role_matches_both_outer_and_inner_row_for_the_nested_fixture(nested_row_perceiver):
    # Confirms the fixture actually exercises the nesting: the "row" ARIA role matches
    # both the outer wrapper <tr> and the inner <tr> inside its nested table, for what is
    # visually a single row -- this is what made an earlier has=get_by_text(exact=True)
    # implementation match 2 rows instead of 1.
    assert nested_row_perceiver.root.get_by_role("row").count() == 5  # header + outer + inner + 2 suffixed rows


def test_find_row_exact_match_resolves_nested_wrapper_row_to_exactly_one(nested_row_perceiver):
    rows = nested_row_perceiver.find_row("100234-S0001", exact=True)

    assert rows.count() == 1
    assert rows.inner_text().split()[0] == "100234-S0001"


# -- pathological case: every cell individually wrapped in its own 1x1 nested table --
# Exercises the most-cells tie-break directly: this markup shape produces 2 rows whose
# first cell is exactly "100234-S0001" (the real 4-column row and the tiny nested table
# wrapping just its own first cell), unlike NESTED_ROW_HTML above where the first-cell
# predicate alone already narrows it to one match.

PER_CELL_NESTED_HTML = """
<html>
<body>
<table>
  <tr><td>Share ID</td><td>Type</td><td>Balance</td><td>Status</td></tr>
  <tr>
    <td><table><tr><td>100234-S0001</td></tr></table></td>
    <td><table><tr><td>Regular Shares</td></tr></table></td>
    <td><table><tr><td>$1,500.00</td></tr></table></td>
    <td><table><tr><td>HOLD</td></tr></table></td>
  </tr>
  <tr><td>100234-S0001-5</td><td>Sub-share</td><td>$10.00</td><td>ACTIVE</td></tr>
  <tr><td>100234-S0001-6</td><td>Sub-share</td><td>$20.00</td><td>ACTIVE</td></tr>
</table>
</body>
</html>
"""


@pytest.fixture
def per_cell_nested_page(browser):
    pg = browser.new_page()
    pg.set_content(PER_CELL_NESTED_HTML)
    yield pg
    pg.close()


@pytest.fixture
def per_cell_nested_perceiver(per_cell_nested_page):
    return Perceiver(per_cell_nested_page)


def test_find_row_exact_match_collapses_duplicate_first_cell_matches_to_the_fuller_row(per_cell_nested_perceiver):
    rows = per_cell_nested_perceiver.find_row("100234-S0001", exact=True)

    assert rows.count() == 1
    # Must resolve to the real 4-column data row, not the 1-cell nested wrapper table.
    assert rows.locator("xpath=./td|./th").count() == 4
