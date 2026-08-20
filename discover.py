#!/usr/bin/env python
"""CLI entry point for the discovery phase.

Usage:
    python discover.py --goal "..." --target "https://..."

Drives a live browser with an LLM loop until the goal is met (or shown
impossible), then writes the resulting artifact to artifacts/*.json.
No replay logic lives here or anywhere it imports.
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

from agent.artifact_writer import ArtifactWriter
from agent.discovery_agent import DEFAULT_ALLOWED_ACTIONS, MODEL, DiscoveryAgent, DiscoveryStatus
from agent.guardrails import GuardrailChecker
from agent.redaction import redact_text


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a discovery session and write a replay artifact.")
    parser.add_argument("--goal", required=True, help="Natural-language goal for the agent to achieve")
    parser.add_argument("--target", required=True, help="Target URL to start from")
    parser.add_argument(
        "--allowed-domain",
        action="append",
        dest="allowed_domains",
        help="Additional domain to allow besides the target's own host (repeatable)",
    )
    parser.add_argument("--model", default=MODEL, help=f"Gemini model id (default: {MODEL})")
    parser.add_argument("--max-turns", type=int, default=25, help="Max LLM turns (steps) before aborting")
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=300.0,
        help="Wall-clock budget in seconds before aborting (default: 300). Pass 0 or a negative "
        "value to disable the wall-clock limit and rely on --max-turns alone.",
    )
    parser.add_argument("--output-dir", default="artifacts", help="Directory to write the artifact JSON to")
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
        "--root-selector",
        default=None,
        help=(
            "CSS fallback selector scoping every read/action to one DOM subtree. Use this when the page "
            "embeds multiple structurally identical widgets the accessible tree alone can't tell apart "
            "(e.g. two example tables with the same row content) -- row/column resolution within that "
            "root stays purely content-based."
        ),
    )
    args = parser.parse_args()

    target_host = urlparse(args.target).hostname
    if not target_host:
        print(f"Could not parse a hostname from --target {args.target!r}", file=sys.stderr)
        return 2

    allowed_domains = [target_host, *(args.allowed_domains or [])]
    guardrail = GuardrailChecker(
        allowed_domains=allowed_domains,
        allowed_actions=DEFAULT_ALLOWED_ACTIONS,
        allow_confirmed_actions=args.allow_confirmed_actions,
    )

    max_seconds = args.max_seconds if args.max_seconds > 0 else None
    agent = DiscoveryAgent(
        goal=args.goal,
        target_url=args.target,
        guardrail=guardrail,
        model=args.model,
        max_turns=args.max_turns,
        max_seconds=max_seconds,
        headless=not args.headed,
        root_selector=args.root_selector,
    )

    print(f"Starting discovery: goal={args.goal!r} target={args.target!r} domains={allowed_domains}")
    run = agent.run()

    # run.reason is free text that may echo model-observed page content -- redact
    # incidental sensitive-looking data before it ever hits the console. This is
    # discovery's own narrative about what happened, not a declared typed output.
    reason = redact_text(run.reason)

    if not run.success:
        if run.status == DiscoveryStatus.BUDGET_EXCEEDED:
            print(f"Discovery stopped: ran out of step/time budget -- {reason}", file=sys.stderr)
        else:
            print(f"Discovery did not succeed: {reason}", file=sys.stderr)
        return 1

    path = ArtifactWriter().write(run, args.output_dir, scope_selector=args.root_selector)
    print(f"Discovery succeeded: {reason}")
    print(f"Artifact written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
