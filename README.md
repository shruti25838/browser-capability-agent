# browser-capability-agent

A small agent that turns a natural-language goal on a web page into a
reusable, repeatable capability. You point it at a page and describe what
you want done ("find the row for Jason Doe and extract his due amount"),
and an LLM drives a real browser to figure out how. Once it's worked, that
run gets saved as a typed JSON artifact. From then on, running the same
capability again (with different inputs) never touches an LLM again. It just
replays the saved steps against the live page and checks that each one
actually did what it was supposed to.

The point of splitting it this way: figuring out *how* to do something on a
page is a job for a model, but *doing it again reliably* shouldn't depend on
one. The LLM is there once, during discovery. Replay is deterministic,
auditable, and cheap to run as often as you like.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

Discovery calls the Gemini API and needs a key:

1. Grab a free one from [Google AI Studio](https://aistudio.google.com/apikey).
2. Set it as an environment variable:

   ```bash
   export GEMINI_API_KEY="..."          # macOS/Linux
   $env:GEMINI_API_KEY = "..."           # PowerShell
   ```

The default model is `gemini-3.6-flash` (see `MODEL` in
`agent/discovery_agent.py`). You can point it at a different one with
`--model`.

## Discovery: teaching it a new capability

```bash
python discover.py --goal "Find the user named Smith and delete them" --target "https://example.com/users"
```

This opens a browser (headless by default), lets the agent work the page
until the goal is done or it gives up, and writes a JSON artifact to
`artifacts/` if it succeeds. The agent never looks at pixels or a
screenshot. It reasons purely from the page's accessibility tree (roles and
accessible names), so whatever it can "see" is also exactly what it can
click.

A few flags worth knowing:

- `--allowed-domain` (repeatable): extra domains the agent may navigate to,
  beyond the target's own host.
- `--model`: which Gemini model to use.
- `--max-turns` / `--max-seconds`: how many steps or how much wall-clock time
  discovery gets before it stops itself and says so, rather than running
  forever.
- `--headed`: show the actual browser window instead of running headless.
- `--root-selector`: scope every read and action to one part of the page.
  Useful when a page has two structurally identical widgets (say, two
  copies of the same table) and the accessibility tree alone can't tell them
  apart.
- `--allow-confirmed-actions`: permit actions the guardrails have tagged as
  needing explicit confirmation (more on this below).

## Replay: running that capability again

```bash
python replay.py --artifact artifacts/xxx.json --params '{"person_name": "Doe Jason"}' --headed
```

This is the part that never calls an LLM. It resolves the artifact's
`{{parameters}}` against whatever you passed in, walks through the recorded
steps against a live page, and checks each precondition and postcondition
the same way discovery would have. If a page is a little slow to update, a
failed check gets retried a couple of times (about a second apart) before
it's treated as a real failure.

The result is always one of three shapes, not just true or false:

- `{"status": "success", "outputs": {...}}`
- `{"status": "business_outcome", "outcome_name": "...", "reason": "...", "step": N}`,
  meaning the run completed cleanly but landed on a legitimate answer other
  than success (no matching row, a permission denial, whatever the artifact
  was told to recognize). Not a bug, just a real-world outcome.
- `{"status": "hard_failure", "step": N, "expected": "...", "observed": "...", "error": "..."}`,
  meaning something is actually broken: a missing element, a guardrail
  block, an exception.

Every result also reports whether anything needed a retry to succeed
(`recovered_via_retry`, and per-step detail under `steps`) and who was
driving at the end (`owner`, `escalated_to_human`). That way "it worked, no
issues" and "it worked, but only after retrying" or "a human had to step in"
never look identical.

When something does hit `hard_failure`, the escalation handler takes over
automatically: it screenshots the page, logs what happened, and waits for a
person to either fix the live page and say go, or give up. It never closes
the browser while it's waiting, so someone can genuinely reach in and click
around by hand if needed. Pass `--no-escalation` if you'd rather it just
report the failure right away.

## What keeps this safe to run

A few things sit between "the agent decided to do something" and "that
thing actually happens":

- **Domain and action allowlists.** Every action, in both discovery and
  replay, is checked against an explicit list of permitted domains and
  action types before it runs.
- **Per-action risk tiers.** Beyond the coarse allowlist, individual targets
  can be tagged `safe`, `requires_confirmation`, or `blocked`. A "delete"
  link, for instance, is blocked outright, and no flag can override that.
  `requires_confirmation` actions need `--allow-confirmed-actions` to run at
  all. This is enforced independently of anything the model decides, so even
  a model that got talked into attempting something dangerous still can't
  get past it.
- **Step and time budgets.** Discovery stops itself, cleanly, if it runs out
  of turns or wall-clock time, instead of looping forever.
- **Redaction.** Anything that looks like an SSN, a credit card number, or a
  long account-style digit string gets scrubbed out of logs and console
  output before it's written anywhere. The one thing that's never touched is
  an artifact's own declared output values, since those are the actual data
  the capability was built to return.
- **A tamper-evident log.** The intervention log is hash-chained, so if a
  line in it ever gets edited or deleted by hand, that's detectable.
- **Untrusted page content.** Everything read off a page is wrapped and
  labeled clearly as observed data, not instructions, when it's handed to
  the model. The real defense against a page trying to talk the agent into
  something bad isn't the wording of a prompt, though. It's the allowlist
  above, which doesn't care what the model was told or why it decided
  something.

## How it's laid out

- `agent/perceiver.py`: reads the live page as an accessibility tree. This is
  the only thing that knows how to look at a page at all.
- `agent/tools.py`: the tool schemas offered to the model (`find_row`,
  `click`, `type`, `extract`, `finish`), each one requiring a stated
  precondition and postcondition up front.
- `agent/guardrails.py`: the allowlist and risk-tier checks described above.
- `agent/discovery_agent.py`: the actual observe, decide, act loop against
  Gemini.
- `agent/artifact_writer.py` / `agent/artifact.py`: turn a successful
  discovery run into the typed, versioned artifact that replay consumes.
  Literal values typed in become named parameters; values read out become
  typed outputs, never hardcoded into a later check.
- `replay/replay_engine.py`: the LLM-free replay loop and its three-way
  result contract.
- `agent/escalation_handler.py`: the human hand-off on `hard_failure`.
- `agent/redaction.py`, `agent/audit_log.py`: the log-scrubbing and
  hash-chaining described above.

## Tests

```bash
pytest -v
```

The suite is unit-level and doesn't spin up a browser or call an LLM, so it
runs in a couple of seconds. It covers the artifact schema, the guardrails,
parameter resolution, and (the part I'd call most important) the exact
rules for when something counts as a business outcome versus a hard
failure. A couple of real, saved end-to-end runs (a successful replay, one
that hits a business outcome, one that hits a hard failure and escalates)
live in `evidence/` instead, since running a live browser and LLM in CI
isn't worth the flakiness.
