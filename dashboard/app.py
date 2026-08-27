"""Eval-flavored dashboard: reads the run-log catalog/service.py writes and
shows how each capability is performing. A demo surface, not a product --
one Flask view, no client-side JS, no build step.

Read-only: never touches ReplayEngine/GuardrailChecker, never writes to the
run-log, only reads it (catalog.run_log.load_runs) and reduces it
(dashboard.stats.aggregate_stats).
"""

from __future__ import annotations

import html
import json
import os
from typing import Any, Optional

from flask import Flask, abort, request, send_file

from catalog.run_log import DEFAULT_RUN_LOG_PATH, load_runs
from dashboard.stats import CapabilityStats, aggregate_stats

RECENT_RUNS_LIMIT = 50


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _pct(fraction: float) -> str:
    return f"{fraction * 100:.0f}%"


def _params_preview(params: dict[str, Any]) -> str:
    try:
        text = json.dumps(params, separators=(", ", ": "))
    except TypeError:
        text = str(params)
    return text if len(text) <= 80 else text[:77] + "..."


def _status_badge(status: Optional[str]) -> str:
    css_class = {"success": "status-success", "business_outcome": "status-outcome", "hard_failure": "status-failure"}.get(
        status or "", "status-unknown"
    )
    return f'<span class="status-badge {css_class}">{_esc(status or "unknown")}</span>'


def _capability_card_html(stats: CapabilityStats) -> str:
    return f"""
      <div class="card">
        <h2>{_esc(stats.capability)}</h2>
        <div class="stat-row">
          <div class="stat"><div class="stat-value">{stats.total}</div><div class="stat-label">Total runs</div></div>
          <div class="stat"><div class="stat-value stat-success">{stats.success}</div>
            <div class="stat-label">Success ({_pct(stats.success_rate)})</div></div>
          <div class="stat"><div class="stat-value stat-outcome">{stats.business_outcome}</div>
            <div class="stat-label">Business outcome ({_pct(stats.business_outcome_rate)})</div></div>
          <div class="stat"><div class="stat-value stat-failure">{stats.hard_failure}</div>
            <div class="stat-label">Hard failure ({_pct(stats.hard_failure_rate)})</div></div>
          <div class="stat"><div class="stat-value">{stats.avg_retry_count:.1f} / {stats.median_retry_count:.1f}</div>
            <div class="stat-label">Avg / median retry_count</div></div>
          <div class="stat"><div class="stat-value">{stats.recovered_via_retry}</div>
            <div class="stat-label">Recovered via retry</div></div>
        </div>
      </div>
    """


def _evidence_cell(evidence: Optional[str]) -> str:
    if not evidence:
        return "&mdash;"
    return f'<a href="/evidence?path={html.escape(str(evidence), quote=True)}">{_esc(evidence)}</a>'


def _recent_runs_table_html(runs: list[dict[str, Any]]) -> str:
    ordered = sorted(runs, key=lambda r: r.get("timestamp") or "", reverse=True)[:RECENT_RUNS_LIMIT]
    rows = "\n".join(
        f"""<tr>
              <td class="mono">{_esc(r.get('timestamp'))}</td>
              <td>{_esc(r.get('capability'))}</td>
              <td class="mono">{_esc(_params_preview(r.get('params') or {}))}</td>
              <td>{_status_badge(r.get('status'))}</td>
              <td>{_esc(r.get('outcome_name')) if r.get('outcome_name') else '&mdash;'}</td>
              <td>{r.get('retry_count') or 0}</td>
              <td>{_evidence_cell(r.get('evidence'))}</td>
            </tr>"""
        for r in ordered
    )
    return f"""
      <table class="runs-table">
        <thead>
          <tr>
            <th>Timestamp</th><th>Capability</th><th>Params</th><th>Result</th>
            <th>Outcome</th><th>Retries</th><th>Evidence</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    """


