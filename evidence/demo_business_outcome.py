"""Demo: a replay that deliberately hits a business_outcome (no such record).

Reuses the existing discovery artifact but supplies a person_name that has
no matching row in the table -- find_row's condition (row_visible) never
becomes true after retries, which is a legitimate "not found" answer, not
a system break.

Run: python evidence/demo_business_outcome.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright  # noqa: E402

from agent.artifact import Artifact  # noqa: E402
from agent.discovery_agent import DEFAULT_ALLOWED_ACTIONS  # noqa: E402
from agent.guardrails import GuardrailChecker  # noqa: E402
from replay.replay_engine import ReplayEngine  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACT_PATH = os.path.join(
    REPO_ROOT, "artifacts", "find_the_row_for_jason_doe_and_extract_h_1787179986.json"
)


def main() -> int:
    artifact = Artifact.from_json_file(ARTIFACT_PATH)
    guardrail = GuardrailChecker(
        allowed_domains=["the-internet.herokuapp.com"], allowed_actions=DEFAULT_ALLOWED_ACTIONS
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(artifact.metadata.target_url)
            # #table1/#table2 have identical rows on this page; scope to one so
            # find_row resolves unambiguously, same as discovery would have.
            engine = ReplayEngine(page=page, guardrail=guardrail, root_selector="#table1")
            result = engine.run(artifact, {"person_name": "Nobody Fake"})
        finally:
            browser.close()

    print(json.dumps(result, indent=2))
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay_result_business_outcome.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    assert result["status"] == "business_outcome", f"expected business_outcome, got {result['status']!r}"
    print(f"\nOK: business_outcome demo behaved as expected. Saved to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
