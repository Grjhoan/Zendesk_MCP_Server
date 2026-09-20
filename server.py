#!/usr/bin/env python3
"""zendesk-admin-mcp - MCP server for Zendesk administration tasks.

Deliberately narrow in scope: instead of wrapping the hundreds of endpoints in
the API, it exposes twelve tools that solve concrete admin tasks across four
areas: business-rule auditing, backlog and SLA health, security, and user
management / offboarding.

Design principles:
  - Read-only by default. The two write tools live apart, require
    ZENDESK_ALLOW_WRITES=true, and run as a dry run unless told otherwise.
  - The Zendesk role of the token remains the real security boundary: the
    server can never do more than that user could already do.
  - Everything returns aggregated summaries with bounded lists, so the model's
    context is not flooded with thousands of records.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from zendesk_client import ZendeskClient, ZendeskConfigError, ZendeskWriteDisabled


def _load_dotenv() -> None:
    """Load the .env sitting next to this file without overriding what is set.

    No external dependencies and deliberately tolerant: if the file is missing
    or a line is malformed, it is skipped. Variables the MCP client has already
    injected into the environment take precedence over the .env.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except (FileNotFoundError, OSError):
        pass


_load_dotenv()


mcp = MCPServer("zendesk-admin", version="0.1.0")

# Annotations the MCP client can surface to the user before executing.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

_client: ZendeskClient | None = None


def client() -> ZendeskClient:
    global _client
    if _client is None:
        _client = ZendeskClient.from_env()
    return _client


def _today() -> datetime:
    return datetime.now(timezone.utc)


def _days_since(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (_today() - parsed).days


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, **extra}


# ======================================================================
# Connection
# ======================================================================


@mcp.tool(annotations=READ_ONLY)
async def whoami() -> dict[str, Any]:
    """Verify the Zendesk connection and show which account and role the server
    operates as. Use this first to confirm the credentials work."""
    try:
        zd = client()
    except ZendeskConfigError as exc:
        return _error(str(exc))

    try:
        payload = await zd.get("/users/me")
    except httpx.HTTPStatusError as exc:
        return _error(
            f"Zendesk rejected the authentication ({exc.response.status_code}).",
            hint="Check the subdomain and the token. If using an API token, the user "
            "must be the token owner and the account must have token access "
            "enabled in Admin Center.",
        )

    user = payload.get("user") or {}
    return {
        "subdomain": zd.subdomain,
        "auth_mode": zd.auth_mode,
        "writes_enabled": zd.allow_writes,
        "user": {
            "id": user.get("id"),
            "name": user.get("name"),
            "email": user.get("email"),
            "role": user.get("role"),
            "custom_role_id": user.get("custom_role_id"),
        },
        "note": (
            "The tools can only do what this role is allowed to do in Zendesk."
        ),
    }


# ======================================================================
# Area 1 - Business-rule auditing
# ======================================================================


@mcp.tool(annotations=READ_ONLY)
async def audit_business_rules(
    kind: str = "all",
    stale_days: int = 180,
    max_per_kind: int = 500,
) -> dict[str, Any]:
    """Audit triggers, automations and macros looking for dead rules.

    Detects four problems: inactive rules, rules unused over the last 30 days,
    rules not modified in `stale_days`, and duplicate titles. `kind` accepts
    "all", "triggers", "automations" or "macros".
    """
    zd = client()
    kinds = (
        ["triggers", "automations", "macros"] if kind == "all" else [kind]
    )
    if any(k not in ("triggers", "automations", "macros") for k in kinds):
        return _error("kind must be 'all', 'triggers', 'automations' or 'macros'.")

    report: dict[str, Any] = {"stale_days_threshold": stale_days, "areas": {}}

    for k in kinds:
        # The usage sideload exists for triggers and macros; automations do not
        # document it, so we request it anyway and tolerate its absence.
        params = {"include": "usage_30d"} if k in ("triggers", "macros") else {}
        try:
            records = await zd.paginate(
                f"/{k}", k, params=params, max_items=max_per_kind
            )
        except httpx.HTTPStatusError as exc:
            report["areas"][k] = _error(
                f"Could not read /{k} ({exc.response.status_code})."
            )
            continue

        inactive: list[dict[str, Any]] = []
        unused: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        titles = Counter()

        for record in records:
            title = (record.get("title") or "").strip()
            titles[title.lower()] += 1
            age = _days_since(record.get("updated_at"))
            summary = {
                "id": record.get("id"),
                "title": title,
                "active": record.get("active"),
                "days_since_update": age,
                "usage_30d": record.get("usage_30d"),
            }

            if record.get("active") is False:
                inactive.append(summary)
            if record.get("usage_30d") == 0 and record.get("active"):
                unused.append(summary)
            if age is not None and age >= stale_days and record.get("active"):
                stale.append(summary)

        duplicates = [
            {"title": t, "count": c} for t, c in titles.items() if c > 1 and t
        ]

        report["areas"][k] = {
            "total": len(records),
            "truncated": len(records) >= max_per_kind,
            "inactive_count": len(inactive),
            "inactive": inactive[:25],
            "active_but_unused_30d_count": len(unused),
            "active_but_unused_30d": unused[:25],
            "not_updated_recently_count": len(stale),
            "not_updated_recently": stale[:25],
            "duplicate_titles": duplicates[:25],
        }

    report["note"] = (
        "usage_30d is null when Zendesk does not report usage for that rule type. "
        "An unused rule is not always redundant: it may cover a rare case. "
        "Review before deactivating."
    )
    return report


