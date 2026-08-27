"""Tests for chatbot/intent.py -- pure keyword matching + param extraction,
no LLM, no HTTP, no browser.
"""

from __future__ import annotations

from chatbot.intent import extract_params, match_capability

MERIDIAN_CAP = {
    "name": "sign_on_to_meridian_core_as_operator_tel_1787804230",
    "description": (
        "Sign on to Meridian Core as operator teller1 with password 'password', use Member "
        "Inquiry to search by Member Number for 100234, select that member, then on their "
        "Shares/Balances page find the share row with Share ID 100234-S0001 and read its "
        "Type, Balance, and Status."
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
    "outputs": {"type": "object", "properties": {"balance": {}, "type": {}, "status": {}}},
}

LOOKUP_CAP = {
    "name": "find_the_row_for_jason_doe_and_extract_h_1787179354",
    "description": "find the row for Jason Doe and extract his Due amount",
    "parameters": {
        "type": "object",
        "properties": {"person_name": {"type": "string", "description": "Row to select"}},
        "required": ["person_name"],
    },
    "outputs": {"type": "object", "properties": {"due_amount": {}}},
}

CAPABILITIES = [MERIDIAN_CAP, LOOKUP_CAP]


# -- match_capability ----------------------------------------------------------


def test_matches_meridian_capability_for_balance_request():
    result = match_capability("check the balance for member 100234", CAPABILITIES)

    assert result.status == "matched"
    assert result.capability["name"] == MERIDIAN_CAP["name"]


def test_not_found_for_a_capability_that_does_not_exist():
    # No "transfer" capability has ever been discovered -- must not be silently
    # matched to something unrelated.
    result = match_capability("transfer 10 from one share to another for member 100234", CAPABILITIES)

    assert result.status == "not_found"


def test_not_found_for_nonsense_input():
    result = match_capability("asdkjh qwelkj zzxcv", CAPABILITIES)

    assert result.status == "not_found"


def test_ambiguous_when_two_capabilities_tie_on_overlap():
    cap_a = {"name": "cap_a", "description": "look up a customer record", "parameters": {"required": []}}
    cap_b = {"name": "cap_b", "description": "look up a customer invoice", "parameters": {"required": []}}

    result = match_capability("look up a customer", [cap_a, cap_b])

    assert result.status == "ambiguous"
    assert {c["name"] for c in result.candidates} == {"cap_a", "cap_b"}


def test_empty_capabilities_list_is_not_found_not_a_crash():
    result = match_capability("check the balance for member 100234", [])

    assert result.status == "not_found"


# -- extract_params --------------------------------------------------------------


def test_extract_member_number():
    params = extract_params("check the balance for member 100234", ["member_number"])

    assert params == {"member_number": "100234"}


def test_extract_share_id_preserves_original_case():
    params = extract_params("what's the status of share 100234-S0001", ["share_id"])

    assert params == {"share_id": "100234-S0001"}


def test_extract_amount_anchored_to_keyword_not_any_number():
    params = extract_params("transfer 10 from one share to another for member 100234", ["amount", "member_number"])

    assert params["amount"] == "10"
    assert params["member_number"] == "100234"


def test_extract_amount_with_dollar_sign():
    params = extract_params("send $250 please", ["amount"])

    assert params == {"amount": "250"}


def test_extract_operator_and_password():
    params = extract_params(
        "sign on as operator teller1 with password password", ["operator_id", "password"]
    )

    assert params["operator_id"] == "teller1"
    assert params["password"] == "password"


def test_explicit_key_value_pairs_override_heuristics():
    params = extract_params("member_number: 100999", ["member_number"])

    assert params == {"member_number": "100999"}


def test_missing_param_is_simply_absent_not_guessed():
    params = extract_params("check the balance", ["member_number"])

    assert "member_number" not in params
