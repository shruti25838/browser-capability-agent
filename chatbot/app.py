"""Thin chat front-end over the catalog API.

Talks to catalog/service.py's HTTP endpoints only (GET /capabilities,
POST /capabilities/{name}/invoke) via CatalogClient -- no import of
agent.* or replay.* anywhere in this module, so it is structurally
incapable of bypassing guardrails or the replay/catalog result contract.
A demo driver, not a product: single global chat session, no auth,
no persistence beyond the current process.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from flask import Flask, jsonify, request

from .catalog_client import CatalogClient, CatalogUnavailable
from .session import ChatSession, handle_message

DEFAULT_CATALOG_BASE_URL = "http://127.0.0.1:8000"

_STYLE = """
  :root {
    color-scheme: light;
    --page-bg: #f1f3f8;
    --surface: #ffffff;
    --text-primary: #0f172a;
    --text-secondary: #475569;
    --border: rgba(15, 23, 42, 0.07);
    --accent: #0f766e;
    --accent-gradient: linear-gradient(135deg, #0f766e 0%, #0d9488 45%, #22d3ee 100%);
    --font-sans: system-ui, -apple-system, "Segoe UI", sans-serif;
    --radius-lg: 18px;
    --radius-md: 12px;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--page-bg); color: var(--text-primary); font-family: var(--font-sans); }
  .topbar { height: 5px; background: var(--accent-gradient); }
  .page { max-width: 720px; margin: 0 auto; padding: 20px 24px 40px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { font-size: 13px; color: var(--text-secondary); margin-bottom: 16px; }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
    padding: 16px; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03);
  }
  .chat-log { height: 420px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; padding: 6px 4px; margin-bottom: 12px; }
  .bubble { max-width: 80%; padding: 10px 14px; border-radius: var(--radius-md); font-size: 14px; line-height: 1.45; white-space: pre-wrap; }
  .bubble.user { align-self: flex-end; background: var(--accent); color: #fff; }
  .bubble.bot { align-self: flex-start; background: #f1f5f9; color: var(--text-primary); }
  .chat-form { display: flex; gap: 8px; }
  .chat-form input { flex: 1; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-md); font-size: 14px; }
  .chat-form button { padding: 10px 18px; border: none; border-radius: var(--radius-md); background: var(--accent); color: #fff; font-weight: 650; cursor: pointer; }
"""

_PAGE_HTML = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Capability Chat</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>{_STYLE}</style>
  </head>
  <body>
    <div class="topbar"></div>
    <div class="page">
      <h1>Capability Chat</h1>
      <div class="sub">Thin chat front-end over the capability catalog &mdash; try
        &ldquo;check the balance for member 100234&rdquo;.</div>
      <div class="card">
        <div id="log" class="chat-log"></div>
        <form id="form" class="chat-form">
          <input id="input" type="text" autocomplete="off" placeholder="Type a request...">
          <button type="submit">Send</button>
        </form>
      </div>
    </div>
    <script>
      const log = document.getElementById('log');
      const form = document.getElementById('form');
      const input = document.getElementById('input');

      function addBubble(text, who) {{
        const div = document.createElement('div');
        div.className = 'bubble ' + who;
        div.textContent = text;
        log.appendChild(div);
        log.scrollTop = log.scrollHeight;
      }}

      form.addEventListener('submit', async (e) => {{
        e.preventDefault();
        const text = input.value.trim();
        if (!text) return;
        addBubble(text, 'user');
        input.value = '';
        try {{
          const resp = await fetch('/chat', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{message: text}}),
          }});
          const data = await resp.json();
          addBubble(data.reply, 'bot');
        }} catch (err) {{
          addBubble('Could not reach the chat server.', 'bot');
        }}
      }});
    </script>
  </body>
</html>"""


def create_app(catalog_base_url: str = DEFAULT_CATALOG_BASE_URL, client: Optional[Any] = None) -> Flask:
    app = Flask(__name__)
    app.config["CATALOG_CLIENT"] = client or CatalogClient(catalog_base_url)
    app.config["SESSION"] = ChatSession()

    @app.get("/")
    def chat_page() -> str:
        return _PAGE_HTML

    @app.post("/chat")
    def chat():
        payload = request.get_json(silent=True) or {}
        text = (payload.get("message") or "").strip()
        if not text:
            return jsonify({"reply": 'Type a request, e.g. "check the balance for member 100234".'})

        catalog_client = app.config["CATALOG_CLIENT"]
        session: ChatSession = app.config["SESSION"]

        try:
            capabilities = catalog_client.list_capabilities()
        except CatalogUnavailable as e:
            return jsonify({"reply": f"I can't reach the capability catalog right now ({e})."})

        def invoke(name: str, params: dict[str, Any]) -> dict[str, Any]:
            return catalog_client.invoke(name, params)

        try:
            reply = handle_message(session, text, capabilities, invoke)
        except CatalogUnavailable as e:
            reply = f"I can't reach the capability catalog right now ({e})."
        except (LookupError, ValueError) as e:
            reply = f"That didn't go through: {e}"

        return jsonify({"reply": reply})

    return app


app = create_app(os.environ.get("CATALOG_BASE_URL", DEFAULT_CATALOG_BASE_URL))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("CHATBOT_PORT", "5001")), debug=False)
