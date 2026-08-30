"""
frontend/utils/api_client.py
=============================
Thin HTTP client for the Interlock orchestrator API.

The UI owns no orchestration logic.  Every value rendered by Streamlit is
read from one of these endpoints:

    POST /change-requests
    GET  /change-requests/{id}
    GET  /change-requests/{id}/evidence
    GET  /change-requests/{id}/graph
    GET  /change-requests/{id}/gate
    GET  /change-requests/{id}/approvals
    POST /change-requests/{id}/approve

Failures are surfaced as ApiError and are NEVER converted into fake success
values.  The caller is expected to render the error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_BASE_URL = os.environ.get(
    "ORCHESTRATOR_API_URL",
    os.environ.get("INTERLOCK_API_URL", "http://localhost:8000"),
)

DEFAULT_TIMEOUT = 10.0


class ApiError(Exception):
    """
    Raised for any failed API interaction.

    Attributes
    ----------
    status:
        HTTP status code, or None when the backend was unreachable.
    detail:
        Server-supplied ``detail`` string when available.
    unreachable:
        True when no HTTP response was received at all (backend down).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: str | None = None,
        unreachable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail
        self.unreachable = unreachable

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.detail:
            return f"{self.message}: {self.detail}"
        return self.message


@dataclass
class InterlockClient:
    """Synchronous client used by the Streamlit app."""

    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT

    # -- internals ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = self._url(path)
        try:
            resp = requests.request(method, url, timeout=self.timeout, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise ApiError(
                f"Backend unreachable at {self.base_url}",
                unreachable=True,
            ) from exc

        if resp.status_code >= 400:
            detail: str | None
            try:
                payload = resp.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if detail is not None and not isinstance(detail, str):
                    detail = str(detail)
            except ValueError:
                detail = resp.text[:300] or None
            raise ApiError(
                f"{method} {path} failed ({resp.status_code})",
                status=resp.status_code,
                detail=detail,
            )

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError(f"{method} {path} returned non-JSON response") from exc

    # -- endpoints ---------------------------------------------------------

    def health(self) -> bool:
        """Return True when the backend answers at all (any HTTP response)."""
        try:
            requests.get(self._url("/openapi.json"), timeout=self.timeout)
        except requests.exceptions.RequestException:
            return False
        return True

    def create_change_request(
        self, description: str, spec: dict | None = None
    ) -> dict:
        """
        Create a change request.

        `spec` is optional and additive. Without it the orchestrator runs its
        stub workflow; with it, the real agents for that change kind run. The
        UI has no opinion about which — it forwards what the user chose and
        renders whatever comes back.
        """
        payload: dict = {"description": description}
        if spec is not None:
            payload["spec"] = spec
        return self._request("POST", "/change-requests", json=payload)

    def get_spec(self, change_id: str) -> dict:
        """Read back the structured spec, or nulls for a stub-path change."""
        return self._request("GET", f"/change-requests/{change_id}/spec")

    def get_change_request(self, change_id: str) -> dict:
        return self._request("GET", f"/change-requests/{change_id}")

    def get_evidence(self, change_id: str) -> dict:
        return self._request("GET", f"/change-requests/{change_id}/evidence")

    def get_graph(self, change_id: str) -> dict:
        return self._request("GET", f"/change-requests/{change_id}/graph")

    def get_gate(self, change_id: str) -> dict:
        return self._request("GET", f"/change-requests/{change_id}/gate")

    def get_approvals(self, change_id: str) -> dict:
        return self._request("GET", f"/change-requests/{change_id}/approvals")

    def approve(self, change_id: str, gate: str, approved_by: str = "human") -> dict:
        return self._request(
            "POST",
            f"/change-requests/{change_id}/approve",
            json={"gate": gate, "approved_by": approved_by},
        )

    # -- aggregate ---------------------------------------------------------

    def snapshot(self, change_id: str) -> dict:
        """
        Fetch every projection for one change in a single call site.

        Each section is fetched independently; a failing section is recorded
        under ``errors`` instead of aborting the whole snapshot, so a partially
        available backend still renders what it can.
        """
        snap: dict[str, Any] = {"change_id": change_id, "errors": {}}
        sections = {
            "change": self.get_change_request,
            "evidence": self.get_evidence,
            "graph": self.get_graph,
            "gate": self.get_gate,
            "approvals": self.get_approvals,
        }
        for name, fn in sections.items():
            try:
                snap[name] = fn(change_id)
            except ApiError as exc:
                snap[name] = None
                snap["errors"][name] = str(exc)
        return snap
