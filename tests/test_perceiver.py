"""Tests for Perceiver's DOM-fallback resolution helpers.

Uses a real (headless) Playwright page rather than mocks, because the thing
under test is Playwright's own role/accessible-name computation -- a mock
would just assert against itself.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from agent.perceiver import Perceiver

LEGACY_LOGIN_HTML = """
<html>
<body>
<table>
  <tr><td>Operator ID:</td><td><input type="text" id="f1"></td></tr>
  <tr><td>Password:</td><td><input type="password" id="f2"></td></tr>
  <tr><td>Branch:</td><td>
    <select id="f3"><option>HQ</option><option>Branch 2</option></select>
  </td></tr>
  <tr><td colspan="2"><input type="submit" value="Sign On"></td></tr>
</table>
</body>
</html>
"""

MODERN_LOGIN_HTML = """
<html>
<body>
<form>
  <label for="user">Operator ID:</label>
  <input type="text" id="user" name="user">
  <label for="pass">Password:</label>
  <input type="password" id="pass" name="pass">
</form>
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
    yield pg
    pg.close()


def test_resolves_input_by_adjacent_label_text_in_legacy_table(page):
    page.set_content(LEGACY_LOGIN_HTML)
    perceiver = Perceiver(page)

    # No <label for>, so the accessible-name lookup a real caller would try
    # first finds nothing -- confirms this fixture actually exercises the gap.
    assert page.get_by_role("textbox", name="Operator ID:").count() == 0

    operator_id = perceiver.resolve_input_by_label("Operator ID:", "textbox")
    assert operator_id.count() == 1
    assert operator_id.get_attribute("id") == "f1"

    password = perceiver.resolve_input_by_label("Password:", "textbox")
    assert password.count() == 1
    assert password.get_attribute("id") == "f2"


def test_resolves_combobox_by_adjacent_label_text_in_legacy_table(page):
    page.set_content(LEGACY_LOGIN_HTML)
    perceiver = Perceiver(page)

    branch = perceiver.resolve_input_by_label("Branch:", "combobox")
    assert branch.count() == 1
    assert branch.get_attribute("id") == "f3"


def test_label_proximity_fallback_not_needed_on_modern_labelled_form(page):
    page.set_content(MODERN_LOGIN_HTML)

    # A modern page with proper <label for> already exposes an accessible
    # name, so ordinary role+name resolution succeeds without the fallback.
    assert page.get_by_role("textbox", name="Operator ID:").count() == 1