@mcp.tool(annotations=READ_ONLY)
async def list_ticket_fields(include_inactive: bool = True) -> dict[str, Any]:
    """Inventory of ticket fields, flagging which are inactive and which are
    system fields. Useful before cleaning up forms."""
    zd = client()
    fields = await zd.paginate("/ticket_fields", "ticket_fields", max_items=500)

    rows = [
        {
            "id": f.get("id"),
            "title": f.get("title"),
            "type": f.get("type"),
            "active": f.get("active"),
            "required": f.get("required"),
            "system": f.get("removable") is False,
            "days_since_update": _days_since(f.get("updated_at")),
        }
        for f in fields
        if include_inactive or f.get("active")
    ]

    return {
        "total": len(rows),
        "inactive_count": sum(1 for r in rows if not r["active"]),
        "custom_count": sum(1 for r in rows if not r["system"]),
        "fields": rows,
    }


# ======================================================================
# Area 2 - Backlog and SLAs
# ======================================================================


@mcp.tool(annotations=READ_ONLY)
async def list_sla_policies() -> dict[str, Any]:
    """List the SLA policies with their targets by priority and metric."""
    zd = client()
    try:
        policies = await zd.paginate(
            "/slas/policies", "sla_policies", max_items=200
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            return _error(
                "SLA policies are not available on this Zendesk plan."
            )
        raise

    return {
        "total": len(policies),
        "policies": [
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "position": p.get("position"),
                "filter": p.get("filter"),
                "targets": [
                    {
                        "priority": m.get("priority"),
                        "metric": m.get("metric"),
                        "target_minutes": m.get("target"),
                        "business_hours": m.get("business_hours"),
                    }
                    for m in (p.get("policy_metrics") or [])
                ],
            }
            for p in policies
        ],
    }


@mcp.tool(annotations=READ_ONLY)
async def backlog_health(stale_days: int = 7, sample_size: int = 25) -> dict[str, Any]:
    """X-ray of the open backlog: counts by status and priority, plus the most
    stalled tickets. `stale_days` defines how many days without an update make
    a ticket stale."""
    zd = client()
    cutoff = (_today() - timedelta(days=stale_days)).strftime("%Y-%m-%d")

    by_status: dict[str, int] = {}
    for status in ("new", "open", "pending", "hold"):
        by_status[status] = await zd.search_count(f"type:ticket status:{status}")

    by_priority: dict[str, int] = {}
    for priority in ("urgent", "high", "normal", "low"):
        by_priority[priority] = await zd.search_count(
            f"type:ticket status<solved priority:{priority}"
        )

    stale_query = f"type:ticket status<solved updated<{cutoff}"
    stale_total = await zd.search_count(stale_query)

    stale_sample = await zd.search_export(
        stale_query, filter_type="ticket", max_items=max(sample_size, 1)
    )

    unassigned = await zd.search_count("type:ticket status<solved assignee:none")

    return {
        "open_by_status": by_status,
        "open_total": sum(by_status.values()),
        "open_by_priority": by_priority,
        "unassigned_open": unassigned,
        "stale_threshold_days": stale_days,
        "stale_total": stale_total,
        "stale_sample": [
            {
                "id": t.get("id"),
                "subject": t.get("subject"),
                "status": t.get("status"),
                "priority": t.get("priority"),
                "assignee_id": t.get("assignee_id"),
                "days_since_update": _days_since(t.get("updated_at")),
            }
            for t in stale_sample[:sample_size]
        ],
    }


