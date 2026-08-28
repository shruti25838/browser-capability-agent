"""Regex-based redaction of incidental sensitive-looking data.

Applied at every point where text gets written to disk or console *outside*
an artifact's own declared typed outputs: evidence logs, intervention_log.jsonl,
screenshots' accompanying text metadata, and discovery/replay console logging.

Deliberately NOT applied to an artifact's declared outputs themselves (e.g.
a due_amount field): those are the explicitly-requested data the capability
is designed to return, not incidental page text/error messages/observed
state. Callers that need to redact a result-shaped dict while preserving a
specific key (typically "outputs") should use redact_mapping(...,
protected_keys={"outputs"}).

No detector here claims to be exhaustive PII detection -- these are simple,
auditable regex heuristics for the patterns the brief calls out (SSN-like,
credit-card-like, generic long digit sequences), layered so a sequence that
slips past one still gets caught by a broader one.
"""

from __future__ import annotations

import re
from typing import Any, Optional

REDACTED = "[REDACTED]"

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# 13-19 digits, allowing an optional space or dash between any two digits (covers
# both a bare run and common human-readable grouping like "4111 1111 1111 1111").
_CREDIT_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact_ssn_like(text: str) -> str:
    """Redact ###-##-#### sequences (US SSN format)."""
    return _SSN_RE.sub(REDACTED, text)


def redact_credit_card_like(text: str, require_luhn: bool = True) -> str:
    """Redact 13-19 digit sequences (optionally space/dash separated).

    require_luhn=True (default) only redacts sequences that pass the Luhn
    checksum, to avoid over-redacting arbitrary long numbers that happen to
    fall in the credit-card length range. Set False to redact any sequence
    in that length range regardless of checksum.
    """

    def _sub(match: re.Match) -> str:
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and (not require_luhn or _luhn_valid(digits)):
            return REDACTED
        return match.group(0)

    return _CREDIT_CARD_RE.sub(_sub, text)


def redact_long_digit_sequences(text: str, min_length: int = 9) -> str:
    """Redact any standalone digit run of at least `min_length` digits.

    A generic catch-all for "looks like an account number" -- configurable
    threshold so callers can tune how aggressive this net is.
    """
    pattern = re.compile(rf"\b\d{{{min_length},}}\b")
    return pattern.sub(REDACTED, text)


def redact_text(
    text: Optional[str],
    *,
    require_luhn_for_credit_cards: bool = True,
    long_digit_threshold: int = 9,
) -> Optional[str]:
    """Apply all three redaction rules to one string, in order: SSN (most
    specific), then credit-card-like, then the generic long-digit-sequence
    catch-all (which will also mop up any 13-19 digit run the credit-card
    check let through, e.g. because it failed the Luhn check -- that's
    intentional layering, not a bug: a long digit run is still worth
    redacting even if it isn't a valid card number).
    """
    if text is None:
        return None
    text = redact_ssn_like(text)
    text = redact_credit_card_like(text, require_luhn=require_luhn_for_credit_cards)
    text = redact_long_digit_sequences(text, min_length=long_digit_threshold)
    return text


def redact_mapping(data: dict[str, Any], protected_keys: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Return a copy of `data` with every string value redacted, except values
    under any key in `protected_keys` (recursively preserved, not just at the
    top level) -- e.g. "outputs", an artifact's own declared, deliberately
    returned data, which must survive unredacted even if it looks numeric.
    """

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, dict):
            return {k: (v if k in protected_keys else _walk(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    return {k: (v if k in protected_keys else _walk(v)) for k, v in data.items()}


_SENSITIVE_KEY_RE = re.compile(r"(password|passwd|secret|token)", re.IGNORECASE)


def redact_sensitive_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `data` with every value under a credential-shaped key
    (password/passwd/secret/token, case-insensitive substring match -- covers
    "password", "operator_password", "auth_token", "api_secret", etc.)
    replaced with REDACTED, regardless of what the value looks like.

    Complements redact_text's value-shape heuristics (SSN-like, credit-card-
    like, long digit runs): a short alphabetic password like "password" or
    "hunter2" matches none of those shapes, so it survives redact_mapping
    untouched unless the field NAME itself is checked too. Recurses into
    nested dicts/lists the same way redact_mapping does.
    """

    def _walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: (REDACTED if _SENSITIVE_KEY_RE.search(k) else _walk(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    return _walk(data)
