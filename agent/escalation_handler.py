"""EscalationHandler: mediates human intervention when replay hits a hard_failure.

Never closes the Playwright page -- the whole point is to keep the live
session open so a real person can take over the same browser window
(headed mode) and click around by hand. This module has no LLM dependency;
the "operator" interface is a blocking prompt, injectable for tests/CI via
the `prompt` constructor argument.

The blocking-wait for a resume/abort answer is deliberately isolated to one
overridable seam, `_obtain_decision` (plus `_no_channel_action`/
`_no_channel_note` for how a missing channel is reported) -- everything else
(record building, screenshot capture, redaction, hash-chained logging,
console summary) is shared code. operator_console/console.py's
WebConsoleEscalationHandler subclasses this and overrides only that seam to
ask over HTTP instead of a terminal prompt, so the audit trail behaves
identically no matter which channel answered.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from agent.audit_log import append_entry
from agent.redaction import redact_mapping, redact_text
from replay.replay_engine import EscalationContext, EscalationResolution

RESUME_PROMPT = "Escalated. Press Enter after manual intervention to resume, or type 'abort': "


def _default_prompt(message: str) -> str:
    return input(message)


class EscalationHandler:
    def __init__(
        self,
        evidence_dir: str = "evidence",
        prompt: Callable[[str], str] = _default_prompt,
        log_path: Optional[str] = None,
    ):
        self.evidence_dir = evidence_dir
        self.prompt = prompt
        self.log_path = log_path or os.path.join(evidence_dir, "intervention_log.jsonl")
        os.makedirs(self.evidence_dir, exist_ok=True)

    def handle(self, context: EscalationContext) -> EscalationResolution:
        record = self._build_record(context)

        # Log that the escalation happened BEFORE blocking on a decision -- so every
        # screenshot taken always has a matching log entry, even if the decision channel
        # never resolves (no interactive terminal, console unreachable, process killed
        # mid-wait, etc.). An orphaned screenshot with no log entry is itself a small bug.
        self._append_log({**record, "human_action": "escalation_triggered", "resumed": None})
        self._print_summary(record)

        answer = self._obtain_decision(context, record)

        if answer is None:
            self._append_log({**record, "human_action": self._no_channel_action(), "resumed": False})
            return EscalationResolution(resumed=False, note=self._no_channel_note())

        if answer == "abort":
            self._append_log({**record, "human_action": "aborted", "resumed": False})
            return EscalationResolution(resumed=False, note="human aborted")

        check = context.recheck()
        self._append_log(
            {
                **record,
                "human_action": "attempted_resume",
                "resumed": check.passed,
                "recheck_observed": redact_text(check.observed),
            }
        )

        if check.passed:
            return EscalationResolution(resumed=True, note="condition passed after human intervention")
        return EscalationResolution(
            resumed=False, note=f"condition still failing after human intervention ({check.observed})"
        )

    # -- the one seam a different decision channel overrides ----------------

    def _obtain_decision(self, context: EscalationContext, record: dict) -> Optional[str]:
        """Block until a human answers "resume" or "abort", or return None if no
        channel was available to ask at all (the caller reports that as a
        graceful hard_failure, never a crash or an indefinite hang)."""
        try:
            return self.prompt(RESUME_PROMPT).strip().lower()
        except EOFError:
            # No interactive terminal available to read from (e.g. run in a non-interactive
            # process/CI) -- treat as "no human available", not a crash.
            return None

    def _no_channel_action(self) -> str:
        return "no_interactive_terminal"

    def _no_channel_note(self) -> str:
        return "escalation triggered but no interactive terminal was available for human input"

    # -- shared record building / logging / display -------------------------

    def _build_record(self, context: EscalationContext) -> dict:
        base_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            # Every entry EscalationHandler writes happens during the window control is
            # (or is about to be) with a human -- explicit here rather than left to be
            # inferred from log ordering, matching ReplayEngine.owner's ControlOwner.HUMAN.
            "owner": "human",
            "goal": context.artifact.metadata.goal,
            "target_url": context.artifact.metadata.target_url,
            "step": context.step_index,
            "step_description": context.step.description,
            "condition_type": context.condition_type,
            "expected": context.expected,
            "observed": context.observed,
            "error": context.error,
            "screenshot": self._take_screenshot(context),
        }
        # Redact incidental sensitive-looking data (SSN-like, credit-card-like, long
        # digit sequences) before anything here gets written to disk, console, or (for
        # the web console channel) published over HTTP. The screenshot path is
        # protected -- it's a file reference, not observed content, and its
        # epoch-timestamp filename would otherwise itself get redacted away.
        return redact_mapping(base_record, protected_keys=frozenset({"screenshot"}))

    def _print_summary(self, record: dict) -> None:
        print("\n=== ESCALATION: human intervention requested ===")
        print(f"Goal:       {record['goal']}")
        print(f"Step {record['step']}: {record['step_description']}")
        print(f"Expected:   {record['expected']}")
        print(f"Observed:   {record['observed']}")
        if record["error"]:
            print(f"Error:      {record['error']}")
        print(f"Screenshot: {record['screenshot']}")
        print(f"Reason stopped: {record['condition_type']} failed after retries")

    def _take_screenshot(self, context: EscalationContext) -> str:
        path = os.path.join(self.evidence_dir, f"escalation_step{context.step_index}_{int(time.time())}.png")
        try:
            context.page.screenshot(path=path)
            return path
        except Exception as e:  # the page itself may be in a bad state -- don't let logging fail the escalation
            return f"<screenshot failed: {e}>"

    def _append_log(self, record: dict) -> None:
        # Hash-chained: each entry links to the previous one (agent/audit_log.py), so
        # a hand-edited or deleted historical line is detectable via verify_chain().
        append_entry(self.log_path, record)