@mcp.tool(annotations=READ_ONLY)
async def agent_workload(max_tickets: int = 1000) -> dict[str, Any]:
    """Distribution of open tickets by assigned agent, heaviest load first.
    Shows where work is piling up."""
    zd = client()
    tickets = await zd.search_export(
        "type:ticket status<solved", filter_type="ticket", max_items=max_tickets
    )

    counts = Counter()
    for ticket in tickets:
        assignee = ticket.get("assignee_id")
        counts[assignee if assignee else "unassigned"] += 1

    agent_ids = [a for a in counts if isinstance(a, int)]
    names: dict[int, str] = {}
    for i in range(0, len(agent_ids), 100):
        chunk = agent_ids[i : i + 100]
        payload = await zd.get(
            "/users/show_many", {"ids": ",".join(str(a) for a in chunk)}
        )
        for user in payload.get("users") or []:
            names[user["id"]] = user.get("name") or str(user["id"])

    rows = [
        {
            "assignee_id": None if a == "unassigned" else a,
            "name": "Unassigned" if a == "unassigned" else names.get(a, str(a)),
            "open_tickets": c,
        }
        for a, c in counts.most_common()
    ]

    return {
        "tickets_analyzed": len(tickets),
        "truncated": len(tickets) >= max_tickets,
        "workload": rows,
    }


# ======================================================================
# Area 3 - Security and compliance
# ======================================================================


@mcp.tool(annotations=READ_ONLY)
async def security_overview(unused_token_days: int = 90) -> dict[str, Any]:
    """Security overview of the account: who holds the admin role, which OAuth
    tokens exist and which have gone unused, and which apps are installed."""
    zd = client()
    result: dict[str, Any] = {}

    admins = await zd.paginate(
        "/users", "users", params={"role[]": "admin"}, max_items=500
    )
    result["admins"] = {
        "count": len(admins),
        "users": [
            {
                "id": u.get("id"),
                "name": u.get("name"),
                "email": u.get("email"),
                "suspended": u.get("suspended"),
                "two_factor_auth_enabled": u.get("two_factor_auth_enabled"),
                "days_since_last_login": _days_since(u.get("last_login_at")),
            }
            for u in admins
        ],
    }

    try:
        tokens = await zd.paginate("/oauth/tokens", "tokens", max_items=500)
        aging = [
            {
                "id": t.get("id"),
                "client_id": t.get("client_id"),
                "user_id": t.get("user_id"),
                "scopes": t.get("scopes"),
                "days_since_last_use": _days_since(t.get("used_at")),
                "never_used": t.get("used_at") is None,
            }
            for t in tokens
        ]
        result["oauth_tokens"] = {
            "count": len(tokens),
            "never_used": [t for t in aging if t["never_used"]][:25],
            "unused_recently": [
                t
                for t in aging
                if t["days_since_last_use"] is not None
                and t["days_since_last_use"] >= unused_token_days
            ][:25],
        }
    except httpx.HTTPStatusError as exc:
        result["oauth_tokens"] = _error(
            f"Could not read the OAuth tokens ({exc.response.status_code}); "
            "requires the admin role."
        )

    try:
        payload = await zd.get("/apps/installations")
        installations = payload.get("installations") or []
        result["apps"] = {
            "count": len(installations),
            "enabled": sum(1 for a in installations if a.get("enabled")),
            "installations": [
                {
                    "id": a.get("id"),
                    "app_id": a.get("app_id"),
                    "name": (a.get("settings") or {}).get("name"),
                    "enabled": a.get("enabled"),
                    "product": a.get("product"),
                    "role_restrictions": a.get("role_restrictions"),
                }
                for a in installations
            ][:50],
        }
    except httpx.HTTPStatusError as exc:
        result["apps"] = _error(
            f"Could not read the installed apps ({exc.response.status_code})."
        )

    return result


