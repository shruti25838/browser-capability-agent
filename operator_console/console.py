"""A minimal, real web operator console -- a replacement INPUT CHANNEL for
EscalationHandler's resume/abort decision, nothing more.

Architecture (kept deliberately narrow):

  - The Playwright page/browser NEVER leaves the replay process. This module
    starts a small Flask app in a background thread of that SAME process
    (OperatorConsoleServer) -- it does not move browser control anywhere.
  - EscalationHandler still owns every audit-log write, screenshot capture,
    and redaction step, unchanged (see agent/escalation_handler.py).
    WebConsoleEscalationHandler below only overrides the one seam that
    decides HOW a human's answer is obtained (`_obtain_decision`): instead
    of blocking on `input()`, it publishes the intervention record to the
    console over HTTP (OperatorConsoleClient.publish), then polls a decision
    endpoint until the console reports "resume" or "abort" -- same blocking-
    wait semantics as the terminal prompt, just a different channel.
  - A human opens the console's URL in a browser, sees the goal/step/
    expected-vs-observed/screenshot, and can still reach into the actual
    open Playwright window by hand before clicking Resume -- exactly like
    the terminal flow, just with a page instead of a keyboard prompt.
  - If the console can't be reached at all (couldn't publish, or polling
    fails), `_obtain_decision` returns None -- the same "no channel
    available" signal EscalationHandler already uses for a terminal EOFError
    -- so it degrades to a hard_failure instead of hanging or crashing.
"""

from __future__ import annotations

import html
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import requests
from flask import Flask, abort, jsonify, redirect, request, send_file
from werkzeug.serving import make_server

from agent.escalation_handler import EscalationHandler
from replay.replay_engine import EscalationContext


class OperatorConsoleUnavailable(Exception):
    """Raised when the operator console can't be reached -- publish or poll failed."""


# -- in-memory intervention store, shared by every request in the process ---


@dataclass
class _Intervention:
    id: str
    record: dict
    decision: Optional[str] = None


