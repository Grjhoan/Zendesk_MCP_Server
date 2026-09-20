#!/usr/bin/env python3
"""Real connection test against Zendesk, without going through an MCP client.

Reads the .env in this folder, calls /users/me and reports which of the
endpoints the tools rely on respond with this token. It is the fastest way to
learn what works in your account before connecting the server to a client.

    python check_connection.py
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from server import _load_dotenv
from zendesk_client import ZendeskClient, ZendeskConfigError

# Endpoints backing each tool. Some depend on the plan.
PROBES: list[tuple[str, str, dict[str, object], str]] = [
    ("audit_business_rules", "/triggers", {"page[size]": 1}, ""),
    ("audit_business_rules", "/automations", {"page[size]": 1}, ""),
    ("audit_business_rules", "/macros", {"page[size]": 1}, ""),
    ("list_ticket_fields", "/ticket_fields", {"page[size]": 1}, ""),
    ("list_sla_policies", "/slas/policies", {"page[size]": 1}, "requires a plan with SLAs"),
    ("backlog_health", "/search/count", {"query": "type:ticket status:open"}, ""),
    ("security_overview", "/oauth/tokens", {"page[size]": 1}, "requires the admin role"),
    ("security_overview", "/apps/installations", {}, ""),
    ("recent_admin_changes", "/audit_logs", {"page[size]": 1}, "Enterprise plan only"),
]


async def main() -> int:
    _load_dotenv()

    try:
        zd = ZendeskClient.from_env()
    except ZendeskConfigError as exc:
        print(f"Incomplete configuration: {exc}")
        return 1

    print(f"Subdomain: {zd.subdomain}   Auth: {zd.auth_mode}   "
          f"Writes: {'enabled' if zd.allow_writes else 'blocked'}\n")

    try:
        payload = await zd.get("/users/me")
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        print(f"Authentication FAILED ({code}).")
        if code == 401:
            print("  401 usually means a wrong subdomain, email or token, or that")
            print("  API token access is disabled in Admin Center.")
        elif code == 403:
            print("  403 usually means the token's user lacks permissions.")
        await zd.aclose()
        return 1

    user = payload.get("user") or {}
    print(f"Connected as: {user.get('name')} <{user.get('email')}>")
    print(f"Role: {user.get('role')}\n")
    print("Endpoints available with this token:\n")

    failures = 0
    for tool, path, params, note in PROBES:
        try:
            await zd.get(path, params)
            status = "OK   "
        except httpx.HTTPStatusError as exc:
            status = f"{exc.response.status_code}  "
            failures += 1
        except Exception:  # noqa: BLE001
            status = "ERROR"
            failures += 1
        suffix = f"   ({note})" if note else ""
        print(f"  [{status}] {path:<24} → {tool}{suffix}")

    await zd.aclose()

    print()
    if failures:
        print(f"{failures} endpoint(s) did not respond. A 403 or 404 here usually")
        print("means plan or permissions, not a server error: the corresponding tool")
        print("will return a message explaining it.")
    else:
        print("All endpoints respond. The server is ready to connect.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
