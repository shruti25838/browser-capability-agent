"""Thin HTTP client for the catalog API -- the ONLY way the chatbot talks to
capabilities. No import of agent.* or replay.* anywhere in this package:
every action goes through catalog/service.py's HTTP surface, so guardrails
and the three-way result contract apply exactly as they do for any other
caller of that API. This module never touches ReplayEngine/GuardrailChecker
directly and cannot bypass them.
"""

from __future__ import annotations

from typing import Any

import requests


class CatalogUnavailable(Exception):
    """Raised when the catalog API can't be reached at all (network error,
    connection refused, timeout)."""


class CatalogClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def list_capabilities(self) -> list[dict[str, Any]]:
        try:
            resp = requests.get(f"{self.base_url}/capabilities", timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise CatalogUnavailable(f"could not reach catalog at {self.base_url}: {e}") from e
        return resp.json()["capabilities"]

    def invoke(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = requests.post(f"{self.base_url}/capabilities/{name}/invoke", json=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise CatalogUnavailable(f"could not reach catalog at {self.base_url}: {e}") from e
        if resp.status_code == 404:
            raise LookupError(f"no capability named {name!r}")
        if resp.status_code == 422:
            detail = (resp.json() or {}).get("detail", "missing required parameter(s)")
            raise ValueError(str(detail))
        resp.raise_for_status()
        return resp.json()
