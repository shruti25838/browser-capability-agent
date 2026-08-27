"""Pure keyword/intent matching + basic parameter extraction. No LLM, no I/O.

Maps free-text chat input to one of the catalog's capabilities (fetched
fresh from GET /capabilities every time, never hardcoded by name -- capability
names are timestamped artifact filenames, not stable intents) and pulls out
a handful of common parameter shapes (member numbers, share IDs, amounts,
operator credentials) with regexes. Anything not confidently found is left
out -- callers ask the user for it rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

_STOPWORDS = {
    "a", "an", "the", "for", "to", "of", "on", "in", "is", "are", "please",
    "can", "you", "i", "want", "would", "like", "check", "get", "show", "me",
    "my", "and", "from", "with", "what", "whats", "tell", "one", "another",
}


def _tokenize(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS and len(w) > 1}


@dataclass
class MatchResult:
    status: str  # "matched" | "ambiguous" | "not_found"
    capability: Optional[dict[str, Any]] = None
    candidates: list[dict[str, Any]] = field(default_factory=list)


_MIN_MATCH_RATIO = 0.75


def match_capability(text: str, capabilities: list[dict[str, Any]]) -> MatchResult:
    """Score each capability by token overlap between `text` and its
    name + description (goal). The capability with the strictly highest
    overlap wins; a tie for the top score is reported as ambiguous rather
    than picked arbitrarily; no candidate clearing the confidence floor is
    not_found.

    A candidate must cover at least _MIN_MATCH_RATIO of the query's own
    tokens, not just share *some* word with it -- two capabilities in the
    same domain (e.g. a balance lookup and a transfer) will often share
    generic vocabulary ("member", "share", a literal account number), so
    any-overlap-at-all would falsely match a request for a capability that
    doesn't exist yet to one that happens to mention the same account.
    """
    query_tokens = _tokenize(text)
    if not query_tokens:
        return MatchResult(status="not_found")

    scored: list[tuple[int, dict[str, Any]]] = []
    for cap in capabilities:
        cap_text = f"{cap.get('name', '')} {cap.get('description', '')}".replace("_", " ")
        overlap = query_tokens & _tokenize(cap_text)
        if overlap and len(overlap) / len(query_tokens) >= _MIN_MATCH_RATIO:
            scored.append((len(overlap), cap))

    if not scored:
        return MatchResult(status="not_found")

    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_score = scored[0][0]
    top_matches = [cap for score, cap in scored if score == top_score]
    if len(top_matches) > 1:
        return MatchResult(status="ambiguous", candidates=top_matches)
    return MatchResult(status="matched", capability=top_matches[0], candidates=top_matches)


_KV_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9_ ]*?)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)")


def _explicit_kv_pairs(text: str) -> dict[str, str]:
    """"name: value" / "name=value" pairs anywhere in the text, keyed by a
    normalized (lowercase, underscored) name so "operator id: teller1" and
    "operator_id=teller1" both match a parameter named operator_id. Values
    keep their original case.
    """
    pairs: dict[str, str] = {}
    for name, value in _KV_RE.findall(text):
        key = name.strip().lower().replace(" ", "_")
        pairs[key] = value.strip("'\"").rstrip(",.")
    return pairs


def extract_params(text: str, param_names: list[str]) -> dict[str, str]:
    """Best-effort extraction of a handful of common parameter shapes.

    Regexes match case-insensitively but always capture from the original
    text, so extracted values (e.g. a share ID like "100234-S0001") keep
    their original casing -- lowercasing the whole input first would corrupt
    values that matter downstream (replay's row/cell matching can be
    case-sensitive).
    """
    found: dict[str, str] = {}
    explicit = _explicit_kv_pairs(text)

    for name in param_names:
        key = name.lower()
        if key in explicit:
            found[name] = explicit[key]
            continue

        if "member" in key:
            m = re.search(r"member(?:\s*(?:number|#|no\.?))?\s*[:#]?\s*(\d{3,})", text, re.IGNORECASE)
            if m:
                found[name] = m.group(1)
                continue

        if "share" in key:
            m = re.search(r"share(?:\s*id)?\s*[:#]?\s*([a-zA-Z0-9]+-[a-zA-Z0-9]+)", text, re.IGNORECASE)
            if not m:
                m = re.search(r"\b(\d+-[a-zA-Z]\d+)\b", text)
            if m:
                found[name] = m.group(1)
                continue

        if "amount" in key:
            # Anchored to an amount/transfer/worth keyword or a leading "$" -- never just
            # "the first number anywhere," which would happily grab a member number instead.
            m = re.search(r"(?:amount|transfer|worth)\s*(?:of)?\s*[:#]?\s*\$?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
            if not m:
                m = re.search(r"\$\s*(\d+(?:\.\d+)?)", text)
            if m:
                found[name] = m.group(1)
                continue

        if "operator" in key:
            m = re.search(r"\bas\s+(?:operator\s+)?([a-zA-Z][a-zA-Z0-9_]*)", text, re.IGNORECASE)
            if not m:
                m = re.search(r"\boperator\s+([a-zA-Z][a-zA-Z0-9_]*)", text, re.IGNORECASE)
            if m:
                found[name] = m.group(1)
                continue

        if "password" in key:
            m = re.search(r"password(?:\s+is)?\s*[:#]?\s*'?\"?([^\s'\",.]+)", text, re.IGNORECASE)
            if m:
                found[name] = m.group(1)
                continue

    return found
