"""Unit tests for agent/redaction.py -- no browser, no network, pure logic."""

from __future__ import annotations

from agent.redaction import (
    REDACTED,
    redact_credit_card_like,
    redact_long_digit_sequences,
    redact_mapping,
    redact_sensitive_keys,
    redact_ssn_like,
    redact_text,
)


def test_ssn_shaped_text_gets_redacted():
    text = "Observed page text: SSN on file is 123-45-6789 for this record."

    result = redact_text(text)

    assert "123-45-6789" not in result
    assert REDACTED in result


def test_ssn_like_function_redacts_in_isolation_without_affecting_other_digits():
    text = "SSN 123-45-6789 but order number 4111111111111111 stays untouched here"

    result = redact_ssn_like(text)

    assert "123-45-6789" not in result
    assert "4111111111111111" in result  # redact_ssn_like alone must not touch this


def test_ordinary_text_is_unaffected():
    text = "Row containing 'Doe Jason' is visible in the table"

    assert redact_text(text) == text


def test_ordinary_text_with_small_numbers_is_unaffected():
    text = "Extracted due_amount as a non-empty string value ($51.00, row 2 of 4)"

    assert redact_text(text) == text


def test_luhn_valid_credit_card_like_sequence_is_redacted():
    # 4111111111111111 is the well-known Luhn-valid Visa test number.
    text = "Observed value: 4111111111111111 on the payment form"

    result = redact_credit_card_like(text, require_luhn=True)

    assert "4111111111111111" not in result
    assert REDACTED in result


def test_credit_card_like_sequence_with_separators_is_redacted():
    text = "Card on file: 4111-1111-1111-1111"

    result = redact_credit_card_like(text, require_luhn=True)

    assert "4111-1111-1111-1111" not in result
    assert REDACTED in result


def test_non_luhn_valid_sequence_survives_the_credit_card_check_specifically():
    # 1234567890123456 fails the Luhn checksum -- redact_credit_card_like alone (with
    # require_luhn=True) must leave it alone; the generic long-digit-sequence pass
    # (tested separately) is what would catch it in the full redact_text pipeline.
    text = "Reference number 1234567890123456"

    result = redact_credit_card_like(text, require_luhn=True)

    assert result == text


def test_redact_text_still_catches_a_non_luhn_valid_long_sequence_via_the_generic_pass():
    # The full pipeline layers a generic long-digit-sequence catch-all after the
    # credit-card-specific check, so this is still redacted overall.
    text = "Reference number 1234567890123456"

    result = redact_text(text)

    assert "1234567890123456" not in result
    assert REDACTED in result


def test_long_digit_sequence_is_redacted_with_default_threshold():
    text = "Account number on file: 987654321"  # 9 digits

    result = redact_long_digit_sequences(text)

    assert "987654321" not in result
    assert REDACTED in result


def test_short_digit_sequence_below_threshold_is_not_redacted():
    text = "Row 2 of 4, due amount 51.00"

    result = redact_long_digit_sequences(text, min_length=9)

    assert result == text


def test_long_digit_threshold_is_configurable():
    text = "Code: 12345"  # 5 digits

    assert redact_long_digit_sequences(text, min_length=9) == text
    assert redact_long_digit_sequences(text, min_length=5) != text


def test_none_text_returns_none():
    assert redact_text(None) is None


# -- redact_mapping: the artifact-output exclusion ---------------------------


def test_declared_output_value_is_never_redacted_even_if_numeric():
    result = {
        "status": "success",
        "outputs": {"account_number": "123456789012345", "due_amount": "$51.00"},
        "reason": "Observed SSN-shaped text 123-45-6789 in an unrelated error message",
    }

    redacted = redact_mapping(result, protected_keys=frozenset({"outputs"}))

    # Declared outputs survive completely unredacted, even though the account number
    # looks exactly like the kind of thing this module would otherwise redact.
    assert redacted["outputs"]["account_number"] == "123456789012345"
    assert redacted["outputs"]["due_amount"] == "$51.00"
    # But incidental fields outside "outputs" are still redacted.
    assert "123-45-6789" not in redacted["reason"]
    assert REDACTED in redacted["reason"]


def test_redact_mapping_without_protected_keys_redacts_everything():
    record = {"observed": "count matches SSN-like 123-45-6789"}

    redacted = redact_mapping(record)

    assert "123-45-6789" not in redacted["observed"]


def test_redact_mapping_preserves_non_string_values():
    result = {"status": "success", "step": 2, "outputs": {"n": 42}}

    redacted = redact_mapping(result, protected_keys=frozenset({"outputs"}))

    assert redacted["step"] == 2
    assert redacted["outputs"]["n"] == 42


def test_redact_mapping_recurses_into_nested_non_protected_dicts():
    record = {"context": {"observed": "SSN 123-45-6789 seen"}}

    redacted = redact_mapping(record)

    assert "123-45-6789" not in redacted["context"]["observed"]


# -- redact_sensitive_keys: redact by field name, not value shape ------------


def test_redact_sensitive_keys_redacts_a_plain_password_field():
    # "password" the literal string matches none of redact_text's value-shape
    # heuristics (not SSN-, credit-card-, or long-digit-shaped) -- this is exactly
    # the gap that let a plaintext password reach the dashboard.
    params = {"operator_id": "teller1", "password": "password", "member_number": "100234"}

    redacted = redact_sensitive_keys(params)

    assert redacted["password"] == REDACTED
    assert redacted["operator_id"] == "teller1"
    assert redacted["member_number"] == "100234"


def test_redact_sensitive_keys_matches_password_like_and_secret_and_token_keys():
    params = {"operator_password": "hunter2", "api_secret": "shh", "auth_token": "abc123", "username": "tomsmith"}

    redacted = redact_sensitive_keys(params)

    assert redacted["operator_password"] == REDACTED
    assert redacted["api_secret"] == REDACTED
    assert redacted["auth_token"] == REDACTED
    assert redacted["username"] == "tomsmith"


def test_redact_sensitive_keys_recurses_into_nested_dicts():
    params = {"credentials": {"password": "hunter2"}, "member_number": "100234"}

    redacted = redact_sensitive_keys(params)

    assert redacted["credentials"]["password"] == REDACTED
    assert redacted["member_number"] == "100234"


def test_redact_sensitive_keys_composes_with_redact_mapping():
    # This is exactly the call catalog/run_log.py makes: key-name redaction first,
    # then the existing value-shape redaction on top -- both must take effect.
    params = {"password": "password", "notes": "SSN 123-45-6789 on file"}

    redacted = redact_mapping(redact_sensitive_keys(params))

    assert redacted["password"] == REDACTED
    assert "123-45-6789" not in redacted["notes"]
