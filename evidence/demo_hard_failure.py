"""Demo: a replay that deliberately hits a hard_failure (locator that can never match).

Builds a small synthetic artifact (not produced by discovery) whose second
step clicks a link that structurally does not exist in the matched row.
That is a real break, not a "no such record" business outcome, so it must
surface as hard_failure -- and since no --no-escalation flag is used, it
also exercises EscalationHandler end-to-end. The prompt is scripted to
answer "abort" (standing in for an operator who isn't available), so this
demo runs unattended; a real interactive run defaults to a blocking
input() prompt instead.

Run: python evidence/demo_hard_failure.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright  # noqa: E402

from agent.artifact import (  # noqa: E402
    Artifact,
    Condition,
    ConditionKind,
    Locator,
    LocatorKind,
    Metadata,
    NameMatch,
    Parameter,
    Step,
    StepType,
)
from agent.discovery_agent import DEFAULT_ALLOWED_ACTIONS  # noqa: E402
from agent.escalation_handler import EscalationHandler  # noqa: E402
from agent.guardrails import GuardrailChecker  # noqa: E402
from replay.replay_engine import ReplayEngine  # noqa: E402

TARGET_URL = "https://the-internet.herokuapp.com/tables"


def build_artifact() -> Artifact:
    row_visible = Condition(
        kind=ConditionKind.ROW_VISIBLE,
        description="Row for {{person_name}} is visible",
        contains_text="{{person_name}}",
    )
    bogus_link = Locator(
        kind=LocatorKind.ROLE,
        role="link",
        name="Definitely Not A Real Link",
        name_match=NameMatch.EXACT,
        scope="row",
    )
    element_visible = Condition(
        kind=ConditionKind.ELEMENT_VISIBLE,
        description="A link named 'Definitely Not A Real Link' exists in the row",
        locator=bogus_link,
    )
    return Artifact(
        metadata=Metadata(
            goal="deliberately broken demo: click a link that does not exist",
            target_url=TARGET_URL,
            created_at=datetime.now(timezone.utc),
        ),
        parameters=[
            Parameter(name="person_name", type="string", description="Row to select", example_value="Doe Jason")
        ],
        outputs=[],
        steps=[
            Step(
                order=0,
                type=StepType.FIND_ROW,
                description="Find the row containing {{person_name}}",
                precondition=row_visible,
                postcondition=row_visible,
                contains_text="{{person_name}}",
            ),
            Step(
                order=1,
                type=StepType.CLICK,
                description="Click a link that structurally does not exist in this row",
                precondition=element_visible,
                postcondition=element_visible,
                locator=bogus_link,
            ),
        ],
        success_condition=row_visible,
    )


def main() -> int:
    artifact = build_artifact()
    evidence_dir = os.path.dirname(os.path.abspath(__file__))
    artifact_path = os.path.join(evidence_dir, "hard_failure_demo_artifact.json")
    artifact.to_json_file(artifact_path)

    guardrail = GuardrailChecker(
        allowed_domains=["the-internet.herokuapp.com"], allowed_actions=DEFAULT_ALLOWED_ACTIONS
    )
    escalation_handler = EscalationHandler(evidence_dir=evidence_dir, prompt=lambda _msg: "abort")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(artifact.metadata.target_url)
            engine = ReplayEngine(
                page=page,
                guardrail=guardrail,
                root_selector="#table1",
                escalation_handler=escalation_handler,
            )
            result = engine.run(artifact, {"person_name": "Doe Jason"})
        finally:
            browser.close()

    print(json.dumps(result, indent=2))
    out_path = os.path.join(evidence_dir, "replay_result_hard_failure.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    assert result["status"] == "hard_failure", f"expected hard_failure, got {result['status']!r}"
    print(f"\nOK: hard_failure demo behaved as expected (escalation + evidence trail in {evidence_dir}).")
    print(f"Result saved to {out_path}; synthetic artifact saved to {artifact_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
