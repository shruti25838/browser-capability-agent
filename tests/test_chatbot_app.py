"""Tests for chatbot/app.py's Flask wiring -- fake CatalogClient injected,
no real HTTP to a catalog, no browser.
"""

from __future__ import annotations

from chatbot.app import create_app
from chatbot.catalog_client import CatalogUnavailable

MERIDIAN_CAP = {
    "name": "sign_on_to_meridian_core_as_operator_tel_1787804230",
    "description": (
        "Sign on to Meridian Core as operator teller1, use Member Inquiry to search by "
        "Member Number for 100234, select that member, then read Balance and Status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operator_id": {"type": "string", "description": "Operator ID"},
            "password": {"type": "string", "description": "Password"},
            "member_number": {"type": "string", "description": "Member number"},
        },
        "required": ["operator_id", "password", "member_number"],
    },
}


class FakeCatalogClient:
    def __init__(self, capabilities=None, invoke_result=None, unavailable=False):
        self.capabilities = capabilities or []
        self.invoke_result = invoke_result or {"status": "success", "outputs": {}}
        self.unavailable = unavailable
        self.invoked_with = None

    def list_capabilities(self):
        if self.unavailable:
            raise CatalogUnavailable("boom")
        return self.capabilities

    def invoke(self, name, params):
        self.invoked_with = (name, params)
        return self.invoke_result


def test_chat_page_renders():
    app = create_app(client=FakeCatalogClient())
    client = app.test_client()

    resp = client.get("/")

    assert resp.status_code == 200
    assert b"Capability Chat" in resp.data


def test_chat_endpoint_asks_for_missing_params():
    fake = FakeCatalogClient(capabilities=[MERIDIAN_CAP])
    app = create_app(client=fake)
    client = app.test_client()

    resp = client.post("/chat", json={"message": "check the balance for member 100234"})

    assert resp.status_code == 200
    reply = resp.get_json()["reply"]
    assert "operator_id" in reply
    assert fake.invoked_with is None  # not invoked yet -- params still missing


def test_chat_endpoint_invokes_once_params_complete_across_two_turns():
    fake = FakeCatalogClient(
        capabilities=[MERIDIAN_CAP],
        invoke_result={"status": "success", "outputs": {"balance": "$1,500.00"}},
    )
    app = create_app(client=fake)
    client = app.test_client()

    client.post("/chat", json={"message": "check the balance for member 100234"})
    resp = client.post("/chat", json={"message": "operator teller1 password password"})

    assert resp.status_code == 200
    reply = resp.get_json()["reply"]
    assert "balance: $1,500.00" in reply
    assert fake.invoked_with == (
        MERIDIAN_CAP["name"],
        {"member_number": "100234", "operator_id": "teller1", "password": "password"},
    )


def test_chat_endpoint_reports_capability_not_recognized():
    app = create_app(client=FakeCatalogClient(capabilities=[MERIDIAN_CAP]))
    client = app.test_client()

    resp = client.post("/chat", json={"message": "do a barrel roll"})

    assert resp.status_code == 200
    assert "don't recognize" in resp.get_json()["reply"]


def test_chat_endpoint_handles_catalog_unavailable_gracefully():
    app = create_app(client=FakeCatalogClient(unavailable=True))
    client = app.test_client()

    resp = client.post("/chat", json={"message": "check the balance for member 100234"})

    assert resp.status_code == 200
    assert "can't reach the capability catalog" in resp.get_json()["reply"]


def test_chat_endpoint_with_empty_message_asks_for_input():
    app = create_app(client=FakeCatalogClient())
    client = app.test_client()

    resp = client.post("/chat", json={"message": "   "})

    assert resp.status_code == 200
    assert "Type a request" in resp.get_json()["reply"]
