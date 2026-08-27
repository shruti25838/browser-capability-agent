"""Tests for chatbot/formatting.py -- pure, no I/O."""

from __future__ import annotations

from chatbot.formatting import format_result

CAP = {"name": "sign_on_to_meridian_core_as_operator_tel_1787804230"}


def test_success_lists_outputs_in_plain_language():
    result = {"status": "success", "outputs": {"balance": "$1,500.00", "type": "Regular Shares", "status": "HOLD"}}

    reply = format_result(CAP, result)

    assert "balance: $1,500.00" in reply
    assert "type: Regular Shares" in reply
    assert "status: HOLD" in reply


def test_success_with_no_outputs_still_reads_as_success():
    reply = format_result(CAP, {"status": "success", "outputs": {}})

    assert "Done" in reply
    assert "successfully" in reply


def test_business_outcome_reads_as_a_normal_answer_not_an_error():
    result = {"status": "business_outcome", "outcome_name": "not_found", "reason": "no such member"}

    reply = format_result(CAP, result)

    assert "Not found" in reply
    assert "no such member" in reply
    assert "error" not in reply.lower()


def test_hard_failure_is_reported_clearly():
    result = {"status": "hard_failure", "error": "element not found after retries"}

    reply = format_result(CAP, result)

    assert "didn't complete" in reply
    assert "element not found after retries" in reply


def test_hard_failure_falls_back_to_observed_when_no_error_field():
    result = {"status": "hard_failure", "observed": "count=0 visible=False"}

    reply = format_result(CAP, result)

    assert "count=0 visible=False" in reply


def test_unexpected_status_does_not_crash():
    reply = format_result(CAP, {"status": "something_new"})

    assert "Unexpected response" in reply
