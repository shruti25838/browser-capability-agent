"""Unit tests for agent/guardrails.py -- no browser, no network, pure logic."""

from __future__ import annotations

import pytest

from agent.guardrails import ActionRisk, GuardrailChecker, GuardrailViolation


def _checker() -> GuardrailChecker:
    return GuardrailChecker(
        allowed_domains=["the-internet.herokuapp.com"],
        allowed_actions=["find_row", "click", "type", "extract"],
    )


def test_allows_action_within_domain_and_action_allowlist():
    checker = _checker()

    # Should not raise.
    checker.check("click", "https://the-internet.herokuapp.com/tables")


def test_allows_subdomain_of_an_allowed_domain():
    checker = _checker()

    checker.check("click", "https://www.the-internet.herokuapp.com/tables")


def test_blocks_action_on_domain_not_in_allowlist():
    checker = _checker()

    with pytest.raises(GuardrailViolation):
        checker.check("click", "https://evil.example.com/tables")


def test_blocks_action_type_not_permitted_even_on_allowed_domain():
    checker = _checker()

    with pytest.raises(GuardrailViolation):
        checker.check("delete_everything", "https://the-internet.herokuapp.com/tables")


def test_check_domain_alone_raises_for_disallowed_host():
    checker = _checker()

    with pytest.raises(GuardrailViolation):
        checker.check_domain("https://not-allowed.example.com/page")


def test_check_action_alone_raises_for_disallowed_action():
    checker = _checker()

    with pytest.raises(GuardrailViolation):
        checker.check_action("navigate_away")


def test_check_action_is_case_insensitive():
    checker = _checker()

    # Should not raise -- action allowlist is lower-cased on both sides.
    checker.check_action("CLICK")


def test_empty_allowed_domains_rejected_at_construction():
    with pytest.raises(ValueError):
        GuardrailChecker(allowed_domains=[], allowed_actions=["click"])


# -- risk tiers -------------------------------------------------------------


def test_blocked_target_is_refused_before_any_click_reaches_playwright():
    # Uses the real DEFAULT_ACTION_RISK_TIERS (click:delete -> blocked). This test never
    # touches Playwright at all -- proving the refusal happens at the guardrail layer,
    # strictly before any action execution could occur.
    checker = _checker()

    with pytest.raises(GuardrailViolation, match="blocked"):
        checker.check_action("click", target_name="delete")


def test_blocked_target_is_refused_even_with_allow_confirmed_actions():
    checker = GuardrailChecker(
        allowed_domains=["the-internet.herokuapp.com"],
        allowed_actions=["click"],
        allow_confirmed_actions=True,
    )

    with pytest.raises(GuardrailViolation, match="blocked"):
        checker.check_action("click", target_name="delete")


def test_blocked_target_name_is_case_insensitive():
    checker = _checker()

    with pytest.raises(GuardrailViolation):
        checker.check_action("click", target_name="DELETE")


def test_click_on_a_different_target_name_is_unaffected_by_the_delete_block():
    checker = _checker()

    # Should not raise -- only "delete" is tagged blocked, not "click" as a whole.
    checker.check_action("click", target_name="edit")
    checker.check_action("click")  # no target name at all


def test_requires_confirmation_tier_refused_without_the_flag():
    checker = GuardrailChecker(
        allowed_domains=["the-internet.herokuapp.com"],
        allowed_actions=["click"],
        action_risk_tiers={"click:submit": ActionRisk.REQUIRES_CONFIRMATION},
    )

    with pytest.raises(GuardrailViolation, match="requires_confirmation"):
        checker.check_action("click", target_name="submit")


def test_requires_confirmation_tier_permitted_with_the_flag():
    checker = GuardrailChecker(
        allowed_domains=["the-internet.herokuapp.com"],
        allowed_actions=["click"],
        action_risk_tiers={"click:submit": ActionRisk.REQUIRES_CONFIRMATION},
        allow_confirmed_actions=True,
    )

    # Should not raise.
    checker.check_action("click", target_name="submit")


def test_requires_confirmation_flag_does_not_also_unlock_blocked_actions():
    checker = GuardrailChecker(
        allowed_domains=["the-internet.herokuapp.com"],
        allowed_actions=["click"],
        action_risk_tiers={"click:delete": ActionRisk.BLOCKED, "click:submit": ActionRisk.REQUIRES_CONFIRMATION},
        allow_confirmed_actions=True,
    )

    checker.check_action("click", target_name="submit")  # permitted
    with pytest.raises(GuardrailViolation):
        checker.check_action("click", target_name="delete")  # still refused


def test_action_type_level_tier_applies_when_no_target_specific_override_exists():
    checker = GuardrailChecker(
        allowed_domains=["the-internet.herokuapp.com"],
        allowed_actions=["type"],
        action_risk_tiers={"type": ActionRisk.REQUIRES_CONFIRMATION},
    )

    with pytest.raises(GuardrailViolation, match="requires_confirmation"):
        checker.check_action("type", target_name="comment_box")


def test_unconfigured_action_defaults_to_safe():
    checker = _checker()

    # "extract" has no entry in DEFAULT_ACTION_RISK_TIERS -- must default to safe, not blocked.
    checker.check_action("extract", target_name="due_amount")


def test_check_convenience_method_enforces_risk_tier_too():
    checker = _checker()

    with pytest.raises(GuardrailViolation):
        checker.check("click", "https://the-internet.herokuapp.com/tables", target_name="delete")
