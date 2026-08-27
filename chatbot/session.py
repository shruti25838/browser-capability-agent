"""Multi-turn slot-filling orchestration: free text -> capability + params ->
invoke -> plain-language reply. Pure logic -- takes a capabilities list and
an `invoke` callable so it's fully testable without HTTP or Flask.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .formatting import format_result
from .intent import extract_params, match_capability

InvokeFn = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass
class ChatSession:
    """One conversation's slot-filling state. A capability with missing
    required params stays pending across turns -- the next message is
    treated as answering what's still missing -- until every required
    param has been supplied, at which point it's invoked and cleared.
    """

    pending_capability: Optional[dict[str, Any]] = None
    collected_params: dict[str, str] = field(default_factory=dict)

    def reset(self) -> None:
        self.pending_capability = None
        self.collected_params = {}


def _missing(capability: dict[str, Any], collected: dict[str, str]) -> list[str]:
    required = capability["parameters"]["required"]
    return [p for p in required if p not in collected]


def _ask_for_missing(capability: dict[str, Any], missing: list[str]) -> str:
    props = capability["parameters"]["properties"]
    hints = "; ".join(f"{p} ({props.get(p, {}).get('description', 'no description')})" for p in missing)
    return f"I can do that ({capability['name']}), but I still need: {hints}."


def _not_recognized_message(capabilities: list[dict[str, Any]]) -> str:
    if not capabilities:
        return "I don't have any capabilities available to call right now."
    names = "; ".join(f"{c['name']}: {c.get('description', '')}" for c in capabilities)
    return "I don't recognize that as something I can do. Here's what I can help with: " + names


def _ambiguous_message(candidates: list[dict[str, Any]]) -> str:
    names = ", ".join(c["name"] for c in candidates)
    return f"That could match more than one thing I can do ({names}). Could you be more specific?"


def handle_message(session: ChatSession, text: str, capabilities: list[dict[str, Any]], invoke: InvokeFn) -> str:
    if session.pending_capability is not None:
        capability = session.pending_capability
        still_missing_before = _missing(capability, session.collected_params)
        session.collected_params.update(extract_params(text, still_missing_before))
    else:
        match = match_capability(text, capabilities)
        if match.status == "not_found":
            return _not_recognized_message(capabilities)
        if match.status == "ambiguous":
            return _ambiguous_message(match.candidates)
        capability = match.capability
        session.pending_capability = capability
        session.collected_params = extract_params(text, capability["parameters"]["required"])

    missing = _missing(capability, session.collected_params)
    if missing:
        return _ask_for_missing(capability, missing)

    params = dict(session.collected_params)
    session.reset()
    result = invoke(capability["name"], params)
    return format_result(capability, result)
