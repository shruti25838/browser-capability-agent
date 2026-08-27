"""Perceiver: reads the current accessibility-tree state of the page.

This is the only module that turns raw Playwright page state into something
an LLM (or a human debugging a log) can read. It never decides what to do
and never mutates the page -- that's DiscoveryAgent's and ReplayEngine's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from playwright.sync_api import Locator, Page


@dataclass
class PageObservation:
    url: str
    title: str
    accessibility_tree: str


class Perceiver:
    """Wraps a Playwright Page and exposes accessible-tree-first reads.

    `root_selector` scopes every read to one DOM subtree (a CSS fallback
    selector, per the "fall back to DOM selectors only when an element has
    no usable accessible name" rule) -- useful when a page embeds multiple
    structurally identical widgets (e.g. two example tables with the same
    row content) that the accessible tree alone can't tell apart. Row and
    column resolution *within* that root are still purely content-based.
    """

    def __init__(self, page: Page, root_selector: Optional[str] = None):
        self.page = page
        self.root_selector = root_selector
        self.root: Union[Page, Locator] = page.locator(root_selector) if root_selector else page

    def observe(self) -> PageObservation:
        """Snapshot the page (or scoped root) as an accessible-tree (role + name) text tree.

        Uses Playwright's ARIA snapshot (role/name based), the same signal
        get_by_role() resolves against -- so what the LLM reads here is
        exactly what click/type/extract can address. We deliberately never
        take a screenshot or expose pixel coordinates.
        """
        root = self.root if self.root_selector else self.page.locator("body")
        tree = root.aria_snapshot()
        return PageObservation(
            url=self.page.url,
            title=self.page.title(),
            accessibility_tree=tree,
        )

    def visible_text(self) -> str:
        """Plain visible text of the scoped root (or the whole page body).

        Fallback signal for a text_contains/text_equals condition that asserts
        something about page content in general (e.g. a flash message) rather
        than one specific element -- such a condition has no role/name/
        css_selector to build a Locator from, so there's nothing else to read.
        """
        root = self.root if self.root_selector else self.page.locator("body")
        return root.inner_text()

    def find_row(self, contains_text: str):
        """Locate the table row whose content contains `contains_text`.

        Uses Playwright's built-in `has_text` content filter -- never a
        positional selector -- so this survives re-sorting, filtering, and
        new rows being inserted anywhere in the table.
        """
        return self.root.get_by_role("row").filter(has_text=contains_text)

    def resolve_column_index(self, column_header: str) -> int:
        """Find which column a header text corresponds to, by content match.

        This is how "the Due cell in this row" is expressed without ever
        hardcoding a column position: the index is recomputed from the
        current header row every time, so it survives column reordering.
        """
        headers = self.root.get_by_role("columnheader")
        count = headers.count()
        for i in range(count):
            if headers.nth(i).inner_text().strip() == column_header:
                return i
        raise ValueError(f"No column header matching {column_header!r}")
