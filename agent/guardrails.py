"""GuardrailChecker: the single choke point every action must pass through.

Called before every action in both discovery and replay -- it has no
dependency on the LLM or on Playwright. Blocks anything outside the
configured domain allowlist or action-type allowlist, and additionally
enforces a per-action reversibility tier (see ActionRisk) at the same call
site, rather than as a separate system.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


class GuardrailViolation(Exception):
    """Raised when an action or navigation is blocked by policy."""


class ActionRisk:
    """The three reversibility tiers a permitted action type/target can carry.

    Kept as plain string constants (not an enum) to match this module's
    existing minimalist style -- callers compare against these directly.
    """

    SAFE = "safe"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    BLOCKED = "blocked"


# Minimal default: our current demo target (the-internet.herokuapp.com/tables)
# has no genuinely destructive action in the flows this project builds, but
# every row does expose a "delete" link. Tag it blocked as the concrete
# demonstration that the seam works, without inventing a fictitious risk
# example. Keys are "<action_type>" for a tier that applies to every use of
# that action type, or "<action_type>:<target_name>" (lowercased) for a tier
# scoped to one specific target -- e.g. blocking "click" on the "delete" link
# without blocking "click" on "edit" or anything else.
DEFAULT_ACTION_RISK_TIERS: dict[str, str] = {
    "click:delete": ActionRisk.BLOCKED,
}


class GuardrailChecker:
    def __init__(
        self,
        allowed_domains: list[str],
        allowed_actions: list[str],
        action_risk_tiers: Optional[dict[str, str]] = None,
        allow_confirmed_actions: bool = False,
    ):
        if not allowed_domains:
            raise ValueError("allowed_domains must not be empty -- refusing to run with no allowlist")
        self.allowed_domains = {d.lower() for d in allowed_domains}
        self.allowed_actions = {a.lower() for a in allowed_actions}
        tiers = DEFAULT_ACTION_RISK_TIERS if action_risk_tiers is None else action_risk_tiers
        self.action_risk_tiers = {k.lower(): v for k, v in tiers.items()}
        self.allow_confirmed_actions = allow_confirmed_actions

    def check_domain(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if not any(host == d or host.endswith(f".{d}") for d in self.allowed_domains):
            raise GuardrailViolation(
                f"Blocked: {url!r} (host {host!r}) is not in the domain allowlist {sorted(self.allowed_domains)}"
            )

    def check_action(self, action_type: str, target_name: Optional[str] = None) -> None:
        if action_type.lower() not in self.allowed_actions:
            raise GuardrailViolation(
                f"Blocked: action {action_type!r} is not in the permitted action list {sorted(self.allowed_actions)}"
            )

        tier = self._risk_tier(action_type, target_name)
        target_desc = f"{action_type!r} on {target_name!r}" if target_name else f"{action_type!r}"

        if tier == ActionRisk.BLOCKED:
            raise GuardrailViolation(
                f"Blocked: action {target_desc} is tagged {ActionRisk.BLOCKED!r} -- never permitted, "
                f"regardless of allowlist or flags"
            )
        if tier == ActionRisk.REQUIRES_CONFIRMATION and not self.allow_confirmed_actions:
            raise GuardrailViolation(
                f"Blocked: action {target_desc} is tagged {ActionRisk.REQUIRES_CONFIRMATION!r} -- "
                f"pass --allow-confirmed-actions to permit it"
            )

    def _risk_tier(self, action_type: str, target_name: Optional[str]) -> str:
        action_type = action_type.lower()
        if target_name:
            specific = self.action_risk_tiers.get(f"{action_type}:{target_name.lower()}")
            if specific is not None:
                return specific
        return self.action_risk_tiers.get(action_type, ActionRisk.SAFE)

    def check(self, action_type: str, url: str, target_name: Optional[str] = None) -> None:
        """Convenience: run both checks. Raises GuardrailViolation on the first failure."""
        self.check_action(action_type, target_name)
        self.check_domain(url)
