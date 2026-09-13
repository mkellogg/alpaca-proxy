"""Minimal async Alpaca client for the upstream observatory API."""

from __future__ import annotations

import itertools
from typing import Any

import httpx

ERROR_NOT_IMPLEMENTED = 1024


class UpstreamError(Exception):
    """Transport or protocol failure talking to upstream. Counts as a poll failure."""


class UpstreamNotImplemented(Exception):
    """Upstream answered ErrorNumber 1024. Deliberately not an UpstreamError."""


class UpstreamClient:
    """Wraps one reusable httpx.AsyncClient for all upstream GETs."""

    def __init__(
        self,
        base_url: str,
        client_id: int,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # `transport` is optional and exists only so tests can inject
        # httpx.MockTransport; production callers pass the three contract args.
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._transaction_ids = itertools.count(1)
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def get(self, device_type: str, device_number: int, method: str) -> Any:
        """GET one Alpaca property and return its Value.

        Raises UpstreamNotImplemented on ErrorNumber 1024, UpstreamError otherwise.
        """
        url = f"{self._base_url}/api/v1/{device_type}/{device_number}/{method}"
        params = {
            "ClientID": self._client_id,
            "ClientTransactionID": next(self._transaction_ids),
        }

        try:
            response = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"{method}: request to {url} failed: {exc}") from exc

        if response.status_code != 200:
            raise UpstreamError(
                f"{method}: upstream returned HTTP {response.status_code} for {url}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(f"{method}: upstream response was not JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise UpstreamError(
                f"{method}: upstream response was not a JSON object: {payload!r}"
            )

        error_number = payload.get("ErrorNumber", 0)
        error_message = payload.get("ErrorMessage", "")

        if error_number == ERROR_NOT_IMPLEMENTED:
            raise UpstreamNotImplemented(f"{method}: not implemented upstream")
        if error_number:
            raise UpstreamError(
                f"{method}: upstream error {error_number}: {error_message}"
            )
        if "Value" not in payload:
            raise UpstreamError(f"{method}: upstream response had no Value")

        return payload["Value"]

    async def aclose(self) -> None:
        await self._client.aclose()
