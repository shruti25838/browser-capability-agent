# MERIDIAN CORE Adaptation: Write-up

## What this is

I took the computer-use system from my take-home (discover a task once, record it as a typed capability, then replay it deterministically with no model in the loop) and pointed it at MERIDIAN CORE, a legacy credit-union console it had never seen. The goal was to prove the core is real by covering the target's function surface, exposing the capabilities as an API, and wrapping them so anyone can drive the system and watch it work.

The short version: adapting to this target was mostly a configuration and adapter exercise, not a rewrite. Where I did have to change the core, it was to extend the perception layer, and both changes came from the same underlying cause, which I'll explain below.

## What adapting actually took

MERIDIAN is deliberately hostile: 1999-era HTML, table-based layout, no test IDs, unlabeled inputs, and a hidden per-transaction token. My system reads the page through the accessibility tree rather than raw HTML or screenshots, which is what let it survive the messy markup at all. But the legacy structure broke two assumptions I had baked in, and each one was a real fix.

The first was login. The sign-on form's fields ("Operator ID", "Password") have their labels sitting in adjacent table cells with no formal link to the inputs, so the accessibility tree gave those inputs no usable name. My agent could see two text boxes but had no way to tell which was which. I added a fallback to the perceiver that associates an input with the nearest label text when it has no accessible name of its own. It only fires when the normal lookup comes back empty, so nothing changes for forms that are built properly. This is what unblocked login.

The second was reading balances. The shares table has no real column headers, so my existing "find the cell under the Balance header" logic had nothing to anchor to. I added a locator that, once a row is uniquely identified by its content (the share ID), pulls a cell by its position within that row. Position within a content-identified row is stable even when the table has no headers, which is different from absolute row position, which is fragile.

Both problems are the same thing at heart: legacy table markup doesn't expose the semantic structure my accessibility-first approach assumes. The fix in both cases was to extend the perceiver with a text-proximity or position fallback, not to rewrite anything downstream. The artifact schema and replay engine only needed to learn about the new locator kinds, which they pick up through the same generic path as every other locator.

The hidden token was less dramatic than expected. The transfer form carries a hidden `_token` field. In this sample app it happens to be static, but I extract it fresh off the page at replay time rather than hardcoding it, because a real system would rotate it per request. Building for the real behavior rather than the sample's shortcut felt like the right call.

## How I exposed capabilities as an API

Recorded capabilities are served through a small catalog service. It lists every capability, returns each one's typed contract (its parameters and outputs), and invokes one by name with typed arguments. An agent can call a capability without knowing anything about the underlying UI. Under the hood each invocation runs the deterministic replay and returns the same structured result my core already produces: success, a business outcome, or a hard failure. The guardrail allowlist is derived from each artifact's own target URL, so an invocation is automatically scoped to the right host and can't be widened through the HTTP surface. There are auto-generated docs at `/docs`.

One real integration issue surfaced here, and I think it's worth mentioning because I only caught it by testing end to end. My replay uses the synchronous Playwright API, but the catalog is served on an async event loop, and synchronous Playwright refuses to run in a process that has a running event loop. Invoking a capability through the server crashed as a result. I fixed it by running the replay in a separate process and surfacing any crash or timeout back to the caller as a clean hard failure. I verified the fix with a regression test that reproduces the exact event-loop condition rather than just asserting it should work.

## Driving the legacy UI reliably, and handling its exceptional states

Determinism on replay comes from removing the model entirely from the production path. Discovery uses the model once to work out the steps; replay just follows the recorded recipe. Locators resolve by role and name or by content, never by position, so they survive sorting and new rows. When a check fails, I retry the check (not the action) a couple of times in case the page was slow, which avoids ever re-submitting something like a transfer.

The system separates three outcomes deliberately. A legitimate "no" answer (no such member, insufficient funds, a share on hold that can't be debited) is a business outcome, not an error. A genuine break (a missing element, a blocked action, an unexpected state after retries) is a hard failure, reported with what was expected versus what was observed. Everything else that recovers on its own folds back into success. MERIDIAN gave me live examples of all of these: the transfer validation errors, the supervisor-override requirement when a teller attempts a hold, and an injected maintenance interstitial.

## How safety, evidence, and escalation survive the new path

The guardrails, evidence, and escalation all run through the same code whether a capability is invoked from the CLI, the catalog API, or the chatbot. The chatbot never talks to replay directly; it goes through the catalog, so the allowlist and risk tiers still apply. Redaction runs on anything written to logs or evidence, and I redact two ways: by value shape for things like account numbers, and by field name for credentials like passwords and tokens. I actually found and fixed a gap here during my own dry-run, where a password was being logged in plain text because it didn't match any value pattern, only the field name gave it away. The audit log is hash-chained so tampering is detectable, though I'm honest that it's tamper-evident rather than tamper-proof; the production answer is append-only external storage. The pause-and-escalate path keeps a single live session with one clear owner at a time, so control is never ambiguous between the automation and a human.

## Demoability

There's a thin chatbot that turns a plain-language request into a capability invocation, reports the result in plain language, and asks for any missing parameter instead of guessing. And there's an eval dashboard that shows, per capability, how often it succeeded, returned a business outcome, or hard-failed, along with retry counts. I built the dashboard as an evaluation surface on purpose: it's how you'd notice a capability starting to drift or degrade in production, rather than trusting that things still work.

## What I cut, and what I'd do next

I optimized for a thin-but-real version of the whole system with depth on the interesting parts, rather than breadth I couldn't stand behind.

The balance capability's cell locators overfit to the member I recorded them on, so it replays cleanly for that member but not for an arbitrary one. The mechanism to fix it (position-within-row extraction) is built; the remaining work is getting discovery to reliably choose it, or correcting the artifact through the human-review step my design already assumes. I chose a working capability with a documented limitation over chasing a perfect one, since the table's lack of headers made clean extraction genuinely awkward and over-steering discovery made it fail to complete at all.

I don't have a first-class "recoverable" tier. Transient blips that recover on retry fold into success, but I don't yet handle a known interruption like dismissing the maintenance interstitial and continuing. A recovery field on each step is the natural next piece, and MERIDIAN's injected maintenance screen is the exact case to build it against.

My automated tests are all at the unit level, fast and with no live browser or model. I verified the real end-to-end paths through saved evidence runs rather than a flaky browser-driven CI suite. That's a deliberate trade-off; a stable end-to-end suite would be the next addition.

I recorded all seven functions, but the two mandatory ones (balance and transfer) are where I put the depth. Place Account Hold is the one I'd flesh out next, since it triggers the supervisor-override permission case, which is a nice example of two layers of enforcement: my own risk tiering treating the action as sensitive, and the target app itself rejecting a teller, which my taxonomy captures as a permission business outcome.

The through-line I'd leave you with: adapting to a brand-new legacy target came down to extending the perception layer and pointing the same core at a new surface, which is exactly what the two-phase, seam-based design was meant to make possible.
