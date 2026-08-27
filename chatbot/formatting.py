"""Pure formatting of a catalog invoke result into a plain-language reply.

No I/O -- takes exactly the JSON body catalog/service.py's
POST /capabilities/{name}/invoke already returns (the same three-way
success/business_outcome/hard_failure contract ReplayEngine produces) and
turns it into a sentence. Never invents a new outcome vocabulary, and never
reports a business_outcome or hard_failure as if it were success.
"""

from __future__ import annotations

from typing import Any


def format_result(capability: dict[str, Any], result: dict[str, Any]) -> str:
    status = result.get("status")

    if status == "success":
        outputs = result.get("outputs") or {}
        if not outputs:
            return f"Done. {capability.get('name', 'the capability')} completed successfully."
        pieces = ", ".join(f"{k}: {v}" for k, v in outputs.items())
        return f"Done. {pieces}"

    if status == "business_outcome":
        outcome = (result.get("outcome_name") or "unspecified outcome").replace("_", " ")
        sentence = outcome[:1].upper() + outcome[1:] + "."
        reason = result.get("reason")
        return f"{sentence} ({reason})" if reason else sentence

    if status == "hard_failure":
        error = result.get("error") or result.get("observed") or "an unexpected error"
        return f"That didn't complete -- {error}"

    return f"Unexpected response from {capability.get('name', 'the capability')}: {result}"
