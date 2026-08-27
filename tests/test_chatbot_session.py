"""Tests for chatbot/session.py -- pure multi-turn orchestration.

`invoke` is a plain fake callable here -- no HTTP, no catalog, no browser.
"""

from __future__ import annotations

from chatbot.session import ChatSession, handle_message

MERIDIAN_CAP = {
    "name": "sign_on_to_meridian_core_as_operator_tel_1787804230",
    "description": (
        "Sign on to Meridian Core as operator teller1 with password 'password', use Member "
        "Inquiry to search by Member Number for 100234, select that member, then read Type, "
        "Balance, and Status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operator_id": {"type": "string", "description": "Operator ID for sign-on"},
            "password": {"type": "string", "description": "Operator password for sign-on"},
            "member_number": {"type": "string", "description": "Member number to search"},
        },
        "required": ["operator_id", "password", "member_number"],
    },
}

CAPABILITIES = [MERIDIAN_CAP]


def test_not_recognized_reply_when_nothing_matches():
    session = ChatSession()

    reply = handle_message(session, "do a barrel roll", CAPABILITIES, invoke=lambda n, p: {})

    assert "don't recognize" in reply
    assert session.pending_capability is None


def test_asks_for_missing_params_when_some_are_extractable():
    session = ChatSession()

    reply = handle_message(session, "check the balance for member 100234", CAPABILITIES, invoke=lambda n, p: {})

    assert "operator_id" in reply
    assert "password" in reply
    assert "member_number" not in reply  # already extracted, so not asked for
    assert session.pending_capability["name"] == MERIDIAN_CAP["name"]
    assert session.collected_params == {"member_number": "100234"}


def test_second_turn_fills_remaining_slots_and_invokes():
    session = ChatSession()
    handle_message(session, "check the balance for member 100234", CAPABILITIES, invoke=lambda n, p: {})

    captured = {}

    def fake_invoke(name, params):
        captured["name"] = name
        captured["params"] = params
        return {"status": "success", "outputs": {"balance": "$1,500.00"}}

    reply = handle_message(session, "operator teller1 password password", CAPABILITIES, invoke=fake_invoke)

    assert captured["name"] == MERIDIAN_CAP["name"]
    assert captured["params"] == {"member_number": "100234", "operator_id": "teller1", "password": "password"}
    assert "balance: $1,500.00" in reply
    # Session is cleared after a completed invoke -- a new message starts a fresh match.
    assert session.pending_capability is None


def test_invokes_immediately_when_all_params_present_in_one_message():
    session = ChatSession()

    def fake_invoke(name, params):
        return {"status": "business_outcome", "outcome_name": "not_found", "reason": "no such member"}

    reply = handle_message(
        session,
        "sign on as operator teller1 password password for member 999999",
        CAPABILITIES,
        invoke=fake_invoke,
    )

    assert "Not found" in reply
    assert session.pending_capability is None


def test_ambiguous_capabilities_are_reported_not_guessed():
    cap_a = {"name": "cap_a", "description": "look up a customer record", "parameters": {"required": []}}
    cap_b = {"name": "cap_b", "description": "look up a customer invoice", "parameters": {"required": []}}
    session = ChatSession()

    reply = handle_message(session, "look up a customer", [cap_a, cap_b], invoke=lambda n, p: {})

    assert "more than one" in reply
    assert session.pending_capability is None


def test_pending_slot_filling_survives_a_turn_that_extracts_nothing_new():
    session = ChatSession()
    handle_message(session, "check the balance for member 100234", CAPABILITIES, invoke=lambda n, p: {})

    reply = handle_message(session, "hmm not sure", CAPABILITIES, invoke=lambda n, p: {})

    # Still waiting on the same capability -- nothing was silently invoked with guessed params.
    assert session.pending_capability is not None
    assert "operator_id" in reply
