#!/usr/bin/env python
"""CLI entry point for the replay phase.

Usage:
    python replay.py --artifact artifacts/xxx.json --params '{"target_name": "Doe Jason"}' --headed

Deterministically replays a saved Artifact -- no LLM calls anywhere in this
path. On a hard_failure, EscalationHandler is invoked automatically (this
is the default behavior, not opt-in) so a human can take over the same
live page before the run gives up for good.
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from agent.artifact import Artifact
from agent.discovery_agent import DEFAULT_ALLOWED_ACTIONS
from agent.escalation_handler import EscalationHandler
from agent.guardrails import GuardrailChecker
from agent.redaction import redact_mapping
from replay.replay_engine import ReplayEngine


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a saved artifact deterministically (no LLM calls).")
    parser.add_argument("--artifact", required=True, help="Path to the artifact JSON produced by discovery")
    parser.add_argument("--params", default="{}", help="JSON object of input parameter values")
    parser.add_argument(
        "--allowed-domain",
        action="append",
        dest="allowed_domains",
        help="Additional domain to allow besides the artifact's own target host (repeatable)",
    )
    parser.add_argument(
        "--root-selector",
        default=None,
        help=(
            "CSS fallback selector scoping every read/action to one DOM subtree. Defaults to the "
            "artifact's own scope_selector (recorded at discovery time); pass this to override it."
        ),
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser window instead of running headless")
    parser.add_argument(
        "--allow-confirmed-actions",
        action="store_true",
        help=(
            "Permit actions tagged 'requires_confirmation' in the guardrail's action risk tiers "
            "(e.g. a form submit). Actions tagged 'blocked' are never permitted, flag or not."
        ),
    )
    parser.add_argument(
        "--no-escalation",
        action="store_true",
        help="Disable automatic human escalation on hard_failure (returns hard_failure immediately instead)",
    )
    parser.add_argument("--evidence-dir", default="evidence", help="Directory to write escalation screenshots/logs to")
    args = parser.parse_args()

    artifact = Artifact.from_json_file(args.artifact)
    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as e:
        print(f"--params is not valid JSON: {e}", file=sys.stderr)
        return 2

    target_host = urlparse(artifact.metadata.target_url).hostname
    if not target_host:
        print(f"Could not parse a hostname from artifact target_url {artifact.metadata.target_url!r}", file=sys.stderr)
        return 2

    allowed_domains = [target_host, *(args.allowed_domains or [])]
    guardrail = GuardrailChecker(
        allowed_domains=allowed_domains,
        allowed_actions=DEFAULT_ALLOWED_ACTIONS,
        allow_confirmed_actions=args.allow_confirmed_actions,
    )
    escalation_handler = None if args.no_escalation else EscalationHandler(evidence_dir=args.evidence_dir)
    # The artifact's own scope_selector (recorded at discovery time) is the default source of
    # truth; an explicit --root-selector on this CLI overrides it.
    root_selector = args.root_selector or artifact.scope_selector

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        try:
            page.goto(artifact.metadata.target_url)
            engine = ReplayEngine(
                page=page,
                guardrail=guardrail,
                root_selector=root_selector,
                escalation_handler=escalation_handler,
            )
            result = engine.run(artifact, params)
        finally:
            browser.close()

    # Redact incidental sensitive-looking data from the console print (reason/expected/
    # observed/error may echo page content) -- but never from "outputs", the artifact's
    # own declared, deliberately-returned data (e.g. due_amount), even if it looks numeric.
    print(json.dumps(redact_mapping(result, protected_keys=frozenset({"outputs"})), indent=2))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
