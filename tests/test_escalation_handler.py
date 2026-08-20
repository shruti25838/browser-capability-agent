"""Tests for agent/escalation_handler.py -- no live browser, no LLM.

Regression coverage for a real bug found during testing: EscalationHandler's
default input() prompt crashed with an unhandled EOFError when no
interactive terminal was available to read from (e.g. a non-interactive
process), instead of degrading gracefully. That crash also meant a
screenshot could get taken with no matching intervention_log.jsonl entry,
since the log write was originally ordered *after* the prompt call.

These tests use a stub Page (just enough surface for .screenshot()) and a
tmp_path evidence dir -- no Playwright browser, no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.artifact import Artifact, Condition, ConditionKind, Metadata, Parameter, Step, StepType
from agent.audit_log import verify_chain
from agent.escalation_handler import EscalationHandler
from replay.replay_engine import ConditionCheck, EscalationContext


class StubPage:
    def __init__(self):
        self.screenshot_calls = 0

    def screenshot(self, path: str) -> None:
        self.screenshot_calls += 1
        with open(path, "wb") as f:
            f.write(b"fake-png-bytes")


def _make_context(recheck=None, observed: str = "count=0 visible=False", goal: str = "test goal") -> EscalationContext:
    condition = Condition(kind=ConditionKind.ELEMENT_VISIBLE, description="link visible")
    step = Step(
        order=1,
        type=StepType.CLICK,
        description="click a link",
        precondition=condition,
        postcondition=condition,
    )
    artifact = Artifact(
        metadata=Metadata(goal=goal, target_url="https://example.com", created_at=datetime.now(timezone.utc)),
        parameters=[Parameter(name="p", type="string", description="d", example_value="v")],
        outputs=[],
        steps=[step],
        success_condition=condition,
    )
    return EscalationContext(
        artifact=artifact,
        step=step,
        step_index=1,
        condition_type="precondition",
        condition=condition,
        expected="link visible",
        observed=observed,
        error="condition not met after retries",
        page=StubPage(),
        outputs={},
        recheck=recheck or (lambda: ConditionCheck(passed=False, observed="still not visible")),
    )


def _raise_eof(_message: str) -> str:
    raise EOFError("no interactive stdin")


def test_eof_error_during_prompt_returns_graceful_resolution_not_a_crash(tmp_path):
    context = _make_context()
    handler = EscalationHandler(evidence_dir=str(tmp_path), prompt=_raise_eof)

    # Must not raise -- this is the crash being regression-tested.
    resolution = handler.handle(context)

    assert resolution.resumed is False
    assert "no interactive terminal" in resolution.note


def test_screenshot_always_has_a_matching_log_entry_even_when_prompt_raises_eof(tmp_path):
    context = _make_context()
    handler = EscalationHandler(evidence_dir=str(tmp_path), prompt=_raise_eof)

    handler.handle(context)

    assert context.page.screenshot_calls == 1
    lines = (tmp_path / "intervention_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    # The escalation-triggered entry must exist and reference a real screenshot path --
    # this is the ordering guarantee: log-before-prompt, so nothing is ever orphaned.
    assert len(lines) >= 1
    triggered = json.loads(lines[0])
    assert triggered["human_action"] == "escalation_triggered"
    assert triggered["screenshot"]
    # And there's a second entry recording how the EOFError was handled.
    outcome = json.loads(lines[-1])
    assert outcome["human_action"] == "no_interactive_terminal"
    assert outcome["resumed"] is False


def test_normal_abort_flow_still_works_after_the_eof_fix(tmp_path):
    context = _make_context()
    handler = EscalationHandler(evidence_dir=str(tmp_path), prompt=lambda _msg: "abort")

    resolution = handler.handle(context)

    assert resolution.resumed is False
    assert resolution.note == "human aborted"
    lines = (tmp_path / "intervention_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    human_actions = [json.loads(line)["human_action"] for line in lines]
    assert human_actions == ["escalation_triggered", "aborted"]


def test_successful_resume_flow_still_works_after_the_eof_fix(tmp_path):
    context = _make_context(recheck=lambda: ConditionCheck(passed=True, observed="now visible"))
    handler = EscalationHandler(evidence_dir=str(tmp_path), prompt=lambda _msg: "")

    resolution = handler.handle(context)

    assert resolution.resumed is True
    lines = (tmp_path / "intervention_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    human_actions = [json.loads(line)["human_action"] for line in lines]
    assert human_actions == ["escalation_triggered", "attempted_resume"]


# -- redaction ---------------------------------------------------------------


def test_sensitive_looking_observed_text_is_redacted_before_logging(tmp_path):
    context = _make_context(observed="page showed account SSN 123-45-6789 in the row")
    handler = EscalationHandler(evidence_dir=str(tmp_path), prompt=lambda _msg: "abort")

    handler.handle(context)

    lines = (tmp_path / "intervention_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    for line in lines:
        assert "123-45-6789" not in line
    assert "[REDACTED]" in lines[0]


def test_sensitive_looking_observed_text_is_redacted_in_recheck_observed_too(tmp_path):
    context = _make_context(recheck=lambda: ConditionCheck(passed=False, observed="still shows SSN 123-45-6789"))
    handler = EscalationHandler(evidence_dir=str(tmp_path), prompt=lambda _msg: "")

    handler.handle(context)

    lines = (tmp_path / "intervention_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    outcome = json.loads(lines[-1])
    assert "123-45-6789" not in outcome["recheck_observed"]


def test_ordinary_observed_text_is_unaffected_by_redaction(tmp_path):
    context = _make_context(observed="count=0 visible=False")
    handler = EscalationHandler(evidence_dir=str(tmp_path), prompt=lambda _msg: "abort")

    handler.handle(context)

    lines = (tmp_path / "intervention_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    triggered = json.loads(lines[0])
    assert triggered["observed"] == "count=0 visible=False"


# -- audit log hash chaining -------------------------------------------------


def test_intervention_log_forms_a_valid_hash_chain_across_multiple_escalations(tmp_path):
    handler = EscalationHandler(evidence_dir=str(tmp_path), prompt=lambda _msg: "abort")

    # Two separate escalations -> at least four log entries (triggered+aborted each).
    handler.handle(_make_context())
    handler.handle(_make_context())

    result = verify_chain(str(tmp_path / "intervention_log.jsonl"))

    assert result.valid is True
    assert result.entry_count == 4


def test_screenshot_path_survives_redaction_unredacted(tmp_path):
    # The screenshot filename embeds a 10-digit epoch timestamp -- without the
    # "screenshot" protected key, the long-digit-sequence rule would redact it away
    # and the log entry would no longer point at a real file.
    context = _make_context()
    handler = EscalationHandler(evidence_dir=str(tmp_path), prompt=lambda _msg: "abort")

    handler.handle(context)

    lines = (tmp_path / "intervention_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    triggered = json.loads(lines[0])
    assert "[REDACTED]" not in triggered["screenshot"]
    assert triggered["screenshot"].endswith(".png")


def test_every_log_entry_is_explicitly_tagged_with_owner_human(tmp_path):
    # Control ownership must be visible in the log itself, not just inferable from
    # entry ordering -- every entry EscalationHandler writes happens during the window
    # control is (or is about to be) with a human.
    context = _make_context(recheck=lambda: ConditionCheck(passed=True, observed="now visible"))
    handler = EscalationHandler(evidence_dir=str(tmp_path), prompt=lambda _msg: "")

    handler.handle(context)

    lines = (tmp_path / "intervention_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["owner"] == "human" for line in lines)
