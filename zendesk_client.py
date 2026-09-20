"""HTTP client for the Zendesk API.

Wraps authentication, cursor pagination and rate-limit handling.
Verified references:
  - Auth:        https://developer.zendesk.com/api-reference/introduction/security-and-auth/
  - Rate limits: https://developer.zendesk.com/api-reference/introduction/rate-limits/
  - Pagination:  https://developer.zendesk.com/api-reference/introduction/pagination/
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

import httpx

# Offset pagination is capped at 100 pages / 10,000 records.
# Cursor pagination has no such ceiling, so it is what we use by default.
CURSOR_PAGE_SIZE = 100
MAX_RETRIES_ON_429 = 4
MAX_BACKOFF_SECONDS = 60


class ZendeskConfigError(RuntimeError):
    """Configuration is missing or inconsistent."""


class ZendeskWriteDisabled(RuntimeError):
    """A write was attempted without explicitly enabling writes."""


class ZendeskClient:
    def __init__(
        self,
        subdomain: str,
        *,
        email: str | None = None,
        api_token: str | None = None,
        oauth_token: str | None = None,
        allow_writes: bool = False,
        timeout: float = 30.0,
    ) -> None:
        if not subdomain:
            raise ZendeskConfigError("ZENDESK_SUBDOMAIN is not set.")

        self.subdomain = subdomain
        self.allow_writes = allow_writes
        self.base_url = f"https://{subdomain}.zendesk.com/api/v2"

        # OAuth is the method Zendesk recommends. API tokens still work but are
        # deprecated, and they allow impersonating any member of the account,
        # admins included.
        if oauth_token:
            headers = {"Authorization": f"Bearer {oauth_token}"}
            self.auth_mode = "oauth"
        elif email and api_token:
            raw = f"{email}/token:{api_token}".encode()
            headers = {"Authorization": "Basic " + base64.b64encode(raw).decode()}
            self.auth_mode = "api_token"
        else:
            raise ZendeskConfigError(
                "Set ZENDESK_OAUTH_TOKEN, or else ZENDESK_EMAIL together with "
                "ZENDESK_API_TOKEN."
            )

        headers["Accept"] = "application/json"
        headers["User-Agent"] = "zendesk-admin-mcp/0.1"

        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout
        )

    @classmethod
    def from_env(cls) -> "ZendeskClient":
        return cls(
            subdomain=os.environ.get("ZENDESK_SUBDOMAIN", ""),
            email=os.environ.get("ZENDESK_EMAIL"),
            api_token=os.environ.get("ZENDESK_API_TOKEN"),
            oauth_token=os.environ.get("ZENDESK_OAUTH_TOKEN"),
            allow_writes=os.environ.get("ZENDESK_ALLOW_WRITES", "").lower()
            in ("1", "true", "yes"),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Transport layer
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a request, honouring the Retry-After header on 429 responses."""
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES_ON_429):
            response = await self._client.request(
                method, path, params=params, json=json
            )

            if response.status_code == 429:
                retry_after = response.headers.get("retry-after", "10")
                try:
                    wait = int(float(retry_after))
                except ValueError:
                    wait = 10
                wait = min(max(wait, 1), MAX_BACKOFF_SECONDS)
                last_error = httpx.HTTPStatusError(
                    f"429 Too Many Requests; Retry-After={wait}s",
                    request=response.request,
                    response=response,
                )
                await asyncio.sleep(wait)
                continue

            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()

        raise RuntimeError(
            f"Zendesk returned 429 on {MAX_RETRIES_ON_429} consecutive attempts for "
            f"{method} {path}."
        ) from last_error

    async def get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def put(
        self,
        path: str,
        json: dict[str, Any],
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.allow_writes:
            raise ZendeskWriteDisabled(
                "Writes are disabled. Set ZENDESK_ALLOW_WRITES=true in the server "
                "configuration to allow them."
            )
        return await self._request("PUT", path, params=params, json=json)

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def paginate(
        self,
        path: str,
        key: str,
        *,
        params: dict[str, Any] | None = None,
        max_items: int = 2000,
    ) -> list[dict[str, Any]]:
        """Walk a cursor-paginated endpoint and return the records.

        `key` is the name of the array in the response envelope ("triggers",
        "macros", "users", ...). Stops once `max_items` is reached.
        """
        query = dict(params or {})
        query["page[size]"] = min(CURSOR_PAGE_SIZE, max_items)

        items: list[dict[str, Any]] = []
        next_url: str | None = None

        while True:
            if next_url:
                # links.next arrives as an absolute URL already signed with the cursor.
                response = await self._client.get(next_url)
                if response.status_code == 429:
                    retry_after = int(float(response.headers.get("retry-after", "10")))
                    await asyncio.sleep(min(max(retry_after, 1), MAX_BACKOFF_SECONDS))
                    continue
                response.raise_for_status()
                payload = response.json()
            else:
                payload = await self.get(path, query)

            batch = payload.get(key) or []
            items.extend(batch)

            if len(items) >= max_items:
                return items[:max_items]

            meta = payload.get("meta") or {}
            links = payload.get("links") or {}
            if not meta.get("has_more") or not links.get("next"):
                return items
            next_url = links["next"]

    async def search_export(
        self,
        query: str,
        *,
        filter_type: str = "ticket",
        max_items: int = 1000,
    ) -> list[dict[str, Any]]:
        """Cursor-paginated search (/search/export).

        Unlike /api/v2/search, which caps at 1,000 total results, this endpoint
        paginates by cursor without that limit. The cursor expires after 1 hour.
        """
        params = {
            "query": query,
            "filter[type]": filter_type,
            "page[size]": min(1000, max_items),
        }
        items: list[dict[str, Any]] = []
        next_url: str | None = None

        while True:
            if next_url:
                response = await self._client.get(next_url)
                response.raise_for_status()
                payload = response.json()
            else:
                payload = await self.get("/search/export", params)

            items.extend(payload.get("results") or [])
            if len(items) >= max_items:
                return items[:max_items]

            meta = payload.get("meta") or {}
            links = payload.get("links") or {}
            if not meta.get("has_more") or not links.get("next"):
                return items
            next_url = links["next"]

    async def search_count(self, query: str) -> int:
        payload = await self.get("/search/count", {"query": query})
        return int(payload.get("count") or 0)