class _ConsoleState:
    """Thread-safe store of published interventions. One instance per
    OperatorConsoleServer -- there's normally exactly one pending
    intervention at a time (replay blocks on it), but nothing here assumes
    that beyond `current()` picking the most recently published undecided
    one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._interventions: dict[str, _Intervention] = {}
        self._order: list[str] = []

    def publish(self, record: dict) -> str:
        intervention_id = uuid.uuid4().hex
        with self._lock:
            self._interventions[intervention_id] = _Intervention(id=intervention_id, record=record)
            self._order.append(intervention_id)
        return intervention_id

    def get(self, intervention_id: str) -> Optional[_Intervention]:
        with self._lock:
            return self._interventions.get(intervention_id)

    def current(self) -> Optional[_Intervention]:
        with self._lock:
            for intervention_id in reversed(self._order):
                iv = self._interventions[intervention_id]
                if iv.decision is None:
                    return iv
        return None

    def decide(self, intervention_id: str, decision: str) -> bool:
        with self._lock:
            iv = self._interventions.get(intervention_id)
            if iv is None:
                return False
            iv.decision = decision
            return True


def _intervention_to_json(iv: _Intervention) -> dict:
    return {"id": iv.id, "decision": iv.decision, **iv.record}


# -- decorative glyphs (inline SVG, no external assets) ----------------------
# Purely presentational, unbranded shapes -- no wordmark/logo, just the small
# colored icon tiles the dashboard uses next to the title and each section
# label. Kept as plain module-level strings so dashboard() only interpolates
# them, never builds markup from data.

_ICON_HERO = (
    '<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<circle cx="10" cy="10" r="7.25" stroke="white" stroke-width="1.6" opacity="0.9"/>'
    '<circle cx="10" cy="10" r="2.75" fill="white"/>'
    "</svg>"
)

_ICON_LIST = (
    '<svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<rect x="2" y="3" width="12" height="1.6" rx="0.8" fill="white"/>'
    '<rect x="2" y="7.2" width="12" height="1.6" rx="0.8" fill="white"/>'
    '<rect x="2" y="11.4" width="8" height="1.6" rx="0.8" fill="white"/>'
    "</svg>"
)

_ICON_COMPARE = (
    '<svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<path d="M2 5H12M12 5L9 2M12 5L9 8" stroke="white" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '<path d="M14 11H4M4 11L7 8M4 11L7 14" stroke="white" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)

_ICON_FRAME = (
    '<svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<rect x="1.5" y="3.5" width="13" height="9.5" rx="1.6" stroke="white" stroke-width="1.4"/>'
    '<circle cx="8" cy="8.25" r="2.15" stroke="white" stroke-width="1.3"/>'
    "</svg>"
)

_ICON_DOTS = (
    '<svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">'
    '<circle cx="3.2" cy="8" r="1.4" fill="white"/>'
    '<circle cx="8" cy="8" r="1.4" fill="white"/>'
    '<circle cx="12.8" cy="8" r="1.4" fill="white"/>'
    "</svg>"
)


# -- Flask app ----------------------------------------------------------------


def create_app(evidence_dir: str) -> Flask:
    """Build the console's Flask app. Kept as a factory (rather than a
    module-level singleton) so tests can spin up isolated, independent
    console state per test.
    """
    app = Flask(__name__)
    app.config["EVIDENCE_DIR"] = evidence_dir
    state = _ConsoleState()
    app.config["CONSOLE_STATE"] = state

    @app.get("/")
    def dashboard():
        iv = state.current()
        if iv is None:
            badge = """<span class="badge idle"><span class="dot"></span>Idle -- no escalation active</span>"""
            body = f"""
              <div class="card">
                <div class="empty-state">
                  <div class="icon-tile tile-neutral empty-icon">{_ICON_DOTS}</div>
                  <p>No intervention is currently pending. Waiting for the next escalation&hellip;</p>
                </div>
              </div>
            """
        else:
            r = iv.record

            def esc(key: str) -> str:
                return html.escape(str(r.get(key) or ""))

            badge = """<span class="badge waiting"><span class="dot"></span>Awaiting operator decision</span>"""
            error_html = ""
            if r.get("error"):
                error_html = f"""
                  <div class="alert alert-error">
                    <span class="alert-dot"></span>
                    <div><span class="alert-label">Error&nbsp;&mdash;&nbsp;</span>{esc('error')}</div>
                  </div>
                """

            body = f"""
              <div class="dashboard-grid">
                <div class="col-left">
                  <div class="card">
                    <h2><span class="icon-tile tile-teal">{_ICON_LIST}</span>Intervention context</h2>
                    <div class="context-primary">
                      <div class="context-label">Capability / goal</div>
                      <div class="context-value">{esc('goal')}</div>
                    </div>
                    <dl class="meta">
                      <dt>Target</dt><dd class="mono">{esc('target_url')}</dd>
                      <dt>Step</dt><dd>{esc('step')}: {esc('step_description')}</dd>
                      <dt>Why it stopped</dt><dd>{esc('condition_type')} check failed after retries</dd>
                    </dl>
                  </div>

                  <div class="card">
                    <h2><span class="icon-tile tile-cyan">{_ICON_COMPARE}</span>Expected vs. observed</h2>
                    <div class="diff">
                      <div class="diff-col expected">
                        <div class="label">Expected</div>
                        <div class="value">{esc('expected')}</div>
                      </div>
                      <div class="diff-col observed">
                        <div class="label">Observed</div>
                        <div class="value">{esc('observed')}</div>
                      </div>
                    </div>
                    {error_html}
                  </div>

                  <div class="alert alert-info">
                    <span class="alert-dot"></span>
                    <div>You can still take over the actual open browser window by hand before deciding
                      &mdash; this page only reports what replay observed. Fix the live page first if
                      needed, then choose an action below.</div>
                  </div>

                  <div class="actions">
                    <form method="post" action="/interventions/{iv.id}/decision">
                      <input type="hidden" name="decision" value="resume">
                      <button type="submit" class="btn btn-resume">
                        Resume
                        <span class="btn-sub">Re-check the condition and continue the run</span>
                      </button>
                    </form>
                    <form method="post" action="/interventions/{iv.id}/decision">
                      <input type="hidden" name="decision" value="abort">
                      <button type="submit" class="btn btn-abort">
                        Abort
                        <span class="btn-sub">Stop the run now and report a hard failure</span>
                      </button>
                    </form>
                  </div>
                </div>

                <div class="col-right">
                  <div class="card shot-card">
                    <h2><span class="icon-tile tile-emerald">{_ICON_FRAME}</span>Live browser screenshot</h2>
                    <div class="screenshot-frame">
                      <img src="/interventions/{iv.id}/screenshot" alt="Screenshot of the live page at the time replay stopped">
                    </div>
                  </div>
                </div>
              </div>

              <footer class="trace">
                <span>Intervention <code>{html.escape(iv.id)}</code></span>
                <span>{esc('timestamp')}</span>
              </footer>
            """
        return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Browser Capability Agent</title>
    <meta http-equiv="refresh" content="2">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      :root {{
        color-scheme: light;

        /* Neutrals -- cool slate, deliberately not warm/beige. */
        --page-bg: #f1f3f8;
        --surface: #ffffff;
        --surface-sunken: #f8f9fb;
        --text-primary: #0f172a;
        --text-secondary: #475569;
        --text-muted: #64748b;
        --border: rgba(15, 23, 42, 0.07);
        --border-strong: rgba(15, 23, 42, 0.12);
        --divider: #e7e9f0;

        /* Primary accent family -- deep teal through cyan, used with confidence. */
        --accent: #0f766e;
        --accent-2: #0d9488;
        --accent-tint: rgba(13, 148, 136, 0.08);
        --accent-border: rgba(13, 148, 136, 0.20);
        --accent-gradient: linear-gradient(135deg, #0f766e 0%, #0d9488 45%, #22d3ee 100%);

        /* Status -- reserved meanings, not decoration. */
        --warning: #f5a524;
        --warning-ink: #92400e;
        --warning-tint: rgba(245, 165, 36, 0.16);
        --warning-border: rgba(245, 165, 36, 0.32);

        --critical: #c23434;
        --critical-tint: rgba(194, 52, 52, 0.08);
        --critical-border: rgba(194, 52, 52, 0.22);

        --resume-grad: linear-gradient(180deg, #12874f 0%, #0a5c34 100%);
        --resume-grad-hover: linear-gradient(180deg, #0f7a46 0%, #084e2c 100%);
        --resume-shadow: rgba(14, 122, 69, 0.38);
        --abort-grad: linear-gradient(180deg, #c93a3a 0%, #9c2b2b 100%);
        --abort-grad-hover: linear-gradient(180deg, #b53232 0%, #862323 100%);
        --abort-shadow: rgba(194, 52, 52, 0.38);

        --font-sans: system-ui, -apple-system, "Segoe UI", sans-serif;
        --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;

        --radius-lg: 20px;
        --radius-md: 14px;
        --radius-sm: 10px;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        background: var(--page-bg);
        color: var(--text-primary);
        font-family: var(--font-sans);
        -webkit-font-smoothing: antialiased;
        text-rendering: optimizeLegibility;
      }}
      .topbar {{ height: 5px; width: 100%; background: var(--accent-gradient); }}
      .page {{ max-width: 1320px; margin: 0 auto; padding: 18px 28px 22px; }}

      /* Two-column layout: left is the read-and-decide column (context, diff, hint,
         actions); right is the screenshot, sized to match the left column's height so
         it reads as the page's visual anchor instead of a tall stack under everything
         else. Falls back to a single stacked column only on genuinely narrow screens. */
      .dashboard-grid {{ display: grid; grid-template-columns: 5fr 7fr; gap: 18px; align-items: stretch; }}
      .col-left {{ display: flex; flex-direction: column; gap: 14px; min-width: 0; }}
      .col-right {{ display: flex; flex-direction: column; min-width: 0; }}
      .col-right .shot-card {{ flex: 1; display: flex; flex-direction: column; height: 100%; }}
      .col-right .screenshot-frame {{ flex: 1; min-height: 0; }}
      @media (max-width: 860px) {{
        .dashboard-grid {{ grid-template-columns: 1fr; }}
        .col-right .shot-card {{ height: auto; }}
        .col-right .screenshot-frame {{ flex: none; height: 46vh; }}
      }}

      header.masthead {{
        display: flex; align-items: center; justify-content: space-between; gap: 20px;
        margin-bottom: 18px; padding: 16px 24px;
        background: linear-gradient(135deg, rgba(13, 148, 136, 0.09), rgba(34, 211, 238, 0.05));
        border: 1px solid var(--accent-border);
        border-radius: var(--radius-lg);
      }}
      .masthead-content {{ display: flex; align-items: center; gap: 16px; }}
      .hero-tile {{
        width: 46px; height: 46px; border-radius: 13px; flex-shrink: 0;
        background: var(--accent-gradient);
        display: flex; align-items: center; justify-content: center;
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.22), 0 8px 18px -6px rgba(13, 148, 136, 0.50);
      }}
      .hero-tile svg {{ width: 21px; height: 21px; }}
      header.masthead h1 {{ font-size: 22px; font-weight: 700; letter-spacing: -0.014em; margin: 0 0 4px; color: var(--text-primary); }}
      header.masthead .sub {{ font-size: 13.5px; color: var(--text-secondary); font-weight: 480; }}

      .badge {{
        display: inline-flex; align-items: center; gap: 8px; flex-shrink: 0;
        padding: 8px 16px 8px 12px; border-radius: 999px;
        font-size: 12.5px; font-weight: 650; letter-spacing: 0.01em; white-space: nowrap;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
      }}
      .badge .dot {{ width: 7px; height: 7px; border-radius: 50%; box-shadow: 0 0 0 3px currentColor; opacity: 0.9; }}
      .badge.waiting {{ background: #fff8ec; color: var(--warning-ink); border: 1px solid var(--warning-border); }}
      .badge.waiting .dot {{ background: var(--warning); box-shadow: 0 0 0 3px rgba(245, 165, 36, 0.25); }}
      .badge.idle {{ background: var(--surface); color: var(--text-secondary); border: 1px solid var(--border-strong); }}
      .badge.idle .dot {{ background: var(--text-muted); box-shadow: 0 0 0 3px rgba(100, 116, 139, 0.18); }}

      .icon-tile {{
        display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
        width: 28px; height: 28px; border-radius: 9px; margin-right: 12px;
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.18), 0 1px 2px rgba(15, 23, 42, 0.10);
      }}
      .icon-tile svg {{ width: 14px; height: 14px; }}
      .tile-teal {{ background: linear-gradient(135deg, #0d9488, #14b8a6); }}
      .tile-cyan {{ background: linear-gradient(135deg, #0891b2, #22d3ee); }}
      .tile-emerald {{ background: linear-gradient(135deg, #059669, #34d399); }}
      .tile-neutral {{ background: linear-gradient(135deg, #94a3b8, #cbd5e1); }}

      .card {{
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: 18px 22px;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03), 0 20px 40px -18px rgba(15, 23, 42, 0.16);
        transition: transform 0.16s ease, box-shadow 0.16s ease;
      }}
      .card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 10px rgba(15, 23, 42, 0.04), 0 28px 52px -18px rgba(15, 23, 42, 0.20); }}
      .card h2 {{
        display: flex; align-items: center;
        font-size: 12px; text-transform: uppercase; letter-spacing: 0.07em;
        color: var(--text-secondary); margin: 0 0 13px; font-weight: 700;
      }}
      .empty-state {{ text-align: center; padding: 32px 8px; color: var(--text-secondary); font-size: 14.5px; }}
      .empty-icon {{ margin: 0 auto 16px; }}

      /* The goal is usually the longest field -- give it the card's full width instead
         of squeezing it into a narrow value column, so it wraps to fewer lines. */
      .context-primary {{ margin-bottom: 13px; padding-bottom: 13px; border-bottom: 1px solid var(--divider); }}
      .context-label {{ color: var(--text-muted); font-size: 12.5px; font-weight: 550; margin-bottom: 4px; }}
      .context-value {{ font-size: 14.5px; line-height: 1.45; color: var(--text-primary); font-weight: 500; }}

      dl.meta {{ display: grid; grid-template-columns: 128px 1fr; row-gap: 11px; column-gap: 14px; margin: 0; }}
      dl.meta dt {{ color: var(--text-muted); font-size: 13px; font-weight: 550; padding-top: 1px; }}
      dl.meta dd {{ margin: 0; font-size: 14px; line-height: 1.5; color: var(--text-primary); font-weight: 450; }}
      dl.meta dd.mono {{ font-family: var(--font-mono); font-size: 12.5px; color: var(--text-secondary); word-break: break-word; }}

      .diff {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
      .diff-col {{ border-radius: var(--radius-md); padding: 14px 16px; border: 1px solid transparent; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.02); }}
      .diff-col.expected {{ background: linear-gradient(160deg, rgba(13, 148, 136, 0.10), rgba(13, 148, 136, 0.015)); border-color: var(--accent-border); border-left: 3px solid var(--accent); }}
      .diff-col.observed {{ background: linear-gradient(160deg, rgba(194, 52, 52, 0.10), rgba(194, 52, 52, 0.015)); border-color: var(--critical-border); border-left: 3px solid var(--critical); }}
      .diff-col .label {{ font-size: 11px; font-weight: 750; letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 9px; }}
      .diff-col.expected .label {{ color: var(--accent); }}
      .diff-col.observed .label {{ color: var(--critical); }}
      .diff-col .value {{ font-family: var(--font-mono); font-size: 13px; line-height: 1.6; color: var(--text-primary); word-break: break-word; white-space: pre-wrap; }}

      .alert {{ display: flex; align-items: flex-start; gap: 10px; border-radius: var(--radius-md); padding: 12px 16px; font-size: 13px; line-height: 1.5; }}
      .alert-dot {{ width: 7px; height: 7px; border-radius: 50%; margin-top: 6px; flex-shrink: 0; }}
      .alert-error {{ margin-top: 16px; background: var(--critical-tint); border: 1px solid var(--critical-border); border-left: 3px solid var(--critical); color: #7a2020; font-family: var(--font-mono); font-size: 12.5px; }}
      .alert-error .alert-dot {{ background: var(--critical); }}
      .alert-error .alert-label {{ font-family: var(--font-sans); font-weight: 700; text-transform: uppercase; font-size: 11px; letter-spacing: 0.05em; }}
      .alert-info {{ background: var(--accent-tint); border: 1px solid var(--accent-border); border-left: 3px solid var(--accent); color: var(--text-secondary); box-shadow: 0 1px 2px rgba(15, 23, 42, 0.02); }}
      .alert-info .alert-dot {{ background: var(--accent); }}

      .screenshot-frame {{
        border: 1px solid var(--border-strong); border-radius: var(--radius-md);
        overflow: hidden; background: var(--surface-sunken); line-height: 0;
        box-shadow: inset 0 1px 3px rgba(15, 23, 42, 0.08);
        display: flex; align-items: center; justify-content: center;
      }}
      /* object-fit: contain -- a screenshot is evidence, so the frame is sized to fit
         the available column height without ever cropping any of the captured page. */
      .screenshot-frame img {{ display: block; width: 100%; height: 100%; object-fit: contain; }}

      .actions {{ display: flex; gap: 14px; }}
      .actions form {{ flex: 1; margin: 0; }}
      .btn {{
        width: 100%;
        display: flex; flex-direction: column; align-items: center; gap: 3px;
        padding: 15px 20px; border: none; border-radius: var(--radius-md);
        font-family: var(--font-sans); font-size: 15px; font-weight: 650;
        cursor: pointer; letter-spacing: 0.002em;
        transition: box-shadow 0.14s ease, transform 0.08s ease, filter 0.14s ease;
      }}
      .btn:hover {{ transform: translateY(-1px); filter: brightness(1.04); }}
      .btn:active {{ transform: translateY(1px); filter: brightness(0.98); }}
      .btn .btn-sub {{ font-size: 11.5px; font-weight: 500; opacity: 0.85; letter-spacing: 0.005em; }}
      .btn-resume {{
        background: var(--resume-grad); color: #fff;
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.16), 0 1px 2px rgba(15, 23, 42, 0.05), 0 12px 24px -10px var(--resume-shadow);
      }}
      .btn-resume:hover {{ background: var(--resume-grad-hover); box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.16), 0 16px 28px -10px var(--resume-shadow); }}
      .btn-abort {{
        background: var(--abort-grad); color: #fff;
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.16), 0 1px 2px rgba(15, 23, 42, 0.05), 0 12px 24px -10px var(--abort-shadow);
      }}
      .btn-abort:hover {{ background: var(--abort-grad-hover); box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.16), 0 16px 28px -10px var(--abort-shadow); }}

      footer.trace {{
        display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap;
        font-size: 11.5px; color: var(--text-muted); font-family: var(--font-mono);
        padding-top: 20px; border-top: 1px solid var(--divider);
      }}
      footer.trace code {{ font-family: var(--font-mono); color: var(--text-muted); }}
    </style>
  </head>
  <body>
    <div class="topbar"></div>
    <div class="page">
      <header class="masthead">
        <div class="masthead-content">
          <div class="hero-tile">{_ICON_HERO}</div>
          <div>
            <h1>Browser Capability Agent</h1>
            <div class="sub">Human-in-the-loop escalation review</div>
          </div>
        </div>
        {badge}
      </header>
      {body}
    </div>
  </body>
</html>"""

    @app.get("/interventions/<intervention_id>/screenshot")
    def screenshot(intervention_id):
        iv = state.get(intervention_id)
        if iv is None:
            abort(404)
        path = iv.record.get("screenshot")
        if not path or not os.path.isfile(path):
            abort(404)
        # Loopback-only server, but it's still an HTTP file-serving endpoint -- only ever
        # serve a path that actually lives under this console's own evidence_dir.
        evidence_root = os.path.realpath(app.config["EVIDENCE_DIR"])
        real_path = os.path.realpath(path)
        if os.path.commonpath([evidence_root, real_path]) != evidence_root:
            abort(403)
        return send_file(real_path, mimetype="image/png")

    @app.post("/api/interventions")
    def publish_intervention():
        record = request.get_json(force=True, silent=True) or {}
        intervention_id = state.publish(record)
        return jsonify({"id": intervention_id}), 201

    @app.get("/api/interventions/current")
    def get_current():
        iv = state.current()
        if iv is None:
            return jsonify({"pending": None})
        return jsonify({"pending": _intervention_to_json(iv)})

    @app.get("/api/interventions/<intervention_id>/decision")
    def poll_decision(intervention_id):
        iv = state.get(intervention_id)
        if iv is None:
            return jsonify({"error": "unknown intervention_id"}), 404
        return jsonify({"id": intervention_id, "decision": iv.decision})

    @app.post("/interventions/<intervention_id>/decision")
    def submit_decision(intervention_id):
        # The HTML buttons above submit a form; OperatorConsoleClient / tests may also
        # post JSON directly -- one endpoint serves both a real human and a machine.
        decision = request.form.get("decision")
        is_form = decision is not None
        if decision is None:
            payload = request.get_json(silent=True) or {}
            decision = payload.get("decision")

        if decision not in ("resume", "abort"):
            return jsonify({"error": "decision must be 'resume' or 'abort'"}), 400

        if not state.decide(intervention_id, decision):
            return jsonify({"error": "unknown intervention_id"}), 404

        if is_form:
            return redirect("/")
        return jsonify({"id": intervention_id, "decision": decision})

    return app