@mcp.tool(annotations=READ_ONLY)
async def recent_admin_changes(
    days: int = 7,
    action: str | None = None,
    source_type: str | None = None,
    max_items: int = 200,
) -> dict[str, Any]:
    """Recent configuration changes according to the audit log.

    `action` accepts create, destroy, exported, login or update. `source_type`
    accepts user, rule, ticket, apitoken, among others. Requires the Enterprise
    plan.
    """
    zd = client()
    start = (_today() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = _today().strftime("%Y-%m-%dT%H:%M:%SZ")

    base: dict[str, Any] = {}
    if action:
        base["filter[action]"] = action
    if source_type:
        base["filter[source_type]"] = source_type

    # Zendesk documents that filter[created_at] needs a start and an end but does
    # not pin down the encoding. Try the comma-separated pair form first and, if
    # it is rejected, the array form.
    attempts = [
        {**base, "filter[created_at]": f"{start},{end}"},
        {**base, "filter[created_at][]": [start, end]},
    ]

    last_status = None
    for params in attempts:
        try:
            logs = await zd.paginate(
                "/audit_logs", "audit_logs", params=params, max_items=max_items
            )
        except httpx.HTTPStatusError as exc:
            last_status = exc.response.status_code
            if last_status in (403, 404):
                return _error(
                    "The audit log is only available on the Zendesk Enterprise plan, "
                    "or the token lacks permission to read it."
                )
            continue

        return {
            "window_days": days,
            "total": len(logs),
            "truncated": len(logs) >= max_items,
            "changes": [
                {
                    "id": entry.get("id"),
                    "created_at": entry.get("created_at"),
                    "actor": entry.get("actor_name"),
                    "actor_id": entry.get("actor_id"),
                    "action": entry.get("action"),
                    "source_type": entry.get("source_type"),
                    "source_label": entry.get("source_label"),
                    "change_description": entry.get("change_description"),
                    "ip_address": entry.get("ip_address"),
                }
                for entry in logs
            ],
        }

    return _error(
        f"Could not query the audit log (last status {last_status}).",
        hint="Check the account plan and the role of the token.",
    )


# ======================================================================
# Area 4 - Users and offboarding
# ======================================================================


@mcp.tool(annotations=READ_ONLY)
async def find_user(query: str) -> dict[str, Any]:
    """Find users by name, email or any term of the Zendesk search syntax.
    Returns id, role and status."""
    zd = client()
    payload = await zd.get("/users/search", {"query": query, "per_page": 50})
    users = payload.get("users") or []
    return {
        "count": len(users),
        "users": [
            {
                "id": u.get("id"),
                "name": u.get("name"),
                "email": u.get("email"),
                "role": u.get("role"),
                "active": u.get("active"),
                "suspended": u.get("suspended"),
                "days_since_last_login": _days_since(u.get("last_login_at")),
            }
            for u in users
        ],
    }


@mcp.tool(annotations=READ_ONLY)
async def offboarding_report(user_id: int) -> dict[str, Any]:
    """Everything left hanging on an agent before deactivating them: assigned
    open tickets, the groups they belong to, role and last login. Read-only;
    it changes nothing."""
    zd = client()

    try:
        payload = await zd.get(f"/users/{user_id}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _error(f"User {user_id} does not exist.")
        raise

    user = payload.get("user") or {}

    open_tickets = await zd.search_export(
        f"type:ticket assignee:{user_id} status<solved",
        filter_type="ticket",
        max_items=500,
    )

    try:
        memberships = await zd.paginate(
            f"/users/{user_id}/group_memberships",
            "group_memberships",
            max_items=100,
        )
        group_ids = [m.get("group_id") for m in memberships]
    except httpx.HTTPStatusError:
        group_ids = []

    by_status = Counter(t.get("status") for t in open_tickets)

    return {
        "user": {
            "id": user.get("id"),
            "name": user.get("name"),
            "email": user.get("email"),
            "role": user.get("role"),
            "suspended": user.get("suspended"),
            "days_since_last_login": _days_since(user.get("last_login_at")),
        },
        "group_ids": group_ids,
        "open_tickets_total": len(open_tickets),
        "open_tickets_by_status": dict(by_status),
        "open_tickets": [
            {
                "id": t.get("id"),
                "subject": t.get("subject"),
                "status": t.get("status"),
                "priority": t.get("priority"),
                "days_since_update": _days_since(t.get("updated_at")),
            }
            for t in open_tickets[:50]
        ],
        "next_steps": (
            "Reassign with reassign_tickets, then suspend with set_user_status. "
            "Both run as a dry run by default."
        ),
    }


# ======================================================================
# Writes - require ZENDESK_ALLOW_WRITES=true
# ======================================================================


@mcp.tool(annotations=MUTATING)
async def reassign_tickets(
    from_user_id: int,
    to_user_id: int,
    dry_run: bool = True,
    max_tickets: int = 200,
) -> dict[str, Any]:
    """Reassign an agent's open tickets to another agent.

    With dry_run true (the default) it only reports which tickets would be
    touched, without modifying anything. Set dry_run to false to execute.
    """
    zd = client()

    tickets = await zd.search_export(
        f"type:ticket assignee:{from_user_id} status<solved",
        filter_type="ticket",
        max_items=max_tickets,
    )
    ticket_ids = [t["id"] for t in tickets if t.get("id")]

    preview = {
        "from_user_id": from_user_id,
        "to_user_id": to_user_id,
        "tickets_found": len(ticket_ids),
        "ticket_ids": ticket_ids[:100],
        "truncated": len(tickets) >= max_tickets,
    }

    if dry_run:
        preview["dry_run"] = True
        preview["note"] = (
            "Nothing was modified. Call again with dry_run=false to apply."
        )
        return preview

    if not ticket_ids:
        return {**preview, "dry_run": False, "applied": 0}

    jobs = []
    try:
        # update_many accepts at most 100 ids per call.
        for i in range(0, len(ticket_ids), 100):
            chunk = ticket_ids[i : i + 100]
            result = await zd.put(
                "/tickets/update_many",
                {"ticket": {"assignee_id": to_user_id}},
                params={"ids": ",".join(str(t) for t in chunk)},
            )
            status = result.get("job_status") or {}
            jobs.append({"job_id": status.get("id"), "tickets": len(chunk)})
    except ZendeskWriteDisabled as exc:
        return _error(str(exc))

    return {
        **preview,
        "dry_run": False,
        "applied": len(ticket_ids),
        "jobs": jobs,
        "note": "Zendesk processes update_many asynchronously via job_status.",
    }


@mcp.tool(annotations=MUTATING)
async def set_user_status(
    user_id: int,
    suspended: bool | None = None,
    role: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Suspend or reactivate a user, and/or change their role.

    `role` accepts end-user, agent or admin. With dry_run true (the default) it
    only reports the proposed change against the current state.
    """
    zd = client()

    if suspended is None and role is None:
        return _error("Provide at least suspended or role.")
    if role is not None and role not in ("end-user", "agent", "admin"):
        return _error("role must be 'end-user', 'agent' or 'admin'.")

    try:
        payload = await zd.get(f"/users/{user_id}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _error(f"User {user_id} does not exist.")
        raise

    current = payload.get("user") or {}
    changes: dict[str, Any] = {}
    if suspended is not None and current.get("suspended") != suspended:
        changes["suspended"] = suspended
    if role is not None and current.get("role") != role:
        changes["role"] = role

    summary = {
        "user_id": user_id,
        "name": current.get("name"),
        "email": current.get("email"),
        "current": {
            "role": current.get("role"),
            "suspended": current.get("suspended"),
        },
        "proposed_changes": changes or "no changes",
    }

    if dry_run:
        return {
            **summary,
            "dry_run": True,
            "note": "Nothing was modified. Call with dry_run=false to apply.",
        }

    if not changes:
        return {**summary, "dry_run": False, "applied": False}

    try:
        result = await zd.put(f"/users/{user_id}", {"user": changes})
    except ZendeskWriteDisabled as exc:
        return _error(str(exc))

    updated = result.get("user") or {}
    return {
        **summary,
        "dry_run": False,
        "applied": True,
        "result": {"role": updated.get("role"), "suspended": updated.get("suspended")},
    }


def main() -> None:
    missing = [
        var
        for var in ("ZENDESK_SUBDOMAIN",)
        if not os.environ.get(var)
    ]
    if missing:
        # To stderr, never stdout: on the stdio transport, stdout carries the
        # JSON-RPC protocol and anything printed there breaks the session.
        # We do not abort: the server must start anyway so the MCP client can
        # receive the tool list and surface the error when they are invoked.
        print(
            f"[zendesk-admin-mcp] Warning: {', '.join(missing)} is not set",
            file=sys.stderr,
            flush=True,
        )
    mcp.run()


if __name__ == "__main__":
    main()