_STYLE = """
  :root {
    color-scheme: light;
    --page-bg: #f1f3f8;
    --surface: #ffffff;
    --text-primary: #0f172a;
    --text-secondary: #475569;
    --text-muted: #64748b;
    --border: rgba(15, 23, 42, 0.07);
    --divider: #e7e9f0;
    --accent: #0f766e;
    --accent-gradient: linear-gradient(135deg, #0f766e 0%, #0d9488 45%, #22d3ee 100%);
    --success: #0d9488;
    --outcome: #f5a524;
    --failure: #c23434;
    --font-sans: system-ui, -apple-system, "Segoe UI", sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    --radius-lg: 18px;
    --radius-md: 12px;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--page-bg); color: var(--text-primary); font-family: var(--font-sans); }
  .topbar { height: 5px; width: 100%; background: var(--accent-gradient); }
  .page { max-width: 1200px; margin: 0 auto; padding: 20px 28px 40px; }
  h1 { font-size: 21px; font-weight: 700; margin: 0 0 4px; }
  .sub { font-size: 13.5px; color: var(--text-secondary); margin-bottom: 20px; }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
    padding: 18px 22px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03);
  }
  .card h2 { font-size: 15px; font-weight: 700; margin: 0 0 14px; }
  .stat-row { display: flex; flex-wrap: wrap; gap: 22px; }
  .stat { min-width: 110px; }
  .stat-value { font-size: 20px; font-weight: 700; }
  .stat-success { color: var(--success); }
  .stat-outcome { color: #92400e; }
  .stat-failure { color: var(--failure); }
  .stat-label { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
  .empty-state { text-align: center; padding: 30px 10px; color: var(--text-secondary); }
  table.runs-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.runs-table th {
    text-align: left; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--text-muted); padding: 8px 10px; border-bottom: 1px solid var(--divider);
  }
  table.runs-table td { padding: 8px 10px; border-bottom: 1px solid var(--divider); vertical-align: top; }
  .mono { font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); }
  .status-badge { padding: 3px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 650; }
  .status-success { background: rgba(13, 148, 136, 0.12); color: #0f766e; }
  .status-outcome { background: rgba(245, 165, 36, 0.16); color: #92400e; }
  .status-failure { background: rgba(194, 52, 52, 0.12); color: #9c2b2b; }
  .status-unknown { background: rgba(100, 116, 139, 0.14); color: var(--text-muted); }
  section.recent h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-secondary); }
"""


def create_app(run_log_path: str = DEFAULT_RUN_LOG_PATH, evidence_root: str = ".") -> Flask:
    app = Flask(__name__)
    app.config["RUN_LOG_PATH"] = run_log_path
    app.config["EVIDENCE_ROOT"] = evidence_root

    @app.get("/")
    def dashboard() -> str:
        runs = load_runs(run_log_path)

        if not runs:
            body = """
              <div class="card">
                <div class="empty-state">
                  <p>No runs yet. Invoke a capability through the catalog
                  (<code>POST /capabilities/{name}/invoke</code>) to populate this dashboard,
                  or point <code>CATALOG_RUN_LOG_PATH</code> at
                  <code>runs/seed_run_log.jsonl</code> for a demo with sample data.</p>
                </div>
              </div>
            """
        else:
            stats_by_capability = aggregate_stats(runs)
            cards = "\n".join(_capability_card_html(s) for _, s in sorted(stats_by_capability.items()))
            body = f"""
              {cards}
              <section class="recent">
                <h2>Recent runs</h2>
                <div class="card">{_recent_runs_table_html(runs)}</div>
              </section>
            """

        return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Capability Eval Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>{_STYLE}</style>
  </head>
  <body>
    <div class="topbar"></div>
    <div class="page">
      <h1>Capability Eval Dashboard</h1>
      <div class="sub">Per-capability run history, read from {_esc(run_log_path)}</div>
      {body}
    </div>
  </body>
</html>"""

    @app.get("/evidence")
    def evidence():
        path = request.args.get("path")
        if not path or not os.path.isfile(path):
            abort(404)
        # Same containment check as operator_console's screenshot route: only ever serve a
        # path that actually lives under this dashboard's configured evidence root.
        evidence_root = os.path.realpath(app.config["EVIDENCE_ROOT"])
        real_path = os.path.realpath(path)
        if os.path.commonpath([evidence_root, real_path]) != evidence_root:
            abort(403)
        return send_file(real_path)

    return app


app = create_app(
    os.environ.get("CATALOG_RUN_LOG_PATH", DEFAULT_RUN_LOG_PATH),
    os.environ.get("DASHBOARD_EVIDENCE_ROOT", "."),
)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("DASHBOARD_PORT", "5050")), debug=False)