class OperatorConsoleServer:
    """Owns the Flask app and a background HTTP thread.

    Runs IN the same process as replay.py / EscalationHandler -- per the
    architecture constraint, the browser session driving the actual page
    never leaves this process. Only the human's resume/abort decision (and
    the read-only intervention context/screenshot needed to display it)
    travels over this loopback HTTP server.
    """

    def __init__(self, evidence_dir: str = "evidence", host: str = "127.0.0.1", port: int = 0):
        self.app = create_app(evidence_dir)
        self._httpd = make_server(host, port, self.app, threaded=True)
        self.host = host
        self.port = self._httpd.server_port
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "OperatorConsoleServer":
        self._thread.start()
        return self

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)


class OperatorConsoleClient:
    """HTTP client EscalationHandler uses to publish an intervention and
    block-poll for the human's decision. This is the only thing that
    crosses the process/thread boundary over the wire -- the browser/page
    it's reporting on never do.
    """

    def __init__(self, base_url: str, request_timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout

    def publish(self, record: dict) -> str:
        try:
            resp = requests.post(f"{self.base_url}/api/interventions", json=record, timeout=self.request_timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise OperatorConsoleUnavailable(
                f"could not publish intervention to operator console at {self.base_url}: {e}"
            ) from e
        return resp.json()["id"]

    def wait_for_decision(
        self, intervention_id: str, poll_interval: float = 1.0, timeout: Optional[float] = None
    ) -> str:
        start = time.monotonic()
        while True:
            try:
                resp = requests.get(
                    f"{self.base_url}/api/interventions/{intervention_id}/decision", timeout=self.request_timeout
                )
                resp.raise_for_status()
            except requests.RequestException as e:
                raise OperatorConsoleUnavailable(
                    f"could not poll operator console at {self.base_url} for a decision: {e}"
                ) from e
            decision = resp.json().get("decision")
            if decision:
                return decision
            if timeout is not None and (time.monotonic() - start) > timeout:
                raise OperatorConsoleUnavailable(f"no decision received from operator console within {timeout}s")
            time.sleep(poll_interval)


def find_free_port(host: str = "127.0.0.1") -> int:
    """Grab an OS-assigned free port, for callers that want to know it before
    OperatorConsoleServer(port=0) itself picks one (e.g. to print a URL
    ahead of starting the server). Not used by OperatorConsoleServer itself
    -- it lets the OS assign a port directly, which is race-free; this
    helper exists for the rare caller that needs the port up front.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class WebConsoleEscalationHandler(EscalationHandler):
    """Escalation via the web operator console instead of a terminal prompt.

    Same control-transfer model as EscalationHandler: one live Playwright
    page stays open in this process; the ONLY thing this class changes is
    how the human's resume/abort decision is obtained. Every audit-log
    write, screenshot capture, and redaction step is inherited unchanged
    from EscalationHandler.
    """

    def __init__(
        self,
        console_url: str,
        evidence_dir: str = "evidence",
        log_path: Optional[str] = None,
        poll_interval: float = 1.0,
        decision_timeout: Optional[float] = None,
        client: Optional[OperatorConsoleClient] = None,
    ):
        super().__init__(evidence_dir=evidence_dir, log_path=log_path)
        self.client = client or OperatorConsoleClient(console_url)
        self.poll_interval = poll_interval
        self.decision_timeout = decision_timeout
        self._last_unavailable_reason: Optional[str] = None

    def _obtain_decision(self, context: EscalationContext, record: dict) -> Optional[str]:
        try:
            intervention_id = self.client.publish(record)
            return self.client.wait_for_decision(
                intervention_id, poll_interval=self.poll_interval, timeout=self.decision_timeout
            )
        except OperatorConsoleUnavailable as e:
            # Same "no channel available" signal a terminal EOFError produces -- the base
            # class's handle() turns this into a graceful hard_failure, not a crash or hang.
            self._last_unavailable_reason = str(e)
            return None

    def _no_channel_action(self) -> str:
        return "operator_console_unreachable"

    def _no_channel_note(self) -> str:
        reason = self._last_unavailable_reason or "operator console unreachable"
        return f"escalation triggered but the operator console could not be reached ({reason})"
