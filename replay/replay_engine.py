"""ReplayEngine: deterministically replays a saved Artifact against a live page.

No LLM calls anywhere in this module. Perception (Perceiver) and policy
(GuardrailChecker) are reused as-is from discovery -- this module only adds
the replay control flow: {{parameter}} resolution, retry-then-classify
condition checks, and the three-way result contract (success /
business_outcome / hard_failure).

Classification rules (agreed with the assignment author up front):
  - A condition check that raises (Playwright error, bad locator, etc.) is
    always a hard_failure, no matter which step type it happened on --
    that indicates something structurally broken, not an absent record.
  - A condition check that cleanly evaluates to False (no exception) is a
    business_outcome when either: (a) a step declares `outcome_mapping`
    (see agent/artifact.py's OutcomeRule) and one of its rule conditions
    matches current page state, classified under that rule's outcome_name;
    or (b), for backward compatibility with artifacts that declare no
    outcome_mapping, it is a ROW_VISIBLE/ROW_ABSENT check on a find_row step
    (precondition or postcondition) -- "no such record" is a legitimate
    answer there, reported as outcome_name "not_found". Every other
    clean-false condition, on any step type, is a hard_failure.
  - The overall success_condition, checked once after all steps pass,
    follows the same clean-false-is-business_outcome / error-is-hard_failure
    split, since by that point every individual step has already cleared
    its own hard-failure bar.
  - ROW_VISIBLE additionally requires exactly one match to pass -- more than
    one is flagged as an `error` (not a clean False), so an ambiguous
    "which row?" is always a hard_failure, never a silent success or a
    business_outcome, regardless of step type.

Recovery visibility: a condition that only passes after one or two retries
is recorded per-step (recovered_via_retry, retry_count) and rolled up into
the top-level result, so "succeeded cleanly" and "succeeded but only after
a recoverable condition resolved itself" are visibly different outcomes,
not silently identical ones.

Control ownership: ReplayEngine.owner is "automation" except for the exact
window a hard_failure is handed to EscalationHandler, when it's "human" --
surfaced in the final result (`owner`, `escalated_to_human`) and in every
intervention_log.jsonl entry, rather than left to be inferred from log
ordering.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator as PWLocator
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from agent.artifact import (
    Artifact,
    Condition,
    ConditionKind,
    Locator,
    LocatorKind,
    NameMatch,
    OutcomeRule,
    Step,
    StepType,
)
from agent.guardrails import GuardrailChecker, GuardrailViolation
from agent.perceiver import Perceiver

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
CHECK_TIMEOUT_MS = 3000


class ParameterError(Exception):
    """Raised when a {{param}} placeholder has no matching entry in the input params."""


def resolve_placeholders(text: Optional[str], params: dict[str, Any]) -> Optional[str]:
    if text is None:
        return None

    def _sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in params:
            raise ParameterError(f"Missing input parameter {name!r} referenced as {{{{{name}}}}}")
        return str(params[name])

    return PLACEHOLDER_RE.sub(_sub, text)


def resolve_locator(locator: Optional[Locator], params: dict[str, Any]) -> Optional[Locator]:
    if locator is None:
        return None
    return locator.model_copy(update={"name": resolve_placeholders(locator.name, params)})


def resolve_condition(condition: Condition, params: dict[str, Any]) -> Condition:
    return condition.model_copy(
        update={
            "contains_text": resolve_placeholders(condition.contains_text, params),
            "expected_value": resolve_placeholders(condition.expected_value, params),
            "url_pattern": resolve_placeholders(condition.url_pattern, params),
            "locator": resolve_locator(condition.locator, params),
        }
    )


@dataclass
class ConditionCheck:
    passed: bool
    observed: str
    error: Optional[str] = None
    attempts: int = 1  # 1 = passed/failed on the first try; >1 means it only resolved after retries


class ControlOwner:
    """Who is currently driving the live session -- surfaced in the result and
    the intervention log rather than left to be inferred from log ordering."""

    AUTOMATION = "automation"
    HUMAN = "human"


@dataclass
class EscalationContext:
    """Everything EscalationHandler needs to log, screenshot, and re-check --
    without reaching into ReplayEngine internals."""

    artifact: Artifact
    step: Step
    step_index: int
    condition_type: str  # "precondition" | "postcondition"
    condition: Condition
    expected: str
    observed: str
    error: Optional[str]
    page: Page
    outputs: dict[str, Any]
    recheck: Callable[[], ConditionCheck]


@dataclass
class EscalationResolution:
    resumed: bool
    note: str


class ReplayEngine:
    def __init__(
        self,
        page: Page,
        guardrail: GuardrailChecker,
        root_selector: Optional[str] = None,
        escalation_handler: Optional[Any] = None,
        max_condition_retries: int = 2,
        retry_wait_s: float = 1.0,
    ):
        self.page = page
        self.guardrail = guardrail
        self.perceiver = Perceiver(page, root_selector=root_selector)
        self.escalation_handler = escalation_handler
        self.max_condition_retries = max_condition_retries
        self.retry_wait_s = retry_wait_s
        self.outputs: dict[str, Any] = {}
        self._artifact: Optional[Artifact] = None
        self.owner: str = ControlOwner.AUTOMATION
        self._escalated_to_human: bool = False
        self._step_retry_infos: list[dict] = []

    # -- public entry point ---------------------------------------------------

    def run(self, artifact: Artifact, params: dict[str, Any]) -> dict:
        self.outputs = {}
        self._artifact = artifact
        self.owner = ControlOwner.AUTOMATION
        self._escalated_to_human = False
        self._step_retry_infos = []

        result = self._run_inner(artifact, params)

        # Enriched uniformly here rather than at every individual return point inside
        # _run_inner: recovery visibility (part 2 of the taxonomy fix) and control
        # ownership must appear on every result shape (success/business_outcome/
        # hard_failure) without duplicating this logic at each return site.
        result["owner"] = self.owner
        result["escalated_to_human"] = self._escalated_to_human
        result["recovered_via_retry"] = any(info["recovered_via_retry"] for info in self._step_retry_infos)
        result["steps"] = self._step_retry_infos
        return result

    def _run_inner(self, artifact: Artifact, params: dict[str, Any]) -> dict:
        current_row: Optional[PWLocator] = None
        step_index = 0

        while step_index < len(artifact.steps):
            step = artifact.steps[step_index]
            try:
                outcome = self._run_step(step, step_index, params, current_row)
            except ParameterError as e:
                return self._param_hard_failure(step_index, e)

            if outcome["status"] == "advance":
                current_row = outcome["current_row"]
                step_index += 1
                continue

            if outcome["status"] == "business_outcome":
                return {
                    "status": "business_outcome",
                    "reason": outcome["reason"],
                    "outcome_name": outcome["outcome_name"],
                    "step": step_index,
                }

            # hard_failure: give a human a chance to intervene, if wired up.
            if self.escalation_handler is not None:
                resolution = self._escalate(step, step_index, outcome, current_row)
                if resolution.resumed:
                    if outcome["condition_type"] == "precondition":
                        continue  # redo this step from the top
                    current_row = outcome.get("current_row", current_row)
                    step_index += 1
                    continue
                result = dict(outcome["result"])
                result["error"] = f"{result['error']} | human intervention attempted: {resolution.note}"
                return result
            return outcome["result"]

        return self._check_success_condition(artifact, params, current_row)

    # -- per-step execution -----------------------------------------------------

    def _run_step(self, step: Step, step_index: int, params: dict[str, Any], current_row) -> dict:
        resolved_contains_text = resolve_placeholders(step.contains_text, params)
        resolved_input_text = resolve_placeholders(step.input_text, params)
        resolved_locator = resolve_locator(step.locator, params)
        precondition = resolve_condition(step.precondition, params)
        postcondition = resolve_condition(step.postcondition, params)

        pre_check = self._check_with_retries(precondition, current_row)
        if not pre_check.passed:
            self._record_step_retry_info(step_index, pre_check, None)
            return self._classify_failure(step, step_index, "precondition", precondition, pre_check, current_row, params)

        try:
            # Pass the resolved locator's target name too, so a risk tier scoped to a
            # specific target (e.g. "click:delete") is enforced before _perform_action
            # ever calls into Playwright -- not just the coarse action type.
            target_name = resolved_locator.name if resolved_locator is not None else None
            self.guardrail.check(step.type.value, self.page.url, target_name=target_name)
        except GuardrailViolation as e:
            self._record_step_retry_info(step_index, pre_check, None)
            return self._procedural_hard_failure(
                step_index,
                precondition,
                current_row,
                expected=f"action {step.type.value!r} permitted by guardrails",
                observed="action blocked before execution",
                error=str(e),
            )

        try:
            current_row = self._perform_action(
                step, resolved_contains_text, resolved_input_text, resolved_locator, current_row
            )
        except (PlaywrightError, PlaywrightTimeoutError, ValueError) as e:
            self._record_step_retry_info(step_index, pre_check, None)
            return self._procedural_hard_failure(
                step_index,
                precondition,
                current_row,
                expected=step.description,
                observed="exception while performing action",
                error=str(e),
            )

        post_check = self._check_with_retries(postcondition, current_row)
        if not post_check.passed:
            self._record_step_retry_info(step_index, pre_check, post_check)
            return self._classify_failure(step, step_index, "postcondition", postcondition, post_check, current_row, params)

        self._record_step_retry_info(step_index, pre_check, post_check)
        return {"status": "advance", "current_row": current_row}

    def _perform_action(
        self,
        step: Step,
        contains_text: Optional[str],
        input_text: Optional[str],
        locator: Optional[Locator],
        current_row,
    ):
        if step.type == StepType.FIND_ROW:
            if not contains_text:
                raise ValueError("find_row step has no contains_text to search for")
            return self.perceiver.find_row(contains_text)

        if locator is not None and locator.scope == "row" and current_row is None:
            raise ValueError("row-scoped locator used before any find_row step established a row")
        base = current_row if (locator is not None and locator.scope == "row") else self.perceiver.root

        if step.type == StepType.CLICK:
            target = self._resolve_target(locator, base)
            target.click(timeout=CHECK_TIMEOUT_MS)
            return current_row

        if step.type == StepType.TYPE:
            target = self._resolve_target(locator, base)
            target.fill(input_text or "", timeout=CHECK_TIMEOUT_MS)
            return current_row

        if step.type == StepType.EXTRACT:
            target = self._resolve_target(locator, base)
            value = target.inner_text(timeout=CHECK_TIMEOUT_MS).strip()
            if step.output_field:
                self.outputs[step.output_field] = value
            return current_row

        raise ValueError(f"Unsupported step type {step.type!r}")

    def _resolve_target(self, locator: Optional[Locator], base) -> PWLocator:
        if locator is None:
            raise ValueError("step has no locator to act on")
        if locator.kind == LocatorKind.TABLE_CELL_BY_COLUMN:
            idx = self.perceiver.resolve_column_index(locator.column_header)
            return base.get_by_role("cell").nth(idx)
        if locator.kind == LocatorKind.ROLE:
            if locator.name:
                return base.get_by_role(locator.role, name=locator.name, exact=(locator.name_match == NameMatch.EXACT))
            return base.get_by_role(locator.role)
        if locator.kind == LocatorKind.CSS:
            return base.locator(locator.css_selector)
        raise ValueError(f"Unsupported locator kind {locator.kind!r}")

    # -- condition checking -------------------------------------------------

    def _check_with_retries(self, condition: Condition, current_row) -> ConditionCheck:
        check = self._check_condition(condition, current_row)
        attempts = 1
        while not check.passed and attempts <= self.max_condition_retries:
            time.sleep(self.retry_wait_s)
            check = self._check_condition(condition, current_row)
            attempts += 1
        check.attempts = attempts
        return check

    def _check_condition(self, condition: Condition, current_row) -> ConditionCheck:
        try:
            kind = condition.kind

            if kind in (ConditionKind.ELEMENT_VISIBLE, ConditionKind.ELEMENT_NOT_VISIBLE):
                return self._check_element_visibility(condition, current_row, want_visible=(kind == ConditionKind.ELEMENT_VISIBLE))

            if kind in (ConditionKind.ROW_VISIBLE, ConditionKind.ROW_ABSENT):
                text = condition.contains_text or ""
                count = self.perceiver.find_row(text).count()
                observed = f"rows matching {text!r}: {count}"
                if kind == ConditionKind.ROW_VISIBLE:
                    if count > 1:
                        # Which row would replay even act on? Ambiguity here is a real break,
                        # not a legitimate "not found" business answer -- force hard_failure by
                        # setting `error` regardless of step type.
                        return ConditionCheck(
                            False,
                            observed,
                            error=f"ambiguous match: {count} rows matched {text!r}, expected exactly 1",
                        )
                    return ConditionCheck(count == 1, observed)
                return ConditionCheck(count == 0, observed)

            if kind in (ConditionKind.TEXT_EQUALS, ConditionKind.TEXT_CONTAINS):
                return self._check_text(condition, current_row, contains=(kind == ConditionKind.TEXT_CONTAINS))

            if kind == ConditionKind.URL_CONTAINS:
                url = self.page.url
                pattern = condition.url_pattern or ""
                return ConditionCheck(pattern in url, f"url={url!r}")

            if kind == ConditionKind.VALUE_EXTRACTED:
                field = condition.output_field
                value = self.outputs.get(field) if field else None
                if not value:
                    return ConditionCheck(False, f"output_field {field!r} not set")
                return ConditionCheck(
                    self._value_matches_type(value, condition.expected_type),
                    f"{field}={value!r} expected_type={condition.expected_type}",
                )

            return ConditionCheck(False, f"unsupported condition kind {kind!r}", error=f"unsupported kind {kind!r}")
        except (PlaywrightError, PlaywrightTimeoutError, ValueError) as e:
            return ConditionCheck(False, "exception while evaluating condition", error=str(e))

    def _check_element_visibility(self, condition: Condition, current_row, want_visible: bool) -> ConditionCheck:
        if condition.locator is None:
            return ConditionCheck(False, "no locator on condition", error="condition missing locator")
        if condition.locator.scope == "row" and current_row is None:
            return ConditionCheck(False, "row-scoped condition before any find_row", error="no current row")
        base = current_row if condition.locator.scope == "row" else self.perceiver.root
        target = self._resolve_target(condition.locator, base)
        count = target.count()
        visible = count > 0 and target.first.is_visible(timeout=CHECK_TIMEOUT_MS)
        observed = f"count={count} visible={visible}"
        return ConditionCheck(visible if want_visible else not visible, observed)

    def _check_text(self, condition: Condition, current_row, contains: bool) -> ConditionCheck:
        expected = condition.expected_value or ""
        if condition.locator is None:
            # No specific element to read -- this condition is about page content in
            # general (e.g. "an error message is displayed somewhere"), not one element.
            # Fall back to the scoped root's plain visible text instead of demanding a
            # locator that was never declared.
            text_value = self.perceiver.visible_text().strip()
            observed = f"page_text (len={len(text_value)})"
            matched = (expected in text_value) if contains else (text_value == expected)
            return ConditionCheck(matched, observed)
        if condition.locator.scope == "row" and current_row is None:
            return ConditionCheck(False, "row-scoped condition before any find_row", error="no current row")
        base = current_row if condition.locator.scope == "row" else self.perceiver.root
        target = self._resolve_target(condition.locator, base)
        count = target.count()
        if count != 1:
            return ConditionCheck(False, f"expected exactly one element, found {count}")
        text_value = target.first.inner_text(timeout=CHECK_TIMEOUT_MS).strip()
        observed = f"text={text_value!r}"
        return ConditionCheck((expected in text_value) if contains else (text_value == expected), observed)

    @staticmethod
    def _value_matches_type(value: str, expected_type: Optional[str]) -> bool:
        if not value:
            return False
        if expected_type == "number":
            try:
                float(value.replace(",", "").replace("$", ""))
                return True
            except ValueError:
                return False
        if expected_type == "boolean":
            return value.strip().lower() in ("true", "false")
        return True

    # -- failure classification -------------------------------------------------

    def _classify_failure(
        self,
        step: Step,
        step_index: int,
        condition_type: str,
        condition: Condition,
        check: ConditionCheck,
        current_row,
        params: dict[str, Any],
    ) -> dict:
        if check.error is None:
            # Declared outcome rules take precedence: each one is an independently-checked
            # condition against current page state (a single check, no retry -- the step's
            # own condition has already been retried, so the page state has settled by now).
            # The first rule that matches wins; if none match, fall through to the built-in
            # find_row exemption (kept for backward compatibility with artifacts that declare
            # no outcome_mapping) and finally to hard_failure.
            for rule in step.outcome_mapping:
                rule_condition = resolve_condition(rule.condition, params)
                rule_check = self._check_condition(rule_condition, current_row)
                if rule_check.passed:
                    return {
                        "status": "business_outcome",
                        "outcome_name": rule.outcome_name,
                        "reason": f"{rule_condition.description} ({rule_check.observed})",
                    }

            exempt = step.type == StepType.FIND_ROW and condition.kind in (
                ConditionKind.ROW_VISIBLE,
                ConditionKind.ROW_ABSENT,
            )
            if exempt:
                return {
                    "status": "business_outcome",
                    "outcome_name": "not_found",
                    "reason": f"{condition.description} ({check.observed})",
                }
        return {
            "status": "hard_failure",
            "condition_type": condition_type,
            "condition": condition,
            "current_row": current_row,
            "result": {
                "status": "hard_failure",
                "step": step_index,
                "expected": condition.description,
                "observed": check.observed,
                "error": check.error or "condition not met after retries",
            },
        }

    def _procedural_hard_failure(
        self, step_index: int, precondition_for_recheck: Condition, current_row, expected: str, observed: str, error: str
    ) -> dict:
        # Guardrail blocks and action-execution exceptions aren't a condition evaluating
        # false -- they're procedural stops before/while acting. Tag as "precondition" so
        # a successful escalation re-runs the whole step (guardrail check + action again)
        # rather than skipping past an action that never actually happened.
        return {
            "status": "hard_failure",
            "condition_type": "precondition",
            "condition": precondition_for_recheck,
            "current_row": current_row,
            "result": {
                "status": "hard_failure",
                "step": step_index,
                "expected": expected,
                "observed": observed,
                "error": error,
            },
        }

    @staticmethod
    def _param_hard_failure(step_index: int, error: ParameterError) -> dict:
        return {
            "status": "hard_failure",
            "step": step_index,
            "expected": "all {{parameters}} resolvable from the input params",
            "observed": "missing input parameter",
            "error": str(error),
        }

    def _check_success_condition(self, artifact: Artifact, params: dict[str, Any], current_row) -> dict:
        success_condition = resolve_condition(artifact.success_condition, params)
        check = self._check_with_retries(success_condition, current_row)
        self._record_step_retry_info(len(artifact.steps), check, None)
        if check.passed:
            return {"status": "success", "outputs": dict(self.outputs)}
        if check.error:
            return {
                "status": "hard_failure",
                "step": len(artifact.steps),
                "expected": artifact.success_condition.description,
                "observed": check.observed,
                "error": check.error,
            }
        return {
            "status": "business_outcome",
            "outcome_name": "success_condition_not_met",
            "reason": f"success_condition not met: {artifact.success_condition.description} ({check.observed})",
            "step": len(artifact.steps),
        }

    # -- recovery visibility -----------------------------------------------

    def _record_step_retry_info(
        self, step_index: int, first_check: Optional[ConditionCheck], second_check: Optional[ConditionCheck]
    ) -> None:
        checks = [c for c in (first_check, second_check) if c is not None]
        retry_count = sum(max(c.attempts - 1, 0) for c in checks)
        # "Recovered" means a check that ultimately passed needed more than one attempt --
        # a check that exhausted retries and still failed didn't recover, it just failed
        # slower, so it must not count here.
        recovered_via_retry = any(c.passed and c.attempts > 1 for c in checks)
        self._step_retry_infos.append(
            {"step": step_index, "recovered_via_retry": recovered_via_retry, "retry_count": retry_count}
        )

    # -- escalation wiring -------------------------------------------------

    def _escalate(self, step: Step, step_index: int, outcome: dict, current_row) -> EscalationResolution:
        condition = outcome["condition"]
        result = outcome["result"]

        def recheck() -> ConditionCheck:
            return self._check_with_retries(condition, current_row)

        context = EscalationContext(
            artifact=self._artifact,
            step=step,
            step_index=step_index,
            condition_type=outcome["condition_type"],
            condition=condition,
            expected=result["expected"],
            observed=result["observed"],
            error=result.get("error"),
            page=self.page,
            outputs=dict(self.outputs),
            recheck=recheck,
        )

        # Control ownership: "human" for exactly the window EscalationHandler is active
        # (it may block indefinitely on human input), back to "automation" the instant it
        # returns -- regardless of the outcome, since control is back in this loop's hands
        # either way (continue the run, or return a final hard_failure to the caller).
        self.owner = ControlOwner.HUMAN
        self._escalated_to_human = True
        try:
            return self.escalation_handler.handle(context)
        finally:
            self.owner = ControlOwner.AUTOMATION
