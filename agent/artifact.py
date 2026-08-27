"""Typed artifact schema produced by discovery and consumed by replay.

An Artifact is the sole hand-off point between the two phases described in
the assignment: DiscoveryAgent's only output is an Artifact; ReplayEngine's
only input (besides runtime parameter values) is an Artifact. Neither phase
reaches into the other's internals.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ConditionKind(str, Enum):
    """The vocabulary of checkable facts a precondition/postcondition can assert.

    Kept as a closed enum (rather than free text) so ReplayEngine can later
    dispatch on `kind` programmatically instead of parsing natural language.
    """

    ELEMENT_VISIBLE = "element_visible"
    ELEMENT_NOT_VISIBLE = "element_not_visible"
    ROW_VISIBLE = "row_visible"
    ROW_ABSENT = "row_absent"
    TEXT_EQUALS = "text_equals"
    TEXT_CONTAINS = "text_contains"
    URL_CONTAINS = "url_contains"
    VALUE_EXTRACTED = "value_extracted"


class LocatorKind(str, Enum):
    ROLE = "role"                        # accessible role + accessible name (primary signal)
    TABLE_CELL_BY_COLUMN = "table_cell_by_column"  # cell within a scoped row, found via column header text
    CSS = "css"                          # DOM selector fallback for elements with no accessible name
    LABEL_PROXIMITY = "label_proximity"  # input with no accessible name, found via adjacent label text
    # ROLE fields do double duty here: `role` is the target's role, `name` is the label
    # text to match by proximity (not an accessible name -- there isn't one).


class NameMatch(str, Enum):
    EXACT = "exact"
    CONTAINS = "contains"


class Locator(BaseModel):
    """How to find one element. Accessibility tree first; CSS is a last resort.

    `scope` records whether this locator is resolved against the whole page
    or against the row most recently established by a find_row step -- this
    is what lets replay re-derive "the delete link in THIS row" instead of
    ever addressing a row by position.
    """

    kind: LocatorKind
    role: Optional[str] = None
    name: Optional[str] = None
    name_match: NameMatch = NameMatch.EXACT
    column_header: Optional[str] = None
    column_index: Optional[int] = Field(
        default=None,
        description=(
            "0-based cell position within the row scoped by the most recent find_row. "
            "Only safe because the row itself was already located by content, never by "
            "position -- use only when there's no reliable column header text to match by "
            "name instead (e.g. a legacy table with no <th>/header row at all)."
        ),
    )
    css_selector: Optional[str] = None
    scope: str = Field(default="page", description="'page' or 'row' (most recent find_row)")


class Condition(BaseModel):
    """A precondition or postcondition: a single checkable fact about page state."""

    kind: ConditionKind
    description: str = Field(description="Human-readable statement of what must be true")
    locator: Optional[Locator] = None
    contains_text: Optional[str] = None
    expected_value: Optional[str] = None
    url_pattern: Optional[str] = None
    output_field: Optional[str] = Field(
        default=None,
        description="For VALUE_EXTRACTED: name of the output field that must hold a non-empty value",
    )
    expected_type: Optional[str] = Field(
        default=None, description="For VALUE_EXTRACTED: the type (string | number | boolean) the value must match"
    )


class StepType(str, Enum):
    FIND_ROW = "find_row"
    CLICK = "click"
    TYPE = "type"
    EXTRACT = "extract"


class OutcomeRule(BaseModel):
    """A named business-outcome branch for one step.

    If this step's own precondition/postcondition check comes back cleanly
    false (no exception) after retries, ReplayEngine checks each declared
    OutcomeRule's `condition` against current page state; the first one that
    matches classifies the result as business_outcome with this outcome_name,
    instead of falling through to hard_failure. Lets a step distinguish
    multiple distinct business outcomes (e.g. "permission_denied" vs.
    "validation_error" vs. "unexpected_dialog") rather than only the single
    built-in find_row "not found" case.
    """

    outcome_name: str = Field(description="Name of this business outcome, e.g. 'permission_denied'")
    condition: Condition = Field(description="Checked against current page state to identify this outcome")


class Step(BaseModel):
    """One ordered unit of work: precondition -> action -> postcondition.

    Replay must check `precondition` before acting, perform the action
    described by `type`/`locator`/action-specific fields, then verify
    `postcondition` before moving on. A postcondition failure is a detected
    replay failure, not a crash.
    """

    order: int
    type: StepType
    description: str
    precondition: Condition
    postcondition: Condition

    locator: Optional[Locator] = None            # target of click / type / extract
    contains_text: Optional[str] = None           # find_row: text the row must contain (may be "{{param}}")
    input_text: Optional[str] = None              # type: literal or "{{param}}" text to enter
    output_field: Optional[str] = None            # extract: name of the output this step populates
    outcome_mapping: list[OutcomeRule] = Field(
        default_factory=list,
        description=(
            "Named business-outcome branches for this step's clean-false precondition/"
            "postcondition failures. Empty (the default) means no declared named outcomes -- "
            "replay falls back to its existing default classification (find_row's built-in "
            "row_visible/row_absent exemption, else hard_failure)."
        ),
    )


class Parameter(BaseModel):
    """A discovery-time literal value generalized into a named replay input."""

    name: str
    type: str = Field(description="string | number | boolean")
    description: str
    example_value: str = Field(description="The concrete value observed during discovery")


class OutputField(BaseModel):
    """A named, typed value the flow extracts and returns."""

    name: str
    type: str = Field(description="string | number | boolean")
    description: str


class Metadata(BaseModel):
    goal: str
    target_url: str
    created_at: datetime


class Artifact(BaseModel):
    version: str = "1.0.0"
    metadata: Metadata
    scope_selector: Optional[str] = Field(
        default=None,
        description=(
            "CSS fallback selector scoping every read/action to one DOM subtree, if discovery "
            "was run with --root-selector. Replay uses this automatically so the same selector "
            "doesn't have to be passed by hand; an explicit --root-selector at replay time still "
            "overrides it."
        ),
    )
    parameters: list[Parameter]
    outputs: list[OutputField]
    steps: list[Step]
    success_condition: Condition = Field(
        description="Overall checkpoint for the whole flow, checked after the last step"
    )

    def to_json_file(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))

    @classmethod
    def from_json_file(cls, path: str) -> "Artifact":
        with open(path, encoding="utf-8") as f:
            return cls.model_validate_json(f.read())
